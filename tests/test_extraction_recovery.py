"""Incomplete extraction recovers or reports clearly; nothing is invented; no job stays "in progress"."""
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZipFile

from resume_bot.automation import CONSENT_VERSION, AnswerError, NeedsInput, apply_answers, source_profile
from resume_bot.ingest import ExtractionError, extract_profile, read_resume_text
from resume_bot.ollama import Ollama, OllamaBudgetError

ROOT = Path(__file__).resolve().parents[1]
TOOLS = all(shutil.which(tool) for tool in ('pdflatex', 'pdftotext'))

# Four roles at three employers, one freelance line with no employer, written with commas so the
# same text survives TXT, DOCX and a LaTeX-compiled PDF.
RESUME = """Asha Example
Backend Engineer
asha.example@example.com, +91 90000 00001, Pune, India
Work Experience
Example Cloud Pvt Ltd, Senior Backend Engineer, Jan 2022 - Present
- Built payment APIs in Python and PostgreSQL.
- Led migration of batch jobs to Kubernetes.
Sample Analytics LLP, Backend Engineer, Jul 2019 - Dec 2021
- Developed ETL pipelines with Airflow.
- Maintained REST services in Django.
Demo Retail Inc, Software Intern, Jan 2019 - Jun 2019
- Wrote unit tests for the checkout service.
Freelance Developer, Mar 2018 - Dec 2018
- Built websites for local clients.
Education
B.E. Computer Engineering, Example Institute of Technology, 2019
Skills
Python, SQL, Django, Kubernetes
"""
NO_EMAIL = RESUME.replace('asha.example@example.com, ', '')
NO_PHONE = RESUME.replace('+91 90000 00001, ', '')
ROLES = [('Example Cloud Pvt Ltd', 'Senior Backend Engineer', 'Jan 2022', 'Present',
          ['Built payment APIs in Python and PostgreSQL.', 'Led migration of batch jobs to Kubernetes.']),
         ('Sample Analytics LLP', 'Backend Engineer', 'Jul 2019', 'Dec 2021',
          ['Developed ETL pipelines with Airflow.', 'Maintained REST services in Django.']),
         ('Demo Retail Inc', 'Software Intern', 'Jan 2019', 'Jun 2019', ['Wrote unit tests for the checkout service.'])]


def field(value):
    return {'value': value, 'evidence': value}


def blank_role():
    return {**{key: field('') for key in ('company', 'position', 'start', 'end', 'location')}, 'bullets': []}


class QuotingModel:
    """Answers like qwen2.5 did in production: real roles, a role with no employer, and padding."""
    def __init__(self, roles=ROLES, freelance=True, padding=2, email='asha.example@example.com'):
        self.roles, self.freelance, self.padding, self.email = roles, freelance, padding, email

    def generate(self, prompt, schema=None, on_progress=None):
        experience = [{'company': field(c), 'position': field(p), 'start': field(s), 'end': field(e),
                       'location': field(''), 'bullets': [field(b) for b in bullets]}
                      for c, p, s, e, bullets in self.roles]
        if self.freelance:
            experience.append({**blank_role(), 'position': field('Freelance Developer'),
                               'start': field('Mar 2018'), 'end': field('Dec 2018'),
                               'bullets': [field('Built websites for local clients.')]})
        experience += [blank_role() for _ in range(self.padding)]
        empty = field('')
        return {'full_name': field('Asha Example'), 'headline': field('Backend Engineer'), 'email': field(self.email),
                'phone': empty, 'location': field('Pune, India'), 'linkedin': empty, 'github': empty,
                'portfolio': empty, 'current_company': empty, 'summary': empty,
                'skills': [field(s) for s in ('Python', 'SQL', 'Django', 'Kubernetes')],
                'certifications': [], 'projects': [], 'experience': experience,
                'education': [{'institution': field('Example Institute of Technology'),
                               'degree': field('B.E. Computer Engineering'), 'start': empty,
                               'end': field('2019'), 'details': empty},
                              {key: field('') for key in ('institution', 'degree', 'start', 'end', 'details')}]}


