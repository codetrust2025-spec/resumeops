"""Resume generation standards for every supported role: six sections in order (Career Objective, Technical Skills,
Professional Work Experience, Projects, Certifications, Education), a four-line objective led by the selected role,
selective bold technologies, 7-8 distinct lines for recent jobs, 4-5 bullet projects with coherent stacks and their
technologies beside the title, certifications printed only once confirmed, and education with CGPA above 8.0 only.
All candidates are fictional; the model is faked."""
import json
import re
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from resume_bot.automation import AnswerError, CONSENT_VERSION, verified_automation
from resume_bot.certifications import RECOMMENDED, recommended_certifications
from resume_bot.claim_audit import matches_pdf_text
from resume_bot.generate import (_checked_proposals, composed_summary, conflicting_tools, core_terms, draft_lines,
                                 draft_role_content, role_entry, skill_groups, summary_problems)
from resume_bot.latex import education_details, expected_text, render
from resume_bot.profile import validate_profile
from resume_bot.workflow import ROLES, analyze, load_roles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from automation_jobs import AutomationJobs  # noqa: E402
from resume_dashboard import Workspace  # noqa: E402
try:  # discovered from the tests folder (CI) or run as the tests package
    from test_role_authority import GCP_MODEL, role  # noqa: E402
except ImportError:
    from tests.test_role_authority import GCP_MODEL, role  # noqa: E402

OWNER = 'e' * 64
SECTIONS = ['Career Objective', 'Technical Skills', 'Professional Work Experience', 'Projects', 'Certifications',
            'Education']


def resume(**extra):
    profile = {'full_name': 'Nila Example (fictional)', 'email': 'nila@example.com', 'phone': '+91 90000 00042',
               'headline': 'Java Backend Developer',
               'summary': 'Java Backend Developer with 3 years of professional experience, most recently as Software '
                          'Engineer. Recent work includes building Spring Boot REST APIs for order workflows. Works '
                          'with Java, Spring Boot, MySQL and JUnit. Holds a B.Tech in Information Technology.',
               'skills': ['Java', 'Spring Boot', 'MySQL', 'JUnit', 'REST APIs'],
               'experience': [{'company': 'Example Labs (fictional)', 'position': 'Software Engineer',
                               'start': 'Jan 2023', 'end': 'Present', 'location': '',
                               'bullets': ['Built Spring Boot REST APIs for order workflows with Java and MySQL.',
                                           'Wrote JUnit tests for the order services and repositories.']}],
               'education': [{'institution': 'Example Institute (fictional)', 'degree': 'B.Tech - Information Technology',
                              'start': '2018', 'end': '2022', 'details': 'CGPA 7.6'}],
               'projects': ['Order Service - Tech stack: Java, Spring Boot, MySQL | Built order endpoints with Spring '
                            'Boot and MySQL. | Wrote JUnit tests for the order endpoints.'],
               'certifications': ['Oracle Certified Professional: Java SE 17 Developer']}
    profile.update(extra)
    load_roles()
    profile['skill_groups'] = skill_groups(profile['skills'], ROLES['java-backend-developer'])
    return validate_profile(profile)


