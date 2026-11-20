"""Employer/date-only document regression: no invented previous job titles or redundant questions.

This fixture uses fabricated names and companies solely for tests; the production
candidate's uploaded evidence is never committed to the repository.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZipFile

from resume_bot.automation import NeedsInput, source_profile
from resume_bot.ingest import read_resume_text, repair
from resume_bot.latex import render


SOURCE = """CANDIDATE INFORMATION FORM
Existing candidate details from the supplied resume
PERSONAL DETAILS
Full Name
Sample Candidate
Phone Number
+91 9000000000
Email Address
sample.candidate@example.com
Total employment tenure
Approximately 8 years 9 months across four employment periods.
EMPLOYMENT HISTORY
Company / employer
Employment dates
Example Health Services Pvt Ltd
11 Jul 2022 – 08 Apr 2025
Demo Software Pvt Ltd
23 Aug 2021 – 22 Apr 2022
Example Navigation Services Pvt Ltd
25 Nov 2019 – 30 Jun 2021
Demo Healthcare Pvt Ltd
11 Nov 2015 – 08 Aug 2019
EDUCATION
Highest Qualification
B.Tech (branch not specified in source)
College / University
Example College, Bengaluru
Education Passing Year
2015
Source: candidate-provided. Historical job designations have not been inserted.
"""


def raw_profile():
    return {
        'full_name': 'Sample Candidate', 'headline': '', 'phone': '+91 9000000000',
        'email': 'sample.candidate@example.com', 'location': '', 'linkedin': '',
        'github': '', 'portfolio': '', 'current_company': '', 'summary': '',
        'skills': [], 'certifications': [], 'projects': [],
        'experience': [{'company': 'Example Health Services Pvt Ltd', 'position': '',
                        'start': '', 'end': '', 'location': '', 'bullets': []}],
        'education': [{'institution': '', 'degree': '', 'start': '', 'end': '', 'details': ''}],
    }


class EmployerDateOnlyTests(unittest.TestCase):
    def test_realistic_word_table_restores_all_four_jobs_and_dates_without_inventing_roles(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'candidate.docx'
            para = lambda text: ('<w:p><w:r><w:t xml:space="preserve">' +
                                 escape(text) + '</w:t></w:r></w:p>')
            with ZipFile(path, 'w') as z:
                z.writestr('word/document.xml',
                           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                           '<w:body>' + ''.join(para(line) for line in SOURCE.splitlines())
                           + '</w:body></w:document>')
            extracted = read_resume_text(path)
        data = raw_profile()
        repair(data, extracted)
        self.assertEqual(len(data['experience']), 4)
        self.assertEqual([e['start'] for e in data['experience']],
                         ['11 Jul 2022', '23 Aug 2021', '25 Nov 2019', '11 Nov 2015'])
        self.assertEqual([e['end'] for e in data['experience']],
                         ['08 Apr 2025', '22 Apr 2022', '30 Jun 2021', '08 Aug 2019'])
        self.assertTrue(all(e['position'] == '' for e in data['experience']))
        self.assertEqual(data['education'][0]['institution'], 'Example College, Bengaluru')

    def test_document_asks_no_job_title_or_start_date_questions_when_dates_are_present(self):
        data = raw_profile()
        repair(data, SOURCE)
        with patch('resume_bot.automation.extract_profile', return_value=data):
            result = source_profile(SOURCE, object(), evidence_documents=True)
        self.assertEqual(len(result['experience']), 4)
        self.assertTrue(all(not e['position'] and e['start'] and e['end'] for e in result['experience']))
        self.assertEqual(result['full_name'], 'Sample Candidate')
        self.assertEqual(result['phone'], '+91 9000000000')
        self.assertEqual(result['education'][0]['degree'], 'B.Tech')
        tex = render(result, years=9, complete_sections=True)
        self.assertNotIn(r'\textbf{}', tex)
        for job in result['experience']:
            self.assertIn(job['company'], tex)

    def test_unrelated_resume_without_employer_table_is_unchanged(self):
        data = raw_profile()
        before = [dict(item) for item in data['experience']]
        repair(data, 'Sample Candidate\nExperience\nExample Health Services Pvt Ltd, Jan 2022')
        self.assertEqual(data['experience'], before)

    def test_truly_missing_company_still_requires_evidence(self):
        source = SOURCE.replace('Demo Software Pvt Ltd', '')
        data = raw_profile()
        data['experience'].append({'company': '', 'position': '', 'start': '23 Aug 2021',
                                   'end': '22 Apr 2022', 'location': '', 'bullets': []})
        with patch('resume_bot.automation.extract_profile', return_value=data):
            with self.assertRaises(NeedsInput) as caught:
                source_profile(source, object(), evidence_documents=True)
        self.assertIn('experience.1.company', [q['id'] for q in caught.exception.questions])


if __name__ == '__main__':
    unittest.main()