def documents(folder):
    """The same resume as TXT, DOCX and (when TeX is available) PDF."""
    from resume_bot.compile import compile_latex
    folder.mkdir(parents=True, exist_ok=True)
    text, docx = folder / 'resume.txt', folder / 'resume.docx'
    text.write_text(RESUME, encoding='utf-8')
    with ZipFile(docx, 'w') as archive:
        archive.writestr('word/document.xml',
                         '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                         + ''.join(f'<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>' for line in RESUME.splitlines())
                         + '</w:body></w:document>')
    files = [text, docx]
    if TOOLS:
        latex = folder / 'latex'
        latex.mkdir()
        (latex / 'main.tex').write_text(
            '\\documentclass{article}\\usepackage[margin=1in]{geometry}\\pagestyle{empty}'
            '\\setlength{\\parindent}{0pt}\\begin{document}\n'
            + '\n\n'.join(RESUME.splitlines()) + '\n\\end{document}\n', encoding='utf-8')
        files.append(compile_latex(latex))
    return files


class BudgetServer(BaseHTTPRequestHandler):
    """Reports a context overflow (as Ollama does after a shift) for windows below `fits`."""
    fits = 16384
    requests = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        type(self).requests.append(body['options'])
        ctx = body['options']['num_ctx']
        # Below `fits`, generation runs past the window: Ollama shifts it and still answers "stop".
        reply = {'response': json.dumps({'ok': ctx}), 'done': True, 'done_reason': 'stop',
                 'prompt_eval_count': 1437, 'eval_count': ctx if ctx < self.fits else 400}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class OllamaWindowTests(unittest.TestCase):
    def serve(self, fits):
        BudgetServer.fits, BudgetServer.requests = fits, []
        server = ThreadingHTTPServer(('127.0.0.1', 0), BudgetServer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}'

    def test_silent_context_shift_is_detected_and_recovered_in_a_larger_window(self):
        url = self.serve(fits=8192)
        result = Ollama('qwen2.5:7b', url, timeout=5, max_tokens=1024, num_ctx=4096).generate('p')
        self.assertEqual(result, {'ok': 8192})
        self.assertEqual([r['num_ctx'] for r in BudgetServer.requests], [4096, 8192])
        self.assertEqual([r['num_predict'] for r in BudgetServer.requests], [1024, 2048])

    def test_overflow_in_the_largest_window_is_reported_not_accepted(self):
        url = self.serve(fits=10 ** 9)
        with self.assertRaises(OllamaBudgetError):
            Ollama('qwen2.5:7b', url, timeout=5, num_ctx=16384).generate('p')
        self.assertEqual(len(BudgetServer.requests), 1)

    def test_window_is_sized_for_prompt_and_answer(self):
        client = Ollama('qwen2.5:7b', 'http://127.0.0.1:9')
        self.assertEqual(client.budget('x' * 3000), (8192, 4096))
        num_ctx, num_predict = client.budget('x' * 15000)  # ~5000 prompt tokens + 4096 answer
        self.assertEqual(num_ctx, 16384)
        self.assertEqual(num_predict, 4096)


