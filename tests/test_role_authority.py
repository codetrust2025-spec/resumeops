"""The selected role controls the whole resume: summary, skills, experience and projects are written for it, other
fields' technologies never mix in, and the summary, skills, work lines, projects and ATS analysis agree.

Acceptance cases, with fictional candidates and a faked model:
* a React.js Frontend Developer resume, GCP Data Engineer selected: a GCP-focused resume;
* a GCP Data Engineer resume, Java Backend Developer selected: a Java Backend-focused resume.
Designations, employers, dates and education are facts and always stay; the candidate's own lines about another
field's work stay in the stored facts (for other roles) but are never printed on this role's resume."""
import re
import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from resume_bot.automation import CONSENT_VERSION, verified_automation
from resume_bot.generate import (composed_summary, draft_role_content, other_field_tools, named_tools, proposal_problems,
                                 skill_fits_role, summary_problems, work_skills)
from resume_bot.workflow import analyze

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from automation_jobs import AutomationJobs  # noqa: E402
from resume_dashboard import Workspace  # noqa: E402

OWNER = 'b' * 64
EDUCATION = [{'degree': 'B.Tech - Information Technology', 'university': 'Example Institute of Technology (fictional)',
              'graduation_year': '2022'}]


def role(title):
    from resume_bot.ai_review import role_intelligence
    intelligence = role_intelligence(title)
    return {'role': intelligence['role'], 'keywords': intelligence['terms']}


GCP_DRAFTS = [
    'Built streaming pipelines with Apache Beam on Dataflow that read Pub/Sub events and load cleaned rows into BigQuery.',
    'Scheduled Cloud Composer DAGs that rebuild partitioned BigQuery reporting tables after each nightly batch load.',
    'Wrote Python data quality checks that compare row counts between Cloud Storage files and BigQuery tables.',
    'Modelled BigQuery tables with date partitioning and clustering so reporting queries scan only the requested days.',
    'Processed archived Cloud Storage logs with Apache Spark on Dataproc to backfill historical event tables.',
    'Built BigQuery views behind the React.js dashboards for the operations team.',  # names React.js: rejected
]
JAVA_DRAFTS = [
    'Built Spring Boot REST APIs for order and payment workflows with request validation and consistent error responses.',
    'Mapped order tables to entities with Spring Data JPA and Hibernate, keeping queries in tested repository methods.',
    'Secured the REST APIs with Spring Security filters and method rules for role-based access to admin endpoints.',
    'Wrote JUnit and Mockito tests for service classes and controllers that run on every Maven build.',
    'Published order events to Kafka from Spring Boot services so the inventory service updates stock asynchronously.',
    'Built Dataflow pipelines in Java that load events into BigQuery.',  # another field's tools: rejected
]


class Model:
    """Stands in for Ollama: drafts and projects for the selected role, and nothing for free prose."""

    def __init__(self, drafts, skills, projects):
        self.drafts, self.skills, self.projects, self.prompts = drafts, skills, projects, []

    def __call__(self, ai, prompt, **_):
        self.prompts.append(prompt)
        if prompt.startswith('Draft the professional experience'):
            return {'roles': [{'index': 0, 'bullets': self.drafts}], 'skills': self.skills}
        if 'portfolio projects' in prompt:
            return {'projects': [{'problem': 'Fictional practice data.', 'architecture': 'Pipeline.', 'features': ['A'],
                                  'responsibilities': 'All of it.', **project} for project in self.projects]}
        raise RuntimeError('summary falls back to the composed one')


GCP_MODEL = lambda: Model(GCP_DRAFTS, ['Python', 'SQL', 'BigQuery', 'Dataflow', 'Pub/Sub'], [  # noqa: E731
    {'name': 'Clickstream Event Pipeline', 'stack': ['Pub/Sub', 'Dataflow', 'BigQuery', 'Python'], 'bullets': [
        'Published simulated clickstream events to Pub/Sub topics with a Python generator for repeatable test loads.',
        'Built a Dataflow streaming job that parses the events, drops malformed rows and writes the valid rows to BigQuery.',
        'Partitioned the BigQuery event table by day so the daily summary queries scan only one partition.']},
    {'name': 'Sales Warehouse Loader', 'stack': ['Cloud Composer', 'BigQuery', 'Cloud Storage'], 'bullets': [
        'Scheduled Cloud Composer DAGs that load daily sales files from Cloud Storage into BigQuery staging tables.',
        'Merged the staging rows into BigQuery fact tables with SQL so reruns never create duplicate sales rows.',
        'Added Cloud Composer alerts for failed loads so the next run can retry the missing sales files.']}])
