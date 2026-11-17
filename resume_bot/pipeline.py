"""One-command resume run, immutable artifacts, and unattended directory scanning."""
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from .ollama import Ollama, OllamaError
from .ingest import ExtractionError, extract_profile, read_resume_text
from .latex import render
from .polish import polish, safe_edit
from .compile import BuildError, compile_latex, verify_pdf, verify_remote_pdf
from .overleaf import OVERLEAF_STEP_COUNT, OverleafError, import_zip
from .progress import LegacyTracker, Tracker
from .storage import workspace_lock, write_json
from .profile import ProfileError, validate_profile
from .workflow import DatabaseUnavailable, analyze, normalize_request, persist_analysis, role_keywords


def _ignore_progress(percent, stage, candidate_name=None):
    pass


def _slug(text):
    import re
    return re.sub(r'[^a-z0-9]+','-',text.lower()).strip('-')[:56] or 'candidate'


ARTIFACTS = ('result.json', 'resume.pdf', 'main.tex', 'profile.json',
             'overleaf-project.zip', 'audit.json')


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _source_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _verify_cached_profile(original, cached):
    import copy
    restored = copy.deepcopy(cached)
    if not safe_edit(original['summary'], cached['summary']):
        raise ValueError('Cached summary differs from verified input')
    restored['summary'] = original['summary']
    for before, after, entry in zip(original['experience'], cached['experience'],
                                    restored['experience']):
        if len(before['bullets']) != len(after['bullets']) or not all(
            safe_edit(a, b) for a, b in zip(before['bullets'], after['bullets'])
        ):
            raise ValueError('Cached bullets differ from verified input')
        entry['bullets'] = before['bullets']
    if restored != original:
        raise ValueError('Cached facts differ from verified input')


def _sync_overleaf(folder, report, profile, browser_profile, max_pages, refresh, t):
    t.enter('overleaf', 'Preparing Overleaf publication')
    total = OVERLEAF_STEP_COUNT + 1  # browser steps plus remote PDF verification
    destination = folder / 'overleaf.pdf'
    saved = report.get('overleaf', {})
    checksum = saved.get('pdf_sha256')
    if not refresh and checksum and destination.is_file() and _source_digest(destination) == checksum:
        t.units_done(OVERLEAF_STEP_COUNT, total, 'Verifying the saved Overleaf PDF against the local build')
        verify_remote_pdf(destination, folder / 'resume.pdf', profile, max_pages)
        t.units_done(total, total, 'Saved Overleaf PDF verified against the local build')
        return report
    with tempfile.TemporaryDirectory(dir=folder, prefix='.remote-pdf-') as temp:
        candidate = Path(temp) / 'overleaf.pdf'

        def step(done, current):
            t.units_done(done, total, current)
            if done in (0, 4):  # browser launch and compile/download have no internal measure
                t.unmeasured(current)
        proof = import_zip(folder / 'overleaf-project.zip', report['project_name'], browser_profile,
                           pdf_path=candidate, saved_url=saved.get('url'), on_step=step)
        t.units_done(OVERLEAF_STEP_COUNT, total, 'Verifying the Overleaf PDF against the local build')
        proof['pdf_checks'] = verify_remote_pdf(candidate, folder / 'resume.pdf', profile, max_pages)
        proof['pdf_sha256'] = _source_digest(candidate)
        os.replace(candidate, destination)
    report['overleaf'] = proof
    write_json(folder / 'result.json', report)
    t.units_done(total, total, 'Overleaf PDF verified against the local build')
    return report


def _tracker(progress):
    return progress if isinstance(progress, Tracker) else LegacyTracker(progress)


