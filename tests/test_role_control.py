"""The selected role controls the resume; job titles are never skills; target-role work is never presented as
employment without the candidate's own evidence. Every candidate here is fictional.

Covers the production defect where a GCP Data Engineer resume listed "Data Analyst" as its only skill (and in its
summary) and showed AI-drafted Pub/Sub and Dataflow work under Data Analyst and Accounts Receivable jobs, and the
React.js-to-Java Backend transformation."""
import copy
import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from resume_bot.automation import CONSENT_VERSION, verified_automation, verified_request
from resume_bot.generate import (composed_summary, content_problems, draft_role_content, fits_role, is_job_title,
                                 lacks_role_core, present_for_role, same_field, without_note)
from resume_bot.profile import validate_profile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from automation_jobs import AutomationJobs  # noqa: E402
from resume_dashboard import Workspace  # noqa: E402

OWNER = 'd' * 64


def role(title):
    from resume_bot.ai_review import role_intelligence
    intelligence = role_intelligence(title)
    return {'role': intelligence['role'], 'keywords': intelligence['terms']}


def analyst():
    """Shaped like the production case: an analyst moving into GCP data engineering, every work line an AI draft."""
    return validate_profile({
        'full_name': 'Kavya Example (fictional)', 'phone': '+91 90000 00031', 'email': '',
        'skills': ['Data Analyst'],
        'experience': [
            {'company': 'Example Health Analytics', 'position': 'Data Analyst - Finance & Supply Chain Analytics',
             'start': 'Jul 2022', 'end': 'Apr 2025', 'location': '',
             'bullets': ['Integrated Pub/Sub with Cloud Storage for data ingestion.',
                         'Configured BigQuery datasets for financial analytics.']},
            {'company': 'Sample Receivables Pvt Ltd', 'position': 'Senior Executive AR (Accounts Receivable)',
             'start': 'Nov 2015', 'end': 'Aug 2019', 'location': '',
             'bullets': ['Integrated Pub/Sub with Dataflow for streaming data ingestion.',
                         'Configured BigQuery datasets for efficient data storage and querying.',
                         'Reconciled customer payments against open invoices every month.']}],
        'education': [{'institution': 'Example Institute of Technology', 'degree': 'B.Tech (branch not specified in source)',
                       'end': '2015'}],
        'projects': ['Event Streaming Pipeline - Tech stack: Python, BigQuery, Dataflow, Pub/Sub | '
                     'Configured Pub/Sub topics for event streaming']})


def react_developer():
    return validate_profile({
        'full_name': 'Arjun Example (fictional)', 'phone': '+91 90000 00032', 'email': '',
        'skills': ['React.js', 'Redux', 'TypeScript', 'CSS', 'REST APIs', 'Git', 'SQL'],
        'experience': [{'company': 'Example Web Studio', 'position': 'React.js UI Developer', 'start': 'Jan 2021',
                        'end': 'Present', 'location': '',
                        'bullets': ['Built React.js components with Redux for the checkout flow.',
                                    'Styled responsive layouts with CSS.',
                                    'Integrated REST APIs with the backend team.',
                                    'Reviewed pull requests and mentored two interns.']}],
        'education': [{'institution': 'Example Institute of Technology', 'degree': 'B.E. Computer Science', 'end': '2020'}],
        'projects': ['Recipe Finder - Tech stack: React.js, Redux | Built a recipe search UI with React.js']})


class JobTitlesAreNotSkillsTests(unittest.TestCase):
    def test_designations_are_recognised(self):
        for text, title in (('Data Analyst', True), ('Senior Executive AR', True), ('Scrum Master', True),
                            ('SQL', False), ('BigQuery', False), ('Business Analysis', False), ('Power BI', False),
                            ('Data Engineering', False), ('Python', False)):
            with self.subTest(text=text):
                self.assertEqual(is_job_title(text), title)
        self.assertTrue(is_job_title('Finance Lead Reporting', ['Finance Lead Reporting']), 'a held designation')

    def test_extraction_notes_are_not_resume_text(self):
        self.assertEqual(without_note('B.Tech (branch not specified in source)'), 'B.Tech')
        self.assertEqual(without_note('B.Sc (Physics)'), 'B.Sc (Physics)')

    def test_a_resume_listing_a_job_title_as_a_skill_is_never_verified(self):
        profile = {**analyst(), 'summary': 'Data Analyst with 9 years of experience. Transferable, hands-on skills '
                   'include Data Analyst.', 'headline': 'GCP Data Engineer'}
        problems = content_problems(profile, 'GCP Data Engineer')
        self.assertTrue(any('job titles listed as skills' in p for p in problems))
        self.assertTrue(any('summary lists job titles' in p for p in problems))
        self.assertTrue(any('extraction note' in p for p in problems))


