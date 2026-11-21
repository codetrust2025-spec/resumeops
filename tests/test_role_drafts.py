"""Role-driven drafts: what a candidate's documents leave out (the work done in a job, the skills) is drafted
by AI for the selected role and confirmed once, with the project suggestions, before anything is published.

All candidates are fictional. The model is faked: these tests check what is accepted from it, what reaches
the resume, and that the role decides the content (a Frontend resume never takes GCP work, and the reverse)."""
import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from resume_bot.automation import CONSENT_VERSION, AnswerError, apply_answers
from resume_bot.generate import composed_summary, draft_role_content, propose_projects, write_summary

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from automation_jobs import AutomationJobs  # noqa: E402
from resume_dashboard import Workspace  # noqa: E402

OWNER = 'c' * 64
FRONTEND = {'role': 'frontend-developer', 'keywords': ['JavaScript', 'TypeScript', 'HTML', 'CSS', 'React.js', 'Angular']}
GCP = {'role': 'gcp-cloud-engineer', 'keywords': ['Google Cloud', 'Compute Engine', 'Cloud Run', 'Terraform', 'Docker']}


def profile(skills=(), jobs=None):
    return {'full_name': 'Meera Example (fictional)', 'email': '', 'phone': '+91 90000 00009', 'skills': list(skills),
            'experience': jobs if jobs is not None else [
                {'company': 'Example Health Pvt Ltd', 'position': 'Software Engineer', 'start': 'Jan 2021',
                 'end': 'Present', 'location': '', 'bullets': []},
                {'company': 'Sample Systems', 'position': 'Associate Engineer', 'start': 'Jun 2019',
                 'end': 'Dec 2020', 'location': '', 'bullets': []}],
            'education': [{'institution': 'Example Institute of Technology', 'degree': 'B.Tech', 'end': '2019',
                           'start': '', 'details': ''}]}


class Model:
    """Stands in for Ollama: answers each kind of prompt with fixed output and records the prompts."""

    def __init__(self, drafts=None, projects=None, fail=False):
        self.drafts, self.projects, self.fail, self.prompts = drafts or {}, projects, fail, []

    def __call__(self, ai, prompt, **_):
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError('model unavailable')
        if prompt.startswith('Draft the professional experience'):
            return self.drafts
        if 'portfolio projects' in prompt:
            return {'projects': self.projects or []}
        raise RuntimeError('summary falls back to the composed one')


FRONTEND_DRAFT = {
    'roles': [{'index': 0, 'bullets': [
        'Built reusable React.js components with TypeScript for internal dashboards and forms.',
        'Styled responsive page layouts with HTML and CSS across desktop and mobile browsers.',
        'Integrated REST APIs into React.js pages with shared TypeScript types for order tracking screens.',
        'Deployed frontend containers to Google Cloud Run with Terraform for each release.',  # off-role: rejected
        'Improved page load time by 40 percent through code splitting.']},  # metric: rejected
        {'index': 1, 'bullets': [
            'Fixed JavaScript defects in Angular screens reported by the QA team.',
            'Wrote unit tests for Angular services and components with TypeScript.',
            'Built Angular forms with reactive validation and TypeScript models for internal tools.',
            'Worked at Sample Systems on JavaScript UI tickets for 3 teams.']},  # employer name and number: rejected
        {'index': 7, 'bullets': ['Not a job of this candidate, so never accepted as anything.']}],
    'skills': ['React.js', 'typescript', 'CSS', 'Google Cloud', 'Underwater Basket Weaving']}

GCP_DRAFT = {
    'roles': [{'index': 0, 'bullets': [
        'Deployed containerized services to Cloud Run using Docker images and Terraform modules.',
        'Provisioned Compute Engine instances and networking with Terraform for development environments.',
        'Configured Cloud Monitoring alerts for Cloud Run services and Compute Engine instances.',
        'Built React.js admin screens for the operations team.']},  # off-role for GCP: rejected
        {'index': 1, 'bullets': ['Maintained Docker images for build agents running on Google Cloud.',
                                 'Delivered a scalable platform for every product team on Google Cloud.']}],  # inflated
    'skills': ['Google Cloud', 'Terraform', 'Cloud Run', 'React.js']}