class ExtractionRecoveryTests(unittest.TestCase):
    def test_placeholder_entries_are_dropped_in_review_and_strict_modes(self):
        model = QuotingModel(freelance=False)
        review = extract_profile(RESUME, model, for_review=True)
        self.assertEqual([e['company'] for e in review['experience']], [r[0] for r in ROLES])
        self.assertEqual(len(review['education']), 1)
        strict = extract_profile(RESUME, model)
        self.assertEqual(len(strict['experience']), 3)

    def needs_input(self, source, model):
        with self.assertRaises(NeedsInput) as caught:
            source_profile(source, model)
        return caught.exception

    def test_missing_employer_becomes_a_question_and_every_entry_is_kept(self):
        gap = self.needs_input(RESUME, QuotingModel())
        self.assertEqual([q['id'] for q in gap.questions], ['experience.3.company'])
        question = gap.questions[0]
        self.assertEqual(question['value'], '', 'a missing employer is never pre-filled')
        self.assertIn('does not state the employer', question['explanation'])
        self.assertEqual(question['label'], 'Employer for Freelance Developer (Mar 2018 - Dec 2018)')
        self.assertEqual([e['company'] for e in gap.draft['experience']], [r[0] for r in ROLES] + [''])
        self.assertEqual(gap.draft['experience'][1]['bullets'], ROLES[1][4])
        profile = apply_answers(gap.draft, gap.questions, {'experience.3.company': '  Self-employed '})
        self.assertEqual([(e['company'], e['position']) for e in profile['experience']],
                         [(c, p) for c, p, *_ in ROLES] + [('Self-employed', 'Freelance Developer')])

    def test_answers_are_validated_per_field(self):
        gap = self.needs_input(RESUME, QuotingModel())
        for answers, field, message in (({}, 'experience.3.company', 'Enter the employer'),
                                        ({'experience.3.company': '   '}, 'experience.3.company', 'Enter the employer'),
                                        ({'experience.3.company': 'x' * 251}, 'experience.3.company', 'under 250'),
                                        ({'experience.3.company': 'A', 'experience.0.company': 'B'}, 'experience.0.company', 'not part')):
            with self.subTest(answers=list(answers)):
                with self.assertRaises(AnswerError) as caught:
                    apply_answers(json.loads(json.dumps(gap.draft)), gap.questions, answers)
                self.assertEqual(caught.exception.field, field)
                self.assertIn(message, str(caught.exception))

    def test_email_is_recovered_verbatim_when_the_model_misses_it(self):
        self.assertEqual(self.needs_input(RESUME, QuotingModel(email='')).draft['email'], 'asha.example@example.com')

    def test_job_title_copied_into_employer_is_not_an_employer(self):
        # Observed from real qwen2.5:7b: company "Freelance Developer" == position "Freelance Developer".
        model = QuotingModel(freelance=False, padding=0)
        original = model.generate

        def copied(*args, **kwargs):
            data = original(*args, **kwargs)
            data['experience'].append({'company': field('Freelance Developer'), 'position': field('Freelance Developer'),
                                       'start': field('Mar 2018'), 'end': field('Dec 2018'), 'location': field(''),
                                       'bullets': [field('Built websites for local clients.')]})
            return data
        model.generate = copied
        gap = self.needs_input(RESUME, model)
        self.assertEqual([q['id'] for q in gap.questions], ['experience.3.company'])
        self.assertEqual(gap.draft['experience'][3]['company'], '')
        self.assertEqual(len(gap.draft['experience']), 4)

    def test_missing_phone_is_asked_missing_email_is_not_and_no_history_is_reported(self):
        # Contact rule: a phone number is mandatory; email is optional.
        profile = source_profile(NO_EMAIL, QuotingModel(email='', freelance=False))
        self.assertEqual((profile['email'], profile['phone']), ('', '+91 90000 00001'))
        gap = self.needs_input(NO_PHONE, QuotingModel(freelance=False))
        self.assertEqual([q['id'] for q in gap.questions], ['phone'])
        with self.assertRaises(AnswerError) as caught:
            apply_answers(gap.draft, gap.questions, {'phone': 'not-a-phone'})
        self.assertEqual(caught.exception.field, 'phone')
        nothing = QuotingModel(roles=[], freelance=False, padding=1)
        original = nothing.generate
        nothing.generate = lambda *a, **k: {**original(*a, **k), 'education': []}
        with self.assertRaisesRegex(ExtractionError, 'No work history or education'):
            source_profile(RESUME, nothing)

    def test_complete_resume_needs_no_questions(self):
        profile = source_profile(RESUME, QuotingModel(freelance=False))
        self.assertEqual([e['company'] for e in profile['experience']], [r[0] for r in ROLES])

    def test_multi_company_resume_in_txt_docx_and_pdf(self):
        with tempfile.TemporaryDirectory() as temp:
            for path in documents(Path(temp)):
                with self.subTest(format=path.suffix):
                    gap = self.needs_input(read_resume_text(path), QuotingModel())
                    self.assertEqual([e['company'] for e in gap.draft['experience'][:3]], [r[0] for r in ROLES])
                    self.assertEqual([len(e['bullets']) for e in gap.draft['experience']], [2, 2, 1, 1])
                    self.assertEqual([q['id'] for q in gap.questions], ['experience.3.company'])
                    profile = apply_answers(gap.draft, gap.questions, {'experience.3.company': 'Self-employed'})
                    self.assertEqual(len(profile['experience']), 4)

    def test_malformed_model_output_is_rejected(self):
        class Broken:
            def generate(self, prompt, schema=None, on_progress=None):
                return {'experience': 'not a list'}
        with self.assertRaises(ExtractionError):
            extract_profile(RESUME, Broken(), for_review=True)


class AutomationJobStateTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / 'dashboard'))
        from automation_jobs import AutomationJobs
        from resume_dashboard import Workspace
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Workspace(Path(self.temp.name), code_root=ROOT)
        self.jobs = AutomationJobs(self.workspace)
        payload = {'role': 'Backend Engineer', 'consent': CONSENT_VERSION, 'extension': 'txt',
                   'document': base64.b64encode(RESUME.encode()).decode()}
        with patch.object(self.jobs, 'start'):
            self.id = self.jobs.enqueue('a' * 64, payload)

    def tearDown(self):
        self.temp.cleanup()

    def row(self):
        with closing(self.jobs.connection()) as db:
            return db.execute('SELECT * FROM automation').fetchone()

    def prepare(self, model, source=RESUME):
        with patch.object(self.workspace, 'read_automation_source', return_value=source) as reader, \
                patch('resume_bot.automation.generate', side_effect=lambda ai, *a, **k: model.generate(*a, **k)), \
                patch('resume_bot.automation.role_intelligence',
                      return_value={'role': 'custom', 'label': 'Backend Engineer', 'terms': ['Python'], 'sources': []}), \
                patch('resume_bot.ingest.generate', side_effect=lambda ai, *a, **k: model.generate(*a, **k)):
            self.jobs._prepare(self.row())
            return reader.call_count

    def test_missing_detail_waits_for_an_answer_then_continues_without_redoing_work(self):
        self.prepare(QuotingModel())
        row = self.row()
        self.assertEqual((row['status'], row['stage']), ('needs_input', 'Needs your input'))
        self.assertIsNotNone(row['payload'], 'the submission is kept while waiting')
        listed = self.jobs.list('a' * 64)[0]
        self.assertEqual([q['id'] for q in listed['questions']], ['experience.3.company'])
        self.assertNotIn('checkpoints', listed)
        self.assertNotIn('Built payment APIs', json.dumps(listed), 'only the questions leave the server')
        self.assertEqual(self.jobs.list('b' * 64), [], 'another account sees nothing')
        with self.assertRaises(AnswerError):
            self.jobs.answer('b' * 64, self.id, {'experience.3.company': 'Self-employed'})
        with patch.object(self.jobs, 'start'):
            with self.assertRaises(AnswerError) as caught:
                self.jobs.answer('a' * 64, self.id, {'experience.3.company': ''})
            self.assertEqual(caught.exception.field, 'experience.3.company')
            self.assertEqual(self.row()['status'], 'needs_input', 'a rejected answer changes nothing')
            self.jobs.answer('a' * 64, self.id, {'experience.3.company': 'Self-employed'})
        self.assertEqual(self.row()['status'], 'queued')

        class RankOnly:
            def generate(self, prompt, schema=None, on_progress=None):
                if 'DATA:' not in prompt:
                    raise AssertionError('extraction must not run again')
                return {}
        reads = self.prepare(RankOnly())
        self.assertEqual(reads, 0)
        row = self.row()
        self.assertEqual(row['status'], 'handed_off')
        listed = self.jobs.list('a' * 64)[0]
        self.assertEqual(listed['notice'], ['Confirmed by you: Employer for Freelance Developer (Mar 2018 - Dec 2018).'])

    def test_answer_route_is_owner_scoped_and_reports_the_field(self):
        import hashlib
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        from resume_dashboard import handler
        self.prepare(QuotingModel())
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.workspace, '', 't'))
        origin = f'http://127.0.0.1:{server.server_port}'
        server.RequestHandlerClass = handler(self.workspace, origin, 't', automation_first=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        owner = hashlib.sha256((origin + '\0loopback').encode()).hexdigest()
        with closing(self.jobs.connection()) as db, db:
            db.execute('UPDATE automation SET owner=?', (owner,))

        def post(body, token='t'):
            request = Request(origin + '/api/automation/answer', data=json.dumps(body).encode(), method='POST',
                              headers={'Origin': origin, 'X-Dashboard-Token': token, 'Content-Type': 'application/json'})
            try:
                with urlopen(request, timeout=10) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                return error.code, json.load(error)
        self.assertEqual(post({'id': self.id, 'answers': {}}, token='wrong')[0], 403)
        status, body = post({'id': self.id, 'answers': {'experience.3.company': ''}})
        self.assertEqual((status, body['field']), (400, 'experience.3.company'))
        self.assertEqual(post({'id': 'c' * 64, 'answers': {}})[0], 400)
        with patch('automation_jobs.AutomationJobs.start'):
            status, body = post({'id': self.id, 'answers': {'experience.3.company': 'Self-employed'}})
        self.assertEqual((status, body['status']), (200, 'queued'))

    def test_temporary_failure_keeps_completed_work_and_retries(self):
        class Overflowing:
            def generate(self, *args, **kwargs):
                raise OllamaBudgetError('context window overflowed')
        self.prepare(Overflowing())
        row = self.row()
        self.assertEqual((row['status'], row['stage'], row['attempts']), ('retrying', 'Retrying automatically', 1))
        self.assertIsNotNone(row['payload'])
        self.assertIn('source', json.loads(row['checkpoints']))
        reads = self.prepare(QuotingModel(freelance=False))
        self.assertEqual(reads, 0, 'the source document must not be read again')
        self.assertEqual(self.row()['status'], 'handed_off')

    def test_unreadable_resume_stops_with_a_clear_reason(self):
        nothing = QuotingModel(roles=[], freelance=False, padding=1)
        original = nothing.generate
        nothing.generate = lambda *a, **k: {**original(*a, **k), 'education': []}
        self.prepare(nothing)
        row = self.row()
        self.assertEqual((row['status'], row['stage']), ('blocked', 'Needs your input'))
        self.assertIn('No work history or education', row['error'])


def browser_or_skip(test):
    try:
        from playwright.sync_api import sync_playwright
        playwright = sync_playwright().start()
        return playwright, playwright.chromium.launch()
    except Exception as exc:  # pragma: no cover - CI requires the browser
        if os.environ.get('RESUME_BUILDER_REQUIRE_BROWSER'):
            raise
        test.skipTest(f'browser unavailable: {exc}')


class HistoryStatusTests(unittest.TestCase):
    """My resumes shows a stopped job as stopped, with its reason, and a retry as automatic."""

    def test_stopped_and_retrying_jobs_render_accurately_after_reload(self):
        import hashlib
        import time
        sys.path.insert(0, str(ROOT / 'dashboard'))
        from automation_jobs import AutomationJobs
        from resume_dashboard import Workspace, handler
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(Path(temp), code_root=ROOT)
            jobs = AutomationJobs(workspace)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(workspace, '', 't'))
            origin = f'http://127.0.0.1:{server.server_port}'
            server.RequestHandlerClass = handler(workspace, origin, 't', automation_first=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            owner = hashlib.sha256((origin + '\0loopback').encode()).hexdigest()
            now = time.time()
            with closing(jobs.connection()) as db, db:
                # The production job as it was stored before this fix: stale extraction stage at 25%.
                db.execute("INSERT INTO automation(owner,id,status,stage,percent,created,updated,error) VALUES "
                           "(?,?,'blocked','Extracting source facts with Ollama (15 fields)',25,?,?,?)",
                           (owner, 'b' * 64, now - 60, now - 60, 'experience[1].company: missing required information'))
                db.execute("INSERT INTO automation(owner,id,payload,status,stage,percent,created,updated,next_retry,error,attempts) "
                           "VALUES (?,?,'{}','retrying','Retrying automatically',25,?,?,?,?,1)",
                           (owner, 'c' * 64, now, now, now + 3600, 'A temporary Ollama or storage problem interrupted preparation.'))
            pair = browser_or_skip(self)
            playwright, browser = pair
            try:
                page = browser.new_page()
                with patch.object(jobs, 'start'), patch('automation_jobs.AutomationJobs.start'):
                    for attempt in range(2):  # first load, then a refresh
                        page.goto(origin) if attempt == 0 else page.reload()
                        items = page.locator('#auto-job-list li')
                        items.first.wait_for()
                        blocked = page.locator('#auto-job-list li[data-status="blocked"]')
                        retrying = page.locator('#auto-job-list li[data-status="retrying"]')
                        self.assertEqual(blocked.locator('strong').inner_text(), 'Needs attention')
                        self.assertIn('Work entry 2 has no employer', blocked.inner_text())
                        self.assertNotIn('experience[1]', page.locator('body').inner_text())
                        # The panel follows the job still running, never the stopped job's error.
                        self.assertIn('Retrying automatically', page.locator('#auto-status').inner_text())
                        self.assertNotIn('Work entry', page.locator('#auto-status').inner_text())
                        self.assertNotIn('%', blocked.inner_text())
                        self.assertNotIn('Extracting', blocked.inner_text())
                        self.assertIn('Retrying automatically at', retrying.inner_text())
                        self.assertIn('No action needed', retrying.inner_text())
            finally:
                browser.close()
                playwright.stop()
                server.shutdown()
                server.server_close()


class NeedsInputPageTests(unittest.TestCase):
    """Editable fields for missing details; errors once, next to the field; nothing stale on a fresh form."""

    def test_fresh_form_questions_and_field_errors(self):
        import hashlib
        import time
        sys.path.insert(0, str(ROOT / 'dashboard'))
        from automation_jobs import AutomationJobs
        from resume_bot.pipeline import CODE
        from resume_bot.storage import write_json
        from resume_dashboard import Workspace, handler
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(Path(temp), code_root=ROOT)
            write_json(workspace.inbox / '.resume-bot-health.json', {'last_scan': time.time(), 'code': CODE})
            jobs = AutomationJobs(workspace)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(workspace, '', 't'))
            origin = f'http://127.0.0.1:{server.server_port}'
            server.RequestHandlerClass = handler(workspace, origin, 't', automation_first=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            owner = hashlib.sha256((origin + '\0loopback').encode()).hexdigest()
            gap = None
            try:
                source_profile(RESUME, QuotingModel())
            except NeedsInput as error:
                gap = error
            now = time.time()
            with closing(jobs.connection()) as db, db:
                db.execute("INSERT INTO automation(owner,id,payload,status,stage,percent,created,updated,error,checkpoints) "
                           "VALUES (?,?,?, 'needs_input','Needs your input',25,?,?,?,?)",
                           (owner, 'd' * 64, json.dumps({'role': 'Backend Engineer', 'consent': CONSENT_VERSION}),
                            now, now, str(gap), json.dumps({'draft': gap.draft, 'questions': gap.questions})))
                db.execute("INSERT INTO automation(owner,id,status,stage,percent,created,updated,error) VALUES "
                           "(?,?,'blocked','Extracting source facts with Ollama (15 fields)',25,?,?,?)",
                           (owner, 'e' * 64, now - 60, now - 60, 'experience[1].company: missing required information'))
            playwright, browser = browser_or_skip(self)
            try:
                page = browser.new_page()
                with patch('automation_jobs.AutomationJobs.start'):
                    page.goto(origin)
                    waiting = page.locator('#auto-job-list li[data-status="needs_input"]')
                    waiting.wait_for()
                    self.assertTrue(page.locator('#auto-status').is_hidden(), 'no earlier job on a fresh form')
                    field = waiting.get_by_label('Employer for Freelance Developer (Mar 2018 - Dec 2018)')
                    self.assertEqual(field.input_value(), '')
                    self.assertIn('does not state the employer', waiting.inner_text())
                    waiting.get_by_role('button', name='Continue').click()
                    errors = waiting.locator('.field-error')
                    self.assertEqual(errors.count(), 1)
                    self.assertEqual(errors.inner_text(), 'Enter this detail to continue.')
                    self.assertEqual(page.get_by_text('Enter this detail to continue.').count(), 1)
                    self.assertEqual(field.get_attribute('aria-invalid'), 'true')
                    field.fill('Self-employed')
                    self.assertEqual(errors.count(), 0, 'the error clears as soon as the field is edited')
                    page.wait_for_timeout(2000)  # a background refresh must not wipe what was typed
                    self.assertEqual(field.input_value(), 'Self-employed')
                    with page.expect_response(lambda r: r.url.endswith('/api/automation/answer')) as answered:
                        waiting.get_by_role('button', name='Continue').click()
                    self.assertEqual(answered.value.status, 200)
                    with closing(jobs.connection()) as db:
                        self.assertEqual(db.execute("SELECT status FROM automation WHERE id=?", ('d' * 64,)).fetchone()[0], 'queued')
                    # A submission error appears once, next to the field it concerns.
                    page.get_by_label('Target role').fill('!!')
                    page.locator('input[name=method][value=upload]').check()
                    page.locator('#auto-file').set_input_files({'name': 'resume.txt', 'mimeType': 'text/plain',
                                                                'buffer': RESUME.encode()})
                    page.locator('#auto-consent').check()
                    page.get_by_role('button', name='Create verified resume').click()
                    role_error = page.locator('label:has(input[name="role"]) + .field-error')
                    role_error.wait_for()
                    self.assertIn('target role', role_error.inner_text().lower())
                    self.assertEqual(page.locator('.field-error').count(), 1)
                    self.assertEqual(page.get_by_text(role_error.inner_text(), exact=True).count(), 1, 'shown once')
                    self.assertNotIn('target role', page.locator('#auto-status').inner_text().lower())
                    page.get_by_label('Target role').fill('Backend Engineer')
                    self.assertEqual(page.locator('.field-error').count(), 0)
            finally:
                browser.close()
                playwright.stop()
                server.shutdown()
                server.server_close()


class OverleafInterruptionTests(unittest.TestCase):
    def test_promotion_is_declined_and_upload_dialog_is_left_alone(self):
        from resume_bot.overleaf import dismiss_interruptions
        try:
            from playwright.sync_api import sync_playwright
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover - CI requires the browser
            if os.environ.get('RESUME_BUILDER_REQUIRE_BROWSER'):
                raise
            self.skipTest(f'browser unavailable: {exc}')
        try:
            page = browser.new_page()
            page.set_content('''
                <button onclick="document.body.dataset.clicked = 'new'">New project</button>
                <div role="dialog" aria-modal="true" id="promo"
                     style="position:fixed;inset:0;background:white;z-index:10">
                  <p>70% off all premium plans</p>
                  <button aria-label="Close dialog" onclick="document.body.dataset.closed = 'x'">x</button>
                  <button onclick="document.body.dataset.accepted = 'yes'">Get discounted plan</button>
                  <button onclick="document.getElementById('promo').remove()">Maybe later</button>
                </div>''')
            dismiss_interruptions(page)
            page.get_by_role('button', name=re.compile(r'^New Project$', re.I)).click(timeout=5000)
            self.assertEqual(page.evaluate('document.body.dataset.clicked'), 'new')
            self.assertIsNone(page.evaluate('document.body.dataset.accepted'))
            self.assertEqual(page.locator('#promo').count(), 0)
            page.set_content('''
                <div role="dialog" id="upload"><input type="file"><button>Close</button>
                  <button onclick="document.body.dataset.uploaded = 'yes'">Select a .zip file</button></div>''')
            dismiss_interruptions(page)
            page.get_by_role('button', name='Select a .zip file').click(timeout=5000)
            self.assertEqual(page.locator('#upload').count(), 1)
            self.assertEqual(page.evaluate('document.body.dataset.uploaded'), 'yes')
        finally:
            browser.close()
            playwright.stop()


if __name__ == '__main__':
    unittest.main()
