"""Public multi-candidate intake: validation, candidate identity and isolation, idempotent and duplicate-safe saving,
queue order, capacity, pause and GPU limits, prefill, the public HTTP surface, and 150 simultaneous submissions.
Every candidate is fictional; no model runs."""
import base64
import io
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from automation_jobs import AutomationCapacity, AutomationJobs, INTAKE_SOURCE  # noqa: E402
from intake import (IntakeDesk, IntakeDuplicate, IntakeError, candidate_owner, candidate_page,  # noqa: E402
                    intake_request)
from resume_dashboard import DashboardServer, Workspace, handler  # noqa: E402
from resume_bot import gpu_gate  # noqa: E402
from resume_bot.prefill import document_text, quick_prefill  # noqa: E402


def key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')


def form(n=0, **extra):
    data = {'consent': True, 'role': 'Java Backend Developer', 'full_name': f'Asha Example {n}',
            'email': f'asha.{n}@example.com', 'phone': '+91 90000 1%04d' % n, 'location': 'Pune',
            'linkedin': '', 'github': '', 'portfolio': '', 'skills': 'Java, Spring Boot, MySQL, JUnit',
            'education': [{'degree': 'B.Tech', 'university': 'Example Institute (fictional)', 'graduation_year': '2021',
                           'specialization': '', 'cgpa': ''}],
            'work_experience': [{'company': 'Example Labs (fictional)', 'position': 'Software Engineer',
                                 'start': 'Jan 2022', 'end': 'Present', 'location': '',
                                 'responsibilities': 'Built Spring Boot REST APIs for order workflows with Java and MySQL.\n'
                                                     'Wrote JUnit tests for the order services and repositories.'}],
            'projects': [{'name': f'Order Service {n}', 'stack': 'Java, Spring Boot, MySQL',
                          'description': 'Built order endpoints with Spring Boot and MySQL.\n'
                                         'Wrote JUnit tests for the order endpoints.'}],
            'certifications': '', 'achievements': ''}
    data.update(extra)
    return data


def account_payload(n):
    candidate = {k: v for k, v in form(n).items() if k != 'consent'}
    candidate.update(background='Built Java APIs at Example Labs.', education=[], work_experience=[], projects=[])
    return {'consent': 'resumeops-auto-1', 'role': 'Java Backend Developer', 'candidate': candidate}


class Desk:
    def __init__(self, test):
        temp = tempfile.TemporaryDirectory()
        test.addCleanup(temp.cleanup)
        self.workspace = Workspace(Path(temp.name), code_root=ROOT)
        self.jobs = AutomationJobs(self.workspace)
        self.jobs.start = lambda: None  # nothing is prepared in these tests
        self.desk = IntakeDesk(self.workspace, self.jobs)

    def rows(self, sql, *args):
        with closing(self.jobs.connection()) as db:
            return db.execute(sql, args).fetchall()


