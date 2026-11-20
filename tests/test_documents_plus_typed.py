"""Uploaded documents and typed details go in one submission.

Before, the create form offered "Upload documents" or the typed form, never both: a skill, link, job or note the
documents missed could not be added. Now an optional details section sits under the upload field. Typed values are
the candidate's own statements (attested, as on the form-only path); a typed name, phone or email replaces the
documents' reading; a typed note is read with the documents. Nothing typed gives the same request as before. Every
candidate here is fictional."""
import base64
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import resume_bot.automation as automation
from resume_bot.automation import CONSENT_VERSION, NeedsInput, source_profile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from automation_jobs import AutomationJobs, submission  # noqa: E402
from resume_dashboard import Workspace  # noqa: E402

OWNER = 'e' * 64
RESUME = ('Neha Example (fictional)\n+91 90000 44444\nneha.example@example.com\n\nExperience\n'
          'Data Analyst, Example Insights Pvt Ltd (fictional), Jan 2022 - Present\n'
          'Built Power BI dashboards for weekly sales reviews.\n\nEducation\n'
          'B.Sc Statistics, Example College (fictional), 2021\n')
DOCUMENT = {'extension': 'txt', 'document': base64.b64encode(RESUME.encode()).decode()}
BLANK = {'full_name': '', 'phone': '', 'email': '', 'years_of_experience': None, 'location': '', 'current_company': '',
         'linkedin': '', 'github': '', 'portfolio': '', 'certifications': '', 'achievements': '', 'skills': '',
         'background': '', 'work_experience': [], 'education': [], 'projects': [], 'coding_platforms': []}


def upload(**typed):
    payload = {'consent': CONSENT_VERSION, 'role': 'Data Analyst', 'documents': [DOCUMENT]}
    if typed:
        payload['candidate'] = {**BLANK, **typed}
    return payload


class Submission(unittest.TestCase):
    def test_documents_alone_make_the_same_request_as_before(self):
        self.assertEqual(submission(upload()), {'consent': CONSENT_VERSION, 'role': 'Data Analyst',
                                                'documents': [DOCUMENT]})

    def test_typed_details_travel_with_the_documents(self):
        request = submission(upload(skills='SQL, Tableau', linkedin='https://linkedin.com/in/neha-example',
                                    certifications='Google Data Analytics Certificate', years_of_experience=3,
                                    background='Also built an attrition model in Python for HR.'))
        self.assertEqual(request['documents'], [DOCUMENT])
        self.assertEqual(request['attested']['skills'], ['SQL', 'Tableau'])
        self.assertEqual(request['attested']['linkedin'], 'https://linkedin.com/in/neha-example')
        self.assertEqual(request['attested']['certifications'], ['Google Data Analytics Certificate'])
        self.assertEqual(request['background'], 'Also built an attrition model in Python for HR.')
        self.assertEqual(request['years'], 3)
        self.assertNotIn('identity', request, 'a blank name or phone is left to the documents')
        for value in request['attested'].values():
            self.assertNotIn('Stand In', str(value))

    def test_a_typed_name_phone_or_email_replaces_the_documents(self):
        request = submission(upload(full_name='Neha R. Example', phone='+91 90000 55555'))
        self.assertEqual(request['identity'], {'full_name': 'Neha R. Example', 'phone': '+91 90000 55555'})

    def test_mistakes_in_typed_details_are_reported_on_submit(self):
        for typed in ({'phone': '12'}, {'email': 'not-an-email'}, {'skills': 'SQL, sql'}, {'background': 'x' * 7501}):
            with self.subTest(typed=typed), self.assertRaises(ValueError):
                submission(upload(**typed))

    def test_a_single_document_upload_still_takes_no_form(self):
        with self.assertRaises(ValueError):
            submission({'consent': CONSENT_VERSION, 'role': 'Data Analyst', 'candidate': BLANK, **DOCUMENT})


class Preparation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = AutomationJobs(Workspace(Path(self.temp.name), code_root=ROOT))

    def test_the_note_is_read_with_the_documents_and_typed_details_are_attested(self):
        payload = upload(full_name='Neha R. Example', skills='SQL', background='Also built an attrition model.')
        with patch.object(self.jobs, 'start'):
            identifier = self.jobs.enqueue(OWNER, payload)
        with closing(self.jobs.connection()) as db:
            row = db.execute('SELECT * FROM automation WHERE id=?', (identifier,)).fetchone()
        calls = []

        def extraction(source, *args, **kwargs):
            calls.append((source, kwargs))
            raise RuntimeError('stop after the extraction call')
        with patch('resume_bot.automation.source_profile', side_effect=extraction):
            self.jobs._prepare(row)
        source, options = calls[0]
        self.assertIn('Data Analyst, Example Insights Pvt Ltd (fictional)', source)
        self.assertTrue(source.rstrip().endswith('Candidate-provided facts\nAlso built an attrition model.'))
        self.assertTrue(options['evidence_documents'])
        self.assertEqual(options['identity'], {'full_name': 'Neha R. Example'})
        self.assertEqual(options['attested']['skills'], ['SQL'])


class TypedEntriesBesideDocuments(unittest.TestCase):
    def test_documents_without_work_or_education_are_completed_by_typed_entries(self):
        source = 'Candidate evidence document 1\nNeha Example\n+91 90000 44444\nCertificate: Google Data Analytics'
        extracted = {'full_name': 'Neha Example', 'phone': '+91 90000 44444', 'email': '', 'headline': '',
                     'location': '', 'linkedin': '', 'github': '', 'portfolio': '', 'current_company': '',
                     'summary': '', 'skills': [], 'experience': [], 'education': [], 'certifications': [],
                     'projects': []}
        typed = {'education': [{'institution': 'Example College', 'degree': 'B.Sc Statistics', 'end': '2021',
                                'details': ''}]}
        with patch('resume_bot.automation.extract_profile', return_value=extracted):
            with self.assertRaises(NeedsInput):
                source_profile(source, object(), evidence_documents=True)  # alone: asks for more facts
            profile = source_profile(source, object(), evidence_documents=True, attested=typed)
        self.assertEqual([e['institution'] for e in profile['education']], ['Example College'])
        self.assertIs(automation.source_profile, source_profile)


if __name__ == '__main__':
    unittest.main()
