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
from resume_bot.ingest import find_phone  # noqa: E402
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


OPERATOR = 'e' * 64  # the operator's account in these tests


def started_job(d, candidate):
    """The automation row of the latest resume started from an application."""
    return d.rows('SELECT a.* FROM automation a JOIN intake_runs r ON r.job_owner = a.owner AND r.job_id = a.id '
                  'WHERE r.candidate=? ORDER BY r.started DESC', candidate)[0]


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.d = Desk(self)

    def test_a_submission_is_saved_and_waits_and_a_retry_is_the_same_application(self):
        secret = key()
        first = self.d.desk.submit(form(), secret)
        self.assertTrue(first['created'])
        self.assertRegex(first['candidate'], r'^RO-[2-9A-HJKMNP-Z]{10}$')
        again = self.d.desk.submit(form(), secret)
        self.assertEqual((again['candidate'], again['created']), (first['candidate'], False))
        self.assertEqual(self.d.rows('SELECT 1 FROM automation'), [], 'nothing is processed until the operator starts it')
        stored = self.d.rows('SELECT key_hash, form FROM intake')[0]
        self.assertNotIn(secret, stored['key_hash'], 'only a hash of the private key is stored')
        self.assertEqual(json.loads(stored['form'])['full_name'], 'Asha Example 0')
        self.assertEqual(self.d.desk.candidate_jobs(first['candidate']), [])

    def test_the_same_facts_from_another_device_are_not_saved_twice(self):
        first = self.d.desk.submit(form(), key())
        with self.assertRaises(IntakeDuplicate) as caught:
            self.d.desk.submit(form(), key())
        self.assertEqual(caught.exception.candidate, first['candidate'])
        self.assertEqual(len(self.d.rows('SELECT 1 FROM intake')), 1)

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

    def test_saved_applications_have_their_own_capacity(self):
        made = [self.d.desk.submit(form(n), key()) for n in range(3)]
        with patch('intake.INTAKE_WAITING_LIMIT', 3):
            with self.assertRaises(AutomationCapacity):
                self.d.desk.submit(form(3), key())
            self.d.desk.start(made[0]['candidate'], OPERATOR)  # a started application no longer waits
            self.d.desk.submit(form(3), key())

    def test_start_runs_the_upload_workflow_on_a_word_resume_of_the_form(self):
        from automation_jobs import submission
        from intake import form_document
        from resume_bot.automation import CONSENT_VERSION
        from resume_bot.ingest import read_resume_text
        applicant = form(projects=[{'name': 'Order Service', 'stack': 'Java, Spring Boot, MySQL',
                                    'description': 'Built order endpoints with Spring Boot and MySQL.'}],
                         certifications='Oracle Java SE 11 Developer', linkedin='https://www.linkedin.com/in/asha-example')
        first = self.d.desk.submit(applicant, key())
        started = self.d.desk.start(first['candidate'], OPERATOR)
        self.assertEqual((started['owner'], started['created']), (OPERATOR, True))
        row = started_job(self.d, first['candidate'])
        self.assertEqual((row['owner'], row['source'], row['candidate'], row['status']),
                         (OPERATOR, INTAKE_SOURCE, 'Asha Example 0', 'queued'))
        payload = json.loads(row['payload'])
        # Exactly the request the dashboard makes for an uploaded Word resume (the same validation, unchanged).
        document = base64.b64encode(form_document(applicant)).decode()
        expected = submission({'consent': CONSENT_VERSION, 'role': 'Java Backend Developer', 'extension': 'docx',
                               'document': document})
        self.assertEqual(payload, expected)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'application.docx'
            path.write_bytes(base64.b64decode(payload['document']))
            text = read_resume_text(path)
        for fact in ('Asha Example 0', 'asha.0@example.com', '+91 90000 10000', 'Pune', 'TECHNICAL SKILLS',
                     'Java, Spring Boot, MySQL, JUnit', 'WORK EXPERIENCE', 'Software Engineer — Example Labs (fictional)',
                     'Jan 2022 – Present', 'Built Spring Boot REST APIs for order workflows with Java and MySQL.',
                     'PROJECTS', 'Order Service | Java, Spring Boot, MySQL', 'EDUCATION', 'B.Tech',
                     'Example Institute (fictional)', 'Year of completion: 2021', 'CERTIFICATIONS',
                     'Oracle Java SE 11 Developer', 'LinkedIn: https://www.linkedin.com/in/asha-example'):
            self.assertIn(fact, text)
        again = self.d.desk.start(first['candidate'], OPERATOR)
        self.assertEqual((again['job'], again['created']), (started['job'], False), 'never processed twice')
        self.assertEqual(len(self.d.rows('SELECT 1 FROM automation')), 1)
        # The operator starts many at once: the intake limits apply, not an account's four in progress.
        for n in range(1, 7):
            self.d.desk.start(self.d.desk.submit(form(n), key())['candidate'], OPERATOR)
        self.assertEqual(len(self.d.rows('SELECT 1 FROM automation WHERE owner=?', OPERATOR)), 7)

    def test_a_fresher_application_becomes_a_resume_without_employment(self):
        from intake import form_document
        from resume_bot.ingest import read_resume_text
        applicant = form(fresher=True, work_experience=[])
        first = self.d.desk.submit(applicant, key())
        self.d.desk.start(first['candidate'], OPERATOR)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'application.docx'
            path.write_bytes(form_document(applicant))
            text = read_resume_text(path)
        self.assertNotIn('WORK EXPERIENCE', text)
        self.assertIn('PROJECTS', text)

    def test_corrections_after_a_start_show_and_can_be_started_again(self):
        secret = key()
        first = self.d.desk.submit(form(), secret)
        self.d.desk.start(first['candidate'], OPERATOR)
        entry = next(item for item in self.d.desk.candidates() if item['candidate'] == first['candidate'])
        self.assertFalse(entry['details_changed'])
        self.d.desk.submit(form(location='Bengaluru'), secret, first['candidate'])
        entry = next(item for item in self.d.desk.candidates() if item['candidate'] == first['candidate'])
        self.assertTrue(entry['details_changed'])
        again = self.d.desk.start(first['candidate'], OPERATOR)
        self.assertTrue(again['created'])
        entry = next(item for item in self.d.desk.candidates() if item['candidate'] == first['candidate'])
        self.assertEqual((entry['details_changed'], entry['versions'], entry['job']['id']), (False, 2, again['job']))

    def test_accounts_are_prepared_first_then_started_applications_and_pause_holds_them(self):
        first = self.d.desk.submit(form(0), key())
        second = self.d.desk.submit(form(1), key())
        jobs = [self.d.desk.start(item['candidate'], OPERATOR)['job'] for item in (first, second)]
        account = 'b' * 64
        identifier = self.d.jobs.enqueue(account, account_payload(5))
        ahead = self.d.jobs.queue_positions()
        self.assertEqual([ahead[(account, identifier)], ahead[(OPERATOR, jobs[0])], ahead[(OPERATOR, jobs[1])]], [0, 1, 2])
        self.d.jobs.set_paused(True)
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
        self.assertEqual(prepared[:3], [identifier, *jobs])

    def test_one_network_has_its_own_limit_and_the_queue_takes_turns_between_networks(self):
        flood = [self.d.desk.submit(form(n), key(), client='net-a') for n in range(4)]
        other = self.d.desk.submit(form(9), key(), client='net-b')
        with patch('automation_jobs.INTAKE_CLIENT_LIMIT', 3):
            jobs = [self.d.desk.start(item['candidate'], OPERATOR)['job'] for item in flood[:3]]
            with self.assertRaises(AutomationCapacity):
                self.d.desk.start(flood[3]['candidate'], OPERATOR)
            theirs = self.d.desk.start(other['candidate'], OPERATOR)['job']
        ahead = self.d.jobs.queue_positions()
        # The candidate from another network is second, not fourth, behind the first of the three.
        self.assertEqual([ahead[(OPERATOR, job)] for job in (jobs[0], theirs, jobs[1], jobs[2])], [0, 1, 2, 3])
        stored = self.d.rows('SELECT DISTINCT client FROM automation WHERE source=?', INTAKE_SOURCE)
        self.assertEqual(sorted(row[0] for row in stored), ['net-a', 'net-b'])

    def test_regenerating_a_started_resume_stays_in_the_intake_queue(self):
        first = self.d.desk.submit(form(), key(), client='net-a')
        job = self.d.desk.start(first['candidate'], OPERATOR)['job']
        facts = intake_request(form())[0]['profile']
        with closing(self.d.jobs.connection()) as db, db:
            db.execute("UPDATE automation SET status='handed_off', facts=?, payload=NULL WHERE owner=?",
                       (json.dumps(facts), OPERATOR))
        new = self.d.jobs.regenerate(OPERATOR, job, 'DevOps Engineer')
        row = self.d.rows('SELECT source, client FROM automation WHERE id=?', new)[0]
        self.assertEqual((row['source'], row['client']), (INTAKE_SOURCE, 'net-a'))

    def test_the_candidate_sees_only_progress_and_their_own_finished_files(self):
        first = self.d.desk.submit(form(), key())
        self.d.desk.start(first['candidate'], OPERATOR)
        other = self.d.desk.submit(form(1), key())
        self.d.desk.start(other['candidate'], OPERATOR)
        [job] = self.d.desk.candidate_jobs(first['candidate'])
        from intake import CANDIDATE_JOB_FIELDS
        self.assertLessEqual(set(job), {*CANDIDATE_JOB_FIELDS, 'ats'})
        self.assertEqual(job['status'], 'queued')
        self.assertEqual(self.d.desk.candidate_revisions(first['candidate']), set())

    def test_the_operator_list_shows_every_application_with_its_details_and_state(self):
        made = [self.d.desk.submit(form(n), key()) for n in range(3)]
        self.d.desk.start(made[1]['candidate'], OPERATOR)
        self.d.desk.start(made[2]['candidate'], OPERATOR)
        listed = {entry['candidate']: entry for entry in self.d.desk.candidates()}
        self.assertEqual(set(listed), {item['candidate'] for item in made})
        waiting = listed[made[0]['candidate']]
        self.assertEqual((waiting['started'], waiting.get('job')), (None, None))
        self.assertEqual(waiting['form']['work_experience'][0]['responsibilities'].splitlines()[1],
                         'Wrote JUnit tests for the order services and repositories.')
        started = listed[made[2]['candidate']]
        self.assertEqual((started['name'], started['job']['status'], started['job']['queue_ahead']),
                         ('Asha Example 2', 'queued', 1))

    def test_the_operator_deletes_an_application_with_its_resumes(self):
        secret = key()
        first = self.d.desk.submit(form(), secret)
        self.d.desk.start(first['candidate'], OPERATOR)
        result = self.d.desk.delete_application(first['candidate'], {'via': 'test'})
        self.assertEqual((result['deleted'], result['kept']), (1, []))
        self.assertEqual(self.d.rows('SELECT 1 FROM automation'), [])
        row = self.d.rows('SELECT name, email, phone, form, scrubbed FROM intake')[0]
        self.assertEqual((row['name'], row['email'], row['phone'], row['form']), (None, None, None, None))
        self.assertIsNone(self.d.desk.authorize(first['candidate'], secret))
        self.assertEqual(self.d.rows('SELECT 1 FROM intake_runs'), [])
        self.assertTrue(self.d.rows('SELECT 1 FROM audit'), 'the deletion is recorded')
        self.d.desk.submit(form(), key())  # the same person may apply again

    def test_applications_saved_before_the_operator_started_processing_keep_their_resume(self):
        first = self.d.desk.submit(form(), key())
        owner = candidate_owner(first['candidate'])
        with closing(self.d.jobs.connection()) as db, db:  # how the previous release saved an application
            db.execute('DELETE FROM intake_runs')
            self.d.jobs.insert(db, owner, intake_request(form())[0], INTAKE_SOURCE, 'Asha Example 0')
        reopened = IntakeDesk(self.d.workspace, self.d.jobs)
        [job] = reopened.candidate_jobs(first['candidate'])
        self.assertEqual(job['status'], 'queued')
        entry = next(item for item in reopened.candidates() if item['candidate'] == first['candidate'])
        self.assertTrue(entry['job']['candidates_own'])


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

    def test_a_heading_is_never_taken_for_the_name(self):
        # Seen live: with a first line that is not a plain name, "Employment dates" became the full name.
        text = self.TEXT.replace('Kiran Example\n', 'Kiran Example 1121 (fictional)\n')
        filled = quick_prefill(text)
        self.assertEqual(filled['full_name'], '')
        self.assertEqual(filled['email'], 'kiran.example@example.com')
        self.assertEqual(quick_prefill('Personal Details\nEmployment dates\n' + self.TEXT)['full_name'], '')

    def test_the_phone_is_never_taken_from_an_email_address_or_link(self):
        # Seen live: "meera.1121150700@example.com | +91 90000 31313" prefilled the phone as 1121150700.
        for contact in ('kiran.9876543210@example.com | +91 90000 77777',
                        '9876543210@example.com | +91 90000 77777',
                        'https://example.com/u/98765432101 | kiran.example@example.com | +91 90000 77777'):
            with self.subTest(contact=contact):
                text = self.TEXT.replace('kiran.example@example.com | +91 90000 77777', contact)
                self.assertEqual(quick_prefill(text)['phone'], '+91 90000 77777')
                self.assertEqual(find_phone(text).group(0), '+91 90000 77777')
        self.assertIsNone(find_phone('Email: 9876543210@example.com'))

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
    def test_public_requests_carry_the_sites_cookies(self):
        # Seen live: with credentials 'omit', a signed-in browser's Cloudflare Access cookie was not sent, so Access
        # answered every /apply request with its login redirect and the status page showed no resumes.
        for name in ('apply.js', 'apply-status.js', 'candidate.js'):
            source = (ROOT / 'dashboard' / name).read_text(encoding='utf-8')
            self.assertNotIn("credentials: 'omit'", source, name)
            self.assertIn("credentials: 'same-origin'", source, name)

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
        self.assertIn(b'/apply/assets/apply-status.js', page)
        self.assertNotIn(b'token', page.lower())
        self.assertEqual(self.call('/apply/assets/apply.js')[0], 200)
        self.assertEqual(self.call('/apply/assets/resume_dashboard.py')[0], 404)
        self.assertEqual(self.call('/apply/assets/../automation_jobs.py')[0], 404)
        for path in ('/', '/api/automation', '/api/intake/candidates', '/candidates', '/api/session'):
            self.assertEqual(self.call(path)[0], 403, path)

    def test_submit_follow_and_isolation_over_http(self):
        from intake import IntakeDesk as Desk_
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
        listed = json.loads(body)
        self.assertEqual((status, listed['jobs']), (200, []), 'saved, waiting for the operator')
        self.assertEqual(listed['candidate']['name'], 'Asha Example 0')
        self.assertNotIn('form', listed['candidate'])
        self.assertEqual(self.call('/apply/api/automation', headers={**mine, 'X-Candidate-Key': key()})[0], 403)
        self.assertEqual(json.loads(self.call('/apply/api/form', headers=mine)[1])['form']['location'], 'Pune')
        # Only the operator starts processing; a member account cannot.
        operator = {'X-Dashboard-Token': 'token'}
        self.assertEqual(self.call('/api/intake/start', {'candidate': candidate},
                                   headers={'Cf-Access-Jwt-Assertion': 'member', 'X-Dashboard-Token': 'token'})[0], 403)
        status, body = self.call('/api/intake/start', {'candidate': candidate}, web=False, headers=operator)
        self.assertEqual(status, 202, body)
        started = json.loads(body)
        status, body = self.call('/apply/api/automation', headers=mine)
        [job] = json.loads(body)['jobs']
        self.assertEqual((job['id'], job['status']), (started['job'], 'queued'))
        self.assertFalse({'questions', 'proposals', 'overleaf', 'drafts'} & set(job), 'progress only')
        # The candidate takes no processing actions.
        for action in ('answer', 'projects', 'regenerate', 'delete'):
            status, _ = self.call('/apply/api/automation/' + action, {'id': started['job']}, headers=mine)
            self.assertEqual(status, 403, action)
        # Downloads: only the finished resumes of this candidate's own application.
        folder = self.workspace.private_output / f'private-{started["owner"]}' / 'rev1'
        folder.mkdir(parents=True)
        (folder / 'resume.pdf').write_bytes(b'%PDF-1.4 fictional')
        revision = f'private-{started["owner"]}/rev1'
        other_key = key()
        other = json.loads(self.call('/apply/api/submit', {'form': form(2), 'key': other_key})[1])['candidate']
        theirs = {'X-Candidate-Id': other, 'X-Candidate-Key': other_key}
        self.assertEqual(self.call('/apply/api/artifact?key=' + revision + '&name=resume.pdf', headers=mine)[0], 404,
                         'not finished yet')
        with patch.object(Desk_, 'candidate_revisions', lambda desk, who: {revision} if who == candidate else set()):
            status, data = self.call('/apply/api/artifact?key=' + revision + '&name=resume.pdf', headers=mine)
            self.assertEqual((status, data), (200, b'%PDF-1.4 fictional'))
            self.assertEqual(self.call('/apply/api/artifact?key=' + revision + '&name=resume.pdf', headers=theirs)[0], 404)
            self.assertEqual(self.call('/apply/api/artifact?key=' + revision + '&name=main.py', headers=mine)[0], 404)
        # The operator deletes an application (confirmed); a member account cannot.
        self.assertEqual(self.call('/api/intake/delete', {'candidate': other}, web=False, headers=operator)[0], 400)
        self.assertEqual(self.call('/api/intake/delete', {'candidate': other, 'confirm': 'DELETE'},
                                   headers={'Cf-Access-Jwt-Assertion': 'member', 'X-Dashboard-Token': 'token'})[0], 403)
        status, body = self.call('/api/intake/delete', {'candidate': other, 'confirm': 'DELETE'}, web=False,
                                 headers=operator)
        self.assertEqual((status, json.loads(body)['kept']), (200, []))
        self.assertEqual(self.call('/apply/api/automation', headers=theirs)[0], 403)
        # The operator sees the full details of every application.
        listed = json.loads(self.call('/api/intake/candidates', web=False, headers=operator)[1])
        entry = next(item for item in listed['candidates'] if item['candidate'] == candidate)
        self.assertEqual(entry['form']['projects'][0]['name'], 'Order Service 0')
        self.assertIsNotNone(entry['started'])

    def headers_of(self, path, web=True):
        merged = {'Host': 'resumeops.test', 'X-Forwarded-Proto': 'https', 'Cf-Connecting-IP': '203.0.113.7'} if web else {}
        with urlopen(Request(self.origin + path, headers=merged), timeout=30) as response:
            return response.headers

    def test_responses_forbid_rewriting_and_only_the_form_may_load_turnstile(self):
        form_headers = self.headers_of('/apply')
        self.assertIn('no-transform', form_headers['Cache-Control'])  # no injected analytics beacon
        self.assertIn('no-store', form_headers['Cache-Control'])
        policy = form_headers['Content-Security-Policy']
        self.assertIn("script-src 'self' https://challenges.cloudflare.com", policy)
        self.assertIn('frame-src https://challenges.cloudflare.com', policy)
        self.assertIn("frame-ancestors 'none'", policy)
        for path in ('/apply/status', '/apply/assets/apply.js', '/apply/api/catalog'):
            headers = self.headers_of(path)
            self.assertIn('no-transform', headers['Cache-Control'], path)
            self.assertNotIn('challenges.cloudflare.com', headers['Content-Security-Policy'], path)
        self.assertIn('no-transform', self.headers_of('/', web=False)['Cache-Control'])
        self.assertEqual(json.loads(self.call('/apply/api/config')[1]), {'turnstile': None})

    def test_turnstile_guards_new_applications_and_uploads_through_the_site_once_turned_on(self):
        from intake import HumanCheck
        operator = {'X-Dashboard-Token': 'token'}
        site, secret = '1x00000000000000000000AA', '1x0000000000000000000000000000000AA'
        # Only the operator can turn it on; a member account cannot.
        self.assertEqual(self.call('/api/intake/human-check', {'site_key': site, 'secret': secret},
                                   headers={'Cf-Access-Jwt-Assertion': 'member', 'X-Dashboard-Token': 'token'})[0], 403)
        status, body = self.call('/api/intake/human-check', {'site_key': site, 'secret': secret}, web=False,
                                 headers=operator)
        self.assertEqual((status, json.loads(body)), (200, {'configured': True, 'site_key': site, 'test_keys': True}))
        summary = self.call('/api/intake/summary', web=False, headers=operator)[1]
        self.assertEqual(json.loads(summary)['human_check']['configured'], True)
        self.assertNotIn(secret.encode(), summary)  # the secret never leaves the server
        self.assertEqual(json.loads(self.call('/apply/api/config')[1]), {'turnstile': site})
        self.assertEqual(json.loads(self.call('/apply/api/config', web=False)[1]), {'turnstile': None})
        # Through the site, a new application or an upload needs a pass.
        status, body = self.call('/apply/api/submit', {'form': form(), 'key': key()})
        self.assertEqual((status, json.loads(body).get('human_check')), (403, True))
        upload = {'extension': 'docx', 'document': base64.b64encode(b'not a document').decode(), 'session': 'a' * 24}
        status, body = self.call('/apply/api/prefill', upload)
        self.assertEqual((status, json.loads(body).get('human_check')), (403, True))
        with patch.object(HumanCheck, 'siteverify', return_value={'success': False, 'error-codes': ['invalid']}):
            status, body = self.call('/apply/api/verify', {'token': 'XXXX.DUMMY.TOKEN.XXXX'})
        self.assertEqual((status, json.loads(body).get('human_check')), (403, True))
        with patch.object(HumanCheck, 'siteverify', return_value={'success': True, 'hostname': 'example.com'}) as cf:
            status, body = self.call('/apply/api/verify', {'token': 'XXXX.DUMMY.TOKEN.XXXX'})
        self.assertEqual(status, 200, body)
        self.assertEqual(cf.call_args.args, (secret, 'XXXX.DUMMY.TOKEN.XXXX', '203.0.113.7'))
        proof = {'X-Apply-Pass': json.loads(body)['pass']}
        mine = key()
        status, body = self.call('/apply/api/submit', {'form': form(), 'key': mine}, headers=proof)
        self.assertEqual(status, 201, body)
        candidate = json.loads(body)['candidate']
        forged = proof['X-Apply-Pass'][:-4] + ('0000' if not proof['X-Apply-Pass'].endswith('0000') else '1111')
        self.assertEqual(self.call('/apply/api/submit', {'form': form(1), 'key': key()},
                                   headers={'X-Apply-Pass': forged})[0], 403)
        with patch.object(HumanCheck, 'PASS_USES', {'submit': 1, 'prefill': 1}):
            # The pass is used up by one new application here, but a retry of the saved one needs no check.
            self.assertEqual(self.call('/apply/api/submit', {'form': form(), 'key': mine})[0], 200)
            status, body = self.call('/apply/api/submit', {'form': form(2), 'key': key()}, headers=proof)
            self.assertEqual((status, json.loads(body).get('human_check')), (403, True))
        # A correction is proven by the candidate's private key; this machine is never checked.
        correction = {'form': form(), 'key': mine, 'candidate': candidate}
        self.assertEqual(self.call('/apply/api/submit', correction)[0], 200)
        self.assertEqual(self.call('/apply/api/submit', {'form': form(3), 'key': key()}, web=False)[0], 201)
        # Turned off, the site accepts applications without a pass again.
        status, body = self.call('/api/intake/human-check', {'site_key': '', 'secret': ''}, web=False, headers=operator)
        self.assertEqual(json.loads(body)['configured'], False)
        self.assertEqual(self.call('/apply/api/submit', {'form': form(4), 'key': key()})[0], 201)
        self.assertEqual(self.call('/apply/api/verify', {'token': 'x'})[0], 404)

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
            self.assertEqual(db.execute('SELECT count(*) FROM automation').fetchone()[0], 0, 'saved, not processed')
        # Every candidate reaches exactly their own application.
        for candidate, secret in results[:20]:
            status, body = self.call('/apply/api/automation', headers={'X-Candidate-Id': candidate, 'X-Candidate-Key': secret})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['candidate']['candidate'], candidate)
        self.assertLess(elapsed, 120)


if __name__ == '__main__':
    unittest.main()