class DraftRoleContentTests(unittest.TestCase):
    def test_frontend_drafts_stay_frontend_and_reject_unsupported_lines(self):
        model = Model(FRONTEND_DRAFT)
        with patch('resume_bot.generate.generate', model):
            work, skills = draft_role_content(profile(), FRONTEND, 'Frontend Developer', 5, [0, 1], True, object())
        # Detailed lines first; the shorter one only fills the job up.
        self.assertEqual(work[0], ['Styled responsive page layouts with HTML and CSS across desktop and mobile browsers.',
                                   'Integrated REST APIs into React.js pages with shared TypeScript types for order tracking screens.',
                                   'Built reusable React.js components with TypeScript for internal dashboards and forms.'])
        self.assertEqual(work[1], ['Built Angular forms with reactive validation and TypeScript models for internal tools.',
                                   'Fixed JavaScript defects in Angular screens reported by the QA team.',
                                   'Wrote unit tests for Angular services and components with TypeScript.'])
        self.assertNotIn(7, work)
        self.assertNotIn('Google Cloud', ' '.join(line for lines in work.values() for line in lines))
        # The role's leading technologies come first, then the model's catalog-term suggestions, then the role tools
        # the drafted lines use (Angular), so the suggested skills match the drafted work.
        self.assertEqual(skills, ['JavaScript', 'HTML', 'React.js', 'TypeScript', 'CSS', 'Angular'])
        self.assertNotIn('Underwater Basket Weaving', skills)

    def test_gcp_drafts_stay_on_gcp(self):
        cloud = profile(jobs=[{'company': 'Example Health Pvt Ltd', 'position': 'Cloud Engineer', 'start': 'Jan 2021',
                               'end': 'Present', 'location': '', 'bullets': []},
                              {'company': 'Sample Systems', 'position': 'System Administrator', 'start': 'Jun 2019',
                               'end': 'Dec 2020', 'location': '', 'bullets': []}])
        with patch('resume_bot.generate.generate', Model(GCP_DRAFT)):
            work, skills = draft_role_content(cloud, GCP, 'GCP Cloud Engineer', 5, [0, 1], True, object())
        self.assertEqual(len(work[0]), 3)
        self.assertNotIn('React', ' '.join(work[0]))
        self.assertNotIn(1, work, 'one acceptable line is not enough to draft a job')
        self.assertIn('Terraform', skills)
        self.assertNotIn('React.js', skills)

    def test_a_job_that_ended_gets_past_tense_lines_after_one_retry(self):
        replies = iter([
            {'roles': [{'index': 1, 'bullets': ['Contributes to Angular screens with TypeScript and CSS styling.',
                                                'Writes unit tests for Angular services with TypeScript and Jest.',
                                                'Builds Angular forms with reactive validation for internal tools.']}],
             'skills': []},
            {'roles': [{'index': 1, 'bullets': ['Contributed to Angular screens with TypeScript and CSS styling.',
                                                'Wrote unit tests for Angular services with TypeScript and Jest.',
                                                'Built Angular forms with reactive validation for internal tools.']}],
             'skills': []}])
        prompts = []

        def model(ai, prompt, **_):
            prompts.append(prompt)
            return next(replies)
        with patch('resume_bot.generate.generate', model):
            work, _ = draft_role_content(profile(), FRONTEND, 'Frontend Developer', 5, [1], False, object())
        self.assertEqual(work[1], ['Contributed to Angular screens with TypeScript and CSS styling.',
                                   'Wrote unit tests for Angular services with TypeScript and Jest.',
                                   'Built Angular forms with reactive validation for internal tools.'])
        self.assertEqual(len(prompts), 3, 'still short of its lines after the retry, so asked once more')
        self.assertIn('jobs that ended use past-tense verbs', prompts[1])

    def test_the_model_never_sees_employer_names_and_failures_leave_fields_empty(self):
        model = Model(FRONTEND_DRAFT)
        with patch('resume_bot.generate.generate', model):
            draft_role_content(profile(), FRONTEND, 'Frontend Developer', 5, [0], False, object())
        self.assertNotIn('Example Health', model.prompts[0])
        self.assertIn('Software Engineer', model.prompts[0])
        with patch('resume_bot.generate.generate', Model(fail=True)):
            self.assertEqual(draft_role_content(profile(), FRONTEND, 'Frontend Developer', 5, [0], True, object()),
                             ({}, []))

    def test_summary_and_projects_use_only_the_skills_that_fit_the_role(self):
        mixed = profile(skills=['React.js', 'TypeScript', 'Google Cloud', 'Terraform'], jobs=[
            {'company': 'Example Health Pvt Ltd', 'position': 'Frontend Developer', 'start': 'Jan 2021', 'end': 'Present',
             'location': '', 'bullets': ['Built React.js screens with TypeScript.']}])
        text = composed_summary(mixed, 'Frontend Developer', 3, FRONTEND['keywords'])
        self.assertIn('React.js', text)
        self.assertNotIn('Google Cloud', text)
        model = Model(projects=[])
        with patch('resume_bot.generate.generate', model):
            write_summary(mixed, 'Frontend Developer', 3, FRONTEND['keywords'], object())
            propose_projects(mixed, FRONTEND, 'Frontend Developer', 3, object())
        summary_facts = json.loads(model.prompts[0].split('DATA:\n', 1)[1])
        # The objective's model reads only the candidate's own work lines (to name their business area), never the
        # skills list, so a skill that does not fit the role cannot reach the objective.
        self.assertEqual(summary_facts['work_lines'], ['Built React.js screens with TypeScript.'])
        self.assertNotIn('confirmed_skills', summary_facts)
        self.assertNotIn('Google Cloud', json.dumps(summary_facts))
        # No proposal came back, so the model was asked once more; the retry note follows the facts.
        project_facts = json.loads(model.prompts[-1].split('FACTS:\n', 1)[1].split('\nThe previous')[0])
        self.assertEqual(project_facts['candidate_skills'], ['React.js', 'TypeScript'])


