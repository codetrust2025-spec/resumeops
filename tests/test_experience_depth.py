"""Professional experience for the selected role: detailed, role-focused drafts (confirmed before they count),
never one generic line beside a page of projects. The candidate is the fictional Arun Kumar test form: a Frontend
Developer (July 2023 - Present) with React skills and no stated responsibilities, applying for GCP Data Engineer."""
import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from resume_bot.ats import score as ats_score  # noqa: F401  (imported to keep the module under test explicit)
from resume_bot.automation import CONSENT_VERSION, verified_automation
from resume_bot.generate import composed_summary, draft_role_content, present_for_role, propose_projects, technical_job
from resume_bot.workflow import analyze

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from automation_jobs import AutomationJobs  # noqa: E402
from resume_dashboard import Workspace  # noqa: E402

OWNER = 'f' * 64
GCP_LINES = [
    'Built BigQuery views that feed the React.js reporting dashboards used by the operations team.',
    'Developed Dataflow pipelines with Apache Beam to clean event data before loading it into BigQuery.',
    'Configured Pub/Sub topics to stream frontend activity events into Cloud Storage for later analysis.',
    'Wrote Python jobs on Cloud Composer that refresh BigQuery tables behind the internal dashboards.',
    'Tested Dataflow transformations with Python unit tests before releasing changes to the data pipelines.',
    'Styled the dashboard layout with React.js and CSS for desktop and mobile screens.',  # no GCP technology
]


def role(title):
    from resume_bot.ai_review import role_intelligence
    intelligence = role_intelligence(title)
    return {'role': intelligence['role'], 'keywords': intelligence['terms']}


def arun(bullets=()):
    return {'full_name': 'Arun Kumar (fictional)', 'phone': '+91 9000000000', 'email': 'arun.kumar@example.com',
            'skills': ['React.js', 'JavaScript (ES6+)', 'TypeScript', 'HTML5', 'CSS3', 'Redux Toolkit', 'React Router',
                       'REST APIs', 'Material UI', 'Jest', 'Git'],
            'experience': [{'company': 'DemoTech Solutions Pvt Ltd (fictional)', 'position': 'Frontend Developer',
                            'start': 'July 2023', 'end': 'Present', 'location': '', 'bullets': list(bullets)}],
            'education': [{'institution': 'Example Institute of Technology (fictional)',
                           'degree': 'B.Tech - Information Technology', 'start': '', 'end': '2022', 'details': ''}]}


class Model:
    def __init__(self, lines=GCP_LINES, skills=('Python', 'SQL', 'BigQuery', 'Dataflow', 'Pub/Sub', 'Cloud Storage')):
        self.lines, self.skills, self.prompts = list(lines), list(skills), []

    def __call__(self, ai, prompt, **_):
        self.prompts.append(prompt)
        if prompt.startswith('Draft the professional experience'):
            return {'roles': [{'index': 0, 'bullets': self.lines}], 'skills': self.skills}
        if 'portfolio projects' in prompt:
            bullets = ['Built a Pub/Sub topic that streams order events into a Dataflow pipeline for cleaning.',
                       'Loaded the cleaned events into partitioned BigQuery tables for daily reporting queries.',
                       'Scheduled the Dataflow pipeline with Python scripts and sent failure alerts through Pub/Sub.']
            return {'projects': [{'name': name, 'problem': 'Fictional practice data.', 'stack': ['Pub/Sub', 'Dataflow', 'BigQuery'],
                                  'architecture': 'Streaming pipeline.', 'features': ['Events'], 'responsibilities': 'Pipeline.',
                                  'bullets': bullets} for name in ('Order Event Stream', 'Sales Warehouse', 'Log Archive')]}
        raise RuntimeError('summary falls back to the composed one')


class DraftDepthTests(unittest.TestCase):
    def test_a_frontend_job_gets_detailed_gcp_focused_drafts(self):
        self.assertTrue(technical_job('Frontend Developer'))
        self.assertFalse(technical_job('Senior Executive AR (Accounts Receivable)'))
        model = Model()
        with patch('resume_bot.generate.generate', model):
            work, _ = draft_role_content(arun(), role('GCP Data Engineer'), 'GCP Data Engineer', 3, [0], False, object())
        self.assertEqual(work[0], GCP_LINES[:5], 'five role-focused lines; the line without GCP technology is dropped')
        self.assertTrue(all(line.endswith('.') for line in work[0]))
        self.assertIn('kind "role"', model.prompts[0])
        self.assertNotIn('DemoTech', model.prompts[0], 'the model never sees the employer')

    def test_fragments_become_sentences_and_the_count_does_not_follow_years(self):
        fragments = [line.rstrip('.') for line in GCP_LINES[:5]]
        for years in (0, 3, 12):
            with self.subTest(years=years), patch('resume_bot.generate.generate', Model(lines=fragments)):
                work, _ = draft_role_content(arun(), role('GCP Data Engineer'), 'GCP Data Engineer', years, [0], False,
                                             object())
                self.assertEqual(len(work[0]), 5)
                self.assertTrue(all(line.endswith('.') for line in work[0]))

    def test_existing_lines_are_kept_and_drafts_add_to_them(self):
        own = ['Built React.js screens for the order tracking module.']
        with patch('resume_bot.generate.generate', Model()):
            work, _ = draft_role_content(arun(own), role('GCP Data Engineer'), 'GCP Data Engineer', 3, [0], False, object())
        self.assertEqual(len(work[0]), 4, 'additions fill the job to five lines with the candidate line')