def _ai_progress(t, label):
    """Turn streamed Ollama metrics into measured progress, throttled to about four writes/s."""
    last = [0.0]

    def on_progress(fraction, metrics):
        now = time.monotonic()
        if metrics['phase'] == 'generating' and now - last[0] < 0.25:
            return
        last[0] = now
        rate = metrics['chunks'] / metrics['elapsed'] if metrics.get('elapsed') else 0
        detail = (f"{metrics['chars']} characters · {metrics['chunks']} tokens · "
                  f"{rate:.1f} tokens/s")
        if metrics['phase'] == 'waiting':
            t.unmeasured('Waiting for Ollama to load the model and read the prompt')
        elif metrics['phase'] == 'done':
            evals = metrics.get('eval_count')
            t.fraction(1.0, label + ' finished', detail + (f' · {evals} tokens evaluated' if evals else ''))
        elif fraction is None:
            t.unmeasured(label + ' (output length unknown)', detail)
        else:
            t.fraction(fraction, label, detail)
    return on_progress


def process(source, output_root, *, ai=None, polish_enabled=True, overleaf=False,
            browser_profile=None, max_pages=2, refresh_overleaf=False, progress=_ignore_progress):
    source = Path(source)
    t = _tracker(progress)
    t.enter('validate', 'Reading candidate input')
    output_root = Path(output_root).resolve()
    if overleaf and (not browser_profile or source.suffix.lower() != '.json'):
        raise ValueError('Overleaf requires a browser profile and verified candidate JSON')
    if max_pages < 1:
        raise ValueError('Maximum page count must be positive')
    with workspace_lock(output_root, '.resume-bot-build.sqlite3'):
        with tempfile.TemporaryDirectory(dir=output_root, prefix='.input-') as staging:
            snapshot = Path(staging) / ('candidate' + source.suffix.lower())
            before = source.stat()
            shutil.copyfile(source, snapshot)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size, after.st_mtime_ns, after.st_ino
            ):
                raise ValueError('Source changed while being copied; retry from a complete file')
            t.units_done(1, 2, 'Candidate file copied and checked for changes')
            return _process(snapshot, source, output_root, ai=ai, polish_enabled=polish_enabled,
                            overleaf=overleaf, browser_profile=browser_profile, max_pages=max_pages,
                            refresh_overleaf=refresh_overleaf, t=t)