class LayoutTests(unittest.TestCase):
    def test_six_sections_in_order_and_a_freshers_order(self):
        tex = render(resume(), years=3)
        positions = [tex.index(r'\section*{' + name + '}') for name in SECTIONS]
        self.assertEqual(positions, sorted(positions))
        fresher = render(resume(), years=0)
        self.assertLess(fresher.index(r'\section*{Certifications}'), fresher.index(r'\section*{Education}'))
        self.assertLess(fresher.index(r'\section*{Education}'), fresher.index(r'\section*{Professional Work Experience}'))

    def test_technologies_are_bold_selectively_and_the_text_is_unchanged(self):
        profile = resume()
        tex = render(profile, years=3)
        self.assertIn(r'\textbf{Java Backend Developer} with 3 years', tex, 'the selected role leads, in bold')
        objective = tex.split(r'\section*{Career Objective}')[1].split(r'\section*')[0]
        for sentence in re.split(r'(?<=\.) (?=[A-Z0-9])', objective):
            self.assertLessEqual(sentence.count(r'\textbf{'), 2, sentence)
        for item in re.findall(r'\\item (.*)', tex):
            self.assertLessEqual(item.count(r'\textbf{'), 2, item)
        self.assertIn(r'Built \textbf{Spring Boot} REST APIs', tex)
        self.assertNotIn(r'\textbf{REST APIs}', tex.split(r'\section*{Professional Work Experience}')[1],
                         'practices are not highlighted as technologies')
        # Bold changes typesetting only: the PDF text check reads the same words.
        self.assertTrue(matches_pdf_text(' '.join(expected_text(profile, 3)), profile, 3))

    def test_project_technologies_sit_beside_the_title(self):
        tex = render(resume(), years=3)
        self.assertIn(r'\textbf{Order Service} \textbar{} {\small\color{ink}Java, Spring Boot, MySQL}\par', tex)
        self.assertNotIn('Tech stack:', tex)

    def test_education_states_years_and_cgpa_only_above_eight(self):
        tex = render(resume(), years=3)
        self.assertIn(r'2018 -- 2022', tex)
        self.assertNotIn('CGPA 7.6', tex)
        tex = render(resume(education=[{'institution': 'Example Institute (fictional)', 'degree': 'B.E.',
                                        'start': '', 'end': '2022', 'details': 'Computer Science | CGPA 8.6'}]), years=3)
        self.assertIn('Computer Science | CGPA 8.6', tex)
        self.assertNotIn(' -- 2022', tex, 'a missing start year is never invented')
        for details, shown in (('CGPA 8.0', ''), ('CGPA: 9.1', 'CGPA: 9.1'), ('GPA 3.6/4', 'GPA 3.6/4'),
                               ('GPA 3.0 / 4', ''), ('Honours | CGPA 7.9', 'Honours')):
            self.assertEqual(education_details(details), shown, details)

    def test_without_confirmed_certifications_the_section_is_not_invented(self):
        tex = render(resume(certifications=[]), years=3)
        self.assertNotIn('Certifications', tex)


class ExperienceDepthTests(unittest.TestCase):
    def test_recent_jobs_get_seven_or_eight_lines_and_older_or_short_ones_fewer(self):
        self.assertEqual([draft_lines(rank, 'Software Engineer') for rank in range(5)], [8, 7, 5, 4, 4])
        self.assertEqual(draft_lines(0, 'Software Engineer Intern'), 4)
        self.assertEqual(draft_lines(1, 'Freelance Developer'), 4)

    def test_each_company_describes_different_work(self):
        facts = {'full_name': 'Nila', 'skills': ['Java'], 'education': [],
                 'experience': [{'company': 'A', 'position': 'Software Engineer', 'start': 'Jan 2023', 'end': 'Present',
                                 'location': '', 'bullets': []},
                                {'company': 'B', 'position': 'Software Engineer', 'start': 'Jan 2020',
                                 'end': 'Dec 2022', 'location': '', 'bullets': []}]}
        shared = 'Built Spring Boot REST APIs for order workflows with request validation and consistent error responses.'
        draft = {'roles': [{'index': 0, 'bullets': [
                     shared,
                     'Secured the admin endpoints with Spring Security filters and method rules for role-based access.',
                     'Published payment events to Kafka from Spring Boot services so billing updates asynchronously.']},
                           {'index': 1, 'bullets': [
                     shared.replace('Built', 'Developed'),
                     'Mapped invoice tables to entities with Spring Data JPA and Hibernate in the billing module.',
                     'Wrote JUnit and Mockito tests for the invoice services that run on every Maven build.',
                     'Cached customer lookups in Redis so the invoice screens avoid a database query each time.']}],
                 'skills': []}
        prompts = []
        with patch('resume_bot.generate.generate', lambda ai, prompt, **_: prompts.append(prompt) or draft):
            work, _ = draft_role_content(facts, role('Java Backend Developer'), 'Java Backend Developer', 6, [0, 1],
                                         False, object())
        self.assertIn('bullets_wanted', prompts[0])
        self.assertIn('Every job describes different work', prompts[0])
        lines = [line for job_lines in work.values() for line in job_lines]
        self.assertEqual(sum('order workflows' in line for line in lines), 1, 'never the same work at two companies')