class PresentationDepthTests(unittest.TestCase):
    def test_concepts_never_hide_a_line_and_jobs_keep_three_lines(self):
        lines = ['Implemented responsive design principles for cross-device compatibility.',  # a concept, not a tool
                 'Built React.js components with Redux Toolkit for the checkout flow.',
                 'Styled page layouts with CSS3 and Material UI.',
                 'Reviewed pull requests for the frontend team.']
        shown, _ = present_for_role(arun(lines), role('GCP Data Engineer'), 'GCP Data Engineer', lines)
        kept = shown['experience'][0]['bullets']
        self.assertIn(lines[0], kept)
        self.assertEqual(len(kept), 3, "the candidate's own lines are kept until the job has three")


class ProposalTests(unittest.TestCase):
    """Release 02c7899 was rolled back: an experienced candidate (fictional Asha Example, Python Backend Developer)
    got no project to review, because the model's short complete sentences were rejected and only two proposals
    were considered. A proposal that fails validation must never leave the candidate without any."""
    REJECTED = {'name': 'Travel Booking API', 'stack': ['FastAPI', 'PostgreSQL', 'Celery', 'Redis'], 'bullets': [
        'Designed API endpoints for booking and managing travel reservations.',
        'Integrated Celery and Redis for background job processing and real-time updates.',
        'Wrote unit tests with pytest to ensure API reliability.',  # pytest is outside the stack
        'Deployed application using Docker and Kubernetes for container orchestration.']}  # so is Docker
    SHORT = {'name': 'Microservices Ecosystem', 'stack': ['Django', 'Kubernetes', 'PostgreSQL', 'Redis'], 'bullets': [
        'Developed Django services with REST APIs for service communication.',
        'Managed Kubernetes deployments for the Django services.',
        'Ensured database consistency with PostgreSQL.',  # five words: dropped
        'Integrated Redis for caching and session management.']}  # seven words: a complete sentence, kept
    THIRD = {'name': 'Order Reporting Service', 'stack': ['Django', 'PostgreSQL', 'Celery'], 'bullets': [
        'Built Django REST endpoints that return daily order summaries from PostgreSQL.',
        'Scheduled Celery tasks that rebuild the order summary tables every night.',
        'Wrote Django unit tests for the summary endpoints and the scheduled tasks.']}

    def setUp(self):
        self.asha = {'full_name': 'Asha Example', 'skills': ['Python', 'SQL', 'Django', 'Kubernetes'],
                     'experience': [{'company': 'Example Cloud Pvt Ltd', 'position': 'Senior Backend Engineer',
                                     'start': 'Jan 2022', 'end': 'Present', 'location': '',
                                     'bullets': ['Built payment APIs in Python and PostgreSQL.']}],
                     'education': []}

    def propose(self, *responses, profile=None):
        prompts, replies = [], list(responses)

        def model(ai, prompt, **_):
            prompts.append(prompt)
            context = {'problem': 'Fictional practice.', 'architecture': 'Service.', 'features': ['Orders'],
                       'responsibilities': 'Backend.'}
            return {'projects': [{**context, **item} for item in replies.pop(0)]}
        with patch('resume_bot.generate.generate', model):
            kept = propose_projects(profile or self.asha, role('Python Backend Developer'), 'Python Backend Developer',
                                    7, object())
        return kept, prompts

    def test_short_complete_sentences_are_kept_and_a_spare_proposal_is_asked_for(self):
        kept, prompts = self.propose([self.REJECTED, self.SHORT, self.THIRD])
        self.assertEqual([p['name'] for p in kept], ['Microservices Ecosystem', 'Order Reporting Service'])
        self.assertIn('Integrated Redis for caching and session management.', kept[0]['bullets'])
        self.assertNotIn('Ensured database consistency with PostgreSQL.', kept[0]['bullets'])
        self.assertTrue(prompts[0].startswith('Propose 3 '), 'one spare, so a rejected proposal leaves two')
        self.assertEqual(len(prompts), 1)

    def test_an_experienced_candidate_gets_two_and_a_fresher_three(self):
        kept, _ = self.propose([self.SHORT, self.THIRD, {**self.THIRD, 'name': 'Invoice Export Service'}])
        self.assertEqual(len(kept), 2)
        fresher = {**self.asha, 'experience': []}
        kept, _ = self.propose([self.SHORT, self.THIRD, {**self.THIRD, 'name': 'Invoice Export Service'}], profile=fresher)
        self.assertEqual(len(kept), 3)

    def test_when_every_proposal_is_rejected_the_model_is_asked_once_more(self):
        kept, prompts = self.propose([self.REJECTED], [self.SHORT])
        self.assertEqual([p['name'] for p in kept], ['Microservices Ecosystem'])
        self.assertEqual(len(prompts), 2)
        self.assertIn('previous proposals were rejected', prompts[1])
        kept, prompts = self.propose([self.REJECTED], [self.REJECTED])
        self.assertEqual((kept, len(prompts)), ([], 2), 'no project rather than an unsupported one; one retry only')


