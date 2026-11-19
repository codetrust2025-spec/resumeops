"""Regression checks for AI-first minimal intake: no invented career history."""
import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from resume_bot.automation import CONSENT_VERSION
from resume_bot.generate import same_track
from resume_bot.profile import validate_profile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from automation_jobs import AutomationJobs, submission
from resume_dashboard import Workspace

OWNER = 'a' * 64


def payload(**candidate):
    return {'consent': CONSENT_VERSION, 'role': 'Angular Developer',
            'candidate': {'full_name': 'Arun Kumar (fictional)',
                          'phone': '+91 9000000000', **candidate}}


class MinimalIntakeTests(unittest.TestCase):
    def test_name_and_phone_enqueue_without_invented_profile(self):
        accepted = submission(payload())
        self.assertEqual(accepted['identity']['full_name'], 'Arun Kumar (fictional)')
        self.assertEqual(accepted['identity']['phone'], '+91 9000000000')
        self.assertNotIn('profile', accepted)
        self.assertNotIn('skills', accepted)

    def test_missing_phone_rejected(self):
        with self.assertRaisesRegex(ValueError, 'phone'):
            submission(payload(phone=''))

    def test_phone_only_is_accepted_contact_for_real_facts(self):
        profile = validate_profile({'full_name': 'Arun Kumar', 'phone': '+91 9000000000',
                                    'email': '', 'experience': [], 'education': [
                                        {'institution': 'Example College', 'degree': 'B.Tech'}]})
        self.assertEqual(profile['email'], '')

    def test_named_stack_not_inferred_from_frontend_title(self):
        facts = {'experience': [{'position': 'Frontend Developer', 'bullets': [
            'Maintained React components using TypeScript.']}]}
        self.assertFalse(same_track(facts, 'Angular Developer'))
        self.assertFalse(same_track(facts, 'Python Backend Developer'))
        self.assertFalse(same_track(facts, 'Node.js Developer'))
        self.assertTrue(same_track(facts, 'Frontend Developer'))

    def test_missing_real_work_activities_pause_before_publication(self):
        candidate = {
            'full_name': 'Arun Kumar', 'email': 'arun@example.com',
            'phone': '+91 9000000000', 'years_of_experience': 3,
            'skills': 'React.js, TypeScript',
            'work_experience': [{'company': 'DemoTech', 'position': 'Frontend Developer',
                                 'start': 'July 2023', 'end': 'Present'}],
            'education': [{'degree': 'B.Tech', 'university': 'Example Institute',
                           'graduation_year': '2022'}],
        }
        with tempfile.TemporaryDirectory() as root:
            jobs = AutomationJobs(Workspace(Path(root), code_root=ROOT))
            with patch.object(jobs, 'start'):
                identifier = jobs.enqueue(OWNER, payload(**candidate))
            with closing(jobs.connection()) as db:
                row = db.execute('SELECT * FROM automation WHERE id=?', (identifier,)).fetchone()
            jobs._prepare(row)
            with closing(jobs.connection()) as db:
                waiting = db.execute('SELECT * FROM automation WHERE id=?', (identifier,)).fetchone()
            self.assertEqual(waiting['status'], 'needs_input')
            self.assertEqual([q['id'] for q in json.loads(waiting['checkpoints'])['questions']],
                             ['experience.0.bullets'])
            self.assertIsNone(waiting['job'])
            with patch.object(jobs, 'start'):
                jobs.answer(OWNER, identifier, {'experience.0.bullets':
                    'Developed React components and TypeScript UI forms for internal tools.'})
            with closing(jobs.connection()) as db:
                updated = db.execute('SELECT * FROM automation WHERE id=?', (identifier,)).fetchone()
            self.assertEqual(updated['status'], 'queued')
            self.assertEqual(json.loads(updated['checkpoints'])['facts']['experience'][0]['bullets'],
                             ['Developed React components and TypeScript UI forms for internal tools.'])

    def test_identity_only_waits_for_evidence_without_generating_pdf(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Workspace(Path(root), code_root=ROOT)
            jobs = AutomationJobs(workspace)
            with patch.object(jobs, 'start'):
                identifier = jobs.enqueue(OWNER, payload())
            with closing(jobs.connection()) as db:
                row = db.execute('SELECT * FROM automation WHERE id=?', (identifier,)).fetchone()
            jobs._prepare(row)
            with closing(jobs.connection()) as db:
                waiting = db.execute('SELECT * FROM automation WHERE id=?', (identifier,)).fetchone()
            self.assertEqual(waiting['status'], 'needs_input')
            self.assertIsNone(waiting['job'])
            questions = json.loads(waiting['checkpoints'])['questions']
            self.assertEqual([q['id'] for q in questions], ['source_text'])
            with patch.object(jobs, 'start'):
                jobs.answer(OWNER, identifier, {'source_text':
                    'Worked at DemoTech Solutions as Frontend Developer from July 2023 to Present. '
                    'Built React user interface components. Completed B.Tech in 2022 at Example Institute.'})
            with closing(jobs.connection()) as db:
                continuing = db.execute('SELECT * FROM automation WHERE id=?', (identifier,)).fetchone()
            self.assertEqual(continuing['status'], 'queued')
            self.assertIn('DemoTech Solutions', json.loads(continuing['checkpoints'])['source'])
            self.assertIsNone(continuing['job'])


class ContactRuleTests(unittest.TestCase):
    """Phone is mandatory; email is optional, but a supplied email that cannot be read is never dropped."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / 'tests'))
        import test_facts_v4
        self.fx = test_facts_v4
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def source(self, rows):
        return self.fx.read_resume_text(self.fx.form_docx(Path(self.temp.name) / 'r.docx', rows))

    def rows(self, **values):
        """The fictional form rows, with the phone or email row replaced (or removed when empty)."""
        keys = {'2. Phone Number': 'phone', '3. Email ID': 'email'}
        out = []
        for label, value in self.fx.ROWS:
            value = values.get(keys[label], value) if label in keys else value
            if value:
                out.append((label, value))
        return out

    def test_resume_without_email_but_with_phone_needs_no_email(self):
        from resume_bot.automation import source_profile
        profile = source_profile(self.source(self.rows(email='')), self.fx.JoinedCellModel(email=''))
        self.assertEqual(profile['email'], '')
        self.assertTrue(profile['phone'])

    def test_resume_without_phone_asks_for_it_even_with_an_email(self):
        from resume_bot.automation import NeedsInput, apply_answers, source_profile
        model = self.fx.JoinedCellModel()
        original = model.generate
        model.generate = lambda *a, **k: {**original(*a, **k), 'phone': self.fx.quote('')}
        with self.assertRaises(NeedsInput) as raised:
            source_profile(self.source(self.rows(phone='')), model)
        self.assertEqual([q['id'] for q in raised.exception.questions], ['phone'])
        from resume_bot.automation import AnswerError
        with self.assertRaises(AnswerError) as wrong:
            apply_answers(raised.exception.draft, raised.exception.questions, {'phone': '12345'})
        self.assertEqual(wrong.exception.field, 'phone')
        profile = apply_answers(raised.exception.draft, raised.exception.questions, {'phone': '+91 90000 00000'})
        self.assertEqual(profile['phone'], '+91 90000 00000')

    def test_unreadable_email_in_a_form_row_is_asked_even_when_the_model_omits_it(self):
        from resume_bot.automation import NeedsInput, source_profile
        with self.assertRaises(NeedsInput) as raised:
            source_profile(self.source(self.rows(email='kiran.example at example dot com')), self.fx.JoinedCellModel(email=''))
        question = raised.exception.questions[0]
        self.assertEqual((question['id'], question['value']), ('email', 'kiran.example at example dot com'),
                         'the supplied text is offered for correction, never silently dropped')

    def test_a_phone_note_is_dropped_but_the_number_is_kept(self):
        from resume_bot.automation import source_profile
        model = self.fx.JoinedCellModel()
        original = model.generate
        model.generate = lambda *a, **k: {**original(*a, **k), 'phone': self.fx.quote('+91 90000 00000 (Mobile)')}
        profile = source_profile(self.source(self.rows(phone='+91 90000 00000 (Mobile)')), model)
        self.assertEqual(profile['phone'], '+91 90000 00000')

    def test_every_candidate_form_requires_a_valid_phone_and_accepts_no_email(self):
        full = {'full_name': 'Arun Kumar (fictional)', 'email': '', 'skills': 'React.js, TypeScript',
                'work_experience': [{'company': 'DemoTech Solutions (fictional)', 'position': 'Frontend Developer',
                                     'start': 'July 2023', 'end': 'Present', 'responsibilities': 'Built React forms.'}],
                'education': []}
        with self.assertRaisesRegex(ValueError, 'phone'):
            submission({'consent': CONSENT_VERSION, 'role': 'React.js Developer', 'candidate': full})
        with self.assertRaisesRegex(ValueError, 'email'):
            submission({'consent': CONSENT_VERSION, 'role': 'React.js Developer',
                        'candidate': {**full, 'phone': '+91 9000000000', 'email': 'not-an-email'}})
        accepted = submission({'consent': CONSENT_VERSION, 'role': 'React.js Developer',
                               'candidate': {**full, 'phone': '+91 9000000000'}})
        self.assertEqual(accepted['profile']['email'], '')


class ProposalToolTests(unittest.TestCase):
    def test_a_bullet_naming_a_tool_outside_its_stack_is_dropped(self):
        from resume_bot.generate import propose_projects, tools_outside
        self.assertEqual(tools_outside('Developed a multi-step onboarding flow with Formik.', ['React.js', 'TypeScript']),
                         ['Formik'])
        self.assertEqual(tools_outside('Built product filters in React.js and TypeScript.', ['React.js', 'TypeScript']), [])

        class Proposer:
            model = 'fake'

            def generate(self, prompt, schema=None, on_progress=None):
                bullets = ['Developed product search and filtering components in React.js with TypeScript types.',
                           'Managed product listing state with Redux Toolkit slices and selectors.',
                           'Wrote Jest unit tests for the search and filter components.']
                return {'projects': [
                    {'name': 'Product Catalogue', 'problem': 'Browse products.', 'stack': ['React.js', 'TypeScript',
                                                                                           'Redux Toolkit', 'Jest'],
                     'architecture': 'Single-page app.', 'features': ['Search'], 'responsibilities': 'UI.',
                     'bullets': bullets},
                    {'name': 'Notification Panel', 'problem': 'Show alerts.', 'stack': ['React.js', 'Jest'],
                     'architecture': 'Panel.', 'features': ['Alerts'], 'responsibilities': 'UI.',
                     'bullets': ['Set up real-time communication between client and server with WebSockets.',
                                 'Developed a notification panel component in React.js for new messages.',
                                 'Wrote Jest unit tests for the notification panel component.']}]}
        profile = validate_profile({'full_name': 'Arun Kumar', 'phone': '+91 9000000000',
                                    'skills': ['React.js', 'TypeScript', 'Redux Toolkit', 'Jest'],
                                    'experience': [], 'education': [{'institution': 'Example Institute', 'degree': 'B.Tech'}]})
        with patch('resume_bot.generate.generate', side_effect=lambda ai, prompt, **k: ai.generate(prompt)):
            kept = propose_projects(profile, {'role': 'react-developer'}, 'React.js Developer', 3, Proposer())
        self.assertEqual([p['name'] for p in kept], ['Product Catalogue'],
                         'the WebSockets bullet is dropped, leaving the second project under three bullets')


if __name__ == '__main__':
    unittest.main()