class ProjectTests(unittest.TestCase):
    def proposal(self, stack, bullets):
        return {'projects': [{'name': 'Order Platform', 'problem': 'Fictional.', 'architecture': 'Services.',
                              'features': ['Orders'], 'responsibilities': 'Backend.', 'stack': stack,
                              'bullets': bullets}]}

    def test_alternatives_are_not_mixed_in_one_stack(self):
        self.assertEqual(conflicting_tools(['Java', 'Spring Boot', 'MySQL', 'PostgreSQL']), ['PostgreSQL'])
        self.assertEqual(conflicting_tools(['React.js', 'React Router', 'Redux', 'Redux Toolkit']), [])
        self.assertEqual(conflicting_tools(['Jenkins', 'GitHub Actions', 'Docker']), ['GitHub Actions'])
        self.assertEqual(conflicting_tools(['AWS', 'Amazon Web Services', 'Terraform']), [])
        bullets = ['Built order endpoints in Spring Boot with validated request bodies and clear error responses.',
                   'Stored orders in MySQL tables through Spring Data JPA repositories with paging for history.',
                   'Migrated reporting queries to PostgreSQL views so the finance team reads daily order totals.',
                   'Wrote JUnit tests with Mockito for the order services and the repository query methods.',
                   'Packaged the Spring Boot service with Maven profiles for local and staging configuration.',
                   'Documented the order endpoints with request and response examples for the support team.']
        target = role('Java Backend Developer')
        kept = _checked_proposals(self.proposal(['Java', 'Spring Boot', 'MySQL', 'PostgreSQL', 'JUnit', 'Mockito',
                                                 'Maven', 'Spring Data JPA'], bullets),
                                  {'skills': [], 'experience': [1]}, role_entry(target))
        self.assertEqual(len(kept), 1)
        self.assertNotIn('PostgreSQL', kept[0]['stack'])
        self.assertFalse(any('PostgreSQL' in b for b in kept[0]['bullets']))
        self.assertEqual(len(kept[0]['bullets']), 5, 'four to five bullets per project')

    def test_a_project_mostly_about_another_field_is_not_the_roles(self):
        from resume_bot.generate import named_tools, project_off_role, role_projects
        booking = ('Travel Booking API - Tech stack: FastAPI, PostgreSQL, Celery, Docker | Built booking endpoints '
                   'with FastAPI and PostgreSQL. | Packaged the service with Docker.')
        devops, python = role('DevOps Engineer'), role('Python Backend Developer')
        self.assertTrue(project_off_role(booking, named_tools(), devops), 'one DevOps tool does not make it DevOps work')
        self.assertEqual(role_projects({'projects': [booking]}, devops), [])
        self.assertEqual(role_projects({'projects': [booking]}, python), [booking])

    def test_project_bullets_never_repeat_the_experience(self):
        line = 'Wrote JUnit tests with Mockito for the order services and the repository query methods.'
        bullets = ['Built order endpoints in Spring Boot with validated request bodies and clear error responses.',
                   'Stored orders in MySQL tables through Spring Data JPA repositories with paging for history.',
                   line, 'Packaged the Spring Boot service with Maven profiles for local and staging configuration.']
        target = role('Java Backend Developer')
        kept = _checked_proposals(self.proposal(['Java', 'Spring Boot', 'MySQL', 'JUnit', 'Mockito', 'Maven',
                                                 'Spring Data JPA'], bullets),
                                  {'skills': [], 'experience': [1]}, role_entry(target), avoid=[line])
        self.assertNotIn(line, kept[0]['bullets'])


class CertificationTests(unittest.TestCase):
    def test_every_supported_role_has_relevant_recommendations(self):
        load_roles()
        self.assertEqual(set(ROLES) - set(RECOMMENDED), set(), 'every catalog role')
        for role_id in ROLES:
            names = recommended_certifications({'role': role_id})
            with self.subTest(role=role_id):
                self.assertTrue(3 <= len(names) <= 5, names)
                self.assertEqual(len(names), len(set(names)))
        self.assertIn('Google Cloud Professional Data Engineer', recommended_certifications({'role': 'gcp-data-engineer'}))
        custom = recommended_certifications({'role': 'custom', 'role_title': 'Snowflake Engineer',
                                             'keywords': ['AWS', 'Python']})
        self.assertIn('AWS Certified Solutions Architect - Associate', custom)
        owned = recommended_certifications({'role': 'gcp-data-engineer'}, ['Google Cloud Professional Data Engineer'])
        self.assertNotIn('Google Cloud Professional Data Engineer', owned, 'never recommends one already held')


