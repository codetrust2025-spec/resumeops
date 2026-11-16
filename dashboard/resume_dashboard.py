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
            revision = ''
            if status == 'ok' and isinstance(record.get('output'), str):
                built = Path(record['output'])
                if built.is_absolute() and contained(built, self.output) and (built / 'result.json').is_file():
                    revision = built.resolve().relative_to(self.output).as_posix()
            jobs.append({'name': path.name, 'status': status or 'queued',
                         'last': last or modified, 'failures': record.get('failures', 0) if status else 0,
                         'error': record.get('error', '') if status == 'failed' else '',
                         'candidate': record.get('candidate', '') if status else '',
                         'blocked': bool(record.get('blocked')) if status else False,
                         'percent': record.get('percent', 0) if status else 0,
                         'stage': record.get('stage', '') if status else '',
                         'updated': record.get('updated', last or modified) if status else modified,
                         'revision': revision})
        jobs.sort(key=lambda job: job['last'], reverse=True)
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
            ats = result.get('ats')
            revisions.append({
                'key': path.parent.relative_to(self.output).as_posix(),
                'candidate': result.get('candidate', 'Candidate'),
                'model': result.get('model', ''),
                'ats': ats if isinstance(ats, dict) else None,
                'overleafStatus': remote.get('status', ''),
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
                'alert': alert.is_file(), 'stateReadable': state is not None or not (
                    self.inbox / '.resume-bot-state.json').exists(),
                'logs': [name for name in LOGS if (self.logs / name).is_file()
                         and contained(self.logs / name, self.logs)]}

    def settings(self):
        from resume_bot.workflow import ROLES, TEMPLATES
        ollama = {'url': 'http://127.0.0.1:11434', 'model': self.model, 'reachable': False,
                  'modelInstalled': False, 'loaded': []}
        from urllib.request import urlopen
        try:
            with urlopen(ollama['url'] + '/api/tags', timeout=1.5) as response:
                names = [m.get('name', '') for m in json.load(response).get('models', [])]
            ollama['reachable'] = True
            ollama['modelInstalled'] = self.model in names or self.model + ':latest' in names
            with urlopen(ollama['url'] + '/api/ps', timeout=1.5) as response:
                ollama['loaded'] = [{'name': m.get('name', ''), 'gpu': int(m.get('size_vram', 0) or 0) > 0}
                                    for m in json.load(response).get('models', [])]
        except (OSError, ValueError, AttributeError):
            pass
        return {'ollama': ollama, 'roles': len(ROLES), 'templates': TEMPLATES,
                'mongodb': bool(os.environ.get('RESUMEOPS_MONGODB_URI')),
                'alertWebhook': bool(os.environ.get('RESUME_BUILDER_ALERT_WEBHOOK'))}

    def draft(self, extension, data, report=lambda stage: None):
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
            return {'profile': extract_profile(source_text, model, for_review=True), 'source': source_text}

    @staticmethod
    def _tool(arguments):
        try:
            return subprocess.run(arguments, capture_output=True, encoding='utf-8',
                                  errors='replace', check=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError('Local document conversion failed. Try exporting the file as DOCX or text-based PDF.') from exc

    def enqueue(self, data):
        profile, target = self.normalize(json.loads(data.decode('utf-8-sig')))
        request = {'profile': profile, 'target': target} if target else profile
        payload = json.dumps(request, ensure_ascii=False, indent=2).encode('utf-8')
        slug = re.sub(r'[^a-z0-9]+', '-', profile['full_name'].lower()).strip('-')[:45] or 'candidate'
        digest = hashlib.sha256(payload).hexdigest()
        target = self.inbox / f'{slug}-{digest}.json'
        if target.exists():
            return {'name': target.name, 'created': False}
        fd, temporary = tempfile.mkstemp(prefix='.dashboard-', suffix='.tmp', dir=self.inbox)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # A hard link publishes the finished file atomically without overwriting a job.
                os.link(temporary, target)
            except FileExistsError:
                return {'name': target.name, 'created': False}
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {'name': target.name, 'created': True}

    def artifact(self, key, name):
        if name not in ARTIFACTS or not re.fullmatch(r'[^/\\]+/[^/\\]+', key):
            return None
        path = self.output / key / name
        return path if contained(path, self.output) and path.is_file() else None


def handler(workspace, origin, token, phone_origin=None, phone_user=None, *,
            web_origin=None, access_verifier=None):
    host = urlsplit(origin).netloc
    phone_host = urlsplit(phone_origin).netloc if phone_origin else None
    web_host = urlsplit(web_origin).netloc if web_origin else None

    import_stages = {}
    import_lock = Lock()

    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_content(self, data, mime, status=200, filename=None):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            # Verified PDFs may be previewed inside the dashboard itself; nothing else is frameable.
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

        def do_GET(self):
            if self.request_origin() is None:
                return self.send_json({'error': 'Dashboard access denied for this address or session.'}, 403)
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == '/api/import-progress':
                if not secrets.compare_digest(self.headers.get('X-Dashboard-Token', ''), token):
                    return self.send_json({'error': 'Request not authorized.'}, 403)
                with import_lock:
                    stage = import_stages.get(query.get('id', [''])[0])
                return self.send_json({'stage': stage})
            if url.path == '/api/catalog':
                from resume_bot.workflow import CATALOG, ROLES, TEMPLATES
                return self.send_json({'roles': list(ROLES.values()), 'templates': TEMPLATES,
                                       'keywordGroups': CATALOG})
            if url.path == '/api/status':
                return self.send_json(workspace.status())
            if url.path == '/api/health':
                status = workspace.status()
                return self.send_json({'status': 'OK' if status['healthy'] else 'DEGRADED',
                                       'watcher': status['healthy'], 'lastScan': status['lastScan'],
                                       'timestamp': time.time()})
            if url.path == '/api/settings':
                access = ['Local loopback'] + (['Tailscale phone'] if phone_origin else []) + (
                    ['Cloudflare Access ' + web_host] if web_origin else [])
                return self.send_json({**workspace.settings(), 'access': access})
            if url.path == '/api/artifact':
                path = workspace.artifact(query.get('key', [''])[0], query.get('name', [''])[0])
                if path:
                    mime = 'application/pdf' if path.suffix == '.pdf' else (
                        mimetypes.guess_type(path)[0] or 'application/octet-stream')
                    return self.send_content(path.read_bytes(), mime,
                                             filename=path.name if path.suffix != '.pdf' else None)
            if url.path == '/api/log':
                name = query.get('name', [''])[0]
                path = workspace.logs / name
                if name in LOGS and contained(path, workspace.logs) and path.is_file():
                    with path.open('rb') as stream:
                        stream.seek(max(0, path.stat().st_size - 65536))
                        data = stream.read()
                    return self.send_content(data.decode('utf-8-sig', errors='replace').encode('utf-8'), 'text/plain; charset=utf-8')
            assets = {'/': ('index.html', 'text/html'),
                      '/dashboard.css': ('dashboard.css', 'text/css'),
                      '/dashboard.js': ('dashboard.js', 'text/javascript'),
                      '/workflow.js': ('workflow.js', 'text/javascript'),
                      '/core.js': ('core.js', 'text/javascript')}
            if url.path in assets:
                name, mime = assets[url.path]
                path = Path(__file__).with_name(name)
                data = path.read_bytes()
                if url.path == '/':
                    data = data.replace(b'__TOKEN__', token.encode('ascii'))
                return self.send_content(data, mime + '; charset=utf-8')
            return self.send_json({'error': 'File unavailable.'}, 404)

        def do_POST(self):
            request_origin = self.request_origin()
            if (self.path not in ('/api/queue', '/api/draft', '/api/preview', '/api/jd-keywords')
                    or request_origin is None
                    or self.headers.get('Origin') != request_origin
                    or not secrets.compare_digest(self.headers.get('X-Dashboard-Token', ''), token)):
                return self.send_json({'error': 'Request not authorized.'}, 403)
            drafting = self.path == '/api/draft'
            from resume_bot.ollama import OllamaError
            extension = self.headers.get('X-Resume-Extension', '').lower()
            if drafting and extension not in DOCUMENTS | OFFICE | IMAGES:
                return self.send_json({'error': 'Unsupported résumé format. Try DOCX, PDF, TXT, Markdown, DOC, ODT, RTF, or an image.'}, 415)
            if not drafting and self.headers.get_content_type() != 'application/json':
                return self.send_json({'error': 'Choose a verified candidate JSON file.'}, 415)
            import_id = self.headers.get('X-Resume-Import-ID', '') if drafting else ''
            if import_id and not re.fullmatch(r'[0-9a-f]{32}', import_id):
                return self.send_json({'error': 'Invalid import identifier.'}, 400)

            draft_stage = None

            def report(stage):
                nonlocal draft_stage
                draft_stage = stage
                if import_id:
                    with import_lock:
                        import_stages[import_id] = stage

            limit = MAX_DOCUMENT if drafting else MAX_INPUT
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= limit:
                    return self.send_json({'error': f'Choose a file up to {limit // MAX_INPUT} MB.'}, 413)
                data = self.rfile.read(length)
                if drafting:
                    if not import_id:
                        return self.send_json(workspace.draft(extension, data))
                    with import_lock:
                        if import_id in import_stages:
                            return self.send_json({'error': 'This import is already processing.'}, 409)
                        import_stages[import_id] = 'Reading document'
                    try:
                        draft = workspace.draft(extension, data, report)
                    finally:
                        with import_lock:
                            import_stages.pop(import_id, None)
                    return self.send_json(draft)
                if self.path == '/api/jd-keywords':
                    from resume_bot.workflow import job_description_keywords
                    request = json.loads(data.decode('utf-8-sig'))
                    if not isinstance(request, dict):
                        raise ValueError('Paste a job description')
                    return self.send_json({'keywords': job_description_keywords(request.get('text', ''))})
                if self.path == '/api/preview':
                    from resume_bot.latex import render
                    from resume_bot.workflow import analyze, assess
                    profile, target = workspace.normalize(json.loads(data.decode('utf-8-sig')))
                    if target is None:
                        raise ValueError('Select a target role and template')
                    return self.send_json({'profile': profile, 'ats': analyze(profile, target),
                                           'report': assess(profile, target),
                                           'latex': render(profile, target['template'])})
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
        web_origin=web_url, access_verifier=verifier,
    )
    server = ThreadingHTTPServer(('127.0.0.1', args.port), request_handler)
    print(f'Resume Builder dashboard: {origin}', flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == '__main__':
    main()
