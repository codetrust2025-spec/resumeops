"""One-command resume run, immutable artifacts, and unattended directory scanning."""
import hashlib
import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from .ollama import Ollama, OllamaError
from .ingest import load_profile, ExtractionError
from .latex import render
from .polish import polish, safe_edit
from .compile import BuildError, compile_latex, verify_pdf, verify_remote_pdf
from .overleaf import OverleafError, import_zip
from .storage import workspace_lock, write_json
from .profile import ProfileError
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


def _sync_overleaf(folder, report, profile, browser_profile, max_pages, refresh, progress):
    destination = folder / 'overleaf.pdf'
    saved = report.get('overleaf', {})
    checksum = saved.get('pdf_sha256')
    if not refresh and checksum and destination.is_file() and _source_digest(destination) == checksum:
        progress(90, 'Overleaf PDF retrieved')
        verify_remote_pdf(destination, folder / 'resume.pdf', profile, max_pages)
        return report
    with tempfile.TemporaryDirectory(dir=folder, prefix='.remote-pdf-') as temp:
        candidate = Path(temp) / 'overleaf.pdf'
        proof = import_zip(folder / 'overleaf-project.zip', report['project_name'], browser_profile,
                           pdf_path=candidate, saved_url=saved.get('url'))
        progress(90, 'Overleaf PDF retrieved')
        proof['pdf_checks'] = verify_remote_pdf(candidate, folder / 'resume.pdf', profile, max_pages)
        proof['pdf_sha256'] = _source_digest(candidate)
        os.replace(candidate, destination)
    report['overleaf'] = proof
    write_json(folder / 'result.json', report)
    return report


def process(source, output_root, *, ai=None, polish_enabled=True, overleaf=False,
            browser_profile=None, max_pages=2, refresh_overleaf=False, progress=_ignore_progress):
    source = Path(source)
    progress(0, 'Reading candidate input')
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
            return _process(snapshot, source, output_root, ai=ai, polish_enabled=polish_enabled,
                            overleaf=overleaf, browser_profile=browser_profile, max_pages=max_pages,
                            refresh_overleaf=refresh_overleaf, progress=progress)


def _process(source, original_source, output_root, *, ai, polish_enabled, overleaf,
             browser_profile, max_pages, refresh_overleaf, progress):
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
    else:
        original, target = load_profile(source, ai), None
    progress(10, 'Candidate input validated', original['full_name'])
    template = target['template'] if target else 'standard'
    keywords = role_keywords(target) if target else None
    progress(20, 'Role and keyword analysis complete' if target else 'Default resume template selected')
    folder = output_root / _slug(original['full_name']) / digest
    if (folder / 'result.json').exists():
        if not all((folder / item).is_file() for item in ARTIFACTS):
            raise ValueError('Previously published output is incomplete; restore it from backup')
        completed = _read_json(folder / 'result.json')
        cached = _read_json(folder / 'profile.json')
        _verify_cached_profile(original, cached)
        if completed['source_sha256'] != source_digest:
            raise ValueError('Cached source fingerprint mismatch')
        verify_pdf(folder / 'resume.pdf', cached, max_pages)
        _verify_upload_source(folder, cached, template)
        progress(80, 'Local PDF and source verified')
        if overleaf:
            completed = _sync_overleaf(folder, completed, cached, browser_profile, max_pages,
                                       refresh_overleaf, progress)
        return _finish(completed, overleaf, progress)

    profile = original
    audit = {'source': str(original_source.resolve()), 'source_sha256': source_digest,
             'source_kind': source.suffix, 'ai_model': model_name}
    if polish_enabled:
        profile, audit['polish'] = polish(profile, ai)
    progress(40, 'Ollama generation complete' if polish_enabled else 'Candidate content prepared')
    _verify_cached_profile(original, profile)
    progress(50, 'Generated content checked against source')
    ats = analyze(profile, target, keywords) if target else None
    progress(60, 'ATS keyword coverage calculated' if target else 'Candidate content ready for rendering')
    folder.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder.parent,prefix='resume-build-') as temp:
        working=Path(temp)
        latex = render(profile, template)
        (working/'main.tex').write_text(latex,encoding='utf8')
        (working/'profile.json').write_text(json.dumps(profile,indent=2,ensure_ascii=False),encoding='utf8')
        progress(70, 'LaTeX created')
        built=compile_latex(working)
        audit['pdf_checks']=verify_pdf(built,profile,max_pages=max_pages)
        shutil.copyfile(built,working/'resume.pdf')
        progress(80, 'Local PDF compiled and verified')
        with zipfile.ZipFile(working/'overleaf-project.zip','w',zipfile.ZIP_DEFLATED) as archive:
            archive.write(working/'main.tex','main.tex')
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
        report = _sync_overleaf(folder, report, profile, browser_profile, max_pages, refresh_overleaf, progress)
    return _finish(report, overleaf, progress)


def _finish(report, overleaf, progress):
    progress(95, 'Overleaf PDF verified against local build' if overleaf else 'Local PDF verified')
    persist_analysis(report)
    progress(100, 'Verified PDF ready to download')
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
        if failures and time.time() - previous['last_attempt'] < min(7200, 60 * 2**min(failures, 7)):
            continue
        started_at = time.time()
        activity = {}

        def report_progress(percent, stage, candidate_name=None):
            activity.update(percent=percent, stage=stage, updated=time.time())
            if candidate_name is not None:
                activity['candidate'] = candidate_name
            known[key] = {'sha256': digest, 'status': 'processing', 'last_attempt': started_at,
                          'failures': failures, **activity}
            write_json(state, known)
            _write_health(inbox, known)

        try:
            result = process(candidate, output_root, ai=ai, polish_enabled=polish_enabled,
                             overleaf=overleaf, browser_profile=browser_profile, max_pages=max_pages,
                             progress=report_progress)
            if fingerprint != result['source_sha256']:
                continue
            record = {'sha256': digest, 'status': 'ok', 'output': result['output'], **activity}
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
            }.get(type(exc), f'Processing failed during {activity.get("stage", "input preparation")}.')
            record = {'sha256': digest, 'status': 'failed', 'blocked': blocked, 'error': error,
                      'failures': failures + 1, **activity}
            print(f'FAILED resume processing ({type(exc).__name__}); see local inbox state', flush=True)
        record['last_attempt'] = time.time()
        known[key] = record
        write_json(state, known)
        _write_health(inbox, known)
    _write_health(inbox, known)
    return known


def _write_health(inbox, known):
    status = {'last_scan': time.time(),
              'failures': sum(entry['status'] == 'failed' and (inbox / key).exists()
                              for key, entry in known.items())}
    write_json(inbox / '.resume-bot-health.json', status)
