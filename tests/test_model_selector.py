"""Model selection with a loopback Ollama stand-in and a disposable workspace."""
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from resume_bot import __main__ as cli, pipeline
from resume_bot.ollama import Ollama

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
        self.models = [{'name': 'second:latest'}, {'model': 'first:latest'}, {'name': 'first:latest'},
                       {'name': 'remote:latest', 'remote_host': 'https://example.test'}, {'name': 'third:cloud'}]
        self.generated = []
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
                else:
                    self.reply({}, 404)

            def do_POST(self):
                if self.path == '/api/generate':
                    request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    fixture.generated.append(request['model'])
                    self.reply({'response': '{}'})
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
        self.assertEqual(self.api(), (200, {'default': 'first:latest', 'selected': None,
                                           'installed': ['first:latest', 'second:latest']}))
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

    def test_running_watcher_pins_each_model_without_reprocessing_completed_jobs(self):
        args = SimpleNamespace(inbox=self.workspace.inbox, output=self.workspace.output,
                               no_polish=False, overleaf=False, browser_profile=None,
                               once=True, interval=30, max_pages=2)
        self.workspace.private_inbox.mkdir()
        self.assertEqual(self.api({'model': 'second:latest'})[0], 200)

        def build(candidate, output, *, ai, **kwargs):
            ai.generate('Test routing only.')
            if candidate.name == 'a.json':
                self.assertEqual(self.api({'model': 'first:latest'})[0], 200)
                ai.generate('A second step in the same job.')
            built = output / candidate.stem
            built.mkdir(parents=True)
            for artifact in pipeline.ARTIFACTS:
                (built / artifact).touch()
            return {'source_sha256': pipeline._source_digest(candidate), 'output': str(built)}

        for path in (self.workspace.inbox / 'a.json', self.workspace.inbox / 'b.json',
                     self.workspace.private_inbox / 'c.json'):
            path.write_text('{}', encoding='utf-8')
        with patch.object(pipeline, 'process', side_effect=build) as process:
            launch_model = Ollama('first:latest', self.ollama_url)
            first_scan = cli.watch(args, launch_model)
            self.assertEqual([job['status'] for job in first_scan.values()], ['ok', 'ok'])
            self.assertEqual(self.generated, ['second:latest', 'second:latest', 'first:latest', 'first:latest'])
            self.assertEqual(self.api({'model': 'second:latest'})[0], 200)
            self.assertEqual(cli.watch(args, launch_model), first_scan)
            self.assertEqual(process.call_count, 3)

    def test_operator_can_switch_and_restore_from_the_browser(self):
        from playwright.sync_api import expect

        playwright, browser = browser_or_skip(self)
        self.addCleanup(playwright.stop)
        self.addCleanup(browser.close)
        page = browser.new_page(viewport={'width': 390, 'height': 850})
        page.goto(self.origin + '/candidates')
        page.get_by_role('link', name='Model settings').click()
        section = page.get_by_role('region', name='Ollama model')
        model = section.get_by_label('Model', exact=True)
        status = section.get_by_role('status')
        expect(status).to_have_text('Current model: first:latest.')
        model.select_option('second:latest')
        section.get_by_role('button', name='Save model').click()
        expect(status).to_have_text('Saved. Current model: second:latest.')
        page.reload()
        expect(model).to_have_value('second:latest')
        self.models = [{'name': 'first:latest'}]
        section.get_by_role('button', name='Refresh models').click()
        expect(status).to_contain_text('no longer installed')
        model.select_option('')
        section.get_by_role('button', name='Save model').click()
        expect(status).to_have_text('Saved. Current model: first:latest.')
