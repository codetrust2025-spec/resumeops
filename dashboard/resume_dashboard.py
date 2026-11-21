"""Local dashboard for the existing Resume Builder watcher."""
import argparse
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import stat
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from durable_imports import DurableImports, ImportCapacity, ImportConflict
from automation_jobs import AutomationCapacity, AutomationJobs, REQUEST_LIMIT
from intake import IntakeDesk
from urllib.parse import parse_qs, urlsplit
from zipfile import BadZipFile, ZipFile

# The public candidate pages' files (served under /apply/assets/, the only public path besides the pages).
APPLY_ASSETS = {'apply.js': 'text/javascript', 'apply.css': 'text/css', 'candidate.js': 'text/javascript',
                'candidate.css': 'text/css', 'automation.js': 'text/javascript', 'automation.css': 'text/css',
                'builder.css': 'text/css', 'recovery.js': 'text/javascript'}
PREFILL_BODY = 6 * 1024 * 1024 * 4 // 3 + 4096
ARTIFACTS = ('resume.pdf', 'overleaf.pdf', 'main.tex', 'overleaf-project.zip',
             'result.json', 'audit.json', 'profile.json')
LOGS = ('watcher.log', 'alerts.log', 'current-alert.txt', 'windows-suite-final.log',
        'operations-check-final.log', 'unattended-revision.json')
