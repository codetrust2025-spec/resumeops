"""Unattended operation: standby when another watcher runs, clear Overleaf sign-in failures, and
automatic recovery of a job interrupted by a crash."""
import io
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dashboard.automation_jobs import AutomationJobs
from resume_bot import __main__ as cli
from resume_bot import pipeline
from resume_bot.overleaf import OverleafAuthError, _record_sign_in, _require_session, _signed_out

ROOT = Path(__file__).parents[1]
REQUEST = {'profile': json.loads((ROOT / 'examples/sample_candidate.json').read_text()),
           'target': {'role': 'frontend-developer', 'template': 'standard', 'keywords': []}}


class StandbyTests(unittest.TestCase):
    def test_second_watcher_waits_instead_of_crashing(self):
        calls, sleeps = [], []

        def scan(*args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError('database is locked')
            return {'job.json': {'status': 'ok'}}
        args = SimpleNamespace(inbox='inbox', output='output', no_polish=False, overleaf=True,
                               browser_profile='p', once=False, interval=30, max_pages=2)
        out = io.StringIO()
        with patch.object(cli, 'scan', scan), redirect_stdout(out):
            self.assertEqual(cli.watch(args, None, sleep=sleeps.append), {'job.json': {'status': 'ok'}})
        self.assertEqual(sleeps, [30, 30])
        self.assertEqual(out.getvalue().count('standing by'), 1)

    def test_other_database_errors_are_not_hidden(self):
        args = SimpleNamespace(inbox='i', output='o', no_polish=False, overleaf=False, browser_profile='p',
                               once=False, interval=30, max_pages=2)
        with patch.object(cli, 'scan', side_effect=sqlite3.OperationalError('disk I/O error')):
            with self.assertRaises(sqlite3.OperationalError):
                cli.watch(args, None, sleep=lambda s: None)


class AutomationStatusTests(unittest.TestCase):
    def test_waiting_for_sign_in_replaces_the_old_retry_countdown(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = AutomationJobs(SimpleNamespace(root=Path(directory)))
            task = {'job.json': {'status': 'failed', 'stage_key': 'overleaf', 'percent': 90,
                                 'awaiting_sign_in': True, 'next_retry': 9999, 'error': 'Sign in to Overleaf'}}
            job = jobs._status({'job': 'job.json'}, task, 'owner')
        self.assertEqual((job['status'], job['stage'], job['next_retry']),
                         ('blocked', 'Waiting for Overleaf sign-in', 0))
        self.assertGreaterEqual(job['percent'], 90)
        self.assertLess(job['percent'], 100)


@unittest.skipUnless(shutil.which('pdflatex') and shutil.which('pdftotext'), 'TeX/PDF tools missing')
class OverleafSessionTests(unittest.TestCase):
    def test_overleaf_login_redirects_and_http_unauthorized_are_auth_failures(self):
        for url, status in [('https://www.overleaf.com/user/login', 200),
                            ('https://www.overleaf.com/project', 401)]:
            with self.subTest(url=url, status=status), self.assertRaises(OverleafAuthError):
                _require_session(SimpleNamespace(url=url), SimpleNamespace(status=status))
        self.assertFalse(_signed_out('https://example.test/overleaf.com/login'))

    def test_signed_out_session_is_reported_and_recovers_after_sign_in(self):
        self.assertTrue(_signed_out('https://www.overleaf.com/login?redir=%2Fproject'))
        self.assertFalse(_signed_out('https://www.overleaf.com/project'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / 'inbox'
            inbox.mkdir()
            (inbox / 'job.json').write_text(json.dumps(REQUEST))
            kwargs = dict(polish_enabled=False, overleaf=True, browser_profile=root / 'browser', once=True)
            with patch('resume_bot.pipeline.import_zip', side_effect=OverleafAuthError('Overleaf sign-in required')):
                job = pipeline.scan(inbox, root / 'output', **kwargs)['job.json']
            self.assertEqual(job['status'], 'failed')
            self.assertIn('Overleaf sign-in required', job['error'])
            self.assertEqual(job['failed_stage'], 'overleaf')
            self.assertFalse(job['retryable'])
            self.assertIsNone(job['next_retry'])
            self.assertLess(job['percent'], 100)
            health = json.loads((inbox / '.resume-bot-health.json').read_text())
            self.assertEqual(health['overleaf']['reason'], 'sign-in required')
            self.assertFalse(health['overleaf']['ok'])

            def signed_in(zip_path, title, browser, *, pdf_path, saved_url, on_step):
                shutil.copyfile(Path(zip_path).parent / 'resume.pdf', pdf_path)
                return {'status': 'uploaded', 'project_name': title, 'url': 'https://www.overleaf.com/project/abcdef123'}
            with patch('resume_bot.pipeline.import_zip', side_effect=signed_in) as upload:
                with patch.object(pipeline.time, 'time', return_value=time.time() + 10_000):
                    stalled = pipeline.scan(inbox, root / 'output', **kwargs)['job.json']
                self.assertEqual(stalled, job)
                upload.assert_not_called()
                _record_sign_in(root / 'browser')
                job = pipeline.scan(inbox, root / 'output', **kwargs)['job.json']
                upload.assert_called_once()
            self.assertEqual((job['status'], job['percent']), ('ok', 100))
            report = json.loads((Path(job['output']) / 'result.json').read_text())
            self.assertIn('ats_readiness', report['ats'])
            self.assertTrue(report['overleaf']['pdf_sha256'])
            self.assertTrue(json.loads((inbox / '.resume-bot-health.json').read_text())['overleaf']['ok'])

    def test_job_interrupted_by_a_crash_is_finished_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / 'inbox'
            inbox.mkdir()
            (inbox / 'job.json').write_text(json.dumps(REQUEST))
            # State left by a watcher that was killed mid-job (power loss, reboot).
            digest = pipeline.hashlib.sha256(repr((
                pipeline._source_digest(inbox / 'job.json'), '', False, False,
                str((root / 'output').resolve()), 'None', 2)).encode()).hexdigest()
            (inbox / '.resume-bot-state.json').write_text(json.dumps({'job.json': {
                'sha256': digest, 'status': 'processing', 'percent': 63, 'stage_key': 'latex',
                'last_attempt': time.time() - 600, 'failures': 0, 'heartbeat': time.time() - 590}}))
            job = pipeline.scan(inbox, root / 'output', polish_enabled=False, once=True)['job.json']
            self.assertEqual((job['status'], job['percent'], job['attempt']), ('ok', 100, 1))


if __name__ == '__main__':
    unittest.main()