class ObjectiveAcrossRolesTests(unittest.TestCase):
    def test_every_role_gets_a_defensible_four_line_objective(self):
        load_roles()
        for role_id, entry in ROLES.items():
            tools = core_terms({'role': role_id, 'keywords': entry['keywords']})[:3]
            lines = [f'Built a reporting module with {tools[0]} that validates each request before it is stored.',
                     f'Configured {tools[1 % len(tools)]} for the release checks that run on every merge request.',
                     f'Wrote automated tests around {tools[2 % len(tools)]} for the most used customer workflows.']
            facts = {'full_name': 'Nila', 'skills': entry['keywords'][:6], 'certifications': [], 'projects': [],
                     'experience': [{'company': 'Example Labs', 'position': 'Associate Engineer', 'start': 'Jan 2022',
                                     'end': 'Present', 'location': '', 'bullets': lines}],
                     'education': [{'institution': 'X', 'degree': 'B.Tech - Information Technology', 'start': '',
                                    'end': '2021', 'details': ''}]}
            text = composed_summary(facts, entry['name'], 4, entry['keywords'])
            with self.subTest(role=role_id):
                self.assertEqual(summary_problems(text, facts, entry['name'], 4), [], text)
                self.assertTrue(40 <= len(text.split()) <= 80, text)
                self.assertIn(entry['name'], text)


class SparseJobTests(unittest.TestCase):
    """A same-field job with only a couple of stated lines gets drafted additions to confirm; a job the candidate
    described in depth keeps exactly what they wrote."""
    LINES = ['Built payment APIs in Python and PostgreSQL.', 'Maintained REST services in Django.',
             'Wrote pytest suites for the payment API endpoints.', 'Added Celery tasks for nightly settlement files.',
             'Containerized the Django services with Docker for the staging setup.']

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(Path(self.temp.name), code_root=ROOT)
        self.jobs = AutomationJobs(self.workspace)
        self.jobs.start = lambda: None

    def questions(self, lines):
        candidate = {'full_name': 'Asha Example (fictional)', 'phone': '+91 9000000002', 'email': 'asha@example.com',
                     'skills': 'Python, Django, PostgreSQL, Git',
                     'work_experience': [{'company': 'Example Cloud (fictional)', 'position': 'Backend Engineer',
                                          'start': 'Jan 2022', 'end': 'Present', 'responsibilities': '\n'.join(lines)}],
                     'education': [{'degree': 'B.E. Computer Engineering', 'university': 'Example Institute (fictional)',
                                    'graduation_year': '2019'}]}
        ident = self.jobs.enqueue(OWNER, {'consent': CONSENT_VERSION, 'role': 'Python Backend Developer',
                                          'candidate': candidate})
        draft = {'roles': [{'index': 0, 'bullets': [
            'Built FastAPI endpoints in Python for refund requests with Pydantic validation and clear error messages.',
            'Added Redis caching in front of the Django account lookups so repeated reads skip the database.',
            'Wrote Django REST Framework serializers for invoice exports used by the finance reporting team.']}],
            'skills': []}
        with closing(self.jobs.connection()) as db:
            row = db.execute('SELECT * FROM automation WHERE id=?', (ident,)).fetchone()
        with patch('resume_bot.generate.generate', lambda ai, prompt, **_: draft if prompt.startswith('Draft') else
                   {'projects': []}), patch('resume_bot.automation.generate', return_value={}), \
                patch.object(self.workspace, 'enqueue', return_value={'name': f'private-{OWNER}-{"7" * 64}.json'}):
            self.jobs._prepare(row)
        return self.jobs.list(OWNER, ident)[0].get('questions') or []

    def test_two_stated_lines_get_drafted_additions(self):
        work = next(q for q in self.questions(self.LINES[:2]) if q['field'] == 'bullets')
        self.assertTrue(work['drafted'])
        self.assertEqual(work['value'].split('\n')[:2], self.LINES[:2], "the candidate's own lines come first")
        self.assertEqual(len(work['value'].split('\n')), 5)

    def test_a_job_described_in_depth_keeps_what_the_candidate_wrote(self):
        self.assertFalse([q for q in self.questions(self.LINES) if q['field'] == 'bullets'])