class ValidationTests(unittest.TestCase):
    def test_a_complete_form_becomes_the_typed_facts_request(self):
        request, summary = intake_request(form())
        self.assertEqual(summary['role'], 'Java Backend Developer')
        profile = request['profile']
        self.assertEqual(profile['experience'][0]['company'], 'Example Labs (fictional)')
        self.assertEqual(len(profile['experience'][0]['bullets']), 2)
        self.assertEqual(profile['skills'], ['Java', 'Spring Boot', 'MySQL', 'JUnit'])
        self.assertTrue(profile['projects'][0].startswith('Order Service 0 - Tech stack: Java, Spring Boot, MySQL'))

    def test_each_mandatory_fact_is_asked_for_by_field(self):
        cases = [({'full_name': ''}, 'full_name'), ({'email': 'not-an-email'}, 'email'), ({'phone': '12'}, 'phone'),
                 ({'location': ''}, 'location'), ({'role': ''}, 'role'), ({'skills': ' , '}, 'skills'),
                 ({'education': []}, 'education'), ({'work_experience': []}, 'work_experience'),
                 ({'projects': []}, 'projects'), ({'consent': False}, 'consent')]
        for change, field in cases:
            with self.subTest(field=field), self.assertRaises(IntakeError) as caught:
                intake_request(form(**change))
            self.assertEqual(caught.exception.field, field)
        broken = form()
        broken['work_experience'][0]['responsibilities'] = '  '
        with self.assertRaises(IntakeError) as caught:
            intake_request(broken)
        self.assertEqual(caught.exception.field, 'work_experience.0.responsibilities')
        broken = form()
        broken['education'][0]['graduation_year'] = ''
        with self.assertRaises(IntakeError) as caught:
            intake_request(broken)
        self.assertEqual(caught.exception.field, 'education.0.graduation_year')

    def test_a_fresher_needs_no_employment_and_has_no_years(self):
        request, _ = intake_request(form(work_experience=[], fresher=True))
        self.assertEqual(request['profile']['experience'], [])
        self.assertEqual(request['years'], 0)

    def test_blank_rows_are_ignored_and_a_robot_field_is_refused(self):
        data = form()
        data['projects'].append({'name': '', 'stack': '', 'description': ''})
        self.assertEqual(len(intake_request(data)[0]['profile']['projects']), 1)
        with self.assertRaises(IntakeError):
            intake_request(form(website='http://spam.example'))


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.d = Desk(self)

    def test_a_submission_is_saved_with_its_job_and_a_retry_is_the_same_application(self):
        secret = key()
        first = self.d.desk.submit(form(), secret)
        self.assertTrue(first['created'])
        self.assertRegex(first['candidate'], r'^RO-[2-9A-HJKMNP-Z]{10}$')
        again = self.d.desk.submit(form(), secret)
        self.assertEqual((again['candidate'], again['job'], again['created']), (first['candidate'], first['job'], False))
        rows = self.d.rows('SELECT owner, source, candidate, status FROM automation')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['owner'], candidate_owner(first['candidate']))
        self.assertEqual((rows[0]['source'], rows[0]['candidate'], rows[0]['status']), (INTAKE_SOURCE, 'Asha Example 0', 'queued'))
        stored = self.d.rows('SELECT key_hash, form FROM intake')[0]
        self.assertNotIn(secret, stored['key_hash'], 'only a hash of the private key is stored')

    def test_the_same_facts_from_another_device_are_not_processed_twice(self):
        first = self.d.desk.submit(form(), key())
        with self.assertRaises(IntakeDuplicate) as caught:
            self.d.desk.submit(form(), key())
        self.assertEqual(caught.exception.candidate, first['candidate'])
        self.assertEqual(len(self.d.rows('SELECT 1 FROM automation')), 1)

    def test_private_key_access_and_corrections(self):
        secret = key()
        first = self.d.desk.submit(form(), secret)
        owner = self.d.desk.authorize(first['candidate'], secret)
        self.assertEqual(owner, candidate_owner(first['candidate']))
        for wrong in (key(), secret[:-1] + ('A' if secret[-1] != 'A' else 'B'), '', 'x' * 50):
            self.assertIsNone(self.d.desk.authorize(first['candidate'], wrong))
        self.assertIsNone(self.d.desk.authorize('RO-AAAAAAAAAA', secret))
        corrected = self.d.desk.submit(form(location='Bengaluru'), secret, first['candidate'])
        self.assertEqual(corrected['candidate'], first['candidate'])
        self.assertNotEqual(corrected['job'], first['job'])
        self.assertEqual(len(self.d.rows('SELECT 1 FROM automation WHERE owner=?', owner)), 2)
        self.assertEqual(self.d.desk.profile(first['candidate'])['form']['location'], 'Bengaluru')
        with self.assertRaises(IntakeError) as caught:  # another candidate's key cannot correct this one
            other = key()
            self.d.desk.submit(form(1), other)
            self.d.desk.submit(form(1, location='Delhi'), other, first['candidate'])
        self.assertEqual(caught.exception.status, 403)

    def test_a_new_link_replaces_the_old_one(self):
        secret = key()
        first = self.d.desk.submit(form(), secret)
        fresh = self.d.desk.new_key(first['candidate'])
        self.assertIsNone(self.d.desk.authorize(first['candidate'], secret))
        self.assertIsNotNone(self.d.desk.authorize(first['candidate'], fresh))

    def test_intake_has_its_own_capacity_and_never_blocks_an_account(self):
        for n in range(30):
            self.d.desk.submit(form(n), key())
        account = 'a' * 64
        self.d.jobs.enqueue(account, account_payload(99))
        with patch('automation_jobs.INTAKE_PENDING_LIMIT', 31):
            self.d.desk.submit(form(30), key())
            with self.assertRaises(AutomationCapacity):
                self.d.desk.submit(form(31), key())

    def test_accounts_are_prepared_first_then_applications_oldest_first_and_pause_holds_them(self):
        first = self.d.desk.submit(form(0), key())
        second = self.d.desk.submit(form(1), key())
        account = 'b' * 64
        identifier = self.d.jobs.enqueue(account, account_payload(5))
        ahead = self.d.jobs.queue_positions()
        self.assertEqual(ahead[(account, identifier)], 0)
        self.assertEqual(ahead[(candidate_owner(first['candidate']), first['job'])], 1)
        self.assertEqual(ahead[(candidate_owner(second['candidate']), second['job'])], 2)
        listed = self.d.jobs.list(candidate_owner(second['candidate']))[0]
        self.assertEqual((listed['queue_ahead'], listed['queue_paused']), (2, False))
        self.d.jobs.set_paused(True)
        self.assertTrue(self.d.jobs.paused())
        prepared = []
        self.d.workspace.automation_ready = lambda: True
        with patch.object(self.d.jobs, '_prepare', side_effect=lambda job: prepared.append(job['id'])), \
                patch.object(self.d.jobs, '_recheck_stale_questions'), patch.object(self.d.jobs, '_backfill_labels'):
            worker = threading.Thread(target=self.d.jobs._consume, daemon=True)
            worker.start()
            time.sleep(1)
            self.assertEqual(prepared, [], 'nothing starts while paused')
            self.d.jobs.set_paused(False)
            deadline = time.time() + 10
            while len(prepared) < 3 and time.time() < deadline:
                time.sleep(0.1)
            self.d.jobs.stopping.set()
            self.d.jobs.wake.set()
            worker.join(5)
        self.assertEqual(prepared[:3], [identifier, first['job'], second['job']])

    def test_regenerating_a_candidates_resume_stays_in_the_intake_queue(self):
        first = self.d.desk.submit(form(), key())
        owner = candidate_owner(first['candidate'])
        facts = intake_request(form())[0]['profile']
        with closing(self.d.jobs.connection()) as db, db:
            db.execute("UPDATE automation SET status='handed_off', facts=?, payload=NULL WHERE owner=?",
                       (json.dumps(facts), owner))
        new = self.d.jobs.regenerate(owner, first['job'], 'DevOps Engineer')
        source = self.d.rows('SELECT source FROM automation WHERE id=?', new)[0]['source']
        self.assertEqual(source, INTAKE_SOURCE)

    def test_the_operator_list_shows_every_candidate_with_the_latest_state(self):
        made = [self.d.desk.submit(form(n), key()) for n in range(3)]
        listed = self.d.desk.candidates()
        self.assertEqual({entry['candidate'] for entry in listed}, {item['candidate'] for item in made})
        entry = next(item for item in listed if item['candidate'] == made[2]['candidate'])
        self.assertEqual((entry['name'], entry['job']['status'], entry['job']['queue_ahead']), ('Asha Example 2', 'queued', 2))

    def test_a_candidate_who_deletes_everything_leaves_no_contact_details(self):
        secret = key()
        first = self.d.desk.submit(form(), secret)
        owner = candidate_owner(first['candidate'])
        self.assertFalse(self.d.desk.scrub_if_empty(owner), 'kept while a resume remains')
        with closing(self.d.jobs.connection()) as db, db:
            db.execute('DELETE FROM automation WHERE owner=?', (owner,))
        self.assertTrue(self.d.desk.scrub_if_empty(owner))
        row = self.d.rows('SELECT name, email, phone, form, scrubbed FROM intake')[0]
        self.assertEqual((row['name'], row['email'], row['phone'], row['form']), (None, None, None, None))
        self.assertIsNone(self.d.desk.authorize(first['candidate'], secret))
        self.d.desk.submit(form(), key())  # the same person may apply again


class GpuGateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = patch.dict(os.environ, {'RESUMEOPS_GPU_GATE': temp.name, 'RESUMEOPS_GPU_SLOTS': '1'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def overlap(self):
        active, most, lock = [0], [0], threading.Lock()

        def work():
            with gpu_gate.gpu_slot(poll=0.01):
                with lock:
                    active[0] += 1
                    most[0] = max(most[0], active[0])
                time.sleep(0.15)
                with lock:
                    active[0] -= 1

        threads = [threading.Thread(target=work) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        return most[0]

    def test_one_slot_serializes_generations_and_is_reentrant(self):
        self.assertEqual(self.overlap(), 1)
        with gpu_gate.gpu_slot():
            with gpu_gate.gpu_slot():  # a retry in a larger window keeps its slot
                self.assertEqual(gpu_gate.busy(), 1)
        self.assertEqual(gpu_gate.busy(), 0)

    def test_more_slots_allow_that_many_at_once(self):
        with patch.dict(os.environ, {'RESUMEOPS_GPU_SLOTS': '2'}):
            self.assertEqual(self.overlap(), 2)

    def test_every_ollama_generation_takes_a_slot(self):
        from resume_bot.ollama import Ollama
        seen = []
        with patch.object(Ollama, '_generate_once', side_effect=lambda *a, **k: seen.append(gpu_gate.busy()) or {}):
            Ollama().generate('prompt')
        self.assertEqual(seen, [1])


class PrefillTests(unittest.TestCase):
    TEXT = ('Kiran Example\nkiran.example@example.com | +91 90000 77777 | https://www.linkedin.com/in/kiran-example\n'
            'Skills: Java, Spring Boot, MySQL, Docker\nEMPLOYMENT HISTORY\nCompany / employer\nEmployment dates\n'
            'Example Health Services Pvt Ltd\n11 Jul 2022 – 08 Apr 2025\nDemo Software Pvt Ltd\n23 Aug 2021 – 22 Apr 2022\n'
            'EDUCATION\nHighest Qualification\nB.Tech\nCollege / University\nExample College, Bengaluru\n'
            'Education Passing Year\n2015\n')

    def test_what_the_document_states_fills_the_form_at_once(self):
        filled = quick_prefill(self.TEXT)
        self.assertEqual((filled['full_name'], filled['email'], filled['phone']),
                         ('Kiran Example', 'kiran.example@example.com', '+91 90000 77777'))
        self.assertIn('linkedin.com/in/kiran-example', filled['linkedin'])
        self.assertTrue({'Java', 'Spring Boot', 'MySQL', 'Docker'} <= set(filled['skills'].split(', ')))
        self.assertEqual([row['company'] for row in filled['work_experience']],
                         ['Example Health Services Pvt Ltd', 'Demo Software Pvt Ltd'])
        self.assertEqual(filled['work_experience'][0]['start'], '11 Jul 2022')
        self.assertEqual(filled['education'][0]['graduation_year'], '2015')

    def test_a_word_document_is_read(self):
        body = ''.join(f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>' for line in self.TEXT.splitlines())
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
                             f'2006/main"><w:body>{body}</w:body></w:document>')
        self.assertIn('Example Health Services', document_text('docx', data.getvalue()))
        with self.assertRaises(ValueError):
            document_text('exe', b'MZ')


class PageTests(unittest.TestCase):
    def test_the_status_page_carries_no_dashboard_token_and_only_public_files(self):
        html = candidate_page((ROOT / 'dashboard' / 'automation.html').read_bytes()).decode()
        self.assertNotIn('__TOKEN__', html)
        self.assertIn('/apply/assets/candidate.js', html)
        self.assertNotIn('src="/auth.js"', html)
        for src in ('/automation.js', '/recovery.js', '/builder.css', '/automation.css'):
            self.assertIn('/apply/assets' + src, html)
        with self.assertRaises(RuntimeError):
            candidate_page(b'<html>changed</html>')


class AccessStub:
    def accepts(self, token):
        return token == 'member'

    def claims(self, token):
        return {'email': 'member@example.com', 'exp': time.time() + 3600} if token == 'member' else None


class HttpTests(unittest.TestCase):
    WEB = 'https://resumeops.test'

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Workspace(Path(temp.name), code_root=ROOT)
        self.server = DashboardServer(('127.0.0.1', 0), handler(self.workspace, '', 'token'))
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.server.RequestHandlerClass = handler(self.workspace, self.origin, 'token', web_origin=self.WEB,
                                                  access_verifier=AccessStub())
        (self.workspace.root / '.resumeops-queue-paused').write_text('{}')  # saved, not prepared, in these tests
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.RequestHandlerClass.stop_imports)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, path, body=None, headers=None, web=True, method=None):
        merged = {'Host': 'resumeops.test', 'X-Forwarded-Proto': 'https', 'Cf-Connecting-IP': '203.0.113.7'} if web else {}
        if body is not None:
            merged.update({'Content-Type': 'application/json', 'Origin': self.WEB if web else self.origin})
        merged.update(headers or {})
        request = Request(self.origin + path, data=None if body is None else json.dumps(body).encode(),
                          headers=merged, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def test_the_public_pages_need_no_sign_in_but_nothing_else_opens(self):
        status, page = self.call('/apply')
        self.assertEqual(status, 200)
        self.assertIn(b'id="ap-form"', page)
        status, page = self.call('/apply/status')
        self.assertEqual(status, 200)
        self.assertNotIn(b'token', page.split(b'</head>')[0].replace(b'dashboard-token" content=""', b''))
        self.assertEqual(self.call('/apply/assets/apply.js')[0], 200)
        self.assertEqual(self.call('/apply/assets/resume_dashboard.py')[0], 404)
        self.assertEqual(self.call('/apply/assets/../automation_jobs.py')[0], 404)
        for path in ('/', '/api/automation', '/api/intake/candidates', '/candidates', '/api/session'):
            self.assertEqual(self.call(path)[0], 403, path)

    def test_submit_follow_and_isolation_over_http(self):
        secret = key()
        status, body = self.call('/apply/api/submit', {'form': form(), 'key': secret})
        self.assertEqual(status, 201, body)
        candidate = json.loads(body)['candidate']
        self.assertEqual(self.call('/apply/api/submit', {'form': form(), 'key': secret})[0], 200, 'a retry is the same')
        status, body = self.call('/apply/api/submit', {'form': form(), 'key': key()})
        self.assertEqual((status, json.loads(body)['candidate']), (409, candidate))
        status, body = self.call('/apply/api/submit', {'form': form(1, email='bad'), 'key': key()})
        self.assertEqual((status, json.loads(body)['field']), (400, 'email'))
        mine = {'X-Candidate-Id': candidate, 'X-Candidate-Key': secret}
        status, body = self.call('/apply/api/automation', headers=mine)
        self.assertEqual(status, 200)
        listed = json.loads(body)
        self.assertEqual(len(listed['jobs']), 1)
        self.assertEqual(listed['candidate']['name'], 'Asha Example 0')
        self.assertNotIn('form', listed['candidate'])
        self.assertEqual(self.call('/apply/api/automation', headers={**mine, 'X-Candidate-Key': key()})[0], 403)
        self.assertEqual(json.loads(self.call('/apply/api/form', headers=mine)[1])['form']['location'], 'Pune')
        # Another candidate never reaches this one's files; the operator does; a member account does not.
        owner = candidate_owner(candidate)
        folder = self.workspace.private_output / f'private-{owner}' / 'rev1'
        folder.mkdir(parents=True)
        (folder / 'resume.pdf').write_bytes(b'%PDF-1.4 fictional')
        revision = f'private-{owner}/rev1'
        status, data = self.call('/apply/api/artifact?key=' + revision + '&name=resume.pdf', headers=mine)
        self.assertEqual((status, data), (200, b'%PDF-1.4 fictional'))
        other_key = key()
        other = json.loads(self.call('/apply/api/submit', {'form': form(2), 'key': other_key})[1])['candidate']
        theirs = {'X-Candidate-Id': other, 'X-Candidate-Key': other_key}
        self.assertEqual(self.call('/apply/api/artifact?key=' + revision + '&name=resume.pdf', headers=theirs)[0], 404)
        self.assertEqual(self.call('/api/artifact?key=' + revision + '&name=resume.pdf', web=False)[0], 200)
        self.assertEqual(self.call('/api/artifact?key=' + revision + '&name=resume.pdf',
                                   headers={'Cf-Access-Jwt-Assertion': 'member'})[0], 404)

    def test_posts_need_the_sites_own_origin(self):
        status, _ = self.call('/apply/api/submit', {'form': form(), 'key': key()}, headers={'Origin': 'https://evil.test'})
        self.assertEqual(status, 403)

    def test_operator_list_new_link_and_pause(self):
        secret = key()
        candidate = json.loads(self.call('/apply/api/submit', {'form': form(), 'key': secret})[1])['candidate']
        operator = {'X-Dashboard-Token': 'token'}
        status, body = self.call('/api/intake/candidates', web=False, headers=operator)
        self.assertEqual(status, 200)
        listed = json.loads(body)
        self.assertEqual(listed['form_url'], self.WEB + '/apply')
        self.assertEqual([entry['candidate'] for entry in listed['candidates']], [candidate])
        self.assertTrue(listed['queue']['paused'])
        status, body = self.call('/api/intake/key', {'candidate': candidate}, web=False, headers=operator)
        self.assertEqual(status, 200)
        link = json.loads(body)['link']
        self.assertTrue(link.startswith(self.WEB + '/apply/status#c=' + candidate + '&k='))
        self.assertEqual(self.call('/apply/api/automation', headers={'X-Candidate-Id': candidate, 'X-Candidate-Key': secret})[0], 403)
        status, body = self.call('/api/intake/queue', {'paused': False}, web=False, headers=operator)
        self.assertEqual((status, json.loads(body)['paused']), (200, False))
        self.assertEqual(self.call('/api/intake/candidates', headers={'Cf-Access-Jwt-Assertion': 'member',
                                                                     'X-Dashboard-Token': 'token'})[0], 404)

    def test_150_simultaneous_applications_are_all_saved_once(self):
        def apply(n):
            secret = key()
            for attempt in range(40):
                status, body = self.call('/apply/api/submit', {'form': form(n), 'key': secret})
                if status in (200, 201):
                    return json.loads(body)['candidate'], secret
                if status not in (429, 503):
                    raise AssertionError((n, status, body))
                time.sleep(0.2 + attempt * 0.1)
            raise AssertionError(f'application {n} was never saved')

        started = time.time()
        with ThreadPoolExecutor(max_workers=150) as pool:
            results = list(pool.map(apply, range(150)))
        elapsed = time.time() - started
        candidates = [candidate for candidate, _ in results]
        self.assertEqual(len(set(candidates)), 150)
        with closing(sqlite3.connect(self.workspace.root / '.resumeops-automation.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM intake').fetchone()[0], 150)
            self.assertEqual(db.execute("SELECT count(*) FROM automation WHERE source='intake' AND status='queued'").fetchone()[0], 150)
        # Every candidate reaches exactly their own application.
        for candidate, secret in results[:20]:
            status, body = self.call('/apply/api/automation', headers={'X-Candidate-Id': candidate, 'X-Candidate-Key': secret})
            self.assertEqual(status, 200)
            self.assertEqual(len(json.loads(body)['jobs']), 1)
        self.assertLess(elapsed, 120)


if __name__ == '__main__':
    unittest.main()