class ReviewAndResumeTests(unittest.TestCase):
    """The acceptance case end to end: one review with GCP-focused experience drafts, skills and two projects; the
    resume then shows the confirmed experience in depth, balanced against the projects."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(Path(self.temp.name), code_root=ROOT)
        self.jobs = AutomationJobs(self.workspace)
        self.jobs.start = lambda: None
        candidate = {'full_name': 'Arun Kumar (fictional)', 'phone': '+91 9000000000', 'email': 'arun.kumar@example.com',
                     'skills': 'React.js, TypeScript, Redux Toolkit, REST APIs, Jest, Git',
                     'work_experience': [{'company': 'DemoTech Solutions Pvt Ltd (fictional)', 'position': 'Frontend Developer',
                                          'start': 'July 2023', 'end': 'Present'}],
                     'education': [{'degree': 'B.Tech - Information Technology',
                                    'university': 'Example Institute of Technology (fictional)', 'graduation_year': '2022'}]}
        self.id = self.jobs.enqueue(OWNER, {'consent': CONSENT_VERSION, 'role': 'GCP Data Engineer', 'candidate': candidate})

    def row(self):
        with closing(self.jobs.connection()) as db:
            return db.execute('SELECT * FROM automation WHERE owner=? AND id=?', (OWNER, self.id)).fetchone()

    def prepare(self):
        with patch('resume_bot.generate.generate', Model()), patch('resume_bot.automation.generate', return_value={}), \
                patch.object(self.workspace, 'enqueue', return_value={'name': f'private-{OWNER}-{"3" * 64}.json'}) as enqueue:
            self.jobs._prepare(self.row())
        return enqueue

    def test_gcp_data_engineer_resume_has_substantial_experience(self):
        self.prepare()
        job = self.jobs.list(OWNER)[0]
        self.assertEqual(job['status'], 'needs_input')
        work = next(q for q in job['questions'] if q['field'] == 'bullets')
        self.assertTrue(work['drafted'])
        self.assertEqual(work['value'].split('\n'), GCP_LINES[:5])
        self.assertIn('drafts until you confirm', work['explanation'])
        self.assertEqual(len(job['proposals']), 2, 'two projects beside real work experience, not three')
        # The fictional candidate keeps the drafts and the skills, and includes one project.
        self.jobs.answer(OWNER, self.id, {q['id']: q['value'] for q in job['questions']},
                         [{'include': True}, {'include': False}])
        enqueue = self.prepare()
        self.assertEqual(self.row()['status'], 'handed_off', self.row()['error'])
        packet, approval = json.loads(enqueue.call_args.args[0]), enqueue.call_args.kwargs['approval']
        profile = packet['profile']
        bullets = profile['experience'][0]['bullets']
        self.assertEqual(len(bullets), 5, 'confirmed GCP-focused responsibilities are shown, not one generic line')
        self.assertTrue(all(any(t in b for t in ('BigQuery', 'Dataflow', 'Pub/Sub', 'Cloud Composer')) for b in bullets))
        self.assertEqual(profile['experience'][0]['position'], 'Frontend Developer', 'the designation stays')
        self.assertEqual(profile['experience'][0]['start'], 'July 2023')
        project_lines = sum(len(p.split(' | ')) - 1 for p in profile.get('projects', []))
        self.assertLessEqual(project_lines, len(bullets), 'projects never outweigh the experience')
        self.assertIn('Recent work uses', profile['summary'])
        self.assertIn('B.Tech in Information Technology', profile['summary'])
        self.assertTrue(verified_automation(profile, packet['target'], approval), "the builder's check passes")

    def test_ats_marks_one_line_per_role_as_underdeveloped(self):
        thin = {**arun(['Conducted code reviews and provided feedback to junior developers.']), 'headline': 'GCP Data Engineer',
                'summary': '', 'projects': [], 'certifications': []}
        target = {**role('GCP Data Engineer'), 'experience_years': 3, 'template': 'standard'}
        self.assertIn('Describe at least three responsibilities for every role.', analyze(thin, target)['suggestions'])


if __name__ == '__main__':
    unittest.main()