class ReviewTests(unittest.TestCase):
    """A certification is printed only when the candidate confirms they completed it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(Path(self.temp.name), code_root=ROOT)
        self.jobs = AutomationJobs(self.workspace)
        self.jobs.start = lambda: None
        candidate = {'full_name': 'Arun Kumar (fictional)', 'phone': '+91 9000000000', 'email': 'arun.kumar@example.com',
                     'skills': 'React.js, TypeScript, Git',
                     'work_experience': [{'company': 'DemoTech Solutions Pvt Ltd (fictional)', 'position': 'Frontend Developer',
                                          'start': 'July 2023', 'end': 'Present'}],
                     'education': [{'degree': 'B.Tech - Information Technology', 'university': 'Example Institute (fictional)',
                                    'graduation_year': '2022'}]}
        self.id = self.jobs.enqueue(OWNER, {'consent': CONSENT_VERSION, 'role': 'GCP Data Engineer', 'candidate': candidate})

    def row(self):
        with closing(self.jobs.connection()) as db:
            return db.execute('SELECT * FROM automation WHERE owner=? AND id=?', (OWNER, self.id)).fetchone()

    def prepare(self):
        with patch('resume_bot.generate.generate', GCP_MODEL()), patch('resume_bot.automation.generate', return_value={}), \
                patch.object(self.workspace, 'enqueue', return_value={'name': f'private-{OWNER}-{"6" * 64}.json'}) as enqueue:
            self.jobs._prepare(self.row())
        return enqueue

    def confirm(self, certifications):
        self.prepare()
        job = self.jobs.list(OWNER)[0]
        self.assertIn('Google Cloud Professional Data Engineer', job['certifications'], 'recommended, unticked')
        decisions = [{'include': index == 0} for index, _ in enumerate(job['proposals'])]
        self.jobs.answer(OWNER, self.id, {q['id']: q['value'] for q in job['questions']}, decisions or None,
                         certifications)
        enqueue = self.prepare()
        self.assertEqual(self.row()['status'], 'handed_off', self.row()['error'])
        return json.loads(enqueue.call_args.args[0]), enqueue.call_args.kwargs['approval']

    def test_ticked_and_typed_certifications_are_printed_and_verified(self):
        packet, approval = self.confirm(['Google Cloud Professional Data Engineer', 'Udemy: Apache Beam Basics'])
        profile = packet['profile']
        self.assertEqual(profile['certifications'], ['Google Cloud Professional Data Engineer', 'Udemy: Apache Beam Basics'])
        self.assertTrue(verified_automation(profile, packet['target'], approval))
        tex = render(profile, years=3)
        self.assertEqual([tex.index(r'\section*{' + s + '}') for s in SECTIONS],
                         sorted(tex.index(r'\section*{' + s + '}') for s in SECTIONS), 'all six sections, in order')
        notes = json.loads(self.row()['notice'])
        self.assertIn('Confirmed by you: certification "Google Cloud Professional Data Engineer".', notes)
        report = analyze(profile, {**packet['target']})
        self.assertNotIn('Google Cloud Professional Data Engineer', report['recommended_certifications'])

    def test_unticked_recommendations_are_never_printed(self):
        packet, _ = self.confirm(None)
        self.assertEqual(packet['profile'].get('certifications') or [], [])

    def test_certification_answers_are_bounded(self):
        self.prepare()
        job = self.jobs.list(OWNER)[0]
        with self.assertRaises(AnswerError):
            self.jobs.answer(OWNER, self.id, {q['id']: q['value'] for q in job['questions']},
                             [{'include': False} for _ in job['proposals']] or None, ['Cert'] * 9)


if __name__ == '__main__':
    unittest.main()
