"""Measured progress: monotonic, banded, never 100 before verification, retries, heartbeat, API."""
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from resume_bot import pipeline
from resume_bot.ollama import Ollama
from resume_bot.overleaf import OverleafError
from resume_bot.progress import BANDS, ORDER, LegacyTracker, Tracker

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = json.loads((ROOT / 'examples/sample_candidate.json').read_text(encoding='utf-8'))
TOOLS = all(shutil.which(tool) for tool in ('pdflatex', 'pdfinfo', 'pdftotext'))


class StreamingOllama(BaseHTTPRequestHandler):
    """Ollama-compatible NDJSON stream that echoes the input array in small, delayed chunks."""
    delay = 0.06

    def do_POST(self):
        import re
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        source = json.loads(re.search(r'INPUT ARRAY:\s*(\[.*\])', body['prompt'], re.S).group(1))
        text = json.dumps({'edits': source})
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson')
        self.end_headers()
        for i in range(0, len(text), 12):
            self.wfile.write((json.dumps({'response': text[i:i + 12], 'done': False}) + '\n').encode())
            self.wfile.flush()
            time.sleep(self.delay)
        self.wfile.write((json.dumps({'response': '', 'done': True, 'eval_count': 99}) + '\n').encode())

    def do_GET(self):
        # /api/ps as reported by Ollama for a fully GPU-resident model.
        data = json.dumps({'models': [{'name': 'qwen2.5:7b', 'model': 'qwen2.5:7b', 'size': 6000000000,
                                       'size_vram': 6000000000, 'context_length': 4096}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def overleaf_ok(zip_path, title, browser, *, pdf_path, saved_url, on_step):
    for done, label in ((0, 'Opening authorized browser session'), (1, 'Opening Overleaf project'),
                        (3, 'Waiting for the Overleaf editor'), (4, 'Compiling on Overleaf')):
        on_step(done, label)
    shutil.copyfile(zip_path.parent / 'resume.pdf', pdf_path)
    on_step(6, 'Overleaf PDF downloaded')
    return {'status': 'uploaded', 'project_name': title, 'url': 'https://www.overleaf.com/project/abc123'}


class TrackerTests(unittest.TestCase):
    def test_monotonic_banded_and_never_100_before_complete(self):
        seen = []
        t = Tracker(lambda snap: seen.append(snap))
        t.enter('validate', 'a')
        t.units_done(2, 2, 'b')
        t.enter('ai', 'c')
        t.fraction(0.5, 'd')
        t.fraction(0.2, 'late smaller measurement')     # must not move backwards
        t.unmeasured('e')
        t.enter('verify', 'f')
        t.units_done(2, 2, 'g')
        self.assertEqual(t.percent, 99)
        t.complete()
        percents = [s['percent'] for s in seen]
        self.assertEqual(percents, sorted(percents))
        self.assertEqual(percents.count(100), 1)
        self.assertEqual(percents[-1], 100)
        self.assertNotIn(100, percents[:-1])
        ai = [s for s in seen if s['stage_key'] == 'ai']
        self.assertTrue(all(21 <= s['percent'] <= 45 for s in ai))
        self.assertFalse([s for s in seen if s['stage'] == 'e'][0]['measured'])
        self.assertIn('ai', seen[-1]['completed_stages'])

    def test_skip_and_legacy_adapter(self):
        calls = []
        t = LegacyTracker(lambda percent, stage, candidate=None: calls.append((percent, stage, candidate)))
        t.enter('validate', 'Reading', candidate='Asha')
        t.skip('ai', 'disabled')
        t.enter('verify', 'final')
        t.complete('done')
        self.assertEqual(calls[0], (1, 'Reading', 'Asha'))
        self.assertEqual(calls[-1][0], 100)

    def test_bands_cover_requested_ranges(self):
        self.assertEqual([BANDS[k][1:3] for k in ORDER],
                         [(1, 5), (6, 10), (11, 20), (21, 45), (46, 55), (56, 70), (71, 80), (81, 92), (93, 99)])


@unittest.skipUnless(TOOLS, 'TeX/PDF tools missing')
class WatcherProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / 'inbox'
        self.inbox.mkdir()
        (self.inbox / 'job.json').write_text(json.dumps(SAMPLE), encoding='utf-8')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), StreamingOllama)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.ai = Ollama('qwen2.5:7b', f'http://127.0.0.1:{self.server.server_port}', timeout=10)
        self.snapshots = []
        real_write = pipeline.write_json

        def recording_write(path, value):
            if Path(path).name == '.resume-bot-state.json' and 'job.json' in value:
                self.snapshots.append(dict(value['job.json']))
            real_write(path, value)
        self.recorder = patch('resume_bot.pipeline.write_json', side_effect=recording_write)
        self.recorder.start()

    def tearDown(self):
        self.recorder.stop()
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def scan(self, **kw):
        return pipeline.scan(self.inbox, self.root / 'output', ai=self.ai, overleaf=True,
                             browser_profile=self.root / 'browser', once=True, **kw)['job.json']

    def test_success_is_monotonic_granular_and_100_only_after_verification(self):
        with patch('resume_bot.pipeline.import_zip', side_effect=overleaf_ok):
            result = self.scan()
        self.assertEqual(result['status'], 'ok')
        processing = [s for s in self.snapshots if s['status'] == 'processing']
        percents = [s['percent'] for s in processing]
        self.assertEqual(percents, sorted(percents), 'progress decreased')
        self.assertNotIn(100, percents, '100% shown before the job finished')
        self.assertEqual(result['percent'], 100)
        self.assertEqual(result['stage_key'], 'done')
        # Every stage's values fall inside its band and stages arrive in order.
        order = [s['stage_key'] for s in processing]
        firsts = [ORDER.index(k) for k in dict.fromkeys(order)]
        self.assertEqual(firsts, sorted(firsts))
        for snap in processing:
            low, high = BANDS[snap['stage_key']][1:3]
            self.assertTrue(low <= snap['percent'] <= high, snap)
        # Ollama output is streamed, so AI progress moves in many measured steps, not one jump.
        ai = sorted({s['percent'] for s in processing if s['stage_key'] == 'ai' and s['measured']})
        self.assertGreaterEqual(len(ai), 5, ai)
        jumps = [b - a for a, b in zip(percents, percents[1:])]
        self.assertLessEqual(max(jumps), 8, f'largest single jump {max(jumps)}')
        # Steps without internal measurement are reported as unmeasured, not as invented increments.
        unmeasured = {s['stage'] for s in processing if not s['measured']}
        self.assertIn('Compiling LaTeX with pdfLaTeX', unmeasured)
        overleaf_units = [s['units'] for s in processing if s['stage_key'] == 'overleaf' and s.get('units')]
        self.assertEqual(overleaf_units[-1], {'done': 7, 'total': 7, 'unit': 'steps'})
        self.assertTrue(all('heartbeat' in s for s in processing))

    def test_gpu_runtime_and_code_fingerprint_are_recorded(self):
        with patch('resume_bot.pipeline.import_zip', side_effect=overleaf_ok):
            result = self.scan()
        report = json.loads((Path(result['output']) / 'result.json').read_text())
        self.assertEqual(report['ollama_runtime']['gpu_percent'], 100)
        ai_done = [s for s in self.snapshots if s.get('stage') == 'Ollama generation complete']
        self.assertIn('100% of qwen2.5:7b in GPU memory', ai_done[-1]['detail'])
        health = json.loads((self.inbox / '.resume-bot-health.json').read_text())
        for name, blob in health['code'].items():
            data = (ROOT / 'resume_bot' / name).read_bytes().replace(b'\r\n', b'\n')
            import hashlib
            self.assertEqual(blob, hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest(), name)
        self.assertIn('pid', health)

    def test_restart_request_returns_between_jobs_only(self):
        (self.inbox / 'second.json').write_text(json.dumps({**SAMPLE, 'headline': 'Second'}), encoding='utf-8')
        processed = []
        real_process = pipeline.process

        def process_then_request_restart(*args, **kwargs):
            result = real_process(*args, **kwargs)
            processed.append(args[0].name)
            (self.inbox / pipeline.RESTART_FILE).write_text('deploy', encoding='utf-8')
            return result
        done = threading.Event()

        def watch():
            pipeline.scan(self.inbox, self.root / 'output', ai=self.ai, overleaf=True,
                          browser_profile=self.root / 'browser', once=False, interval=3600)
            done.set()
        with patch('resume_bot.pipeline.import_zip', side_effect=overleaf_ok), \
                patch('resume_bot.pipeline.process', side_effect=process_then_request_restart):
            thread = threading.Thread(target=watch, daemon=True)
            thread.start()
            self.assertTrue(done.wait(60), 'watcher did not return after a restart request')
        # The running job finished; the next one was left for the restarted watcher.
        self.assertEqual(processed, ['job.json'])
        state = json.loads((self.inbox / '.resume-bot-state.json').read_text())
        self.assertEqual(state['job.json']['status'], 'ok')
        self.assertNotIn('second.json', state)
        self.assertFalse((self.inbox / pipeline.RESTART_FILE).exists())
        self.assertTrue(json.loads((self.inbox / '.resume-bot-health.json').read_text())['restarting'])

    def test_failure_retry_now_and_attempt_counting(self):
        def broken(*args, **kwargs):
            kwargs['on_step'](0, 'Opening authorized browser session')
            raise OverleafError('session expired')
        with patch('resume_bot.pipeline.import_zip', side_effect=broken):
            failed = self.scan()
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['failed_stage'], 'overleaf')
        self.assertEqual(failed['attempt'], 1)
        self.assertLess(failed['percent'], 100)
        self.assertGreater(failed['next_retry'], time.time() + 60)
        # Backoff keeps the job waiting...
        with patch('resume_bot.pipeline.import_zip', side_effect=overleaf_ok) as upload:
            self.assertEqual(self.scan()['status'], 'failed')
            upload.assert_not_called()
            # ...until a retry is requested, which runs it at the next scan.
            pipeline.request_retry(self.inbox, 'job.json')
            done = self.scan()
        self.assertEqual(done['status'], 'ok')
        self.assertEqual(done['attempt'], 2)
        self.assertFalse((self.inbox / pipeline.RETRY_DIR / 'job.json').exists())
        with self.assertRaises(ValueError):
            pipeline.request_retry(self.inbox, '../job.json')

    def test_heartbeat_continues_while_an_unmeasured_step_runs(self):
        real_compile = pipeline.compile_latex

        def slow_compile(folder):
            time.sleep(0.8)
            return real_compile(folder)
        with patch('resume_bot.pipeline.HEARTBEAT_SECONDS', 0.2), \
                patch('resume_bot.pipeline.compile_latex', side_effect=slow_compile), \
                patch('resume_bot.pipeline.import_zip', side_effect=overleaf_ok):
            self.scan()
        during = [s for s in self.snapshots if s.get('stage') == 'Compiling LaTeX with pdfLaTeX']
        self.assertGreaterEqual(len(during), 3, 'heartbeat did not refresh during compilation')
        self.assertEqual(len({s['percent'] for s in during}), 1, 'heartbeat changed the percentage')
        self.assertGreater(during[-1]['heartbeat'], during[0]['heartbeat'])


class DashboardLiveApiTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / 'dashboard'))
        from resume_dashboard import Workspace, handler
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / 'resume_bot', self.root / 'resume_bot', ignore=shutil.ignore_patterns('__pycache__'))
        workspace = Workspace(self.root)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(workspace, '', 't'))
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.server.RequestHandlerClass = handler(workspace, self.origin, 't')
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.headers = {'Origin': self.origin, 'X-Dashboard-Token': 't', 'Content-Type': 'application/json'}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def post(self, path, body, headers=None):
        with urlopen(Request(self.origin + path, data=json.dumps(body).encode(),
                             headers=headers or self.headers), timeout=5) as response:
            return json.load(response)

    def status(self):
        with urlopen(self.origin + '/api/status', timeout=5) as response:
            return json.load(response)

    def test_live_fields_queue_position_and_retry_endpoint(self):
        first = self.post('/api/queue', SAMPLE)['name']
        second = self.post('/api/queue', {**SAMPLE, 'headline': 'Second revision'})['name']
        snapshot = self.status()
        self.assertIsInstance(snapshot['now'], float)
        positions = {job['name']: job['queue_position'] for job in snapshot['jobs']}
        self.assertEqual(sorted(positions.values()), [1, 2])
        with self.assertRaises(HTTPError) as not_failed:
            self.post('/api/retry', {'name': first})
        self.assertEqual(not_failed.exception.code, 400)
        state = {first: {'status': 'failed', 'last_attempt': 4102444800, 'failures': 1, 'attempt': 1,
                         'percent': 84, 'stage_key': 'overleaf', 'stage_label': 'Upload and compile on Overleaf',
                         'measured': False, 'next_retry': 4102444900, 'heartbeat': 1.0,
                         'completed_stages': ['validate'], 'error': 'Overleaf is unavailable'}}
        (self.root / 'inbox/.resume-bot-state.json').write_text(json.dumps(state))
        job = next(item for item in self.status()['jobs'] if item['name'] == first)
        for field in ('stage_key', 'stage_label', 'measured', 'next_retry', 'attempt', 'completed_stages'):
            self.assertEqual(job[field], state[first][field])
        with self.assertRaises(HTTPError) as no_token:
            self.post('/api/retry', {'name': first}, {'Content-Type': 'application/json'})
        self.assertEqual(no_token.exception.code, 403)
        self.assertEqual(self.post('/api/retry', {'name': first})['retry'], 'requested')
        self.assertTrue((self.root / 'inbox/.resume-bot-retry' / first).is_file())
        self.assertTrue((self.root / 'inbox' / second).is_file())
        with urlopen(self.origin + '/live.js', timeout=5) as response:
            self.assertIn(b'ResumeLive', response.read())


if __name__ == '__main__':
    unittest.main()