def _process(source, original_source, output_root, *, ai, polish_enabled, overleaf,
             browser_profile, max_pages, refresh_overleaf, t):
    source_digest = _source_digest(source)
    uses_ai = polish_enabled or source.suffix != '.json'
    ai = (ai or Ollama()) if uses_ai else None
    model_name = getattr(ai, 'model', '') if uses_ai else 'disabled'
    # Preserve v2 project names for the same source and configuration.
    config = json.dumps({'source_sha256': source_digest, 'model': model_name,
                         'polish': polish_enabled, 'v': 'resume-bot-0.2.0'}, sort_keys=True)
    digest = hashlib.sha256(config.encode('utf8')).hexdigest()[:12]
    if source.suffix == '.json':
        original, target = normalize_request(_read_json(source))
        t.units_done(2, 2, 'Candidate information validated')
        t.candidate = original['full_name']
    else:
        t.units_done(2, 2, 'Candidate document received')
        t.enter('source', 'Reading text from the source resume')
        text = read_resume_text(source)
        t.units_done(1, 2, f'Read {len(text)} characters from the source resume')
        t.enter('ai', 'Waiting for Ollama to load the model and read the prompt')
        report_ai = _ai_progress(t, 'Extracting candidate facts with Ollama')
        # Extraction output length is not known in advance, so it is reported as unmeasured.
        extracted = extract_profile(text, ai, on_progress=lambda metrics: report_ai(None, metrics))
        try:
            original = validate_profile(extracted)
        except ProfileError as exc:
            raise ExtractionError(f'Source does not provide complete reliable profile: {exc}') from exc
        target = None
        t.candidate = original['full_name']
        t.unmeasured('Candidate facts extracted and validated')
    t.enter('job', 'Creating job fingerprint')
    folder = output_root / _slug(original['full_name']) / digest
    t.units_done(1, 2, 'Job fingerprint ' + digest + ' created')
    cached_result = (folder / 'result.json').exists()
    t.units_done(2, 2, 'Existing verified revision found' if cached_result else 'New revision; no earlier output')
    template = target['template'] if target else 'standard'
    if source.suffix == '.json':
        t.enter('source', 'Loading the target role and template')
        t.units_done(1, 2, f"Target role {target['role']} with {target['template']} template" if target
                     else 'No target role; default template selected')
        keywords = role_keywords(target) if target else None
        t.units_done(2, 2, f'{len(keywords)} target keywords prepared' if target else 'No target keywords needed')
    else:
        keywords = None
    if cached_result:
        if not all((folder / item).is_file() for item in ARTIFACTS):
            raise ValueError('Previously published output is incomplete; restore it from backup')
        t.skip('ai', 'Identical verified revision exists; no AI work needed')
        t.enter('checks', 'Checking the saved revision against the candidate input')
        completed = _read_json(folder / 'result.json')
        cached = _read_json(folder / 'profile.json')
        _verify_cached_profile(original, cached)
        if completed['source_sha256'] != source_digest:
            raise ValueError('Cached source fingerprint mismatch')
        t.units_done(1, 1, 'Saved revision matches the candidate input')
        t.enter('latex', 'Checking saved LaTeX and Overleaf ZIP')
        _verify_upload_source(folder, cached, template)
        t.units_done(1, 1, 'Saved LaTeX matches the verified profile')
        t.enter('pdf', 'Verifying the saved local PDF')
        verify_pdf(folder / 'resume.pdf', cached, max_pages)
        t.units_done(1, 1, 'Saved local PDF verified')
        if overleaf:
            completed = _sync_overleaf(folder, completed, cached, browser_profile, max_pages,
                                       refresh_overleaf, t)
        else:
            t.skip('overleaf', 'Overleaf publishing is disabled')
        return _finish(completed, folder, overleaf, t)

    profile = original
    audit = {'source': str(original_source.resolve()), 'source_sha256': source_digest,
             'source_kind': source.suffix, 'ai_model': model_name}
    if polish_enabled:
        t.enter('ai', 'Waiting for Ollama to load the model and read the prompt')
        profile, audit['polish'] = polish(profile, ai, on_progress=_ai_progress(
            t, 'Generating resume using Ollama'))
        t.fraction(1.0, 'Ollama generation complete')
    else:
        t.skip('ai', 'AI polishing is disabled; candidate text used as supplied')
    t.enter('checks', 'Checking generated content against the candidate input')
    _verify_cached_profile(original, profile)
    t.units_done(1, 2, 'Generated content matches the candidate input')
    ats = analyze(profile, target, keywords) if target else None
    t.units_done(2, 2, f"ATS keyword coverage {ats['score']}%" if ats else 'No target role; ATS check not applicable')
    folder.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder.parent,prefix='resume-build-') as temp:
        working=Path(temp)
        t.enter('latex', 'Rendering LaTeX from the verified profile')
        latex = render(profile, template)
        (working/'main.tex').write_text(latex,encoding='utf8')
        t.units_done(1, 3, 'LaTeX rendered')
        (working/'profile.json').write_text(json.dumps(profile,indent=2,ensure_ascii=False),encoding='utf8')
        t.units_done(2, 3, 'Verified profile saved with the build')
        with zipfile.ZipFile(working/'overleaf-project.zip','w',zipfile.ZIP_DEFLATED) as archive:
            archive.write(working/'main.tex','main.tex')
        t.units_done(3, 3, 'Overleaf project ZIP created')
        t.enter('pdf', 'Compiling LaTeX with pdfLaTeX')
        t.unmeasured('Compiling LaTeX with pdfLaTeX')
        built=compile_latex(working)
        t.units_done(1, 2, 'Local PDF compiled; checking pages and text')
        audit['pdf_checks']=verify_pdf(built,profile,max_pages=max_pages)
        shutil.copyfile(built,working/'resume.pdf')
        pages = audit['pdf_checks'].get('pages')
        t.units_done(2, 2, f'Local PDF verified ({pages} page{"s" if pages != 1 else ""})')
        # Canonical LaTeX avoids new projects for identical resumes from other inboxes or models.
        revision = hashlib.sha256(latex.encode('utf-8')).hexdigest()[:24]
        project_name = 'ResumeBot-' + revision
        report={'status':'built','candidate':profile['full_name'],'model':audit['ai_model'],
                'source_sha256':audit['source_sha256'],'project_name':project_name,
                'output':str(folder),'pdf_checks':audit['pdf_checks'],
                'notes':['Source facts are not independently employer-verified; only traceable to candidate input.']}
        if ats is not None:
            report['ats'] = ats
        (working/'audit.json').write_text(json.dumps(audit,indent=2),encoding='utf8')
        (working/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        # Move only published outputs; TeX temporary build files are deliberately omitted.
        folder.mkdir(parents=True,exist_ok=True)
        for item in ('main.tex','profile.json','resume.pdf','overleaf-project.zip','audit.json','result.json'):
            os.replace(working/item,folder/item)
    _verify_upload_source(folder, profile, template)
    if overleaf:
        report = _sync_overleaf(folder, report, profile, browser_profile, max_pages, refresh_overleaf, t)
    else:
        t.skip('overleaf', 'Overleaf publishing is disabled')
    return _finish(report, folder, overleaf, t)


def _finish(report, folder, overleaf, t):
    t.enter('verify', 'Checking every published output file')
    required = ARTIFACTS + (('overleaf.pdf',) if overleaf else ())
    missing = [item for item in required if not (folder / item).is_file()]
    if missing:
        raise BuildError('Published outputs are missing: ' + ', '.join(missing))
    if overleaf and not (report.get('overleaf') or {}).get('pdf_sha256'):
        raise BuildError('Overleaf verification evidence is missing')
    t.units_done(1, 2, f'{len(required)} output files present' + (' with Overleaf evidence' if overleaf else ''))
    t.unmeasured('Saving ATS evidence')
    persist_analysis(report)
    t.units_done(2, 2, 'Evidence saved')
    t.complete('Verified PDF ready to download')
    return report


def _verify_upload_source(folder, profile, template):
    expected = render(profile, template).encode('utf-8').replace(b'\r\n', b'\n')
    with zipfile.ZipFile(folder / 'overleaf-project.zip') as archive:
        if archive.namelist() != ['main.tex']:
            raise ValueError('Cached Overleaf ZIP contains unexpected files')
        for actual in ((folder / 'main.tex').read_bytes(), archive.read('main.tex')):
            if actual.replace(b'\r\n', b'\n') != expected:
                raise ValueError('Cached LaTeX differs from verified profile')


def scan(inbox, output_root, *, ai=None, polish_enabled=True, overleaf=False,
         browser_profile=None, once=False, interval=30, max_pages=2):
    inbox = Path(inbox).resolve()
    with workspace_lock(inbox, '.resume-bot-watch.sqlite3'):
        while True:
            known = _scan_once(inbox, output_root, ai=ai, polish_enabled=polish_enabled,
                               overleaf=overleaf, browser_profile=browser_profile,
                               max_pages=max_pages)
            if once:
                return known
            time.sleep(max(10, interval))


def _scan_once(inbox, output_root, *, ai, polish_enabled, overleaf, browser_profile, max_pages):
    state = inbox / '.resume-bot-state.json'
    known = _read_json(state) if state.exists() else {}
    allowed = ('.json', '.pdf', '.docx', '.txt')
    for candidate in sorted(inbox.iterdir()):
        if candidate.name.startswith('.') or not candidate.is_file() or candidate.suffix.lower() not in allowed:
            continue
        key = candidate.name
        previous = known.get(key, {})
        try:
            fingerprint = _source_digest(candidate)
        except OSError:
            continue  # File is still being copied or was moved out of the inbox.
        config = (fingerprint, getattr(ai, 'model', ''), polish_enabled, overleaf,
                  str(Path(output_root).resolve()), str(browser_profile), max_pages)
        digest = hashlib.sha256(repr(config).encode('utf-8')).hexdigest()
        same = previous.get('sha256') == digest
        failures = previous.get('failures', 0) if same else 0
        if same and previous.get('status') == 'ok':
            built = Path(previous['output'])
            required = ARTIFACTS + (('overleaf.pdf',) if overleaf else ())
            if all((built / item).is_file() for item in required):
                continue
        retry_marker = inbox / RETRY_DIR / key
        retry_now = retry_marker.is_file()
        if failures and not retry_now and time.time() - previous['last_attempt'] < _backoff(failures):
            continue
        retry_marker.unlink(missing_ok=True)
        started_at = time.time()
        activity = {}
        lock = threading.Lock()

        def save(snapshot=None, *, beat=False):
            with lock:
                if snapshot is not None:
                    activity.update(snapshot)
                    if snapshot.get('stage_key') == 'done':
                        return  # 100% is written only with the final status 'ok' record below
                record = {'sha256': digest, 'status': 'processing', 'last_attempt': started_at,
                          'failures': failures, 'attempt': failures + 1, **activity,
                          'heartbeat': time.time()}
                known[key] = record
                write_json(state, known)
                if not beat:
                    _write_health(inbox, known)

        # The heartbeat proves the worker is alive during steps without internal progress
        # (pdfLaTeX, Overleaf compile). It never changes the percentage.
        stop = threading.Event()

        def beat():
            while not stop.wait(HEARTBEAT_SECONDS):
                save(beat=True)
        beater = threading.Thread(target=beat, name='resume-heartbeat', daemon=True)
        tracker = Tracker(save)
        beater.start()
        try:
            result = process(candidate, output_root, ai=ai, polish_enabled=polish_enabled,
                             overleaf=overleaf, browser_profile=browser_profile, max_pages=max_pages,
                             progress=tracker)
            if fingerprint != result['source_sha256']:
                continue
            record = {'sha256': digest, 'status': 'ok', 'output': result['output'],
                      'attempt': failures + 1, **activity}
            print('OK resume processed', flush=True)
        except Exception as exc:
            blocked = isinstance(exc, (OllamaError, OverleafError, BuildError, DatabaseUnavailable,
                                       ProfileError, ExtractionError, json.JSONDecodeError))
            error = {
                OllamaError: 'Local Ollama is unavailable or returned an unusable result.',
                OverleafError: 'Overleaf is unavailable or requires an authorized browser session.',
                BuildError: 'Local or Overleaf PDF verification failed. Check the build dependencies.',
                DatabaseUnavailable: str(exc) if isinstance(exc, DatabaseUnavailable) else '',
                ProfileError: str(exc) if isinstance(exc, ProfileError) else '',
                ExtractionError: 'Candidate input could not be verified against the source.',
                json.JSONDecodeError: 'Candidate JSON is incomplete or invalid.',
            }.get(type(exc), f'Processing failed during {activity.get("stage_label") or activity.get("stage", "input preparation")}.')
            now = time.time()
            record = {'sha256': digest, 'status': 'failed', 'blocked': blocked, 'error': error,
                      'failures': failures + 1, 'attempt': failures + 1, **activity,
                      'failed_stage': activity.get('stage_key', ''),
                      'next_retry': now + _backoff(failures + 1)}
            print(f'FAILED resume processing ({type(exc).__name__}); see local inbox state', flush=True)
        finally:
            stop.set()
            beater.join()
        record['last_attempt'] = time.time()
        known[key] = record
        write_json(state, known)
        _write_health(inbox, known)
    _write_health(inbox, known)
    return known


RETRY_DIR = '.resume-bot-retry'
HEARTBEAT_SECONDS = 5


def _backoff(failures):
    return min(7200, 60 * 2**min(failures, 7))


def request_retry(inbox, name):
    """Ask the watcher to retry a failed job at its next scan instead of after backoff."""
    inbox = Path(inbox)
    if '/' in name or '\\' in name or name.startswith('.') or not (inbox / name).is_file():
        raise ValueError('Unknown inbox job')
    folder = inbox / RETRY_DIR
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(str(time.time()), encoding='utf-8')


def _write_health(inbox, known):
    status = {'last_scan': time.time(),
              'failures': sum(entry['status'] == 'failed' and (inbox / key).exists()
                              for key, entry in known.items())}
    write_json(inbox / '.resume-bot-health.json', status)