class DraftedAnswerTests(unittest.TestCase):
    QUESTIONS = [{'id': 'experience.0.bullets', 'section': 'experience', 'entry': 0, 'field': 'bullets'},
                 {'id': 'experience.1.bullets', 'section': 'experience', 'entry': 1, 'field': 'bullets'},
                 {'id': 'skills', 'section': 'profile', 'entry': None, 'field': 'skills'}]

    def test_lines_are_kept_edited_or_left_out(self):
        facts = apply_answers(profile(), self.QUESTIONS, {
            'experience.0.bullets': '- Built React.js screens with TypeScript.\n\nWrote CSS for forms.',
            'experience.1.bullets': '', 'skills': 'React.js, TypeScript\nCSS'})
        self.assertEqual(facts['experience'][0]['bullets'], ['Built React.js screens with TypeScript.', 'Wrote CSS for forms.'])
        self.assertEqual(facts['experience'][1]['bullets'], [])
        self.assertEqual(facts['skills'], ['React.js', 'TypeScript', 'CSS'])

    def test_limits_name_the_field(self):
        for answers, field in (({'experience.0.bullets': '\n'.join(f'Line {i}' for i in range(11))}, 'experience.0.bullets'),
                               ({'experience.0.bullets': 'x' * 241}, 'experience.0.bullets'),
                               ({'skills': 'y' * 61}, 'skills')):
            with self.subTest(field=field), self.assertRaises(AnswerError) as error:
                apply_answers(profile(), self.QUESTIONS, answers)
            self.assertEqual(error.exception.field, field)

    def test_identity_answers_are_still_required(self):
        with self.assertRaises(AnswerError) as error:
            apply_answers(profile(), [{'id': 'experience.0.company', 'section': 'experience', 'entry': 0,
                                       'field': 'company'}], {'experience.0.company': ''})
        self.assertEqual(error.exception.field, 'experience.0.company')


def proposal(name):
    return {'name': name, 'problem': 'A fictional practice problem.', 'stack': ['React.js', 'TypeScript'],
            'architecture': 'Single page app.', 'features': ['Search'], 'responsibilities': 'Build the UI.',
            'bullets': [f'Built the {name} screens with React.js and TypeScript for practice users.',
                        'Added search and filters with React.js state and TypeScript types.',
                        'Wrote component tests for the main flows with TypeScript.']}


