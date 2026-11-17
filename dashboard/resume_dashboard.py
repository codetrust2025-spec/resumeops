"""Local dashboard for the existing Resume Builder watcher."""
import argparse
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from durable_imports import DurableImports, ImportCapacity, ImportConflict
from urllib.parse import parse_qs, urlsplit
from zipfile import BadZipFile, ZipFile

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


def contained(path, directory):
    return path.resolve().is_relative_to(directory.resolve())


class Workspace:
    def __init__(self, root, model="qwen2.5:7b", code_root=None):
        self.root = root.resolve()
        self.model = model
        self.inbox = self.root / 'inbox'
        self.output = self.root / 'output'
        self.logs = self.root / 'logs'
        self.approvals = self.root / 'approvals'
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
        revisions = []
        for path in self.output.glob('*/*/result.json'):
            if not contained(path, self.output):
                continue
            result = read_json(path)
            if not isinstance(result, dict):
                continue
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            remote = result.get('overleaf') or {}
            url = remote.get('url', '')
            if not re.fullmatch(r'https://www\.overleaf\.com/project/[A-Za-z0-9_-]+', url):
                url = ''
            revisions.append({
                'key': path.parent.relative_to(self.output).as_posix(),
                'candidate': result.get('candidate', 'Candidate'),
                'model': result.get('model', ''),
                'ats': result.get('ats'),
                'modified': modified, 'url': url,
                'pages': (result.get('pdf_checks') or {}).get('pages'),
                'sha256': remote.get('pdf_sha256', ''),
                'files': [name for name in ARTIFACTS if (path.parent / name).is_file()
                          and contained(path.parent / name, self.output)],
            })
        revisions.sort(key=lambda item: item['modified'], reverse=True)
        alert = self.logs / 'current-alert.txt'
        last_scan = (health or {}).get('last_scan')
        return {'jobs': jobs, 'revisions': revisions,
                'lastScan': last_scan, 'healthy': bool(last_scan and time.time() - last_scan < 900),
                'worker': {key: (health or {}).get(key) for key in ('pid', 'started', 'ollama', 'overleaf', 'mongodb')},
                'alert': alert.is_file(), 'stateReadable': state is not None or not (
                    self.inbox / '.resume-bot-state.json').exists(),
                'logs': [name for name in LOGS if (self.logs / name).is_file()
                         and contained(self.logs / name, self.logs)]}

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
            report('Extracting candidate details with Ollama')
            model = Ollama(model=self.model, url='http://127.0.0.1:11434')

            def measured(metrics):
                # Extraction output length is unknown, so only measured counters are reported.
                report('Extracting candidate details with Ollama', {
                    'phase': metrics['phase'], 'chars': metrics['chars'], 'tokens': metrics['chunks'],
                    'elapsed': round(metrics.get('elapsed', 0), 1)})
            return {'profile': extract_profile(source_text, model, for_review=True, on_progress=measured),
                    'source': source_text}

    @staticmethod
    def _tool(arguments):
        try:
            return subprocess.run(arguments, capture_output=True, encoding='utf-8',
                                  errors='replace', check=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError('Local document conversion failed. Try exporting the file as DOCX or text-based PDF.') from exc

    def enqueue(self, data, approval=None):
        profile, target = self.normalize(json.loads(data.decode('utf-8-sig')))
        if target and target.get('approved') and approval is None:
            raise ValueError('Only the AI review can mark a resume as candidate-approved')
        if approval is not None:
            if not target:
                raise ValueError('An approved resume needs a target role')
            target['approved'] = True
        request = {'profile': profile, 'target': target} if target else profile
        payload = json.dumps(request, ensure_ascii=False, indent=2).encode('utf-8')
        slug = re.sub(r'[^a-z0-9]+', '-', profile['full_name'].lower()).strip('-')[:45] or 'candidate'
        digest = hashlib.sha256(payload).hexdigest()
        target = self.inbox / f'{slug}-{digest}.json'
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
                   'attested': True, 'claims': approval['claims'], 'project_names': approval.get('project_names', [])}
        identity = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:32]
        record = {**content, 'record_id': identity,
                  'recorded_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        target = self.approvals / Path(job).stem / f'{identity}.json'
        self.publish(target, json.dumps(record, ensure_ascii=False, indent=2).encode('utf-8'))
        return target

    def approvals_for(self, job):
        from resume_bot.claim_audit import records_for
        return records_for(self.approvals, job)

    def revision_key(self, output):
        path = Path(output) if output else None
        return path.resolve().relative_to(self.output.resolve()).as_posix() if path and contained(path, self.output) else ''

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
        path = self.output / key / name
        return path if contained(path, self.output) and path.is_file() else None


RENEWED_PAGE = b'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Signed in \xc2\xb7 ResumeOps</title>
<link rel="stylesheet" href="/builder.css"><script defer src="/auth-renewed.js"></script></head>
<body><main class="width intro"><h1>Signed in</h1><p>ResumeOps is resuming your work.</p></main></body></html>'''


def handler(workspace, origin, token, phone_origin=None, phone_user=None, *,
            web_origin=None, access_verifier=None, ai_first=False):
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
        intelligence = role_intelligence(candidate['role'])
        report('Drafting suggestions with Ollama')
        model = Ollama(model=workspace.model, url='http://127.0.0.1:11434')
        draft = suggest_sections(candidate, model, evidence=evidence, market_terms=intelligence['terms'])
        draft.update(source=evidence, intelligence=intelligence)
        return {**draft, 'claims': claims_for_review(draft)}

    durable = DurableImports(workspace.root, process_draft)

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

        @staticmethod
        def stop_imports():
            durable.stop()

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

        def do_GET(self):
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
            if url.path == '/api/catalog':
                from resume_bot.workflow import ROLES, TEMPLATES
                return self.send_json({'roles': list(ROLES.values()), 'templates': TEMPLATES})
            if url.path == '/api/keywords':
                try:
                    return self.send_json(workspace.keywords(query.get('role', [''])[0]))
                except ValueError as error:
                    return self.send_json({'error': str(error)}, 400)
            if url.path == '/api/revision':
                revision = workspace.revision(query.get('key', [''])[0])
                if revision:
                    return self.send_json(revision)
                return self.send_json({'error': 'Resume revision unavailable.'}, 404)
            if url.path == '/api/status':
                # Server time lets browsers compute elapsed time without trusting their own clock.
                return self.send_json({**workspace.status(), 'now': time.time()})
            if url.path == '/api/artifact':
                path = workspace.artifact(query.get('key', [''])[0], query.get('name', [''])[0])
                if path:
                    mime = 'application/pdf' if path.suffix == '.pdf' else (
                        mimetypes.guess_type(path)[0] or 'application/octet-stream')
                    return self.send_content(path.read_bytes(), mime,
                                             filename=path.name if path.suffix != '.pdf' or query.get('download') == ['1'] else None)
            if url.path == '/api/log':
                name = query.get('name', [''])[0]
                path = workspace.logs / name
                if name in LOGS and contained(path, workspace.logs) and path.is_file():
                    with path.open('rb') as stream:
                        stream.seek(max(0, path.stat().st_size - 65536))
                        data = stream.read()
                    return self.send_content(data.decode('utf-8-sig', errors='replace').encode('utf-8'), 'text/plain; charset=utf-8')
            assets = {'/': ('ai-builder.html' if ai_first else 'index.html', 'text/html'),
                      '/workflow': ('index.html', 'text/html'),
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
                if url.path in ('/', '/operations', '/ai-builder', '/workflow'):
                    data = data.replace(b'__TOKEN__', token.encode('ascii'))
                return self.send_content(data, mime + '; charset=utf-8')
            return self.send_json({'error': 'File unavailable.'}, 404)

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
            request_origin = self.request_origin()
            if (self.path not in ('/api/queue', '/api/draft', '/api/preview', '/api/keywords/update', '/api/keywords/extract', '/api/retry', '/api/ai-draft', '/api/ai-review', '/api/ai-queue') or request_origin is None
                    or self.headers.get('Origin') != request_origin
                    or not secrets.compare_digest(self.headers.get('X-Dashboard-Token', ''), token)):
                return self.reject_post('Request not authorized.')
            if not self.account_matches(request_origin):
                return self.reject_post('Dashboard access denied for this session.')
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
                        'project_names': reviewed['project_names']})
                    return self.send_json(result, 201 if result['created'] else 200)
                if self.path == '/api/retry':
                    from resume_bot.pipeline import request_retry
                    request = json.loads(data.decode('utf-8-sig'))
                    name = request.get('name', '') if isinstance(request, dict) else ''
                    job = next((item for item in workspace.status()['jobs'] if item['name'] == name), None)
                    if not job or job['status'] != 'failed':
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
    )
    request_handler.resume_imports()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), request_handler)
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
