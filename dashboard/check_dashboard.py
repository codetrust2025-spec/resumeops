"""HTTP integration check with an isolated copy of the deployed profile validator."""
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from resume_dashboard import Workspace, handler


class DashboardTest(unittest.TestCase):
    def test_queue_and_inspect_existing_watcher_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(BUILDER / 'resume_bot', root / 'resume_bot',
                            ignore=shutil.ignore_patterns('__pycache__'))
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(Workspace(root), '', 'token'))
            origin = f'http://127.0.0.1:{server.server_port}'
            phone_url = 'https://rtx.example.ts.net:8443'
            verifier = Mock()
            verifier.accepts.side_effect = lambda token: token == 'verified-user-token'
            server.RequestHandlerClass = handler(
                Workspace(root), origin, 'token', phone_url, 'owner@example.com',
                web_origin='https://resumeops.tech', access_verifier=verifier,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                headers = {'Origin': origin, 'X-Dashboard-Token': 'token',
                           'Content-Type': 'application/json'}
                request = lambda path, **kwargs: urlopen(Request(origin + path, **kwargs), timeout=3)
                phone_headers = {'Host': 'rtx.example.ts.net:8443', 'X-Forwarded-Proto': 'https'}
                with self.assertRaises(HTTPError) as anonymous:
                    request('/api/status', headers=phone_headers)
                self.assertEqual(anonymous.exception.code, 403)
                phone_headers['Tailscale-User-Login'] = 'owner@example.com'
                with request('/api/status', headers=phone_headers) as response:
                    self.assertEqual(response.status, 200)
                with self.assertRaises(HTTPError) as wrong_origin:
                    request('/api/queue', data=b'{}', headers={**headers, **phone_headers})
                self.assertEqual(wrong_origin.exception.code, 403)
                with self.assertRaises(HTTPError) as valid_origin:
                    request('/api/queue', data=b'{}',
                            headers={**headers, **phone_headers, 'Origin': phone_url})
                self.assertEqual(valid_origin.exception.code, 400)
                web_headers = {'Host': 'resumeops.tech', 'X-Forwarded-Proto': 'https'}
                with self.assertRaises(HTTPError) as anonymous:
                    request('/api/status', headers=web_headers)
                self.assertEqual(anonymous.exception.code, 403)
                web_headers['Cf-Access-Jwt-Assertion'] = 'verified-user-token'
                with request('/api/status', headers=web_headers) as response:
                    self.assertEqual(response.status, 200)
                with request('/') as response:
                    self.assertIn(b'content="token"', response.read())
                with request('/dashboard.js') as response:
                    self.assertEqual(response.headers.get_content_type(), 'text/javascript')
                source = (BUILDER / 'examples/sample_candidate.json').read_bytes()
                # Exercise real DOCX text extraction and the draft-only path using a mock model.
                import io
                docx = io.BytesIO()
                expected_text = 'Verified candidate résumé text. ' * 5
                with ZipFile(docx, 'w') as archive:
                    archive.writestr('word/document.xml',
                        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                        '<w:body><w:p><w:r><w:t>' + expected_text +
                        '</w:t></w:r></w:p></w:body></w:document>')
                draft_headers = dict(headers, **{'X-Resume-Extension': '.docx'})
                with patch('resume_bot.ingest.extract_profile', return_value=json.loads(source)) as extract:
                    with request('/api/draft', data=docx.getvalue(), headers=draft_headers) as response:
                        draft = json.load(response)
                    self.assertEqual(draft['profile'], json.loads(source))
                    self.assertEqual(draft['source'], expected_text)
                    self.assertEqual(extract.call_args.args[0], expected_text)
                progress_headers = {**draft_headers, 'X-Resume-Import-ID': 'a' * 32}
                progress_path = '/api/import-progress?id=' + 'a' * 32

                def verify_progress(*args, for_review):
                    self.assertTrue(for_review)
                    with request(progress_path, headers=headers) as response:
                        self.assertEqual(json.load(response)['stage'],
                                         'Extracting candidate details with Ollama')
                    with self.assertRaises(HTTPError) as denied_progress:
                        request(progress_path)
                    self.assertEqual(denied_progress.exception.code, 403)
                    return json.loads(source)

                with patch('resume_bot.ingest.extract_profile', side_effect=verify_progress):
                    with request('/api/draft', data=docx.getvalue(), headers=progress_headers) as response:
                        self.assertEqual(json.load(response)['profile'], json.loads(source))
                with request(progress_path, headers=headers) as response:
                    self.assertIsNone(json.load(response)['stage'])
                self.assertFalse(list((root / 'inbox').glob('*.json')))
                with self.assertRaises(HTTPError) as invalid_type:
                    request('/api/draft', data=b'x',
                            headers=dict(headers, **{'X-Resume-Extension': '.exe'}))
                self.assertEqual(invalid_type.exception.code, 415)
                with self.assertRaises(HTTPError) as invalid:
                    request('/api/queue', data=b'{}', headers=headers)
                self.assertEqual(invalid.exception.code, 400)
                with self.assertRaises(HTTPError) as denied:
                    request('/api/queue', data=source, headers={'Content-Type': 'application/json'})
                self.assertEqual(denied.exception.code, 403)
                with request('/api/queue', data=source, headers=headers) as response:
                    self.assertEqual(response.status, 201)
                    name = json.load(response)['name']
                self.assertTrue((root / 'inbox' / name).is_file())
                with request('/api/queue', data=source, headers=headers) as response:
                    self.assertFalse(json.load(response)['created'])
                with request('/api/status') as response:
                    self.assertEqual(json.load(response)['jobs'][0]['status'], 'queued')
                now = time.time()
                (root / 'inbox/.resume-bot-state.json').write_text(json.dumps({
                    name: {'status': 'ok', 'last_attempt': now}}))
                (root / 'inbox/.resume-bot-health.json').write_text(json.dumps({'last_scan': now}))
                output = root / 'output/candidate/revision'
                output.mkdir(parents=True)
                (output / 'resume.pdf').write_bytes(b'%PDF fixture')
                (output / 'result.json').write_text(json.dumps({'candidate': 'Demo',
                    'pdf_checks': {'pages': 1},
                    'overleaf': {'url': 'https://www.overleaf.com/project/test123', 'pdf_sha256': 'fixture'}}))
                (root / 'logs').mkdir()
                (root / 'logs/watcher.log').write_text('OK resume processed')
                with request('/api/status') as response:
                    status = json.load(response)
                    self.assertTrue(status['healthy'])
                    self.assertEqual(status['jobs'][0]['status'], 'ok')
                    self.assertEqual(status['revisions'][0]['pages'], 1)
                query = urlencode({'key': 'candidate/revision', 'name': 'resume.pdf'})
                with request('/api/artifact?' + query) as response:
                    self.assertEqual(response.read(), b'%PDF fixture')
                with request('/api/log?name=watcher.log') as response:
                    self.assertEqual(response.read(), b'OK resume processed')
                traversal = urlencode({'key': '../..', 'name': 'resume.pdf'})
                with self.assertRaises(HTTPError) as denied:
                    request('/api/artifact?' + traversal)
                self.assertEqual(denied.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    BUILDER = Path(sys.argv.pop(1)).resolve()
    unittest.main()