JAVA_MODEL = lambda: Model(JAVA_DRAFTS, ['Java', 'Spring Boot', 'Spring Data JPA', 'JUnit'], [  # noqa: E731
    {'name': 'Order Management Service', 'stack': ['Java', 'Spring Boot', 'Spring Data JPA', 'MySQL'], 'bullets': [
        'Exposed Spring Boot REST endpoints to create, update and cancel orders with validated request bodies.',
        'Stored orders and order lines in MySQL through Spring Data JPA repositories with paging for order history.',
        'Handled duplicate order requests in Spring Boot with an idempotency key stored beside each order in MySQL.']},
    {'name': 'Inventory Event Service', 'stack': ['Java', 'Spring Boot', 'Kafka', 'Redis'], 'bullets': [
        'Consumed order events from Kafka in a Spring Boot listener that reserves stock for each order line.',
        'Cached stock levels in Redis so the order API reads availability without a database query each time.',
        'Retried failed Kafka messages with a dead letter topic so a bad event never blocks the stock updates.']}])


class AcceptanceCases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(Path(self.temp.name), code_root=ROOT)
        self.jobs = AutomationJobs(self.workspace)
        self.jobs.start = lambda: None

    def row(self, ident):
        with closing(self.jobs.connection()) as db:
            return db.execute('SELECT * FROM automation WHERE owner=? AND id=?', (OWNER, ident)).fetchone()

    def prepare(self, ident, model):
        with patch('resume_bot.generate.generate', model), patch('resume_bot.automation.generate', return_value={}), \
                patch.object(self.workspace, 'enqueue', return_value={'name': f'private-{OWNER}-{"4" * 64}.json'}) as enqueue:
            self.jobs._prepare(self.row(ident))
        return enqueue

    def confirm(self, ident, model):
        """Prepare, then confirm the review card exactly as drafted (the first suggested project included)."""
        self.prepare(ident, model)
        job = self.jobs.list(OWNER, ident)[0]
        self.assertEqual(job['status'], 'needs_input', job.get('error'))
        self.jobs.answer(OWNER, ident, {q['id']: q['value'] for q in job['questions']},
                         [{'include': index == 0} for index, _ in enumerate(job.get('proposals') or [])] or None)
        enqueue = self.prepare(ident, model)
        self.assertEqual(self.row(ident)['status'], 'handed_off', self.row(ident)['error'])
        packet, approval = json.loads(enqueue.call_args.args[0]), enqueue.call_args.kwargs['approval']
        self.assertTrue(verified_automation(packet['profile'], packet['target'], approval), "the builder's check passes")
        return job, packet

    def printed(self, profile):
        """The role narrative: everything the resume says except the factual job titles, employers and education."""
        jobs = [{k: v for k, v in job.items() if k == 'bullets'} for job in profile['experience']]
        return json.dumps([profile['summary'], profile['skills'], jobs, profile.get('projects', [])])

    def consistent(self, profile, target):
        """Every role technology in the work lines and projects is a listed skill; the ATS analysis agrees."""
        self.assertEqual(work_skills(profile, target), [])
        report = analyze(profile, {**target, 'experience_years': 3, 'template': 'standard'})
        experience = next(c for c in report['categories'] if c['key'] == 'experience')
        self.assertEqual(experience['points'], experience['max'], experience)
        self.assertGreaterEqual(report['ats_readiness'], 90)

    def test_react_input_with_gcp_data_engineer_selected_gives_a_gcp_resume(self):
        candidate = {'full_name': 'Arun Kumar (fictional)', 'phone': '+91 9000000000', 'email': 'arun.kumar@example.com',
                     'skills': 'React.js, JavaScript (ES6+), TypeScript, HTML5, CSS3, Redux Toolkit, React Router, '
                               'REST APIs, Material UI, Jest, Git, Python, SQL, BigQuery, Dataflow, Pub/Sub, Cloud Storage, Cloud Composer, Apache Beam, Apache Spark, Dataproc',
                     'work_experience': [{'company': 'DemoTech Solutions Pvt Ltd (fictional)', 'position': 'Frontend Developer',
                                          'start': 'July 2023', 'end': 'Present',
                                          'responsibilities': 'Developed responsive web pages using React.js.'}],
                     'education': EDUCATION}
        ident = self.jobs.enqueue(OWNER, {'consent': CONSENT_VERSION, 'role': 'GCP Data Engineer', 'candidate': candidate})
        job, packet = self.confirm(ident, GCP_MODEL())
        work = next(q for q in job['questions'] if q['field'] == 'bullets')
        self.assertEqual(work['value'].split('\n'), GCP_DRAFTS[:5], 'the React.js line is not on the GCP review card')
        self.assertIn('kept for other roles', work['explanation'])
        self.assertTrue(job['proposals'][0]['name'].startswith('Clickstream'))

        profile = packet['profile']
        from resume_bot.generate import title_problems
        self.assertEqual(title_problems(profile['headline'], 'GCP Data Engineer', profile['skills']), [])
        self.assertTrue(re.match(r'I am .*GCP Data Engineer.* focused on ', profile['summary']), profile['summary'])
        self.assertNotIn('years', profile['summary'])
        self.assertNotIn('B.Tech', profile['summary'])
        self.assertEqual(profile['experience'][0]['bullets'], GCP_DRAFTS[:5])
        narrative = self.printed(profile)
        for other in ('Frontend', 'React', 'Redux', 'TypeScript', 'JavaScript', 'HTML', 'CSS', 'Jest', 'Material UI',
                      'REST APIs', 'moving into', 'Transferable'):
            self.assertNotIn(other, narrative, f'"{other}" does not belong on a GCP Data Engineer resume')
        for used in ('BigQuery', 'Dataflow', 'Pub/Sub', 'Cloud Composer', 'Cloud Storage', 'Apache Beam', 'Apache Spark',
                     'Dataproc', 'Python', 'Git'):
            self.assertIn(used, profile['skills'])
        self.assertEqual([p.split(' - ')[0] for p in profile['projects']], ['Clickstream Event Pipeline'])
        # Facts stay as they are: designation, employer, dates and education.
        self.assertEqual({k: profile['experience'][0][k] for k in ('position', 'company', 'start', 'end')},
                         {'position': 'Frontend Developer', 'company': 'DemoTech Solutions Pvt Ltd (fictional)',
                          'start': 'July 2023', 'end': 'Present'})
        self.assertEqual(profile['education'][0]['institution'], 'Example Institute of Technology (fictional)')
        self.consistent(profile, packet['target'])
        stored = json.loads(self.row(ident)['facts'])
        self.assertIn('Developed responsive web pages using React.js.', stored['experience'][0]['bullets'],
                      'the candidate\'s own line stays in the facts for other roles')
        self.assertIn('React.js', stored['skills'])

    def test_gcp_input_with_java_backend_selected_gives_a_java_resume(self):
        candidate = {'full_name': 'Ravi Example (fictional)', 'phone': '+91 9000000001', 'email': 'ravi@example.com',
                     'skills': 'Python, SQL, BigQuery, Dataflow, Pub/Sub, Apache Airflow, Cloud Composer, Git, Java, Spring Boot, REST APIs, Spring Data JPA, Hibernate, Spring Security, JUnit, Mockito, Maven, Kafka, MySQL',
                     'work_experience': [{'company': 'Sample Data Labs (fictional)', 'position': 'GCP Data Engineer',
                                          'start': 'Jan 2022', 'end': 'Present',
                                          'responsibilities': 'Built Dataflow pipelines that load event data into BigQuery.\n'
                                                              'Scheduled Cloud Composer DAGs that refresh reporting tables.\n'
                                                              'Configured Pub/Sub topics for streaming ingestion.'}],
                     'projects': [{'name': 'Sales Warehouse', 'stack': 'BigQuery, Dataflow, Python',
                                   'description': 'Built a partitioned BigQuery warehouse loaded by Dataflow jobs.'}],
                     'education': EDUCATION}
        ident = self.jobs.enqueue(OWNER, {'consent': CONSENT_VERSION, 'role': 'Java Backend Developer', 'candidate': candidate})
        model = JAVA_MODEL()
        job, packet = self.confirm(ident, model)
        drafts_prompt = next(p for p in model.prompts if p.startswith('Draft the professional experience'))
        self.assertNotIn('Cloud Composer DAGs', drafts_prompt, 'the model never sees the other field\'s lines')
        work = next(q for q in job['questions'] if q['field'] == 'bullets')
        self.assertEqual(work['value'].split('\n'), JAVA_DRAFTS[:5])

        profile = packet['profile']
        self.assertTrue(re.match(r'I am .*Java Backend Developer.* focused on ', profile['summary']), profile['summary'])
        self.assertEqual(profile['experience'][0]['bullets'], JAVA_DRAFTS[:5])
        narrative = self.printed(profile)
        for other in ('BigQuery', 'Dataflow', 'Pub/Sub', 'Cloud Composer', 'Airflow', 'Python', 'GCP', 'Sales Warehouse'):
            self.assertNotIn(other, narrative, f'"{other}" does not belong on a Java Backend resume')
        for used in ('Java', 'Spring Boot', 'Spring Data JPA', 'Spring Security', 'Hibernate', 'JUnit', 'Mockito', 'Kafka',
                     'MySQL', 'SQL', 'Git'):
            self.assertIn(used, profile['skills'])
        self.assertEqual(profile['experience'][0]['position'], 'GCP Data Engineer', 'the designation is a fact')
        self.consistent(profile, packet['target'])
        stored = json.loads(self.row(ident)['facts'])
        self.assertEqual(len(stored['experience'][0]['bullets']), 8, 'own GCP lines kept beside the Java lines')
        self.assertTrue(any(p.startswith('Sales Warehouse') for p in stored['projects']))

    def test_new_role_does_not_establish_candidate_technology_expertise(self):
        candidate = {'full_name': 'Example Applicant', 'phone': '+91 9000000001', 'skills': 'Python, BigQuery',
                     'work_experience': [{'company': 'Example', 'position': 'Engineer', 'start': '2022',
                                          'end': 'Present', 'responsibilities': 'Built BigQuery reporting views.'}],
                     'education': EDUCATION}
        ident = self.jobs.enqueue(OWNER, {'consent': CONSENT_VERSION, 'role': 'Java Backend Developer',
                                          'candidate': candidate})
        enqueue = self.prepare(ident, JAVA_MODEL())
        job = self.jobs.list(OWNER, ident)[0]
        self.assertEqual(job['status'], 'needs_input')
        self.assertTrue(any(q['field'] == 'skills' for q in job['questions']))
        # Nothing the role suggests is pre-filled as the candidate's work: Java lines are unticked suggestions and
        # projects undecided, and nothing is published before the review.
        work = [q for q in job['questions'] if q['field'] == 'bullets']
        self.assertTrue(all(not q['value'] for q in work))
        self.assertTrue(any(q.get('suggestions') for q in work))
        enqueue.assert_not_called()

    def test_a_regenerated_resume_gets_work_lines_for_the_new_role(self):
        facts = {'full_name': 'Ravi Example (fictional)', 'email': '', 'phone': '+91 9000000001', 'location': '',
                 'skills': ['Python', 'BigQuery', 'Dataflow', 'SQL', 'Git', *'Java, Spring Boot, REST APIs, Spring Data JPA, Hibernate, Spring Security, JUnit, Mockito, Maven, Kafka, MySQL'.split(', ')],
                 'experience': [{'company': 'Sample Data Labs (fictional)', 'position': 'GCP Data Engineer',
                                 'start': 'Jan 2022', 'end': 'Present', 'location': '',
                                 'bullets': ['Built Dataflow pipelines that load event data into BigQuery.',
                                             'Scheduled Cloud Composer DAGs that refresh reporting tables.']}],
                 'education': [{'institution': 'Example Institute of Technology (fictional)',
                                'degree': 'B.Tech - Information Technology', 'start': '', 'end': '2022', 'details': ''}]}
        with closing(self.jobs.connection()) as db, db:
            db.execute("INSERT INTO automation(owner,id,status,created,updated,role,candidate,facts,origin) "
                       "VALUES (?,?,'handed_off',1,1,'GCP Data Engineer',?,?,?)",
                       (OWNER, 'c' * 64, facts['full_name'], json.dumps(facts),
                        json.dumps({'bullets': facts['experience'][0]['bullets']})))
        ident = self.jobs.regenerate(OWNER, 'c' * 64, 'Java Backend Developer')
        job, packet = self.confirm(ident, JAVA_MODEL())
        self.assertIn('experience.0.bullets', [q['id'] for q in job['questions']], 'Java work is drafted for review')
        self.assertEqual(packet['profile']['experience'][0]['bullets'], JAVA_DRAFTS[:5])
        self.assertNotIn('Dataflow', self.printed(packet['profile']))


