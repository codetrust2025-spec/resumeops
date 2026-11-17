"""Server side of session resilience: token refresh endpoint, sign-in return page, and import
drafts that survive a lost reply."""
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from resume_dashboard import Workspace, handler  # noqa: E402


class SessionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Workspace(Path(self.temp.name), code_root=ROOT)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.workspace, '', 'first-token'))
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.server.RequestHandlerClass = handler(self.workspace, self.origin, 'first-token')
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def get(self, path, headers=None):
        with urlopen(Request(self.origin + path, headers=headers or {}), timeout=5) as response:
            return response.status, response.read()

    def test_session_endpoint_returns_the_current_token_after_a_restart(self):
        status, body = self.get('/api/session')
        self.assertEqual((status, json.loads(body)['token']), (200, 'first-token'))
        self.server.RequestHandlerClass = handler(self.workspace, self.origin, 'second-token')  # redeploy
        self.assertEqual(json.loads(self.get('/api/session')[1])['token'], 'second-token')

    def test_session_endpoint_is_refused_without_access(self):
        # A request that is neither loopback nor carrying a valid Access token is denied.
        with self.assertRaises(HTTPError) as denied:
            self.get('/api/session', {'Cf-Access-Jwt-Assertion': 'forged'})
        self.assertEqual(denied.exception.code, 403)
        self.assertIn(b'access denied', denied.exception.read())

    def test_sign_in_return_page_notifies_the_waiting_tab(self):
        status, body = self.get('/auth/renewed')
        self.assertEqual(status, 200)
        for marker in (b"BroadcastChannel('resumeops-auth')", b'resumeops.authRenewed', b'window.close()'):
            self.assertIn(marker, body)

    def test_finished_import_can_be_fetched_again_if_the_reply_was_lost(self):
        original = self.workspace.draft
        self.workspace.draft = lambda extension, data, report=None: {'profile': {'full_name': 'Lost Reply Example'},
                                                                     'source': 'text'}
        try:
            headers = {'Origin': self.origin, 'X-Dashboard-Token': 'first-token', 'X-Resume-Extension': '.txt',
                       'X-Resume-Import-ID': 'a' * 32, 'X-Resume-Async': '1'}
            with urlopen(Request(self.origin + '/api/draft', data=b'x' * 200, headers=headers), timeout=5) as r:
                self.assertEqual(r.status, 202)
            url = '/api/import-progress?id=' + 'a' * 32
            auth = {'X-Dashboard-Token': 'first-token'}
            for _ in range(50):
                result = json.loads(self.get(url, auth)[1])
                if result['status'] == 'complete':
                    break
                time.sleep(0.05)
            self.assertEqual(result['profile']['full_name'], 'Lost Reply Example')
            again = json.loads(self.get(url, auth)[1])          # first reply lost in transit
            self.assertEqual(again['profile']['full_name'], 'Lost Reply Example')
            self.assertNotIn('delivered', again)
            # The same upload re-sent after a sign-in is not processed twice.
            with self.assertRaises(HTTPError) as duplicate:
                urlopen(Request(self.origin + '/api/draft', data=b'x' * 200, headers=headers), timeout=5)
            self.assertEqual(duplicate.exception.code, 409)
        finally:
            self.workspace.draft = original


if __name__ == '__main__':
    unittest.main()