class ReviewOnceTests(unittest.TestCase):
    """The job stops once for the drafts and the projects together, then completes without asking again."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(Path(self.temp.name), code_root=ROOT)
        self.jobs = AutomationJobs(self.workspace)
        self.jobs.start = lambda: None
        candidate = {'full_name': 'Meera Example (fictional)', 'phone': '+91 90000 00009', 'email': '',
                     'work_experience': [{'company': 'Example Health Pvt Ltd', 'position': 'Software Engineer',
                                          'start': 'Jan 2021', 'end': 'Present'},
                                         {'company': 'Sample Systems', 'position': 'Associate Engineer',
                                          'start': 'Jun 2019', 'end': 'Dec 2020'}],
                     'education': [{'degree': 'B.Tech', 'university': 'Example Institute of Technology',
                                    'graduation_year': '2019'}]}
        self.id = self.jobs.enqueue(OWNER, {'consent': CONSENT_VERSION, 'role': 'Frontend Developer',
                                            'candidate': candidate})
        self.model = Model(FRONTEND_DRAFT, projects=[proposal('Practice Catalogue'), proposal('Practice Planner')])

    def row(self):
        with closing(self.jobs.connection()) as db:
            return db.execute('SELECT * FROM automation WHERE owner=? AND id=?', (OWNER, self.id)).fetchone()

    def prepare(self):
        with patch('resume_bot.generate.generate', self.model), patch('resume_bot.automation.generate', return_value={}), \
                patch.object(self.workspace, 'enqueue', return_value={'name': f'private-{OWNER}-{"1" * 64}.json'}) as enqueue:
            self.jobs._prepare(self.row())
        return enqueue

    def test_drafts_and_projects_are_confirmed_once_and_only_confirmed_lines_are_published(self):
        self.prepare()
        row = self.row()
        self.assertEqual((row['status'], row['stage']), ('needs_input', 'Review AI drafts for Frontend Developer'))
        self.assertIsNone(row['job'], 'nothing is published before the review')
        job = self.jobs.list(OWNER)[0]
        self.assertEqual([q['id'] for q in job['questions']], ['experience.0.bullets', 'experience.1.bullets', 'skills'])
        self.assertTrue(all(q['drafted'] for q in job['questions']))
        self.assertIn('React.js components', job['questions'][0]['value'])
        self.assertEqual(job['questions'][2]['value'], 'JavaScript, HTML, React.js, TypeScript, CSS, Angular')
        self.assertEqual([p['name'] for p in job['proposals']], ['Practice Catalogue', 'Practice Planner'])
        self.assertEqual(job['error'], 'AI drafted work lines from your job titles and dates and skill suggestions for '
                                       'Frontend Developer. Keep only what is true for you and edit anything. Then '
                                       'confirm once. Suggested projects are included as drafts: skip any you have not built.')

        with self.assertRaises(AnswerError):  # the projects are decided together with the drafts
            self.jobs.decide_projects(OWNER, self.id, [{'include': False}, {'include': False}])
        with self.assertRaises(AnswerError):
            self.jobs.answer(OWNER, self.id, {'experience.0.bullets': 'x', 'experience.1.bullets': '', 'skills': ''})
        self.jobs.answer(OWNER, self.id, {
            'experience.0.bullets': 'Built reusable React.js components with TypeScript for internal dashboards and forms.',
            'experience.1.bullets': '', 'skills': 'React.js, TypeScript'},
            [{'include': True}, {'include': False}])
        checkpoints = json.loads(self.row()['checkpoints'])
        self.assertEqual(self.row()['status'], 'queued')
        self.assertTrue(checkpoints['drafts_reviewed'] and checkpoints['projects_reviewed'])
        self.assertEqual(checkpoints['facts']['experience'][1]['bullets'], [])
        self.assertEqual(len(checkpoints['confirmed_projects']), 1)

        enqueue = self.prepare()  # continues to the builder without asking again
        row = self.row()
        self.assertEqual(row['status'], 'handed_off', row['error'])
        approval = enqueue.call_args.kwargs['approval']
        facts = approval['automation']['facts']
        self.assertEqual(facts['experience'][0]['bullets'],
                         ['Built reusable React.js components with TypeScript for internal dashboards and forms.'])
        self.assertEqual(facts['experience'][1]['bullets'], [])
        self.assertEqual(facts['skills'], ['React.js', 'TypeScript'])
        printed = json.dumps(json.loads(enqueue.call_args.args[0])['profile'])
        self.assertNotIn('Styled responsive page layouts', printed, 'an unconfirmed draft line is never published')
        self.assertNotIn('Practice Planner', printed, 'a skipped project is never published')
        self.assertIn('Practice Catalogue', printed)
        self.assertNotIn('Example Health Pvt Ltd built', printed)

    def test_without_the_model_the_review_still_appears_with_empty_fields(self):
        self.model = Model(fail=True)
        self.prepare()
        job = self.jobs.list(OWNER)[0]
        self.assertEqual(job['status'], 'needs_input')
        self.assertFalse(any(q['drafted'] for q in job['questions']))
        self.assertEqual([q['value'] for q in job['questions']], ['', '', ''])
        self.jobs.answer(OWNER, self.id, {'experience.0.bullets': 'Fixed Angular form defects.',
                                          'experience.1.bullets': '', 'skills': 'Angular'})
        self.assertEqual(self.row()['status'], 'queued')

    def test_regenerating_a_finished_resume_drafts_work_for_the_new_role(self):
        facts = {**profile(skills=['React.js']), 'experience': [
            {'company': 'Example Health Pvt Ltd', 'position': 'Software Engineer', 'start': 'Jan 2021', 'end': 'Present',
             'location': '', 'bullets': []}]}
        with closing(self.jobs.connection()) as db, db:
            db.execute("UPDATE automation SET status='handed_off',facts=?,payload=NULL WHERE owner=? AND id=?",
                       (json.dumps(facts), OWNER, self.id))
        new = self.jobs.regenerate(OWNER, self.id, 'Angular Developer')
        with closing(self.jobs.connection()) as db:
            checkpoints = json.loads(db.execute('SELECT checkpoints FROM automation WHERE id=?', (new,)).fetchone()[0])
        self.assertNotIn('drafts_reviewed', checkpoints, 'the new role decides the work lines again')


if __name__ == '__main__':
    unittest.main()
