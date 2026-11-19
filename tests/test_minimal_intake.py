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


if __name__ == '__main__':
    unittest.main()