class RoleFilterTests(unittest.TestCase):
    def test_common_tools_never_make_another_fields_line_fit(self):
        gcp, tools = role('GCP Data Engineer'), named_tools()
        self.assertEqual(other_field_tools('Built React.js components and kept them in Git.', tools, gcp), ['React.js'])
        self.assertEqual(other_field_tools('Reviewed pull requests in Git.', tools, gcp), [])
        self.assertEqual(other_field_tools('Built BigQuery views for the React.js dashboard.', tools, gcp), [])

    def test_skills_are_the_roles_own(self):
        gcp, java = role('GCP Data Engineer'), role('Java Backend Developer')
        self.assertFalse(skill_fits_role('REST APIs', gcp), 'a frontend leftover on a data resume')
        self.assertTrue(skill_fits_role('REST APIs', java))
        self.assertFalse(skill_fits_role('Postman', gcp))
        for universal in ('Git', 'Jira', 'Agile'):
            self.assertTrue(skill_fits_role(universal, gcp) and skill_fits_role(universal, java))
        self.assertFalse(skill_fits_role('Python', java))

    def test_skills_include_the_tools_of_the_confirmed_work(self):
        profile = {'skills': ['BigQuery'], 'experience': [{'bullets': ['Built Dataflow jobs that read Pub/Sub topics.']}],
                   'projects': ['Loader - Tech stack: Cloud Composer, BigQuery | Scheduled loads.']}
        self.assertEqual(sorted(work_skills(profile, role('GCP Data Engineer'))), ['Cloud Composer', 'Dataflow', 'Pub/Sub'])