class SelectedRoleControlsTheResumeTests(unittest.TestCase):
    def test_gcp_data_engineer_resume_for_an_analyst(self):
        target = role('GCP Data Engineer')
        self.assertFalse(same_field('Data Analyst - Finance & Supply Chain Analytics', 'GCP Data Engineer'))
        self.assertFalse(same_field('Senior Executive AR (Accounts Receivable)', 'GCP Data Engineer'))
        shown, left = present_for_role(analyst(), target, 'GCP Data Engineer', origin_bullets=None)
        self.assertEqual(shown['skills'], [], '"Data Analyst" is not a skill')
        jobs = shown['experience']
        self.assertEqual([j['position'] for j in jobs], [j['position'] for j in analyst()['experience']],
                         'every employer and designation stays')
        self.assertEqual(jobs[0]['bullets'], [], 'drafted GCP work is not presented as Data Analyst employment')
        self.assertEqual(jobs[1]['bullets'], ['Reconciled customer payments against open invoices every month.'])
        self.assertEqual(shown['education'][0]['degree'], 'B.Tech')
        self.assertIn('Event Streaming Pipeline', shown['projects'][0], 'GCP work belongs in projects')
        self.assertTrue(any('job title' in item for item in left))
        self.assertTrue(lacks_role_core(shown['skills'], target), 'so GCP Data Engineer skills are suggested')
        summary = composed_summary(shown, 'GCP Data Engineer', 9, target['keywords'])
        self.assertNotIn('skills include Data Analyst', summary)
        self.assertIn('moving into a GCP Data Engineer role', summary)
        self.assertEqual(content_problems({**shown, 'summary': summary}, 'GCP Data Engineer'), [])

    def test_the_candidates_own_target_role_lines_stay_in_an_other_field_job(self):
        own = 'Configured BigQuery datasets for financial analytics.'
        shown, _ = present_for_role(analyst(), role('GCP Data Engineer'), 'GCP Data Engineer', origin_bullets=[own])
        self.assertEqual(shown['experience'][0]['bullets'], [own])

    def test_react_developer_applying_for_java_backend(self):
        target = role('Java Backend Developer')
        self.assertFalse(same_field('React.js UI Developer', 'Java Backend Developer'))
        facts = react_developer()
        origin = [line for job in facts['experience'] for line in job['bullets']]
        shown, left = present_for_role(facts, target, 'Java Backend Developer', origin)
        self.assertEqual(sorted(shown['skills']), ['Git', 'REST APIs', 'SQL'])
        # React-only lines are left out only while the job keeps three lines: the first one is kept (in order).
        self.assertEqual(shown['experience'][0]['bullets'], ['Built React.js components with Redux for the checkout flow.',
                                                            'Integrated REST APIs with the backend team.',
                                                            'Reviewed pull requests and mentored two interns.'])
        self.assertNotIn('projects', shown, 'a React project is not a Java Backend project')
        self.assertEqual(shown['experience'][0]['position'], 'React.js UI Developer', 'the designation stays')
        self.assertTrue(lacks_role_core(shown['skills'], target), 'Java and Spring Boot are suggested for confirmation')
        summary = composed_summary(shown, 'Java Backend Developer', 5, target['keywords'])
        self.assertNotIn('Java Backend Developer with', summary, 'no professional Java experience is claimed')
        self.assertTrue(summary.startswith('React.js UI Developer with 5 years'), 'the real designation leads')
        self.assertNotIn('React', summary.split('moving into', 1)[1], 'no React in the role-focused part')
        self.assertEqual(facts['skills'][0], 'React.js', 'the stored facts stay complete')

    def test_an_application_keeps_all_of_the_candidates_own_facts(self):
        # keep_own: a candidate's application, typed for this one role. Seen live: a Full Stack Developer applicant's
        # only project (React.js with Java and Spring Boot) was left off as "not about" the role, and HTML, CSS, Java
        # and Spring Boot with it from their skills, because the role's catalog is the JavaScript stack.
        facts = validate_profile({
            'full_name': 'Kavya Example (fictional)', 'phone': '+91 90000 00034', 'email': '',
            'skills': ['HTML', 'CSS', 'JavaScript', 'React.js', 'Java', 'Spring Boot', 'MySQL', 'Git'],
            'experience': [{'company': 'Example Web Studio', 'position': 'Software Developer', 'start': 'Jan 2022',
                            'end': 'Present', 'location': '',
                            'bullets': ['Built Spring Boot REST APIs in Java for the order service.',
                                        'Built React.js pages with HTML and CSS for the order screens.']}],
            'education': [{'institution': 'Example Institute of Technology', 'degree': 'B.Tech', 'end': '2021'}],
            'projects': ['Order Tracker - Tech stack: React.js, Java, Spring Boot, HTML, CSS | Built order tracking '
                         'screens in React.js on a Spring Boot API.']})
        target = role('Full Stack Developer')
        origin = [line for job in facts['experience'] for line in job['bullets']]
        by_role, _ = present_for_role(facts, target, 'Full Stack Developer', origin)
        # The Full Stack Developer catalog now spans the web basics and Java/Spring Boot as well (requested), so even
        # a resume built by role alone keeps this project and these skills.
        self.assertEqual(by_role['projects'], facts['projects'])
        for skill in ('HTML', 'CSS', 'Java', 'Spring Boot'):
            self.assertIn(skill, by_role['skills'])
        shown, left = present_for_role(facts, target, 'Full Stack Developer', origin, keep_own=True)
        self.assertEqual(shown['projects'], facts['projects'])
        self.assertEqual(shown['skills'], facts['skills'])
        self.assertEqual(shown['experience'][0]['bullets'], origin)
        self.assertEqual(left, [])
        self.assertEqual(facts['skills'][0], 'HTML', 'the stored facts are unchanged')

    def test_every_catalog_role_shows_only_its_own_stack(self):
        mixed = validate_profile({
            'full_name': 'Mixed Example (fictional)', 'phone': '+91 90000 00033', 'email': '',
            'skills': ['React.js', 'Java', 'Spring Boot', 'Python', 'Django', 'BigQuery', 'Dataflow', 'Terraform',
                       'Kubernetes', 'Docker', 'Git'],
            'experience': [{'company': 'Example Labs', 'position': 'Software Engineer', 'start': 'Jan 2020',
                            'end': 'Present', 'location': '', 'bullets': []}],
            'education': [{'institution': 'Example Institute of Technology', 'degree': 'B.Tech', 'end': '2019'}]})
        expected = {'React.js Developer': {'React.js', 'Git'},
                    'Java Backend Developer': {'Java', 'Spring Boot', 'Git', 'Docker'},
                    'Python Backend Developer': {'Python', 'Django', 'Git'},
                    'GCP Data Engineer': {'Python', 'BigQuery', 'Dataflow', 'Git'},
                    'DevOps Engineer': {'Python', 'Terraform', 'Kubernetes', 'Docker', 'Git'}}
        for title, wanted in expected.items():
            with self.subTest(role=title):
                target = role(title)
                shown, _ = present_for_role(mixed, target, title, [])
                self.assertTrue(wanted <= set(shown['skills']), shown['skills'])
                self.assertTrue(all(fits_role(skill, target) for skill in shown['skills']))
        self.assertNotIn('React.js', present_for_role(mixed, role('GCP Data Engineer'), 'GCP Data Engineer', [])[0]['skills'])
        self.assertNotIn('BigQuery', present_for_role(mixed, role('React.js Developer'), 'React.js Developer', [])[0]['skills'])

    def test_role_skills_are_asked_for_only_when_the_roles_own_stack_is_missing(self):
        # Production acceptance caught an earlier rule asking a React.js developer with React.js and Redux for skills.
        for title, skills, asked in (
                ('React.js Developer', ['React.js', 'Redux', 'TypeScript', 'REST APIs'], False),
                ('Python Backend Developer', ['Python', 'SQL', 'Django', 'Kubernetes'], False),
                ('DevOps Engineer', ['Python', 'Kubernetes'], False),
                ('GCP Data Engineer', ['Python', 'SQL'], True),
                ('GCP Data Engineer', [], True),
                ('Java Backend Developer', ['REST APIs', 'Git', 'SQL'], True),
                ('Angular Developer', ['TypeScript'], True)):
            with self.subTest(role=title, skills=skills):
                self.assertEqual(lacks_role_core(skills, role(title)), asked)

    def test_a_custom_role_with_few_terms_does_not_drop_the_candidates_skills(self):
        custom = {'role': 'custom', 'keywords': ['Python']}
        shown, _ = present_for_role(react_developer(), custom, 'Backend Engineer', [])
        self.assertEqual(shown['skills'], react_developer()['skills'])

    def test_drafts_for_an_other_field_job_describe_that_job_not_the_target_role(self):
        facts = {**analyst(), 'skills': []}
        facts['experience'] = [{**job, 'bullets': []} for job in facts['experience']]
        draft = {'roles': [{'index': 1, 'bullets': ['Integrated Pub/Sub with Dataflow for streaming data ingestion.',
                                                    'Reconciled customer payments against open invoices each month.',
                                                    'Prepared aging reports for overdue customer accounts and followed up.',
                                                    'Coordinated with the sales team to resolve billing disputes on invoices.']}],
                 'skills': ['Python', 'SQL', 'BigQuery', 'Dataflow', 'Pub/Sub', 'Cloud Storage', 'Data Analyst']}
        with patch('resume_bot.generate.generate', lambda ai, prompt, **_: draft):
            work, skills = draft_role_content(facts, role('GCP Data Engineer'), 'GCP Data Engineer', 9, [1], True, object())
        self.assertEqual(work[1], ['Reconciled customer payments against open invoices each month.',
                                   'Prepared aging reports for overdue customer accounts and followed up.',
                                   'Coordinated with the sales team to resolve billing disputes on invoices.'])
        self.assertEqual(skills, ['Python', 'SQL', 'BigQuery', 'Dataflow', 'Pub/Sub', 'Cloud Storage'])