MAX_INPUT = 1024 * 1024
MAX_DOCUMENT = 12 * MAX_INPUT
DOCUMENTS = {'.docx', '.pdf', '.txt', '.md'}
OFFICE = {'.doc', '.odt', '.rtf'}
IMAGES = {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp'}


# Measured progress fields written by the watcher (resume_bot/progress.py and pipeline.scan).
LIVE_FIELDS = ('stage_key', 'stage_label', 'measured', 'units', 'detail', 'started', 'stage_started',
               'activity', 'expected_seconds', 'completed_stages', 'skipped_stages', 'heartbeat',
               'attempt', 'next_retry', 'failed_stage', 'error_detail', 'ollama_runtime', 'retryable')


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None


def linked(entry):
    """Symlink or Windows junction/reparse point, judged from directory-listing data only."""
    if entry.is_symlink() or getattr(entry, 'is_junction', lambda: False)():
        return True
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0)
    except OSError:
        return True
    return bool(attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def contained(path, directory):
    return path.resolve().is_relative_to(directory.resolve())


def private_owner(key):
    match = re.match(r'^private-([0-9a-f]{64})(?:/|-[0-9a-f]{64}\.json$)', key)
    return match[1] if match else None


class Workspace:
    def __init__(self, root, model="qwen2.5:7b", code_root=None):
        self.root = root.resolve()
        self.model = model
        self.inbox = self.root / 'inbox'
        self.output = self.root / 'output'
        self.logs = self.root / 'logs'
        self.private_inbox = self.root / 'inbox-private'
        self.private_output = self.root / 'output-private'
        self.approvals = self.root / 'approvals'
        self._result_cache = {}
        self._revision_lock = Lock()
        # The operator-managed role catalog lives with the data, not the code (watcher: cwd is the data root).
        os.environ.setdefault('RESUMEOPS_ROLE_CATALOG', str(self.root / 'role-catalog.json'))
        # Resolved once here (it may be a link or on another drive); never per poll.
        self._output_bases = list(dict.fromkeys(
            os.path.normcase(os.path.abspath(str(base))) for base in (self.output, self.output.resolve())))
        self.inbox.mkdir(exist_ok=True)
        sys.path.insert(0, str(code_root.resolve() if code_root else self.root))
        from resume_bot.workflow import normalize_request
        self.normalize = normalize_request

    def status(self):
        state = read_json(self.inbox / '.resume-bot-state.json')
        health = read_json(self.inbox / '.resume-bot-health.json')
        jobs = []
        for path in self.inbox.glob('*.json'):
            if path.name.startswith('.') or path.is_symlink() or not path.is_file():
                continue
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            record = (state or {}).get(path.name, {})
            last = record.get('last_attempt', 0)
            status = record.get('status') if last >= modified else None
            jobs.append({'name': path.name, 'status': status or 'queued',
                         'last': last or modified, 'failures': record.get('failures', 0) if status else 0,
                         'error': record.get('error', '') if status == 'failed' else '',
                         'candidate': record.get('candidate', '') if status else '',
                         'revision': self.revision_key(record.get('output')) if status == 'ok' else '',
                         'blocked': bool(record.get('blocked')) if status else False,
                         'percent': record.get('percent', 0) if status else 0,
                         'stage': record.get('stage', '') if status else '',
                         'updated': record.get('updated', last or modified) if status else modified,
                         **{field: record.get(field) for field in LIVE_FIELDS if status and field in record}})
        jobs.sort(key=lambda job: job['last'], reverse=True)
        # The watcher handles files in name order; report each waiting job's place in that line.
        waiting = sorted(job['name'] for job in jobs if job['status'] == 'queued')
        for job in jobs:
            if job['status'] == 'queued':
                job['queue_position'] = waiting.index(job['name']) + 1
                request = read_json(self.inbox / job['name'])
                if isinstance(request, dict):
                    profile = request.get('profile', request)
                    name = profile.get('full_name') if isinstance(profile, dict) else None
                    if isinstance(name, str):
                        job['candidate'] = name[:120]
        with self._revision_lock:
            revisions = self._revisions()
        alert = self.logs / 'current-alert.txt'
        last_scan = (health or {}).get('last_scan')
        return {'jobs': jobs, 'revisions': revisions,
                'lastScan': last_scan, 'healthy': bool(last_scan and time.time() - last_scan < 900),
                'worker': {key: (health or {}).get(key) for key in ('pid', 'started', 'ollama', 'overleaf', 'mongodb')},
                'alert': alert.is_file(), 'stateReadable': state is not None or not (
                    self.inbox / '.resume-bot-state.json').exists(),
                'logs': [name for name in LOGS if (self.logs / name).is_file()
                         and contained(self.logs / name, self.logs)]}

    def automation_ready(self):
        from resume_bot.pipeline import CODE
        health = read_json(self.inbox / '.resume-bot-health.json') or {}
        checked = health.get('last_scan', 0)
        return health.get('code') == CODE and time.time() - checked < 900

    def read_automation_source(self, extension, data):
        from resume_bot.document_evidence import read_evidence
        with tempfile.TemporaryDirectory(prefix='resumeops-source-') as temp:
            path = Path(temp) / ('resume.' + extension)
            path.write_bytes(data)
            if extension == 'docx':
                try:
                    with ZipFile(path) as document:
                        if document.getinfo('word/document.xml').file_size > 8 * MAX_INPUT:
                            raise ValueError('The Word document text is too large.')
                except (BadZipFile, KeyError) as exc:
                    raise ValueError('The Word document could not be read.') from exc
            return read_evidence(path)

    def draft(self, extension, data, report=lambda stage, metrics=None: None):
        from resume_bot.ingest import extract_profile, read_resume_text
        from resume_bot.ollama import Ollama

        report('Reading document')
        with tempfile.TemporaryDirectory(prefix='resume-dashboard-') as directory:
            folder = Path(directory)
            source = folder / ('resume' + extension)
            source.write_bytes(data)
            if extension in OFFICE:
                office = shutil.which('soffice') or shutil.which('libreoffice')
                if not office and os.name == 'nt':
                    candidate = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'LibreOffice/program/soffice.exe'
                    office = str(candidate) if candidate.is_file() else None
                if not office:
                    raise ValueError('This format needs LibreOffice. Save it as DOCX or PDF and try again.')
                self._tool([office, '-env:UserInstallation=' + (folder / 'lo-profile').as_uri(),
                            '--headless', '--convert-to', 'docx', '--outdir', str(folder), str(source)])
                source = source.with_suffix('.docx')
                if not source.is_file():
                    raise ValueError('Document conversion failed. Save it as DOCX and try again.')
            if extension in IMAGES:
                ocr = shutil.which('tesseract')
                if not ocr:
                    raise ValueError('Image import needs Tesseract OCR on PATH. You can also upload a text-based document.')
                source_text = self._tool([ocr, str(source), 'stdout'])
            elif extension in {'.txt', '.md'}:
                source_text = source.read_text(encoding='utf-8-sig')
            else:
                if source.suffix == '.docx':
                    try:
                        with ZipFile(source) as document:
                            if document.getinfo('word/document.xml').file_size > 8 * MAX_INPUT:
                                raise ValueError('The Word document text is too large.')
                    except (BadZipFile, KeyError) as exc:
                        raise ValueError('Could not read the Word document. Open it in Word and save it as DOCX.') from exc
                source_text = read_resume_text(source)
            if not 120 <= len(source_text.strip()) <= 75000:
                raise ValueError('The source needs 120–75,000 characters of readable résumé text.')
            report('Document text ready')
            report('Extracting candidate details with Ollama')
            model = Ollama(model=self.model, url='http://127.0.0.1:11434')

            def measured(metrics):
                # Count top-level JSON fields as the model actually emits them. This
                # is a completed-field fraction, never a predicted time-to-finish.
                fields = {'phase': metrics['phase'], 'chars': metrics['chars'],
                          'tokens': metrics['chunks'], 'fields_emitted': metrics.get('fields_emitted', 0),
                          'fields_total': 15, 'elapsed': round(metrics.get('elapsed', 0), 1)}
                stage = ('Ollama output received' if metrics['phase'] == 'done' else
                         'Ollama retrying without streaming' if metrics['phase'] == 'fallback'
                         else 'Extracting candidate details with Ollama')
                report(stage, fields)
            profile = extract_profile(source_text, model, for_review=True, on_progress=measured)
            report('Candidate fields validated')
            return {'profile': profile, 'source': source_text}

    @staticmethod
    def _tool(arguments):
        try:
            return subprocess.run(arguments, capture_output=True, encoding='utf-8',
                                  errors='replace', check=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError('Local document conversion failed. Try exporting the file as DOCX or text-based PDF.') from exc

    def enqueue(self, data, approval=None):
        document = json.loads(data.decode('utf-8-sig'))
        if approval and approval.get('automation'):
            document['target']['approved'] = True
        profile, target = self.normalize(document)
        if target and target.get('approved') and approval is None:
            raise ValueError('Only the AI review can mark a resume as candidate-approved')
        if approval is not None:
            if not target:
                raise ValueError('An approved resume needs a target role')
            target['approved'] = True
        owner = target.get('automation_owner') if target else None
        if owner and (not approval or not approval.get('automation') or approval['approved_by'] != owner
                      or approval['operation'] != target['automation_id']):
            raise ValueError('Private automation requires a matching evidence record')
        request = {'profile': profile, 'target': target} if target else profile
        payload = json.dumps(request, ensure_ascii=False, indent=2).encode('utf-8')
        slug = re.sub(r'[^a-z0-9]+', '-', profile['full_name'].lower()).strip('-')[:45] or 'candidate'
        digest = hashlib.sha256(payload).hexdigest()
        prefix = 'private-' + owner if owner else slug
        target = (self.private_inbox if owner else self.inbox) / f'{prefix}-{digest}.json'
        if approval is not None:
            # The approval record is written before the job file is published.
            self.record_approval(target.name, payload, approval)
        if target.exists():
            return {'name': target.name, 'created': False}
        if not self.publish(target, payload):
            return {'name': target.name, 'created': False}
        return {'name': target.name, 'created': True}

    def publish(self, target, payload):
        """Atomically create target with payload; never overwrite. Returns False if it already existed."""
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.dashboard-', suffix='.tmp', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # A hard link publishes the finished file atomically without overwriting a job.
                os.link(temporary, target)
            except FileExistsError:
                return False
        finally:
            Path(temporary).unlink(missing_ok=True)
        return True

    def record_approval(self, job, payload, approval):
        """Append-only, permanent record of what the candidate approved for one inbox job."""
        content = {'schema': 'resumeops-approval-1', 'job': job,
                   'resume_sha256': hashlib.sha256(payload).hexdigest(),
                   'operation': approval['operation'], 'approved_by': approval['approved_by'],
                   'attested': True, 'claims': approval['claims'], 'project_names': approval.get('project_names', []),
                   'market_sources': approval.get('market_sources', [])}
        if approval.get('automation'):
            content['automation'] = approval['automation']
        identity = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:32]
        record = {**content, 'record_id': identity,
                  'recorded_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        target = self.approvals / Path(job).stem / f'{identity}.json'
        self.publish(target, json.dumps(record, ensure_ascii=False, indent=2).encode('utf-8'))
        return target

    def approvals_for(self, job):
        from resume_bot.claim_audit import records_for
        return records_for(self.approvals, job)

    def automation_state(self):
        return read_json(self.private_inbox / '.resume-bot-state.json') or {}

    def private_revision_key(self, output, owner):
        if not output:
            return ''
        folder = Path(output)
        key = folder.parent.name + '/' + folder.name
        return key if private_owner(key) == owner and self.artifact(key, 'result.json') else ''

    def revision_key(self, output):
        # Pure path arithmetic: the watcher records absolute output paths, so no file is opened here.
        # Downloads still go through artifact(), which resolves and contains every served path.
        if not output:
            return ''
        text = str(output)
        if text.startswith('\\\\?\\UNC\\'):
            text = '\\\\' + text[8:]
        elif text.startswith('\\\\?\\'):
            text = text[4:]
        original = os.path.abspath(text)
        path = os.path.normcase(original)
        for base in self._output_bases:
            try:
                relative = os.path.relpath(path, base)
            except ValueError:
                continue
            parts = Path(relative).parts
            if len(parts) == 2 and '..' not in parts:
                # Keep the on-disk case of the two folder names; listing keys use it.
                return '/'.join(Path(original).parts[-2:])
        return ''

    def _revisions(self):
        """List verified revisions with one directory listing per folder and cached result.json.

        Path.resolve() opens a handle per file on Windows, which real-time scanning makes slow;
        this scan opens only result.json files that changed since the previous scan."""
        revisions, seen = [], set()

        def plain_dirs(folder):
            try:
                with os.scandir(folder) as entries:
                    return [entry for entry in entries if entry.is_dir(follow_symlinks=False)
                            and not linked(entry)]
            except OSError:
                return []

        for candidate in plain_dirs(self.output):
            for revision in plain_dirs(candidate.path):
                if revision.name.startswith('resume-build-'):
                    continue  # the watcher's unpublished build folder, possibly left by a failed attempt
                try:
                    with os.scandir(revision.path) as entries:
                        files = {entry.name: entry for entry in entries
                                 if entry.is_file(follow_symlinks=False) and not linked(entry)}
                except OSError:
                    continue
                entry = files.get('result.json')
                if entry is None:
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                stamp = (info.st_mtime_ns, info.st_size)
                cached = self._result_cache.get(entry.path)
                if cached is None or cached[0] != stamp:
                    cached = (stamp, read_json(Path(entry.path)))
                    if isinstance(cached[1], dict):
                        # A failed read (scanner or watcher lock) is retried on the next poll.
                        self._result_cache[entry.path] = cached
                seen.add(entry.path)
                result = cached[1]
                if not isinstance(result, dict):
                    continue
                remote = result.get('overleaf') or {}
                url = remote.get('url', '')
                if not re.fullmatch(r'https://www\.overleaf\.com/project/[A-Za-z0-9_-]+', url):
                    url = ''
                revisions.append({
                    'key': candidate.name + '/' + revision.name,
                    'candidate': result.get('candidate', 'Candidate'),
                    'model': result.get('model', ''),
                    'ats': result.get('ats'),
                    'modified': info.st_mtime, 'url': url,
                    'pages': (result.get('pdf_checks') or {}).get('pages'),
                    'sha256': remote.get('pdf_sha256', ''),
                    'files': [name for name in ARTIFACTS if name in files],
                })
        for stale in set(self._result_cache) - seen:
            del self._result_cache[stale]
        revisions.sort(key=lambda item: item['modified'], reverse=True)
        return revisions

    def revision(self, key):
        from resume_bot.workflow import analyze
        path = self.artifact(key, 'result.json')
        profile = self.artifact(key, 'profile.json')
        if not path or not profile:
            return None
        report = read_json(path)
        candidate = read_json(profile)
        if not isinstance(report, dict) or not isinstance(candidate, dict):
            return None
        ats = report.get('ats')
        if ats and 'ats_readiness' not in ats:
            target = {'role': ats['role'], 'template': ats['template']}
            ats = analyze(candidate, target, ats.get('matched', []) + ats.get('missing', []))
        return {'profile': candidate, 'ats': ats, 'tailoring': report.get('tailoring'),
                'overleaf': report.get('overleaf'),
                'experience': report.get('experience')}

    def keywords(self, role):
        from resume_bot.workflow import ROLES
        if not isinstance(role, str) or role not in ROLES:
            raise ValueError('Select a supported role')
        extras = (read_json(self.root / 'role-keywords.json') or {}).get(role, [])
        return {'role': role, 'catalog': ROLES[role]['keywords'], 'additional': extras}

    def save_keywords(self, role, extras):
        from resume_bot.storage import workspace_lock, write_json
        from resume_bot.workflow import validate_keywords
        self.keywords(role)
        extras = validate_keywords(extras)
        path = self.root / 'role-keywords.json'
        with workspace_lock(self.root, '.resumeops-keywords.sqlite3'):
            data = read_json(path) or {}
            data[role] = extras
            write_json(path, data)
        return self.keywords(role)

    def artifact(self, key, name):
        if name not in ARTIFACTS or not re.fullmatch(r'[^/\\]+/[^/\\]+', key):
            return None
        root = self.private_output if private_owner(key) else self.output
        path = root / key / name
        return path if contained(path, root) and path.is_file() else None


RENEWED_PAGE = b'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Signed in \xc2\xb7 ResumeOps</title>
<link rel="stylesheet" href="/builder.css"><script defer src="/auth-renewed.js"></script></head>
<body><main class="width intro"><h1>Signed in</h1><p>ResumeOps is resuming your work.</p></main></body></html>'''


class DashboardServer(ThreadingHTTPServer):
    # A burst of candidates (100+ at once through the tunnel) needs a deep listen backlog: the default of 5
    # refuses connections before a thread can take them.
    request_queue_size = 512
    daemon_threads = True


def handler(workspace, origin, token, phone_origin=None, phone_user=None, *,
            web_origin=None, access_verifier=None, ai_first=False, automation_first=False):
    host = urlsplit(origin).netloc
    phone_host = urlsplit(phone_origin).netloc if phone_origin else None
    web_host = urlsplit(web_origin).netloc if web_origin else None

    import_stages = {}
    import_lock = Lock()
    def process_draft(extension, data, report):
        if extension != '.ai':
            return workspace.draft(extension, data, report)
        from resume_bot.ai_first import suggest_sections
        from resume_bot.ai_review import claims_for_review, role_intelligence
        from resume_bot.ollama import Ollama
        request = json.loads(data)
        candidate = request['candidate']
        evidence = request['source']
        report('Preparing role intelligence')
        intelligence = role_intelligence(candidate['role'], workspace.root)
        report('Drafting suggestions with Ollama')
        model = Ollama(model=workspace.model, url='http://127.0.0.1:11434')

        def measured(metrics):
            payload = {'phase': metrics['phase'], 'chars': metrics['chars'],
                       'tokens': metrics['chunks'], 'fields_emitted': metrics.get('fields_emitted', 0),
                       'fields_total': 8, 'elapsed': round(metrics.get('elapsed', 0), 1)}
            report('Ollama output received' if metrics['phase'] == 'done' else
                   'Ollama retrying without streaming' if metrics['phase'] == 'fallback' else
                   'Drafting suggestions with Ollama', payload)

        draft = suggest_sections(candidate, model, evidence=evidence, market_terms=intelligence['terms'],
                                 requirements=[line for item in intelligence['sources']
                                               for line in item.get('responsibilities', [])],
                                 on_progress=measured)
        draft.update(source=evidence, intelligence=intelligence)
        claims = claims_for_review(draft)
        report('AI suggestions validated')
        return {**draft, 'claims': claims}

    durable = DurableImports(workspace.root, process_draft)
    automation = AutomationJobs(workspace)
    intake = IntakeDesk(workspace, automation)
    operators = {email.strip().casefold() for email in os.getenv('RESUMEOPS_OPERATOR_ACCOUNTS', '').split(',')
                 if email.strip()}

    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_content(self, data, mime, status=200, filename=None):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            ancestors = "'self'" if mime == 'application/pdf' else "'none'"
            self.send_header('Content-Security-Policy',
                             f"default-src 'self'; frame-ancestors {ancestors}; base-uri 'none'; form-action 'none'")
            if filename:
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, data, status=200):
            self.send_content(json.dumps(data).encode('utf-8'), 'application/json', status)

        def request_origin(self):
            request_host = self.headers.get('Host')
            identity = self.headers.get('Tailscale-User-Login')
            forwarded = self.headers.get('X-Forwarded-Proto')
            access_token = self.headers.get('Cf-Access-Jwt-Assertion')
            if request_host == host and not identity and not forwarded and not access_token:
                return origin
            if phone_origin and request_host == phone_host and identity == phone_user:
                return phone_origin
            if web_origin and request_host == web_host and access_verifier.accepts(access_token):
                return web_origin
            return None

        @staticmethod
        def resume_imports():
            durable.start()
            automation.start()

        @staticmethod
        def stop_imports():
            durable.stop()
            automation.stop()

        def session_owner(self, request_origin):
            if request_origin == web_origin:
                claims = access_verifier.claims(self.headers.get('Cf-Access-Jwt-Assertion'))
                if not isinstance(claims, dict) or not isinstance(claims.get('email'), str):
                    return None
                identity = claims['email'].strip().casefold()
            elif request_origin == phone_origin:
                identity = phone_user
            else:
                identity = 'loopback'
            return hashlib.sha256((request_origin + '\0' + identity).encode('utf-8')).hexdigest()

        def account_matches(self, request_origin):
            expected = self.headers.get('X-Resume-Owner')
            return expected is None or secrets.compare_digest(expected.encode('utf-8'),
                                                               (self.session_owner(request_origin) or '').encode('utf-8'))

        def is_operator(self, request_origin):
            if request_origin == origin:
                return True
            if request_origin == phone_origin:
                return True  # request_origin already verified the configured Tailscale operator
            claims = access_verifier.claims(self.headers.get('Cf-Access-Jwt-Assertion')) or {}
            return str(claims.get('email', '')).strip().casefold() in operators

        def can_read_job(self, key, request_origin):
            owner = private_owner(key)
            if owner and owner != self.session_owner(request_origin):
                # The operator reads public-intake candidates' outputs (never another account's own resumes).
                return self.is_operator(request_origin) and intake.is_candidate_owner(owner)
            return (owner == self.session_owner(request_origin) if owner
                    else self.is_operator(request_origin))

        def do_GET(self):
            if self.path == '/apply' or self.path.startswith(('/apply/', '/apply?')):
                return self.apply_get()
            request_origin = self.request_origin()
            if request_origin is None or not self.account_matches(request_origin):
                return self.send_json({'error': 'Dashboard access denied for this address or session.'}, 403)
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == '/api/session':
                # The page asks for the current request token after a dashboard restart or a
                # re-sign-in, so open work can continue without a reload. Reading it requires the
                # same Access check as the page itself; browsers block cross-site reads.
                expires = None
                assertion = self.headers.get('Cf-Access-Jwt-Assertion')
                if assertion and access_verifier is not None:
                    claims = access_verifier.claims(assertion)
                    expires = claims.get('exp') if claims else None
                owner = self.session_owner(request_origin)
                if not owner:
                    return self.send_json({'error': 'Dashboard access denied for this session.'}, 403)
                return self.send_json({'ok': True, 'token': token, 'owner': owner, 'expires': expires, 'now': time.time()})
            if url.path == '/auth/renewed':
                return self.send_content(RENEWED_PAGE, 'text/html; charset=utf-8')
            if url.path == '/api/import-progress':
                if not secrets.compare_digest(self.headers.get('X-Dashboard-Token', ''), token):
                    return self.send_json({'error': 'Request not authorized.'}, 403)
                owner = self.session_owner(request_origin)
                if not owner:
                    return self.send_json({'error': 'Dashboard access denied for this session.'}, 403)
                identifier = query.get('id', [''])[0]
                response = durable.get(owner, identifier)
                if response is None:
                    with import_lock:
                        stage = import_stages.get((owner, identifier))
                    response = {'stage': stage, 'status': 'processing' if stage else 'unknown'}
                return self.send_json(response)
            if url.path == '/api/roles':
                if not self.is_operator(request_origin):
                    return self.send_json({'error': 'An operator account is required for this route.'}, 403)
                from resume_bot.workflow import CATALOG_STATUS, ROLES, catalog_path, load_roles
                load_roles()
                managed = None
                if catalog_path().is_file():
                    try:
                        managed = json.loads(catalog_path().read_text(encoding='utf-8'))
                    except (OSError, ValueError):
                        managed = None
                return self.send_json({'roles': list(ROLES.values()), 'status': dict(CATALOG_STATUS),
                                       'file': catalog_path().name, 'managed': managed})
            if url.path == '/api/catalog':
                from resume_bot.workflow import ROLES, TEMPLATES, load_roles
                load_roles()
                if query.get('visible') == ['1']:
                    return self.send_json({'roles': [{'id': r['id'], 'name': r['name'], 'groups': r['groups']}
                                                     for r in ROLES.values() if not r.get('hidden')],
                                           'templates': TEMPLATES})
                return self.send_json({'roles': list(ROLES.values()), 'templates': TEMPLATES})
            if url.path == '/api/keywords':
                try:
                    return self.send_json(workspace.keywords(query.get('role', [''])[0]))
                except ValueError as error:
                    return self.send_json({'error': str(error)}, 400)
            if url.path == '/api/automation':
                owner = self.session_owner(request_origin)
                if not owner:
                    return self.send_json({'error': 'Sign in to access your resumes.'}, 403)
                operation = query.get('id', [None])[0]
                return self.send_json({'jobs': automation.list(owner, operation),
                                       'ready': workspace.automation_ready()})
            if url.path in ('/api/intake/candidates', '/api/intake/summary'):
                if not self.is_operator(request_origin):
                    return self.send_json({'error': 'An operator account is required for this route.'}, 404)
                from resume_bot import gpu_gate
                summary = {'queue': automation.queue_summary(), 'form_url': (web_origin or origin) + '/apply',
                           'gpu': {'slots': gpu_gate.slots(), 'busy': gpu_gate.busy()}}
                if url.path == '/api/intake/summary':
                    return self.send_json(summary)
                return self.send_json({**summary, 'candidates': intake.candidates(), 'now': time.time()})
            if url.path == '/api/automation/audit':
                # Deletion records: an account sees its own; the operator sees every account's.
                owner = self.session_owner(request_origin)
                if not owner:
                    return self.send_json({'error': 'Sign in to access your resumes.'}, 403)
                everyone = self.is_operator(request_origin) and query.get('all', [''])[0] == '1'
                return self.send_json({'records': automation.audit_log(None if everyone else owner)})
            if url.path == '/api/revision':
                key = query.get('key', [''])[0]
                revision = workspace.revision(key) if self.can_read_job(key, request_origin) else None
                if revision:
                    return self.send_json(revision)
                return self.send_json({'error': 'Resume revision unavailable.'}, 404)
            if url.path == '/api/status':
                # Server time lets browsers compute elapsed time without trusting their own clock.
                status = workspace.status()
                if not self.is_operator(request_origin):
                    status.update(logs=[], jobs=[], revisions=[], worker={}, alert=False)
                if request_origin != origin:
                    status['logs'] = []
                for field, key in (('jobs', 'name'), ('revisions', 'key')):
                    status[field] = [item for item in status[field]
                                     if self.can_read_job(item[key], request_origin)]
                return self.send_json({**status, 'now': time.time()})
            if url.path == '/api/artifact':
                key = query.get('key', [''])[0]
                name = query.get('name', [''])[0]
                allowed = self.can_read_job(key, request_origin)
                if private_owner(key):
                    allowed = allowed and name in {'resume.pdf', 'main.tex', 'overleaf.pdf'}
                path = workspace.artifact(key, name) if allowed else None
                if path:
                    mime = 'application/pdf' if path.suffix == '.pdf' else (
                        mimetypes.guess_type(path)[0] or 'application/octet-stream')
                    return self.send_content(path.read_bytes(), mime,
                                             filename=path.name if path.suffix != '.pdf' or query.get('download') == ['1'] else None)
            if url.path == '/api/log':
                name = query.get('name', [''])[0]
                path = workspace.logs / name
                if request_origin == origin and name in LOGS and contained(path, workspace.logs) and path.is_file():
                    with path.open('rb') as stream:
                        stream.seek(max(0, path.stat().st_size - 65536))
                        data = stream.read()
                    return self.send_content(data.decode('utf-8-sig', errors='replace').encode('utf-8'), 'text/plain; charset=utf-8')
            homepage = 'automation.html' if automation_first else 'ai-builder.html' if ai_first else 'index.html'
            assets = {'/': (homepage, 'text/html'),
                      '/automated': ('automation.html', 'text/html'),
                      '/automation.js': ('automation.js', 'text/javascript'),
                      '/automation.css': ('automation.css', 'text/css'),
                      '/candidates': ('candidates.html', 'text/html'),
                      '/candidates.js': ('candidates.js', 'text/javascript'),
                      '/candidates.css': ('candidates.css', 'text/css'),
                      '/workflow': ('index.html', 'text/html'),
                      '/roles': ('roles.html', 'text/html'),
                      '/roles.js': ('roles.js', 'text/javascript'),
                      '/ai-builder': ('ai-builder.html', 'text/html'),
                      '/ai-builder.js': ('ai-builder.js', 'text/javascript'),
                      '/ai-builder.css': ('ai-builder.css', 'text/css'),
                      '/operations': ('operations.html', 'text/html'),
                      '/builder.css': ('builder.css', 'text/css'),
                      '/builder.js': ('builder.js', 'text/javascript'),
                      '/live.js': ('live.js', 'text/javascript'),
                      '/import.js': ('import.js', 'text/javascript'),
                      '/auth.js': ('auth.js', 'text/javascript'),
                      '/auth-renewed.js': ('auth-renewed.js', 'text/javascript'),
                      '/recovery.js': ('recovery.js', 'text/javascript'),
                      '/dashboard.css': ('dashboard.css', 'text/css'),
                      '/dashboard.js': ('dashboard.js', 'text/javascript'),
                      '/workflow.js': ('workflow.js', 'text/javascript')}
            if url.path in assets:
                name, mime = assets[url.path]
                path = Path(__file__).with_name(name)
                data = path.read_bytes()
                if url.path == '/candidates' and not self.is_operator(request_origin):
                    return self.send_json({'error': 'An operator account is required for this page.'}, 403)
                if url.path in ('/', '/automated', '/operations', '/ai-builder', '/workflow', '/roles', '/candidates'):
                    data = data.replace(b'__TOKEN__', token.encode('ascii'))
                return self.send_content(data, mime + '; charset=utf-8')
            return self.send_json({'error': 'File unavailable.'}, 404)

        def submit_automation(self, request_origin):
            owner = self.session_owner(request_origin)
            if not owner:
                return self.reject_post('Sign in before submitting a candidate.')
            if not workspace.automation_ready():
                return self.send_json({'error': 'The resume watcher version is not ready. Retry shortly.'}, 503)
            try:
                size = int(self.headers.get('Content-Length', ''))
                if not 0 < size <= REQUEST_LIMIT or self.headers.get_content_type() != 'application/json':
                    return self.send_json({'error': 'Invalid or oversized resume submission.'}, 413)
                self.connection.settimeout(45)
                body = self.rfile.read(size)
                if len(body) != size:
                    return self.send_json({'error': 'Upload was incomplete. Retry the same submission.'}, 400)
                identifier = automation.enqueue(owner, json.loads(body))
            except AutomationCapacity as exc:
                return self.send_json({'error': str(exc)}, 429)
            except (ValueError, UnicodeError) as exc:
                return self.send_json({'error': str(exc)}, 400)
            except OSError:
                return self.send_json({'error': 'Upload interrupted. Retry the same submission.'}, 408)
            return self.send_json({'id': identifier}, 202)

        # Public candidate intake (/apply). These are the only pages and endpoints served without a Cloudflare
        # Access identity: the form, the prefill reader, the submission, and the candidate's own status page and
        # resumes behind their private key. Nothing else is reachable through them.
        def public_origin(self):
            request_host = self.headers.get('Host')
            if web_origin and request_host == web_host:
                return web_origin
            if (request_host == host and not self.headers.get('Tailscale-User-Login')
                    and not self.headers.get('X-Forwarded-Proto') and not self.headers.get('Cf-Access-Jwt-Assertion')):
                return origin
            return None

        def intake_client(self, public):
            """The network a public request came from (Cloudflare's view on the site), for rate limits."""
            address = self.headers.get('Cf-Connecting-IP') if public == web_origin else None
            return str(address or 'local-' + self.client_address[0])[:64]

        def candidate_session(self):
            """(candidate id, owner) for the private key sent with the request, or (None, None)."""
            candidate = self.headers.get('X-Candidate-Id', '')
            owner = intake.authorize(candidate, self.headers.get('X-Candidate-Key', ''))
            return (candidate, owner) if owner else (None, None)

        def apply_get(self):
            from intake import candidate_page, prefill_owner
            public = self.public_origin()
            if public is None:
                return self.send_json({'error': 'Not available at this address.'}, 403)
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if not intake.limiter.allow('read', self.intake_client(public)):
                return self.send_json({'error': 'Too many requests. Wait a moment and retry.'}, 429)
            if url.path in ('/apply', '/apply/'):
                return self.send_content(Path(__file__).with_name('apply.html').read_bytes(), 'text/html; charset=utf-8')
            if url.path == '/apply/status':
                return self.send_content(candidate_page(Path(__file__).with_name('automation.html').read_bytes()),
                                         'text/html; charset=utf-8')
            if url.path.startswith('/apply/assets/'):
                name = url.path[len('/apply/assets/'):]
                if name in APPLY_ASSETS:
                    return self.send_content(Path(__file__).with_name(name).read_bytes(),
                                             APPLY_ASSETS[name] + '; charset=utf-8')
                return self.send_json({'error': 'File unavailable.'}, 404)
            if url.path == '/apply/api/catalog':
                from resume_bot.workflow import TEMPLATES, ROLES, load_roles
                load_roles()
                return self.send_json({'roles': [{'id': r['id'], 'name': r['name'], 'groups': r['groups']}
                                                 for r in ROLES.values() if not r.get('hidden')], 'templates': TEMPLATES})
            if url.path == '/apply/api/prefill':
                session, identifier = query.get('session', [''])[0], query.get('id', [''])[0]
                if not re.fullmatch(r'[A-Za-z0-9_-]{20,64}', session) or not re.fullmatch(r'[0-9a-f]{64}', identifier):
                    return self.send_json({'error': 'Invalid request.'}, 400)
                item = durable.get(prefill_owner(session), identifier)
                if not item:
                    return self.send_json({'error': 'This upload is no longer available.'}, 404)
                if item['status'] == 'complete':
                    from resume_bot.prefill import profile_to_form
                    return self.send_json({'status': 'complete', 'candidate': profile_to_form(item.get('profile') or {})})
                return self.send_json({'status': item['status'], 'stage': item['stage'],
                                       'error': item.get('error') if item['status'] == 'error' else None})
            candidate, owner = self.candidate_session()
            if not owner:
                return self.send_json({'error': 'This private link is not valid. Use the link from your application.'}, 403)
            if url.path == '/apply/api/automation':
                profile = intake.profile(candidate) or {}
                profile.pop('form', None)
                return self.send_json({'jobs': automation.list(owner, query.get('id', [None])[0]),
                                       'ready': workspace.automation_ready(), 'candidate': profile})
            if url.path == '/apply/api/form':
                return self.send_json(intake.profile(candidate) or {})
            if url.path in ('/apply/api/artifact', '/apply/api/revision'):
                key = query.get('key', [''])[0]
                if private_owner(key) != owner:
                    return self.send_json({'error': 'Resume unavailable.'}, 404)
                if url.path == '/apply/api/revision':
                    revision = workspace.revision(key)
                    return self.send_json(revision) if revision else self.send_json({'error': 'Resume unavailable.'}, 404)
                name = query.get('name', [''])[0]
                path = workspace.artifact(key, name) if name in ('resume.pdf', 'main.tex', 'overleaf.pdf') else None
                if not path:
                    return self.send_json({'error': 'Resume file unavailable.'}, 404)
                mime = 'application/pdf' if path.suffix == '.pdf' else 'application/x-tex'
                return self.send_content(path.read_bytes(), mime, filename=path.name if path.suffix != '.pdf'
                                         or query.get('download') == ['1'] else None)
            return self.send_json({'error': 'Not found.'}, 404)

        def apply_post(self):
            from intake import FORM_LIMIT, IntakeDuplicate, IntakeError, prefill_owner
            public = self.public_origin()
            if public is None or self.headers.get('Origin') != public:
                return self.reject_post('Request not authorized.')
            if self.headers.get_content_type() != 'application/json':
                return self.reject_post('Invalid request.')
            path = urlsplit(self.path).path
            purpose = {'/apply/api/submit': 'submit', '/apply/api/prefill': 'prefill'}.get(path, 'action')
            if not intake.limiter.allow(purpose, self.intake_client(public)):
                return self.send_json({'error': 'Many applications are arriving from this network. Your form is kept '
                                                'and sent again automatically.', 'retry': True}, 429)
            if purpose == 'action':
                candidate, owner = self.candidate_session()
                action = path.rsplit('/', 1)[1]
                if not owner or path not in ('/apply/api/automation/answer', '/apply/api/automation/projects',
                                             '/apply/api/automation/regenerate', '/apply/api/automation/delete'):
                    return self.reject_post('This private link is not valid.')
                response = self.automation_action(public, action, owner)
                if action == 'delete':
                    intake.scrub_if_empty(owner)
                return response
            try:
                size = int(self.headers.get('Content-Length', ''))
            except ValueError:
                size = 0
            if not 0 < size <= (FORM_LIMIT if purpose == 'submit' else PREFILL_BODY):
                return self.send_json({'error': 'The upload is too large (PDF or DOCX up to 6 MB).'}, 413)
            if not intake.heavy.acquire(timeout=15):
                return self.send_json({'error': 'Busy receiving other applications. Sending again shortly.',
                                       'retry': True}, 503)
            try:
                self.connection.settimeout(45)
                raw = self.rfile.read(size)
                if len(raw) != size:
                    return self.send_json({'error': 'Upload interrupted. Sending again.', 'retry': True}, 400)
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError('Invalid request.')
                if purpose == 'prefill':
                    return self.prefill(body, prefill_owner)
                result = intake.submit(body.get('form'), body.get('key'), body.get('candidate'))
                session = body.get('prefill_session')
                if isinstance(session, str) and re.fullmatch(r'[A-Za-z0-9_-]{20,64}', session):
                    try:  # the uploaded document and its extraction are not kept once the form is in
                        durable.delete_owner(prefill_owner(session))
                    except sqlite3.Error:
                        pass
                return self.send_json(result, 201 if result['created'] else 200)
            except IntakeDuplicate as exc:
                return self.send_json({'error': str(exc), 'candidate': exc.candidate}, exc.status)
            except IntakeError as exc:
                return self.send_json({'error': str(exc), 'field': exc.field}, exc.status)
            except AutomationCapacity as exc:
                return self.send_json({'error': str(exc), 'retry': True}, 503)
            except sqlite3.OperationalError:
                return self.send_json({'error': 'Busy saving other applications. Sending again shortly.',
                                       'retry': True}, 503)
            except (ValueError, UnicodeError):
                return self.send_json({'error': 'Invalid request.'}, 400)
            except OSError:
                return self.send_json({'error': 'Upload interrupted. Sending again.', 'retry': True}, 408)
            finally:
                intake.heavy.release()

        def prefill(self, body, prefill_owner):
            """Read an uploaded PDF/DOCX now (no GPU) and queue its AI extraction for a fuller prefill."""
            from resume_bot.prefill import PREFILL_EXTENSIONS, document_text, quick_prefill
            extension, session = body.get('extension'), body.get('session')
            if extension not in PREFILL_EXTENSIONS or not isinstance(body.get('document'), str) \
                    or not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,64}', session):
                return self.send_json({'error': 'Upload a PDF or DOCX file.'}, 400)
            try:
                data = base64.b64decode(body['document'], validate=True)
                candidate = quick_prefill(document_text(extension, data))
            except (ValueError, binascii.Error) as exc:
                return self.send_json({'error': str(exc) if isinstance(exc, ValueError) and str(exc) else
                                       'Could not read this file. Upload a text-based PDF or a DOCX.'}, 400)
            ai = None
            identifier = hashlib.sha256(data).hexdigest()
            try:
                item = durable.enqueue(prefill_owner(session), identifier, '.' + extension, data)
                ai = {'id': identifier, 'status': item['status'] if item else 'queued'}
            except (ImportCapacity, ImportConflict, sqlite3.Error):
                ai = None  # the quick prefill stands; the AI reading is skipped while the reader is busy
            return self.send_json({'candidate': candidate, 'ai': ai})

        def intake_operator_post(self):
            from intake import IntakeError
            try:
                size = int(self.headers.get('Content-Length', ''))
                if not 0 < size <= 4096 or self.headers.get_content_type() != 'application/json':
                    return self.send_json({'error': 'Invalid request.'}, 413)
                request = json.loads(self.rfile.read(size))
                if self.path == '/api/intake/queue':
                    if not isinstance(request, dict) or not isinstance(request.get('paused'), bool):
                        return self.send_json({'error': 'Invalid request.'}, 400)
                    automation.set_paused(request['paused'])
                    return self.send_json({'paused': automation.paused()})
                candidate = request.get('candidate') if isinstance(request, dict) else None
                key = intake.new_key(candidate if isinstance(candidate, str) else '')
                return self.send_json({'candidate': candidate, 'link': (web_origin or origin) + '/apply/status#c='
                                       + candidate + '&k=' + key})
            except IntakeError as exc:
                return self.send_json({'error': str(exc)}, exc.status)
            except (ValueError, UnicodeError):
                return self.send_json({'error': 'Invalid request.'}, 400)

        def automation_action(self, request_origin, action, owner=None):
            """Owner-scoped follow-ups on one resume: answers to missing details, decisions on suggested
            projects, or regeneration for another role. Errors name the field they concern."""
            from resume_bot.automation import AnswerError
            owner = owner or self.session_owner(request_origin)
            if not owner:
                return self.reject_post('Sign in first.')
            try:
                size = int(self.headers.get('Content-Length', ''))
                if not 0 < size <= 131072 or self.headers.get_content_type() != 'application/json':
                    return self.send_json({'error': 'Invalid request.'}, 413)
                request = json.loads(self.rfile.read(size))
                if action == 'delete':
                    return self.delete_resumes(owner, request, request_origin)
                identifier = request.get('id') if isinstance(request, dict) else None
                if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}', identifier):
                    return self.send_json({'error': 'Invalid resume identifier.'}, 400)
                if action == 'answer':
                    automation.answer(owner, identifier, request.get('answers'), request.get('decisions'),
                                      request.get('certifications'))
                elif action == 'projects':
                    automation.decide_projects(owner, identifier, request.get('decisions'), request.get('certifications'))
                else:
                    if not workspace.automation_ready():
                        return self.send_json({'error': 'The resume watcher version is not ready. Retry shortly.'}, 503)
                    identifier = automation.regenerate(owner, identifier, request.get('role'))
                    return self.send_json({'id': identifier, 'status': 'queued'}, 202)
            except AnswerError as exc:
                return self.send_json({'error': str(exc), 'field': exc.field}, 400)
            except (ValueError, UnicodeError) as exc:
                return self.send_json({'error': str(exc) if action == 'regenerate' else 'Invalid request.'}, 400)
            return self.send_json({'id': identifier, 'status': 'queued'})

        def audit_client(self, request_origin):
            """Who asked, for the deletion record: the access path, the client address (Cloudflare's view of it on
            the public site) and the browser, without the session token or any request body."""
            kind = ('web' if request_origin == web_origin else 'phone' if request_origin == phone_origin
                    else 'loopback')
            address = (self.headers.get('Cf-Connecting-IP') if kind == 'web' else None) or self.client_address[0]
            return {'via': kind, 'address': str(address)[:64],
                    'agent': ' '.join(str(self.headers.get('User-Agent', '')).split())[:200]}

        def delete_resumes(self, owner, request, request_origin=None):
            """Permanent deletion for the signed-in account only. The confirmation is checked here, not
            only in the page: {"id": <id>, "confirm": "DELETE"} or {"all": true, "confirm": "DELETE ALL"}.
            Every deletion request that reaches the deletion itself is recorded (automation.audit)."""
            from automation_jobs import (DELETE_ALL_PHRASE, DELETE_ONE_PHRASE, DeleteBusy, DeleteFailed,
                                         DeleteRefused)
            if not isinstance(request, dict):
                return self.send_json({'error': 'Invalid request.'}, 400)
            everything = request.get('all') is True
            if everything:
                if set(request) != {'all', 'confirm'} or request['confirm'] != DELETE_ALL_PHRASE:
                    return self.send_json({'error': 'Type DELETE ALL to confirm. Nothing was deleted.',
                                           'field': 'confirm'}, 400)
                identifier = None
            else:
                identifier = request.get('id')
                if set(request) != {'id', 'confirm'} or not isinstance(identifier, str) \
                        or not re.fullmatch(r'[0-9a-f]{64}', identifier):
                    return self.send_json({'error': 'Invalid resume identifier.'}, 400)
                if request['confirm'] != DELETE_ONE_PHRASE:
                    return self.send_json({'error': 'Confirm the deletion first. Nothing was deleted.'}, 400)
            try:
                result = automation.delete(owner, identifier, client=self.audit_client(request_origin))
            except DeleteRefused as exc:
                return self.send_json({'error': str(exc)}, exc.status)
            except DeleteBusy as exc:
                return self.send_json({'error': str(exc)}, 503)
            except DeleteFailed as exc:
                return self.send_json({'error': str(exc)}, 500)
            if everything:
                try:
                    result['drafts'] = durable.delete_owner(owner)
                except sqlite3.Error:
                    return self.send_json({**result, 'error': 'Your resumes were deleted, but saved import drafts '
                                                              'could not be removed. Run Delete all again.'}, 500)
            return self.send_json(result)

        def save_roles(self):
            """Operator: replace the managed role catalog after validation. Takes effect immediately."""
            from resume_bot.workflow import catalog_path, load_roles, validate_catalog
            try:
                size = int(self.headers.get('Content-Length', ''))
                if not 0 < size <= 524288 or self.headers.get_content_type() != 'application/json':
                    return self.send_json({'error': 'Send the catalog as JSON up to 512 KB.'}, 413)
                catalog = json.loads(self.rfile.read(size))
                roles, _ = validate_catalog(catalog)
            except (ValueError, UnicodeError) as exc:
                return self.send_json({'error': f'Catalog not saved: {exc}'}, 400)
            path = catalog_path()
            temporary = path.with_name(path.name + '.tmp')
            temporary.write_text(json.dumps(catalog, indent=1, ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, path)
            load_roles(force=True)
            return self.send_json({'saved': len(roles), 'file': path.name})

        def reject_post(self, message):
            # Closing a socket with unread request bytes can reset it before Windows clients see
            # the 403. Drain small requests without accepting the payload or waiting indefinitely.
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if 0 < size <= 65536:
                    self.connection.settimeout(1)
                    self.rfile.read(size)
            except (ValueError, OSError):
                pass
            return self.send_json({'error': message}, 403)

        def do_POST(self):
            if self.path.startswith('/apply/'):
                return self.apply_post()
            request_origin = self.request_origin()
            if (self.path not in ('/api/automation', '/api/automation/answer', '/api/automation/projects',
                                  '/api/automation/regenerate', '/api/automation/delete', '/api/intake/key',
                                  '/api/intake/queue', '/api/roles', '/api/queue', '/api/draft', '/api/preview', '/api/keywords/update', '/api/keywords/extract', '/api/retry', '/api/ai-draft', '/api/ai-review', '/api/ai-queue') or request_origin is None
                    or self.headers.get('Origin') != request_origin
                    or not secrets.compare_digest(self.headers.get('X-Dashboard-Token', ''), token)):
                return self.reject_post('Request not authorized.')
            if not self.account_matches(request_origin):
                return self.reject_post('Dashboard access denied for this session.')
            if self.path == '/api/automation':
                return self.submit_automation(request_origin)
            if self.path in ('/api/automation/answer', '/api/automation/projects', '/api/automation/regenerate', '/api/automation/delete'):
                return self.automation_action(request_origin, self.path.rsplit('/', 1)[1])
            if self.path in ('/api/intake/key', '/api/intake/queue'):
                if not self.is_operator(request_origin):
                    return self.reject_post('An operator account is required for this route.')
                return self.intake_operator_post()
            if self.path == '/api/roles':
                if not self.is_operator(request_origin):
                    return self.reject_post('An operator account is required for this route.')
                return self.save_roles()
            if self.path in ('/api/queue', '/api/retry', '/api/ai-queue', '/api/keywords/update') and not self.is_operator(request_origin):
                return self.reject_post('An operator account is required for this route.')
            drafting = self.path == '/api/draft'
            from resume_bot.ollama import OllamaError
            extension = self.headers.get('X-Resume-Extension', '').lower()
            if drafting and extension not in DOCUMENTS | OFFICE | IMAGES:
                return self.send_json({'error': 'Unsupported résumé format. Try DOCX, PDF, TXT, Markdown, DOC, ODT, RTF, or an image.'}, 415)
            if not drafting and self.headers.get_content_type() != 'application/json':
                return self.send_json({'error': 'Choose a verified candidate JSON file.'}, 415)
            import_id = self.headers.get('X-Resume-Import-ID', '') if drafting else ''
            asynchronous = drafting and self.headers.get('X-Resume-Async') == '1'
            if import_id and not re.fullmatch(r'[0-9a-f]{32}', import_id):
                return self.send_json({'error': 'Invalid import identifier.'}, 400)
            if asynchronous and not import_id:
                return self.send_json({'error': 'An import identifier is required.'}, 400)

            owner = self.session_owner(request_origin) if drafting and import_id else None
            if drafting and import_id and not owner:
                return self.send_json({'error': 'Dashboard access denied for this session.'}, 403)
            import_key = (owner, import_id)
            draft_stage = None

            def report(stage, metrics=None):
                nonlocal draft_stage
                draft_stage = stage
                if import_id:
                    with import_lock:
                        import_stages[import_key] = stage

            limit = MAX_DOCUMENT if drafting else MAX_INPUT
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= limit:
                    return self.send_json({'error': f'Choose a file up to {limit // MAX_INPUT} MB.'}, 413)
                data = self.rfile.read(length)
                if len(data) != length:
                    return self.send_json({'error': 'Upload incomplete; retry using the same import identifier.'}, 400)
                if asynchronous:
                    try:
                        item = durable.enqueue(owner, import_id, extension, data)
                    except ImportConflict as exc:
                        return self.send_json({'error': str(exc)}, 409)
                    except ImportCapacity as exc:
                        return self.send_json({'error': str(exc)}, 429)
                    return self.send_json({'id': import_id, **item}, 202)
                if drafting:
                    if not import_id:
                        return self.send_json(workspace.draft(extension, data))
                    with import_lock:
                        if import_key in import_stages:
                            return self.send_json({'error': 'This import is already processing.'}, 409)
                        import_stages[import_key] = 'Reading document'
                    try:
                        draft = workspace.draft(extension, data, report)
                    finally:
                        with import_lock:
                            import_stages.pop(import_key, None)
                    return self.send_json(draft)
                if self.path.startswith('/api/ai-'):
                    from resume_bot.ai_first import validate_intake
                    from resume_bot.ai_review import approved_resume, role_intelligence
                    account = self.session_owner(request_origin)
                    request = json.loads(data)
                    if not account or not isinstance(request, dict):
                        return self.send_json({'error': 'Expected an authenticated review request.'}, 400)
                    operation = request.get('id')
                    if not isinstance(operation, str) or not re.fullmatch(r'[0-9a-f]{32}', operation):
                        raise ValueError('Invalid draft identifier')
                    if self.path == '/api/ai-draft':
                        candidate = validate_intake(request.get('candidate'))
                        role_intelligence(candidate['role'])
                        source = ''
                        evidence_id = request.get('import_id')
                        if evidence_id:
                            if not isinstance(evidence_id, str) or not re.fullmatch(r'[0-9a-f]{32}', evidence_id):
                                raise ValueError('Invalid source import identifier')
                            source_draft = durable.get(account, evidence_id)
                            if not source_draft or source_draft['status'] != 'complete' or 'profile' not in source_draft:
                                raise ValueError('The source import expired or is unavailable. Import the file again.')
                            source = source_draft['source']
                        try:
                            item = durable.enqueue(account, operation, '.ai',
                                                   json.dumps({'candidate': candidate, 'source': source}).encode())
                        except ImportConflict as exc:
                            return self.send_json({'error': str(exc)}, 409)
                        except ImportCapacity as exc:
                            return self.send_json({'error': str(exc)}, 429)
                        return self.send_json({'id': operation, **item}, 202)
                    draft = durable.get(account, operation)
                    if not draft or draft['status'] != 'complete' or 'suggestions' not in draft:
                        return self.send_json({'error': 'AI draft is incomplete, expired, or unavailable.'}, 404)
                    reviewed = approved_resume(draft, request.get('approval'))
                    if self.path == '/api/ai-review':
                        return self.send_json(reviewed)
                    payload = {key: reviewed[key] for key in ('profile', 'target')}
                    try:
                        durable.freeze_review(account, operation, {'resume': payload, 'claims': reviewed['approved_claims']})
                    except ImportConflict as exc:
                        return self.send_json({'error': str(exc)}, 409)
                    result = workspace.enqueue(json.dumps(payload).encode(), approval={
                        'operation': operation, 'approved_by': account, 'claims': reviewed['approved_claims'],
                        'project_names': reviewed['project_names'],
                        'market_sources': draft.get('intelligence', {}).get('sources', [])})
                    return self.send_json(result, 201 if result['created'] else 200)
                if self.path == '/api/retry':
                    from resume_bot.pipeline import request_retry
                    request = json.loads(data.decode('utf-8-sig'))
                    name = request.get('name', '') if isinstance(request, dict) else ''
                    job = next((item for item in workspace.status()['jobs'] if item['name'] == name), None)
                    if not job or job['status'] != 'failed' or not self.can_read_job(name, request_origin):
                        raise ValueError('Only a failed job can be retried')
                    request_retry(workspace.inbox, name)
                    return self.send_json({'name': name, 'retry': 'requested'})
                if self.path.startswith('/api/keywords/'):
                    from resume_bot.workflow import matched_terms
                    request = json.loads(data.decode('utf-8-sig'))
                    if not isinstance(request, dict):
                        raise ValueError('Expected keyword settings')
                    role = request.get('role')
                    if self.path.endswith('/update'):
                        return self.send_json(workspace.save_keywords(role, request.get('keywords')))
                    description = request.get('description', '')
                    if not isinstance(description, str) or len(description) > 15000:
                        raise ValueError('Job description must be text under 15,000 characters')
                    terms = workspace.keywords(role)['catalog']
                    return self.send_json({'keywords': matched_terms(terms, description) if description.strip() else terms})
                if self.path == '/api/preview':
                    from resume_bot.workflow import analyze
                    profile, target = workspace.normalize(json.loads(data.decode('utf-8-sig')))
                    if target is None:
                        raise ValueError('Select a target role and template')
                    return self.send_json({'profile': profile, 'ats': analyze(profile, target)})
                result = workspace.enqueue(data)
            except (ValueError, UnicodeError) as exc:
                return self.send_json({'error': str(exc), 'stage': draft_stage}, 400)
            except OllamaError:
                return self.send_json({'error': 'Local Ollama extraction failed. Check that the configured model is running and retry.',
                                       'stage': draft_stage}, 503)
            except OSError:
                return self.send_json({'error': 'Could not read the document or write to the watcher inbox.',
                                       'stage': draft_stage}, 500)
            return self.send_json(result, 201 if result['created'] else 200)

    return DashboardHandler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--code-root', type=Path, default=os.environ.get('RESUMEOPS_CODE_ROOT'),
                        help='ResumeOps source checkout when private data lives elsewhere')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--model', default=os.environ.get('OLLAMA_MODEL', 'qwen2.5:7b'))
    parser.add_argument('--phone-url', help='HTTPS URL printed by Tailscale Serve')
    parser.add_argument('--phone-user', help='Allowed Tailscale login name')
    parser.add_argument('--web-url', help='Dashboard HTTPS origin on Cloudflare')
    parser.add_argument('--access-team', help='Cloudflare Zero Trust team name')
    parser.add_argument('--access-audience', help='Cloudflare Access application AUD tag')
    parser.add_argument('--ai-first', action='store_true',
                        help='Serve the AI builder at / (the classic builder stays at /workflow); '
                             'the supervisor sets RESUMEOPS_AI_FIRST=1 instead')
    args = parser.parse_args()
    # One GPU gate for this dashboard and the resume watcher: a folder in the shared workspace.
    os.environ.setdefault('RESUMEOPS_GPU_GATE', str(Path(args.root).resolve() / '.resumeops-gpu'))
    web_url = args.web_url.rstrip('/') if args.web_url else None
    access_options = (web_url, args.access_team, args.access_audience)
    verifier = None
    if any(access_options):
        if not all(access_options):
            parser.error('Use --web-url, --access-team, and --access-audience together.')
        if not re.fullmatch(r'https://(?:[a-z0-9-]+\.)+[a-z0-9-]+', web_url):
            parser.error('Use the exact HTTPS dashboard origin, with no path.')
        if not re.fullmatch(r'[a-z0-9-]+', args.access_team):
            parser.error('Use the Cloudflare team name, without the cloudflareaccess.com suffix.')
        try:
            from cloudflare_access import AccessVerifier
            verifier = AccessVerifier(args.access_team, args.access_audience)
            import cryptography
        except ImportError:
            parser.error('Install Cloudflare Access support with py -3 -m pip install "PyJWT[crypto]>=2.10,<3".')
    if bool(args.phone_url) != bool(args.phone_user):
        parser.error('Use --phone-url and --phone-user together.')
    phone_url = args.phone_url.rstrip('/') if args.phone_url else None
    if phone_url and not re.fullmatch(r'https://(?:[a-z0-9-]+\.)+[a-z0-9-]+\.ts\.net(?::[0-9]+)?', phone_url):
        parser.error('Use the HTTPS .ts.net URL printed by Tailscale Serve.')
    if not ((args.code_root or args.root) / 'resume_bot/profile.py').is_file():
        parser.error('Point --code-root or --root to the ResumeOps source checkout.')
    origin = f'http://127.0.0.1:{args.port}'
    request_handler = handler(
        Workspace(args.root, args.model, args.code_root), origin, secrets.token_hex(32), phone_url, args.phone_user,
        web_origin=web_url, access_verifier=verifier, ai_first=args.ai_first or os.environ.get('RESUMEOPS_AI_FIRST') == '1',
        automation_first=os.environ.get('RESUMEOPS_AUTOMATION_FIRST') == '1',
    )
    request_handler.resume_imports()
    server = DashboardServer(('127.0.0.1', args.port), request_handler)
    print(f'Resume Builder dashboard: {origin}', flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
        request_handler.stop_imports()


if __name__ == '__main__':
    main()