class SummaryTests(unittest.TestCase):
    def profile(self, bullets):
        return {'full_name': 'Arun Kumar (fictional)', 'skills': ['Python', 'SQL', 'BigQuery', 'Git'],
                'experience': [{'company': 'DemoTech', 'position': 'Frontend Developer', 'start': 'July 2023',
                                'end': 'Present', 'location': '', 'bullets': bullets}],
                'education': [{'institution': 'X', 'degree': 'B.Tech - Information Technology', 'start': '',
                               'end': '2022', 'details': ''}]}

    def test_a_career_change_summary_is_led_by_the_selected_role(self):
        facts = self.profile(GCP_DRAFTS[:3])
        keywords = role('GCP Data Engineer')['keywords']
        text = composed_summary(facts, 'GCP Data Engineer', 3, keywords)
        self.assertEqual(text, 'GCP Data Engineer with hands-on experience in streaming using Apache Beam, BigQuery, '
                               'Cloud Composer, Cloud Storage and Dataflow. Recent work includes building streaming '
                               'pipelines with Apache Beam on Dataflow that read Pub/Sub events and load cleaned rows into '
                               'BigQuery, along with scheduling Cloud Composer DAGs that rebuild partitioned BigQuery '
                               'reporting tables after each nightly batch load. Works with Python, SQL and Git. 3 years '
                               'of professional experience and a B.Tech in Information Technology.')
        self.assertEqual(summary_problems(text, facts, 'GCP Data Engineer', 3), [])
        self.assertTrue(55 <= len(text.split()) <= 80, 'one paragraph of about four lines')

    def test_years_are_never_tied_to_a_title_not_held_and_no_transition_story(self):
        facts = self.profile(GCP_DRAFTS[:3])
        for text, problem in (
                ('GCP Data Engineer with 3 years of professional experience building Dataflow pipelines into BigQuery '
                 'with Apache Beam.', 'career change'),
                ('Builds Dataflow pipelines with Apache Beam into BigQuery, with 3 years of experience as a GCP Data '
                 'Engineer and a B.Tech in Information Technology.', 'career change'),
                ('GCP Data Engineer with hands-on experience in Dataflow pipelines into BigQuery with Apache Beam, '
                 'moving into the role from frontend work.', 'transition'),
                ('GCP Data Engineer with hands-on experience in Dataflow pipelines with Apache Beam, previously a '
                 'Frontend Developer working on BigQuery.', 'previous job titles')):
            with self.subTest(text=text):
                self.assertTrue(any(problem in p for p in summary_problems(text, facts, 'GCP Data Engineer', 3)),
                                summary_problems(text, facts, 'GCP Data Engineer', 3))

    def test_without_role_work_the_summary_states_the_facts(self):
        facts = self.profile(['Developed responsive web pages using React.js.'])
        text = composed_summary(facts, 'GCP Data Engineer', 3, role('GCP Data Engineer')['keywords'])
        self.assertTrue(text.startswith('Frontend Developer with 3 years of professional experience'), text)
        self.assertEqual(summary_problems(text, facts, 'GCP Data Engineer', 3), [])