class RegenerationTests(unittest.TestCase):
    """Regenerating an earlier resume for the selected role: no job-title skills, drafted target-role work is not
    presented as employment, role skills are suggested once, and the builder's check accepts the result."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(Path(self.temp.name), code_root=ROOT)
        self.jobs = AutomationJobs(self.workspace)
        self.jobs.start = lambda: None
        facts = analyst()
        notices = [f'Reviewed and confirmed by you (AI draft): Work as {job["position"]} at {job["company"]}.'
                   for job in facts['experience']]
        with closing(self.jobs.connection()) as db, db:  # a resume finished before origins were recorded
            db.execute("INSERT INTO automation(owner,id,status,created,updated,role,candidate,facts,notice) "
                       "VALUES (?,?,'handed_off',1,1,'GCP Data Engineer',?,?,?)",
                       (OWNER, 'e' * 64, facts['full_name'], json.dumps(facts), json.dumps(notices)))

    def row(self, ident):
        with closing(self.jobs.connection()) as db:
            return db.execute('SELECT * FROM automation WHERE owner=? AND id=?', (OWNER, ident)).fetchone()

    def prepare(self, ident):
        suggestions = {'roles': [], 'skills': ['Python', 'SQL', 'BigQuery', 'Dataflow', 'Pub/Sub', 'Cloud Storage']}

        def model(ai, prompt, **_):
            if prompt.startswith('Draft the professional experience'):
                return suggestions
            raise RuntimeError('summary falls back to the composed one')
        with patch('resume_bot.generate.generate', model), patch('resume_bot.automation.generate', return_value={}), \
                patch.object(self.workspace, 'enqueue', return_value={'name': f'private-{OWNER}-{"2" * 64}.json'}) as enqueue:
            self.jobs._prepare(self.row(ident))
        return enqueue

    def test_regenerated_gcp_data_engineer_resume(self):
        new = self.jobs.regenerate(OWNER, 'e' * 64, 'GCP Data Engineer')
        self.assertEqual(json.loads(self.row(new)['origin']), {'bullets': []}, 'all lines were AI drafts')
        self.prepare(new)
        job = self.jobs.list(OWNER, new)[0]
        self.assertEqual(job['status'], 'needs_input')
        self.assertEqual([q['id'] for q in job['questions']], ['skills'], 'one question: the role skills')
        # No AI suggestion is pre-filled: the box holds the candidate's own role skills (none here) and the
        # suggestions start unticked beside it.
        self.assertEqual(job['questions'][0]['value'], '')
        self.assertEqual(job['questions'][0]['suggestions'],
                         ['Python', 'SQL', 'BigQuery', 'Dataflow', 'Pub/Sub', 'Cloud Storage'])
        self.assertNotIn('Data Analyst', job['questions'][0]['suggestions'])
        self.assertEqual(job['error'], 'AI drafted skill suggestions for GCP Data Engineer. Keep only what is true for '
                                       'you and edit anything. Then confirm once.', 'only what was drafted is named')
        self.jobs.answer(OWNER, new, {'skills': 'Python, SQL, BigQuery'})
        self.assertIn('Reviewed and confirmed by you (AI draft): Skills for GCP Data Engineer.',
                      json.loads(self.row(new)['notice']))
        enqueue = self.prepare(new)
        self.assertEqual(self.row(new)['status'], 'handed_off', self.row(new)['error'])
        packet, approval = json.loads(enqueue.call_args.args[0]), enqueue.call_args.kwargs['approval']
        profile = packet['profile']
        # The confirmed skills, plus the role tools the resume's own project uses (its stack names Dataflow and
        # Pub/Sub): Technical Skills and Projects tell one story.
        self.assertEqual(profile['skills'], ['Python', 'SQL', 'BigQuery', 'Dataflow', 'Pub/Sub'])
        self.assertEqual(profile['headline'], 'GCP Data Engineer')
        self.assertEqual(profile['experience'][0]['bullets'], [])
        self.assertEqual(profile['experience'][1]['bullets'],
                         ['Reconciled customer payments against open invoices every month.'])
        self.assertNotIn('Data Analyst', profile['summary'].split('moving into')[1] if 'moving into' in profile['summary']
                         else profile['summary'].split('.', 1)[1])
        self.assertEqual(profile['education'][0]['degree'], 'B.Tech')
        self.assertTrue(verified_automation(profile, packet['target'], approval), "the builder's own check passes")
        stored = json.loads(self.row(new)['facts'])
        self.assertEqual(len(stored['experience'][0]['bullets']), 2, 'stored facts stay complete for other roles')
        self.assertIn('Not shown on this GCP Data Engineer resume', ' '.join(json.loads(self.row(new)['notice'])))

    def test_the_builder_refuses_a_resume_with_a_job_title_as_a_skill(self):
        facts = {**react_developer(), 'skills': ['React.js', 'Data Analyst']}
        target = role('React.js Developer')
        profile = {**copy.deepcopy(facts), 'headline': 'React.js Developer',
                   'summary': composed_summary(facts, 'React.js Developer', 5, target['keywords'])}
        intelligence = {'role': target['role'], 'terms': target['keywords'], 'label': 'React.js Developer'}
        packet, claims = verified_request(profile, 'React.js Developer', 5, intelligence)
        packet['target'].update(automation_owner=OWNER, automation_id='f' * 64)
        approval = {'operation': 'f' * 64, 'approved_by': OWNER,
                    'automation': {'v': 3, 'consent': CONSENT_VERSION, 'submission_sha256': 'f' * 64, 'facts': facts}}
        self.assertFalse(verified_automation(packet['profile'], packet['target'], approval))


if __name__ == '__main__':
    unittest.main()
