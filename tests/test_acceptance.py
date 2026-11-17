"""Integration tests with a local Ollama-compatible HTTP server; NOT a real model or Overleaf account."""
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from resume_bot.ollama import Ollama
from resume_bot.pipeline import process
from resume_bot.overleaf import _editor_url, _existing_project_url, OverleafError

ROOT=Path(__file__).resolve().parents[1]

class OllamaCompatibleHandler(BaseHTTPRequestHandler):
    requests=[]
    def do_POST(self):
        if self.path!='/api/generate':
            self.send_error(404);return
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append(data)
        m=re.search(r'INPUT ARRAY:\s*(\[.*\])',data['prompt'],re.S)
        if not m:
            self.send_error(400);return
        input_strings=json.loads(m.group(1))
        text=json.dumps({'edits':input_strings})
        if data.get('stream'):
            # Real Ollama streams NDJSON chunks and ends with a done record carrying counters.
            pieces=[text[i:i+16] for i in range(0,len(text),16)]
            lines=[json.dumps({'response':p,'done':False}) for p in pieces]
            lines.append(json.dumps({'response':'','done':True,'eval_count':len(pieces)}))
            output=('\n'.join(lines)+'\n').encode()
        else:
            output=json.dumps({'response':text}).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(output)))
        self.end_headers()
        self.wfile.write(output)
    def log_message(self,*args):
        pass

class EndToEndTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('pdflatex') and shutil.which('pdfinfo') and shutil.which('pdftotext'),'TeX tools missing')
    def test_http_ai_to_pdf_and_overleaf_connector_state(self):
        """Real HTTP and real TeX compilation; Overleaf upload is deliberately mocked."""
        OllamaCompatibleHandler.requests=[]
        server=ThreadingHTTPServer(('127.0.0.1',0),OllamaCompatibleHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            ai=Ollama('qwen2.5:7b',f'http://127.0.0.1:{server.server_port}',timeout=4)
            def fake_download(zip_path, title, profile, *, pdf_path, saved_url, on_step=lambda *a: None):
                shutil.copyfile(zip_path.parent / 'resume.pdf', pdf_path)
                return {'status': 'uploaded', 'project_name': title,
                        'url': 'https://www.overleaf.com/project/abcdef123456'}

            with tempfile.TemporaryDirectory() as td, patch(
                'resume_bot.pipeline.import_zip', side_effect=fake_download
            ) as uploader:
                candidate=ROOT/'examples/sample_candidate.json'
                result=process(candidate,Path(td)/'output',ai=ai,overleaf=True,browser_profile=Path(td)/'fake-browser')
                self.assertEqual(result['overleaf']['status'],'uploaded')
                self.assertEqual(result['pdf_checks']['pages'],1)
                self.assertTrue((Path(result['output'])/'resume.pdf').exists())
                self.assertTrue(result["overleaf"]["pdf_sha256"])
                self.assertTrue((Path(result["output"])/"overleaf.pdf").is_file())
                self.assertTrue(OllamaCompatibleHandler.requests)
                self.assertEqual(OllamaCompatibleHandler.requests[0]['model'],'qwen2.5:7b')
                self.assertEqual(uploader.call_count,1)
                process(candidate,Path(td)/'output',ai=ai,overleaf=True,browser_profile=Path(td)/'fake-browser')
                self.assertEqual(uploader.call_count,1,'Cached uploads must not be duplicated')
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_ambiguous_submission_never_submits_twice(self):
        from playwright.sync_api import TimeoutError
        from resume_bot.overleaf import import_zip

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'upload.zip'
            source.write_bytes(b'fixture')
            page = MagicMock()
            page.url = 'https://www.overleaf.com/project'
            page.wait_for_url.side_effect = TimeoutError('simulated post-submit timeout')
            context = MagicMock()
            context.pages = [page]
            with patch('playwright.sync_api.sync_playwright') as browser, patch(
                'resume_bot.overleaf._find_project', return_value=None
            ):
                browser.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = context
                for _ in range(2):
                    with self.assertRaises(OverleafError):
                        import_zip(source, 'ResumeBot-revision', root / 'profile',
                                   pdf_path=root / 'overleaf.pdf')
            page.locator.return_value.set_input_files.assert_called_once()

    def test_missing_or_invalid_remote_pdf_keeps_local_build_and_retries_same_project(self):
        from resume_bot.compile import BuildError

        with tempfile.TemporaryDirectory() as td:
            def remote(zip_path, title, browser, *, pdf_path, saved_url, on_step=lambda *a: None):
                if uploader.call_count == 1:
                    pdf_path.write_bytes(b'not a PDF')
                else:
                    shutil.copyfile(zip_path.parent / 'resume.pdf', pdf_path)
                return {'status': 'exists', 'project_name': title,
                        'url': 'https://www.overleaf.com/project/abcdef123456'}

            with patch('resume_bot.pipeline.import_zip', side_effect=remote) as uploader:
                kwargs = {'polish_enabled': False, 'overleaf': True,
                          'browser_profile': Path(td) / 'profile'}
                source = ROOT / 'examples/sample_candidate.json'
                with self.assertRaises(BuildError):
                    process(source, td, **kwargs)
                result = process(source, td, **kwargs)
            self.assertEqual(uploader.call_args_list[0].args[1],
                             uploader.call_args_list[1].args[1])
            self.assertTrue((Path(result['output']) / 'overleaf.pdf').is_file())
            self.assertTrue(result['overleaf']['pdf_checks']['required_facts_present'])

    def test_overleaf_project_url_validation(self):
        self.assertTrue(_editor_url('https://www.overleaf.com/project/abcdef123456'))
        for bad in ['https://www.overleaf.com/project','https://www.overleaf.com/project/',
                    'https://www.overleaf.com.attacker.test/project/abcdef12',
                    'http://www.overleaf.com/project/abcdef123','https://www.overleaf.com/login']:
            self.assertFalse(_editor_url(bad),bad)

    def test_overleaf_link_detection_on_browser_fixture(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright not installed')
        executable=shutil.which('chromium') or shutil.which('google-chrome')
        with sync_playwright() as p:
            opts={'executable_path':executable} if executable else {}
            try:
                browser=p.chromium.launch(headless=True,args=['--no-sandbox'],**opts)
            except Exception as exc:
                if os.environ.get('RESUME_BUILDER_REQUIRE_BROWSER'):
                    raise
                self.skipTest('Chromium unavailable: '+str(exc)[:100])
            try:
                page=browser.new_page()
                page.set_content('''<html><body>
                    <a href="/project/a1b2c3d4">ResumeBot-asha-001</a>
                    <a href="/project/a1b2c3d5">Another Project</a>
                </body></html>''')
                self.assertEqual(_existing_project_url(page,'ResumeBot-asha-001'),
                                 'https://www.overleaf.com/project/a1b2c3d4')
                self.assertIsNone(_existing_project_url(page,'not found'))
                page.set_content('''<html><a href="/project/a1b2c3d4">Same Name</a>
                                      <a href="/project/a1b2c3d6">Same Name</a></html>''')
                with self.assertRaises(OverleafError):
                    _existing_project_url(page,'Same Name')
            finally:
                browser.close()

if __name__=='__main__':unittest.main()