class DraftQualityTests(unittest.TestCase):
    """Detailed lines are preferred over short or vague ones, and both attempts are pooled; wording alone never
    leaves a job without lines."""

    def arun(self):
        return {'full_name': 'Arun', 'skills': ['React.js', 'Git'], 'education': [],
                'experience': [{'company': 'DemoTech', 'position': 'Frontend Developer', 'start': 'July 2023',
                                'end': 'Present', 'location': '', 'bullets': []}]}

    def draft(self, *attempts):
        replies, prompts = list(attempts), []

        def model(ai, prompt, **_):
            prompts.append(prompt)
            return {'roles': [{'index': 0, 'bullets': replies.pop(0)}], 'skills': []}
        with patch('resume_bot.generate.generate', model):
            work, _ = draft_role_content(self.arun(), role('GCP Data Engineer'), 'GCP Data Engineer', 3, [0], False,
                                         object())
        return work.get(0), prompts

    WEAK = ['Configured Cloud Composer to automate ETL processes, reducing manual intervention.',
            'Developed data models using Apache Spark for efficient data processing and analysis.',
            'Automated data ingestion into Cloud Storage using Dataflow.']
    # As offered: a fill line loses its generic ending (tests/test_writing_quality.py).
    FILLED = ['Configured Cloud Composer to automate ETL processes.', *WEAK[1:]]

    def test_detailed_lines_from_the_retry_come_first(self):
        lines, prompts = self.draft(self.WEAK, GCP_DRAFTS[:5])
        self.assertEqual(len(prompts), 3, 'a job still short of its lines is asked twice more')
        self.assertIn('previous draft was rejected', prompts[1])
        self.assertIn('Configured Cloud Composer to automate ETL processes', prompts[1], 'the retry sees kept lines')
        # The five detailed lines; thinner ones are never used to pad a job up to eight (only to reach three).
        self.assertEqual(lines, GCP_DRAFTS[:5])

    def test_short_or_vague_lines_still_fill_the_job_when_nothing_better_comes(self):
        lines, prompts = self.draft(self.WEAK, self.WEAK)
        self.assertEqual(len(prompts), 3)
        self.assertEqual(lines, self.FILLED)

    def test_a_word_added_only_for_length_is_rejected(self):
        self.assertIn('padding words at the end',
                      proposal_problems('Wrote Python code for Apache Beam to process Pub/Sub event processing period.'))


if __name__ == '__main__':
    unittest.main()
