"""Employer/date-only document regression: no invented previous job titles or redundant questions.

This fixture uses fabricated names and companies solely for tests; the production
candidate's uploaded evidence is never committed to the repository.
"""
import json
import re
import sys
import tempfile
from contextlib import closing
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZipFile

from resume_bot.automation import CONSENT_VERSION, NeedsInput, source_profile
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

    def test_numbered_company_name_and_employment_dates_headers(self):
        source = SOURCE.replace('Company / employer', '5. Company Name').replace(
            'Employment dates', '6. Employment Dates')
        data = raw_profile()
        data['experience'] = []
        repair(data, source)
        with patch('resume_bot.automation.extract_profile', return_value=data):
            result = source_profile(source, object(), evidence_documents=True)
        self.assertEqual(len(result['experience']), 4)
        self.assertEqual([(job['start'], job['end']) for job in result['experience']], [
            ('11 Jul 2022', '08 Apr 2025'), ('23 Aug 2021', '22 Apr 2022'),
            ('25 Nov 2019', '30 Jun 2021'), ('11 Nov 2015', '08 Aug 2019'),
        ])
        self.assertTrue(all(not job['position'] for job in result['experience']))

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

    def test_existing_eight_question_job_is_automatically_requeued(self):
        from resume_bot.automation import EXTRACTION_VERSION
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
        from automation_jobs import AutomationJobs
        from resume_dashboard import Workspace
        with tempfile.TemporaryDirectory() as folder:
            jobs = AutomationJobs(Workspace(Path(folder)))
            owner, identity = 'a' * 64, 'b' * 64
            checkpoint = {'source': SOURCE, 'extraction': 'facts-v4',
                          'draft': raw_profile(), 'questions': [
                              {'id': 'experience.0.position', 'field': 'position'}]}
            with closing(jobs.connection()) as db, db:
                db.execute("INSERT INTO automation (owner,id,payload,status,created,updated,checkpoints) "
                           "VALUES (?,?,?,'needs_input',1,1,?)",
                           (owner, identity, '{"role":"GCP Data Engineer"}',
                            json.dumps(checkpoint)))
            jobs._recheck_stale_questions()
            with closing(jobs.connection()) as db:
                row = db.execute('SELECT status,checkpoints FROM automation WHERE id=?', (identity,)).fetchone()
            self.assertNotEqual(EXTRACTION_VERSION, 'facts-v4')
            self.assertEqual(row['status'], 'queued')
            saved = json.loads(row['checkpoints'])
            self.assertEqual(saved['source'], SOURCE)
            self.assertNotIn('questions', saved)
            self.assertNotIn('draft', saved)
            self.assertNotIn('extraction', saved)

    def test_dates_only_document_gets_ai_drafts_skills_and_projects_then_a_complete_resume(self):
        """The regression this guards: an upload stating only employers and dates skipped AI drafting, so the
        resume went out with every job, the skills and the projects empty. Now the AI drafts the role's work, skills
        and projects, the candidate confirms them once, and every section of the resume has content."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
        from automation_jobs import AutomationJobs
        from resume_dashboard import Workspace

        data = raw_profile()
        repair(data, SOURCE)
        owner, identity = 'a' * 64, 'b' * 64
        work = {
            0: ['Provisioned GCP Compute Engine and Cloud Storage resources with Terraform modules for each environment.',
                'Deployed containerised services to Google Kubernetes Engine with Helm charts and rolling updates.',
                'Configured Cloud Monitoring alerts and dashboards for the production Kubernetes clusters.'],
            1: ['Wrote Terraform for VPC networks, firewall rules and Cloud NAT in a shared GCP project.',
                'Built Cloud Build pipelines that test and deploy Docker images to Cloud Run.'],
            2: ['Managed IAM roles and service accounts for GCP projects with least-privilege policies.',
                'Automated Linux server patching with Ansible playbooks and Bash scripts.'],
            3: ['Maintained Linux servers and Bash scripts for nightly backups to Cloud Storage.',
                'Monitored application logs in Cloud Logging and fixed recurring Linux service failures.']}
        proposals = [{'name': name, 'problem': 'A fictional practice problem.',
                      'stack': ['GCP', 'Terraform', 'Kubernetes'], 'architecture': 'Infrastructure as code.',
                      'features': ['Environments'], 'responsibilities': 'Build the platform.',
                      'bullets': [f'Provisioned the {name} GKE cluster and VPC network with Terraform modules.',
                                  f'Deployed the {name} services to Google Kubernetes Engine with Helm charts.',
                                  f'Configured Cloud Monitoring alerts for the {name} Kubernetes workloads.',
                                  f'Automated {name} releases with Cloud Build triggers and Docker images.',
                                  f'Managed {name} IAM service accounts with least-privilege Terraform policies.']}
                     for name in ('Practice Platform', 'Practice Landing Zone')]
        with tempfile.TemporaryDirectory() as folder:
            workspace = Workspace(Path(folder))
            jobs = AutomationJobs(workspace)
            jobs.start = lambda: None
            with closing(jobs.connection()) as db, db:
                db.execute("INSERT INTO automation (owner,id,payload,status,created,updated,checkpoints) "
                           "VALUES (?,?,?,'queued',1,1,?)",
                           (owner, identity, json.dumps({'role': 'GCP Cloud Engineer', 'extension': 'docx',
                                                        'consent': CONSENT_VERSION}),
                            json.dumps({'source': SOURCE})))

            def prepare():
                with closing(jobs.connection()) as db:
                    row = db.execute('SELECT * FROM automation').fetchone()
                with patch('resume_bot.automation.extract_profile', return_value=data), \
                        patch('resume_bot.automation.generate', return_value={}), \
                        patch('resume_bot.generate.generate', return_value={}), \
                        patch('resume_bot.generate.draft_role_content',
                              return_value=(work, ['GCP', 'Terraform', 'Kubernetes', 'Docker', 'Linux'])) as drafts, \
                        patch('resume_bot.generate.propose_projects', return_value=proposals):
                    jobs._prepare(row)
                return drafts

            drafts = prepare()
            self.assertEqual(drafts.call_args.args[4], [0, 1, 2, 3], 'every job known only by employer and dates')
            result = jobs.list(owner, identity)[0]
            self.assertEqual(result['status'], 'needs_input')
            self.assertEqual(list(workspace.private_inbox.glob('*.json')), [], 'nothing is built before the review')
            questions = result['questions']
            self.assertEqual([q['id'] for q in questions],
                             ['experience.0.bullets', 'experience.1.bullets', 'experience.2.bullets',
                              'experience.3.bullets', 'skills'])
            self.assertEqual(questions[0]['label'],
                             'Work at Example Health Services Pvt Ltd (11 Jul 2022 - 08 Apr 2025) (AI draft)')
            self.assertNotIn('position', json.dumps([q['field'] for q in questions]), 'no job title is asked')
            self.assertEqual([p['name'] for p in result['proposals']], ['Practice Platform', 'Practice Landing Zone'])
            self.assertIn('work lines from your employers and dates', result['error'])

            jobs.answer(owner, identity, {q['id']: q['value'] for q in questions}, [{'include': True}, {'include': False}])
            prepare()
            result = jobs.list(owner, identity)[0]
            queued = list(workspace.private_inbox.glob('*.json'))
            self.assertEqual(len(queued), 1, result)
            from resume_bot.automation import verified_automation
            from resume_bot.claim_audit import records_for
            packet = json.loads(queued[0].read_text(encoding='utf-8'))
            profile = packet['profile']
            [approval] = records_for(workspace.root / 'approvals', queued[0].name)
            self.assertTrue(verified_automation(profile, packet['target'], approval), profile['summary'])
            self.assertEqual(profile['headline'], 'GCP Cloud Engineer')
            self.assertEqual(len(profile['experience']), 4)
            self.assertTrue(all(not job['position'] for job in profile['experience']), 'no job title is invented')
            self.assertTrue(all(job['bullets'] for job in profile['experience']), 'every job describes the work')
            self.assertTrue(profile['skills'])
            self.assertEqual([p.split(' - ')[0] for p in profile['projects']], ['Practice Platform'])
            self.assertGreaterEqual(len(profile['summary'].split()), 18, profile['summary'])
            tex = render(profile, years=packet['target']['experience_years'], complete_sections=True)
            sections = re.findall(r'\\section\*{([^}]+)}', tex)
            self.assertEqual(sections, ['Career Objective', 'Technical Skills', 'Professional Work Experience',
                                        'Projects', 'Education'])
            for chunk in tex.split(r'\section*{')[1:]:
                self.assertTrue(chunk.split('}', 1)[1].strip(), 'an empty section is never printed')

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
