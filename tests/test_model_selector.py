"""Model selection with a loopback Ollama stand-in and a disposable workspace."""
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from resume_bot import __main__ as cli, pipeline
from resume_bot.ollama import Ollama, OllamaUnavailableError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from resume_dashboard import Workspace, handler  # noqa: E402
from test_extraction_recovery import browser_or_skip  # noqa: E402


class ModelSelectorTests(unittest.TestCase):
    def serve(self, request_handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), request_handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f'http://127.0.0.1:{server.server_port}'

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.models = [{'name': 'second:latest', 'size': 2 * 1024 ** 3, 'digest': 'b' * 64},
                       {'model': 'first:latest', 'size': 1024 ** 3, 'digest': 'a' * 64}, {'name': 'first:latest'},
                       {'name': 'remote:latest', 'remote_host': 'https://example.test'}, {'name': 'third:cloud'}]
        self.generated = []
        self.reported_model = None
        self.loaded_model = 'first:latest'
        self.available = True
        fixture = self

        class FakeOllama(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, data, status=200):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def do_GET(self):
                if self.path == '/api/tags':
                    self.reply({'models': fixture.models}, 200 if fixture.available else 503)
                elif self.path == '/api/ps':
                    match = next((item for item in fixture.models
                                  if (item.get('name') or item.get('model')) == fixture.loaded_model), {})
                    self.reply({'models': [{**match, 'name': fixture.loaded_model, 'size_vram': 1024 ** 3}]})
                else:
                    self.reply({}, 404)

            def do_POST(self):
                if self.path == '/api/generate':
                    request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    fixture.generated.append(request['model'])
                    fixture.loaded_model = fixture.reported_model or request['model']
                    answer = {'resumeops_model_probe': True} if 'resumeops_model_probe' in request['prompt'] else {}
                    self.reply({'response': json.dumps(answer), 'model': fixture.loaded_model},
                               200 if fixture.available else 404)
                else:
                    self.reply({}, 404)

        _, self.ollama_url = self.serve(FakeOllama)
        self.enterContext(patch.dict('os.environ', {'OLLAMA_URL': self.ollama_url,
                                                   'RESUMEOPS_GPU_GATE': str(self.root / 'gpu')}))
        self.workspace = Workspace(self.root, 'first:latest', ROOT)
        server, self.origin = self.serve(BaseHTTPRequestHandler)
        verifier = SimpleNamespace(accepts=lambda token: token == 'member',
                                   claims=lambda token: {'email': 'member@example.test'})
        with patch.dict('os.environ', {'RESUMEOPS_OPERATOR_ACCOUNTS': 'operator@example.test'}):
            server.RequestHandlerClass = handler(self.workspace, self.origin, 'test-token',
                                                 web_origin='https://resume.example.test', access_verifier=verifier)

    def api(self, request=None, headers=None):
        response = Request(self.origin + '/api/ollama-model',
                           data=None if request is None else json.dumps(request).encode(),
                           headers={'Origin': self.origin, 'Content-Type': 'application/json',
                                    'X-Dashboard-Token': 'test-token', **(headers or {})})
        try:
            with urlopen(response) as result:
                return result.status, json.load(result)
        except HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def test_operator_can_persist_an_installed_model_and_reset_when_ollama_is_down(self):
        code, status = self.api()
        self.assertEqual(code, 200)
        self.assertIsNone(status['selected'])
        self.assertEqual(status['default'], 'first:latest')
        self.assertEqual(status['installed'], ['first:latest', 'second:latest'])
        self.assertEqual(status['models'][1], {'name': 'second:latest', 'size': 2 * 1024 ** 3, 'digest': 'b' * 64})
        self.assertEqual(status['loaded'][0]['size_vram'], 1024 ** 3)
        self.assertEqual(self.api({'model': 'second:latest'}, {'X-Dashboard-Token': 'invalid'})[0], 403)
        self.assertEqual(self.api({'model': 'second:latest'}, {'Origin': 'https://other.example.test'})[0], 403)
        member = {'Host': 'resume.example.test', 'Cf-Access-Jwt-Assertion': 'member',
                  'Origin': 'https://resume.example.test'}
        self.assertEqual(self.api(headers=member)[0], 403)
        self.assertEqual(self.api({'model': 'second:latest'}, member)[0], 403)
        self.assertEqual(self.api({'model': 'second:latest'}), (200, {'selected': 'second:latest'}))
        restarted = Workspace(self.root, 'first:latest', ROOT)
        self.assertEqual(restarted.model, 'second:latest')
        for rejected in ({}, {'model': 3}, {'model': 'unknown'}, {'model': 'remote:latest'}):
            with self.subTest(rejected=rejected):
                self.assertEqual(self.api(rejected)[0], 400)
                self.assertEqual(restarted.model, 'second:latest')
        self.available = False
        self.assertIsNone(self.api()[1]['installed'])
        self.assertEqual(self.api({'model': 'first:latest'})[0], 503)
        self.assertEqual(restarted.model, 'second:latest')
        self.assertEqual(self.api({'model': None}), (200, {'selected': None}))
        self.assertEqual(restarted.model, 'first:latest')

    def test_invalid_config_and_model_failures_do_not_fall_back(self):
        self.workspace.model_settings.path.write_text('broken', encoding='utf-8')
        self.assertIn('Cannot read', self.api()[1]['selection_error'])
        with self.assertRaises(OllamaUnavailableError):
            self.workspace.ai()
        self.assertEqual(self.api({'model': 'second:latest'})[0], 200)
        self.reported_model = 'first:latest'
        with self.assertRaisesRegex(OllamaUnavailableError, 'different model'):
            self.workspace.ai().generate('Synthetic request.')
        self.available = False
        with self.assertRaisesRegex(OllamaUnavailableError, 'second:latest is unavailable'):
            self.workspace.ai().generate('Synthetic request.')
        self.assertEqual(self.generated, ['second:latest', 'second:latest'])

    def test_host_probe_requires_two_persisted_gpu_backed_models(self):
        from ci.check_ollama_models_live import verify_switching

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        evidence = verify_switching(root, ['first:latest', 'second:latest'], self.ollama_url)
        self.assertEqual([item['model'] for item in evidence], ['first:latest', 'second:latest'])
        self.assertTrue(all(item['persisted'] and item['vram_bytes'] > 0 for item in evidence))
        self.assertIsNone(self.workspace.model_settings.selected())

    def test_import_migration_preserves_legacy_data_and_resumes_with_its_model(self):
        from durable_imports import DurableImports

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with closing(sqlite3.connect(root / '.resumeops-imports.sqlite3')) as db, db:
            db.execute('''CREATE TABLE imports (
                owner TEXT,id TEXT,extension TEXT,digest TEXT,document BLOB,status TEXT,stage TEXT,
                created REAL,started REAL,activity REAL,metrics TEXT,result TEXT,PRIMARY KEY(owner,id))''')
            db.execute("INSERT INTO imports(owner,id,extension,digest,document,status) VALUES (?,?,?,?,?,'queued')",
                       ('owner', 'old', '.txt', 'old-hash', b'Synthetic evidence'))

        def draft(extension, document, report, *, model):
            self.assertEqual((extension, document, model), ('.txt', b'Synthetic evidence', 'second:latest'))
            return {'profile': {'full_name': 'Synthetic Example'}}

        imports = DurableImports(root, draft)
        with closing(imports.connection()) as db, db:
            old = db.execute("SELECT digest,document,ai_model FROM imports WHERE id='old'").fetchone()
            self.assertEqual(tuple(old), ('old-hash', b'Synthetic evidence', None))
            db.execute("UPDATE imports SET ai_model='second:latest' WHERE id='old'")
            interrupted = db.execute("SELECT * FROM imports WHERE id='old'").fetchone()
        imports.process(interrupted)
        self.assertEqual(imports.get('owner', 'old')['ai_model'], 'second:latest')

    def test_automation_tracks_pinned_model_and_retains_job_on_service_failure(self):
        from automation_jobs import AutomationJobs

        jobs = AutomationJobs(self.workspace)
        payload = json.dumps({'role': 'frontend-developer'})
        checkpoints = json.dumps({'source': 'Synthetic evidence only'})
        with closing(jobs.connection()) as db, db:
            db.execute('INSERT INTO automation(owner,id,payload,checkpoints,created,updated) VALUES (?,?,?,?,1,1)',
                       ('owner', 'job', payload, checkpoints))

        def row():
            with closing(jobs.connection()) as db:
                return db.execute('SELECT * FROM automation WHERE owner=? AND id=?', ('owner', 'job')).fetchone()

        seen = []

        def extract(source, ai, *args, **kwargs):
            seen.append(ai.model)
            raise OllamaUnavailableError(f'Ollama could not run {ai.model}.')

        self.assertEqual(self.api({'model': 'second:latest'})[0], 200)
        with patch('resume_bot.automation.source_profile', side_effect=extract):
            jobs._prepare(row())
            self.assertEqual(self.api({'model': 'first:latest'})[0], 200)
            jobs._prepare(row())
        self.assertEqual(seen, ['second:latest', 'second:latest'])
        self.assertEqual((row()['payload'], row()['checkpoints']), (payload, checkpoints))
        status = jobs.list('owner')[0]
        self.assertEqual((status['ai_model'], status['status']), ('second:latest', 'retrying'))
        self.assertIn('second:latest', status['error'])

    def test_running_watcher_pins_each_model_without_reprocessing_completed_jobs(self):
        args = SimpleNamespace(inbox=self.workspace.inbox, output=self.workspace.output,
                               no_polish=False, overleaf=False, browser_profile=None,
                               once=True, interval=30, max_pages=2)
        self.workspace.private_inbox.mkdir()
        self.assertEqual(self.api({'model': 'second:latest'})[0], 200)

        def build(candidate, output, *, ai, polish_enabled, **kwargs):
            if polish_enabled:
                ai.generate('Test routing only.')
            if candidate.name == 'a.json':
                self.assertEqual(self.api({'model': 'first:latest'})[0], 200)
                ai.generate('A second step in the same job.')
            built = output / candidate.stem
            built.mkdir(parents=True)
            for artifact in pipeline.ARTIFACTS:
                (built / artifact).touch()
            return {'source_sha256': pipeline._source_digest(candidate), 'output': str(built),
                    'model': ai.model if polish_enabled else 'disabled'}

        for path in (self.workspace.inbox / 'a.json', self.workspace.inbox / 'b.json',
                     self.workspace.private_inbox / 'c.json'):
            path.write_text('{}', encoding='utf-8')
        with patch.object(pipeline, 'process', side_effect=build) as process:
            launch_model = Ollama('first:latest', self.ollama_url)
            first_scan = cli.watch(args, launch_model)
            self.assertEqual([job['status'] for job in first_scan.values()], ['ok', 'ok'])
            self.assertEqual(self.generated, ['second:latest', 'second:latest', 'first:latest'])
            self.assertEqual([job['ai_model'] for job in first_scan.values()], ['second:latest', 'first:latest'])
            self.assertEqual(self.api({'model': 'second:latest'})[0], 200)
            self.assertEqual(cli.watch(args, launch_model), first_scan)
            self.assertEqual(process.call_count, 3)

    def test_operator_can_switch_and_restore_from_the_browser(self):
        from playwright.sync_api import expect

        playwright, browser = browser_or_skip(self)
        self.addCleanup(playwright.stop)
        self.addCleanup(browser.close)
        page = browser.new_page(viewport={'width': 390, 'height': 850})
        page.goto(self.origin + '/automated')
        expect(page.get_by_role('heading', name='My Resumes')).to_be_visible()
        section = page.locator('#auto-jobs #ollama-model-settings')
        section.locator('summary').click()
        model = section.get_by_label('Model', exact=True)
        status = section.get_by_role('status')
        expect(status).to_have_text('Current model: first:latest.')
        expect(section.locator('#ollama-model-loaded')).to_have_text('Loaded in Ollama: first:latest (1 GiB in VRAM)')
        model.select_option('second:latest')
        expect(section.locator('#ollama-model-info')).to_contain_text('2 GiB. Digest: ' + 'b' * 64)
        section.get_by_role('button', name='Save model').click()
        expect(status).to_have_text('Saved. Current model: second:latest.')
        page.reload()
        section.locator('summary').click()
        expect(model).to_have_value('second:latest')
        self.models = [{'name': 'first:latest'}]
        section.get_by_role('button', name='Refresh models').click()
        expect(status).to_contain_text('no longer installed')
        model.select_option('')
        section.get_by_role('button', name='Save model').click()
        expect(status).to_have_text('Saved. Current model: first:latest.')
