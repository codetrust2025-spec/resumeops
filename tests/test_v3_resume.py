"""ResumeOps V3: role catalog, role-based generation, projects, explainable ATS, formatting, workflow."""
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from resume_bot import workflow
from resume_bot.ai_review import role_intelligence
from resume_bot.automation import CONSENT_VERSION, verified_automation, verified_request
from resume_bot.generate import (clean_bullet, composed_summary, confirmed_project, propose_projects, skill_groups,
                                 objective_problems, split_project, summary_problems, title_problems, write_summary)
from resume_bot.latex import render
from resume_bot.profile import validate_profile
from resume_bot.workflow import ROLES, analyze, load_roles, matched_terms, validate_catalog

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/java_backend_fictional.json').read_text(encoding='utf-8'))
TOOLS = all(shutil.which(tool) for tool in ('pdflatex', 'pdftotext', 'pdfinfo'))
VISIBLE = ['UI Developer', 'Frontend Developer', 'React.js Developer', 'Angular Developer', 'Java Backend Developer',
           'Python Backend Developer', 'Node.js Developer', 'Java Full Stack Developer', 'MERN Stack Developer',
           '.NET Developer', 'DevOps Engineer', 'AWS Cloud Engineer', 'Azure Cloud Engineer', 'GCP Cloud Engineer',
           'Data Engineer', 'Azure Data Engineer', 'GCP Data Engineer', 'Data Analyst', 'Data Scientist',
           'AI / ML Engineer', 'Generative AI Engineer', 'QA Automation Engineer', 'Software Tester',
           'Salesforce Developer', 'Mobile App Developer', 'Android Developer', 'iOS Developer', 'Full Stack Developer',
           'Software Engineer']


def facts():
    profile = copy.deepcopy(FIXTURE)
    for job in profile['experience']:
        job['bullets'] = [clean_bullet(b) for b in job['bullets']]
    return validate_profile(profile)


class NoModel:
    """Ollama is unavailable: generation must fall back to composed, fact-only content."""
    model = 'none'

    def generate(self, *args, **kwargs):
        raise RuntimeError('no model')


class CatalogTests(unittest.TestCase):
    def test_every_requested_role_is_offered(self):
        load_roles(force=True)
        self.assertEqual([r['name'] for r in ROLES.values() if not r['hidden']], VISIBLE)
        # Full Stack Developer is offered again (requested): generic full-stack applicants did not fit Java or MERN.
        for legacy in ('backend-developer', 'cloud-engineer'):
            self.assertTrue(ROLES[legacy]['hidden'], 'saved jobs keep rendering with retired roles')

    def test_operator_catalog_adds_replaces_and_hides_without_code(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'role-catalog.json'
            with patch.dict(os.environ, {workflow.CATALOG_ENV: str(path)}):
                path.write_text(json.dumps({'roles': [
                    {'id': 'kotlin-backend-developer', 'name': 'Kotlin Backend Developer', 'groups': {'Languages': ['Kotlin']}},
                    {'id': 'data-analyst', 'name': 'Data Analyst', 'hidden': True, 'groups': {'Tools': ['Excel']}}]}))
                load_roles(force=True)
                self.assertEqual(ROLES['kotlin-backend-developer']['keywords'], ['Kotlin'])
                self.assertTrue(ROLES['data-analyst']['hidden'])
                self.assertEqual(workflow.CATALOG_STATUS['source'], 'bundled+role-catalog.json')
                path.write_text('{"roles": [{"id": "Bad Id", "name": "x"}]}')
                load_roles(force=True)
                self.assertNotIn('kotlin-backend-developer', ROLES, 'an invalid file falls back to the bundled catalog')
                self.assertIn('ignored', workflow.CATALOG_STATUS['error'])
        load_roles(force=True)

    def test_catalog_validation_names_the_problem(self):
        for bad, message in (({'roles': 'x'}, 'roles'), ({'roles': [{'id': 'ok-id', 'name': 'X'}]}, 'skill group'),
                             ({'roles': [{'id': 'ok-id', 'name': 'X', 'groups': {'A': ['x' * 70]}}]}, 'up to 60')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_catalog(bad)


class RoleKeywordTests(unittest.TestCase):
    def test_catalog_roles_use_curated_role_keywords(self):
        java = role_intelligence('Java Backend Developer')
        self.assertEqual(java['role'], 'java-backend-developer')
        self.assertIn('Spring Boot', java['terms'])
        for unrelated in ('Node.js', 'Django', 'Flask', 'Express.js'):
            self.assertNotIn(unrelated, java['terms'])
        self.assertEqual(role_intelligence('Java Developer')['role'], 'java-backend-developer', 'title variations map')

    def test_keyword_matching_understands_aliases_without_false_positives(self):
        text = 'Java 17, REST API, MySQL, Node.js, Azure Data Factory, React'
        self.assertEqual(matched_terms(['Java', 'REST APIs', 'JavaScript', 'SQL', 'Node.js', 'React.js', 'Microsoft Azure'], text),
                         ['Java', 'REST APIs', 'Node.js', 'React.js', 'Microsoft Azure'])


class AtsTests(unittest.TestCase):
    def target(self, **extra):
        return {'role': 'java-backend-developer', 'template': 'standard', 'keywords': [], 'experience_years': 3,
                'automation_owner': 'a' * 64, **extra}

    def test_java_regression_profile_scores_on_real_skills_only(self):
        profile = facts()
        profile['headline'] = 'Java Backend Developer'
        profile['summary'] = composed_summary(profile, 'Java Backend Developer', 3, ROLES['java-backend-developer']['keywords'])
        before = json.dumps(profile, sort_keys=True)
        report = analyze(profile, self.target())
        self.assertEqual(json.dumps(profile, sort_keys=True), before, 'scoring never edits the resume')
        self.assertEqual(sum(c['max'] for c in report['categories']), 100)
        self.assertEqual(len(report['categories']), 8)
        for skill in ('Java', 'Spring Boot', 'Hibernate', 'JUnit', 'Mockito', 'REST APIs'):
            self.assertIn(skill, report['matched'])
        self.assertGreater(report['ats_readiness'], 60, 'the earlier 60/100 came from the wrong keyword set')
        self.assertEqual(next(c for c in report['categories'] if c['key'] == 'project_relevance')['points'], 0)
        self.assertTrue(set(report['suggested_skills']).isdisjoint(profile['skills']))
        self.assertIn('Add a project: a real one, or confirm one of the suggested proposals.', report['recommendations'])

    def test_inflated_experience_level_costs_points_and_says_why(self):
        honest = analyze(facts(), self.target())['ats_readiness']
        inflated = analyze(facts(), self.target(experience_years=12))
        self.assertLess(inflated['ats_readiness'], honest)
        self.assertTrue(any('dated roles show 3 years' in tip for tip in inflated['recommendations']))


class SummaryTests(unittest.TestCase):
    def test_checks_reject_unsupported_claims(self):
        profile, title = facts(), 'Java Backend Developer'
        good = ('Java Backend Developer with 3 years of professional experience building Spring Boot REST APIs on '
                'PostgreSQL, tested with JUnit 5 and Mockito.')
        self.assertEqual(summary_problems(good, profile, title, 3), [])
        for bad, reason in ((good.replace('PostgreSQL', 'Kafka'), 'technology'), (good + ' Cut latency by 40%.', 'metric'),
                            ('I am a ' + good, 'I, my'), (good.replace('3 years', '5 years'), '3 years'),
                            ('Results-driven ' + good, 'generic')):
            with self.subTest(reason=reason):
                self.assertTrue(any(reason in p for p in summary_problems(bad, profile, title, 3)))

    def test_role_title_is_not_evidence_for_a_career_change(self):
        profile = facts()
        profile['skills'] = ['Selenium WebDriver', 'TestNG']
        profile['experience'][0].update(position='QA Engineer', bullets=['Automated regression tests with Selenium WebDriver.'])
        text = 'QA Engineer with 3 years of professional experience, now moving into a Java Backend Developer role using Java.'
        self.assertTrue(any('Java' in p for p in summary_problems(text, profile, 'Java Backend Developer', 3)))
        composed = composed_summary(profile, 'Java Backend Developer', 3, ROLES['java-backend-developer']['keywords'])
        self.assertIn('moving into a Java Backend Developer role', composed)
        self.assertEqual(summary_problems(composed, profile, 'Java Backend Developer', 3), [])

    def test_unavailable_model_falls_back_to_facts(self):
        text, how = write_summary(facts(), 'Java Backend Developer', 3, ROLES['java-backend-developer']['keywords'], NoModel())
        self.assertEqual(how, 'composed')
        self.assertEqual(objective_problems(text, facts(), 'Java Backend Developer'), [])

    def test_every_role_gets_role_specific_content_from_its_own_skills(self):
        for name in VISIBLE:
            role = next(r for r in ROLES.values() if r['name'] == name)
            with self.subTest(role=name):
                profile = facts()
                skills = [items[0] for items in role['groups'].values()][:6]
                profile['skills'] = list(dict.fromkeys(skills))
                profile['experience'][0].update(position=name, bullets=[f'Worked with {profile["skills"][0]} on internal tools.'])
                summary = composed_summary(profile, name, 3, role['keywords'])
                self.assertEqual(summary_problems(summary, profile, name, 3), [])
                groups = skill_groups(profile['skills'], role)
                self.assertEqual(sorted(i for g in groups for i in g['items']), sorted(profile['skills']))
                self.assertIn(groups[0]['name'], role['resumeGroups'])  # the role's recruiter-style categories
                profile.update(headline=name, summary=summary, skill_groups=groups)
                report = analyze(validate_profile(profile), {'role': role['id'], 'template': 'standard', 'keywords': [],
                                                             'experience_years': 3, 'automation_owner': 'a' * 64})
                self.assertEqual(next(c for c in report['categories'] if c['key'] == 'role_relevance')['points'], 15)


class ProjectTests(unittest.TestCase):
    class Proposer:
        model = 'fake'

        def __init__(self, data):
            self.data = data

        def generate(self, prompt, schema=None, on_progress=None):
            return self.data

    def test_proposals_are_filtered_for_defensible_content(self):
        good = {'name': 'Order Service', 'problem': 'Track customer orders.', 'stack': ['Java 17', 'Spring Boot', 'Quantum DB'],
                'architecture': 'REST service with PostgreSQL.', 'features': ['Create orders'], 'responsibilities': 'Design and tests.',
                'bullets': ['Designed REST endpoints for creating and tracking orders.', 'Cut response time by 40% with caching.',
                            'Mapped entities with Spring Data JPA and PostgreSQL.', 'Wrote JUnit 5 tests for the service layer.']}
        metric_only = {**good, 'name': 'Bad', 'bullets': ['Served 10,000 users in production.'] * 3}
        bad_security = {**good, 'name': 'Bad Security Design', 'bullets': [
            'Hashed user passwords using PostgreSQL and Spring Boot for account authentication.',
            'Designed REST endpoints for account registration and login workflows.',
            'Wrote JUnit 5 tests for authentication and account service behavior.']}
        projects = propose_projects(facts(), {'role': 'java-backend-developer'}, 'Java Backend Developer', 3,
                                    self.Proposer({'projects': [good, metric_only, bad_security, 'not an object']}))
        self.assertEqual([p['name'] for p in projects], ['Order Service'])
        self.assertNotIn('Quantum DB', projects[0]['stack'])
        self.assertFalse(any('40%' in b for b in projects[0]['bullets']))
        self.assertFalse(any('Bad Security Design' == p['name'] for p in projects),
                         'a database must not be presented as a password-hashing algorithm')

    def test_confirmed_project_format_round_trips(self):
        text = confirmed_project('Order Service', ['Java 17', 'Spring Boot'], ['- Designed REST endpoints.', 'Wrote tests.'])
        self.assertEqual(text, 'Order Service - Tech stack: Java 17, Spring Boot | Designed REST endpoints. | Wrote tests.')
        self.assertEqual(split_project(text), ('Order Service', ['Java 17', 'Spring Boot'], ['Designed REST endpoints.', 'Wrote tests.']))
        with self.assertRaises(ValueError):
            confirmed_project('', [], ['x'])


class RenderTests(unittest.TestCase):
    def v3_profile(self):
        profile = facts()
        role = ROLES['java-backend-developer']
        profile['headline'] = role['name']
        profile['summary'] = composed_summary(profile, role['name'], 3, role['keywords'])
        profile['skill_groups'] = skill_groups(profile['skills'], role)
        profile['projects'] = [confirmed_project('Order Service', ['Java 17', 'Spring Boot'],
                                                 ['Designed REST endpoints.', 'Wrote JUnit 5 tests.'])]
        return validate_profile(profile)

    def test_no_duplicate_markers_grouped_skills_and_structured_projects(self):
        profile = facts()
        profile['experience'][0]['bullets'] = FIXTURE['experience'][0]['bullets']  # "- Built ..." as uploaded
        tex = render(validate_profile(profile), years=3)
        self.assertNotIn(r'\item - ', tex)
        tex = render(self.v3_profile(), years=3)
        self.assertIn(r'\textbf{Backend Frameworks:} Spring Boot', tex)
        # The technologies sit beside the project title; bullets highlight them; the six-section headings.
        self.assertIn(r'\textbf{Order Service} \textbar{} {\small\color{ink}Java 17, Spring Boot}', tex)
        self.assertIn(r'\item Wrote \textbf{JUnit 5} tests.', tex)
        self.assertIn(r'\section*{Career Objective}', tex)
        self.assertNotIn('Target role:', tex)

    @unittest.skipUnless(TOOLS, 'TeX/PDF tools missing')
    def test_page_budget_and_exact_pdf_text(self):
        from resume_bot.claim_audit import matches_pdf_text
        from resume_bot.compile import compile_latex, verify_pdf
        from resume_bot.ingest import read_resume_text
        short = self.v3_profile()
        long = copy.deepcopy(short)
        long['experience'] = [{**long['experience'][0], 'company': f'Example Company {i} (fictional)',
                               'bullets': [f'Maintained Spring Boot service {i}.{n} with PostgreSQL and JUnit 5 tests.'
                                           for n in range(6)]} for i in range(7)]
        for profile, pages in ((short, 1), (validate_profile(long), 2)):
            with tempfile.TemporaryDirectory() as temp:
                (Path(temp) / 'main.tex').write_text(render(profile, years=3), encoding='utf-8')
                pdf = compile_latex(Path(temp))
                self.assertEqual(verify_pdf(pdf, profile, 2)['pages'], pages)
                text = read_resume_text(pdf)
                self.assertTrue(matches_pdf_text(text, profile, 3))
                self.assertFalse(any(line.strip().startswith('• -') for line in text.splitlines()))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / 'dashboard'))
        from automation_jobs import AutomationJobs
        from resume_dashboard import Workspace
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Workspace(Path(self.temp.name), code_root=ROOT)
        self.jobs = AutomationJobs(self.workspace)
        self.enqueued = []
        candidate = {'full_name': 'Arun Sample', 'email': 'arun.sample@example.com', 'phone': '+91 90000 00002',
                     'years_of_experience': 3,
                     'skills': ', '.join(FIXTURE['skills']),
                     'work_experience': [{'company': 'DemoByte Technologies (fictional)', 'position': 'Java Backend Developer',
                                          'start': 'July 2023', 'end': 'October 2026', 'location': '',
                                          'responsibilities': '\n'.join(FIXTURE['experience'][0]['bullets'])}],
                     'education': [{'university': 'Example Institute of Technology (fictional)', 'degree': 'B.Tech',
                                    'specialization': 'Information Technology', 'graduation_year': '2023', 'cgpa': ''}]}
        with patch.object(self.jobs, 'start'):
            self.id = self.jobs.enqueue('a' * 64, {'role': 'Java Backend Developer', 'consent': CONSENT_VERSION,
                                                   'candidate': candidate})

    def tearDown(self):
        self.temp.cleanup()

    def row(self, identifier=None):
        with closing(self.jobs.connection()) as db:
            return db.execute('SELECT * FROM automation WHERE id=?', (identifier or self.id,)).fetchone()

    def prepare(self, identifier=None, proposals=None):
        proposer = ProjectTests.Proposer({'projects': proposals or []})
        with patch('resume_bot.automation.generate', return_value={}), \
                patch('resume_bot.generate.generate', side_effect=lambda ai, prompt, **k: proposer.generate(prompt)
                      if 'portfolio projects' in prompt else (_ for _ in ()).throw(RuntimeError('no model'))), \
                patch.object(self.workspace, 'enqueue', side_effect=self.capture):
            self.jobs._prepare(self.row(identifier))

    def capture(self, data, approval=None):
        self.enqueued.append((json.loads(data), approval))
        return {'name': f'private-{"a" * 64}-{len(self.enqueued):064x}.json'}

    def proposal(self, name):
        return {'name': name, 'problem': 'Track orders.', 'stack': ['Java 17', 'Spring Boot', 'PostgreSQL'],
                'architecture': 'REST service.', 'features': ['Orders'], 'responsibilities': 'API and tests.',
                'bullets': ['Designed REST endpoints for creating and tracking customer orders with Spring Boot.',
                            'Mapped order and payment entities to PostgreSQL tables with Spring Data JPA.',
                            'Wrote JUnit 5 tests for the order services and the payment validation rules.']}

    def test_projects_pause_for_review_and_only_confirmed_ones_are_published(self):
        self.prepare(proposals=[self.proposal('Order Service'), self.proposal('Inventory Service')])
        row = self.row()
        self.assertEqual((row['status'], row['stage'], row['percent']), ('needs_input', 'Review suggested projects', 60))
        self.assertEqual(self.enqueued, [], 'nothing is published while proposals await review')
        listed = self.jobs.list('a' * 64)[0]
        self.assertEqual([p['name'] for p in listed['proposals']], ['Order Service', 'Inventory Service'])
        self.assertEqual((listed['candidate'], listed['role']), ('Arun Sample', 'Java Backend Developer'))
        with patch.object(self.jobs, 'start'):
            self.jobs.decide_projects('a' * 64, self.id, [
                {'include': True, 'name': 'Order Service', 'stack': 'Java 17, Spring Boot',
                 'bullets': 'Designed REST endpoints for orders.\nWrote JUnit 5 tests for services.'},
                {'include': False}])
        self.prepare()
        packet, approval = self.enqueued[-1]
        projects = packet['profile']['projects']
        self.assertEqual([split_project(p)[0] for p in projects], ['Order Service'])
        self.assertNotIn('Inventory Service', json.dumps(packet))
        self.assertEqual(title_problems(packet['profile']['headline'], 'Java Backend Developer',
                                        packet['profile']['skills']), [])
        self.assertTrue(all(not b.startswith('- ') for job in packet['profile']['experience'] for b in job['bullets']))
        self.assertEqual(self.row()['percent'], 65)
        notes = self.jobs.list('a' * 64)[0]['notice']
        self.assertIn('Confirmed by you: project "Order Service".', notes)
        self.assertIn('Not included: suggested project "Inventory Service".', notes)
        target = {**packet['target'], 'automation_owner': 'a' * 64, 'automation_id': self.id}
        self.assertTrue(verified_automation(packet['profile'], target, approval))
        for tamper in ('summary', 'skills', 'projects'):
            with self.subTest(tamper=tamper):
                changed = copy.deepcopy(packet['profile'])
                changed[tamper] = changed[tamper] + (' Led a team of 10.' if tamper == 'summary' else ['Kubernetes'])
                self.assertFalse(verified_automation(changed, target, approval))

    def test_regeneration_reuses_facts_for_another_role(self):
        self.prepare(proposals=[])
        self.assertEqual(self.row()['status'], 'handed_off')
        facts_before = json.loads(self.row()['facts'])
        with patch.object(self.jobs, 'start'), patch.object(self.jobs, '_status', side_effect=lambda r, s, o: {**dict(r), 'status': 'complete'}):
            new_id = self.jobs.regenerate('a' * 64, self.id, 'Java Full Stack Developer')
        self.prepare(new_id, proposals=[])
        packet, _ = self.enqueued[-1]
        self.assertEqual(title_problems(packet['profile']['headline'], 'Java Full Stack Developer',
                                        packet['profile']['skills']), [])
        self.assertEqual([j['company'] for j in packet['profile']['experience']], [j['company'] for j in facts_before['experience']])
        self.assertEqual(sorted(packet['profile']['skills']), sorted(facts_before['skills']))
        with patch.object(self.jobs, 'start'):
            self.assertEqual(self.jobs.regenerate('a' * 64, self.id, 'Java Full Stack Developer'), new_id, 'no duplicates')

    def test_watcher_progress_uses_the_published_bands(self):
        for key, percent, expected in (('checks', 46, 68), ('latex', 70, 85), ('pdf', 71, 85), ('overleaf', 92, 95), ('verify', 99, 99)):
            self.assertEqual(self.jobs._watcher_percent({'stage_key': key, 'percent': percent}), expected)


class PageTests(unittest.TestCase):
    def test_role_picker_project_review_history_and_mobile(self):
        try:
            from test_extraction_recovery import browser_or_skip
        except ImportError:
            from tests.test_extraction_recovery import browser_or_skip
        sys.path.insert(0, str(ROOT / 'dashboard'))
        from automation_jobs import AutomationJobs
        from resume_bot.pipeline import CODE
        from resume_bot.storage import write_json
        from resume_dashboard import Workspace, handler
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(Path(temp), code_root=ROOT)
            write_json(workspace.inbox / '.resume-bot-health.json', {'last_scan': time.time(), 'code': CODE})
            jobs = AutomationJobs(workspace)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(workspace, '', 't'))
            origin = f'http://127.0.0.1:{server.server_port}'
            server.RequestHandlerClass = handler(workspace, origin, 't', automation_first=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            owner = hashlib.sha256((origin + '\0loopback').encode()).hexdigest()
            proposals = [WorkflowTests.proposal(None, 'Order Service'), WorkflowTests.proposal(None, 'Inventory Service')]
            now = time.time()
            with closing(jobs.connection()) as db, db:
                db.execute("INSERT INTO automation(owner,id,payload,status,stage,percent,created,updated,error,checkpoints,role,candidate) "
                           "VALUES (?,?,?,'needs_input','Review suggested projects',60,?,?,?,?,?,?)",
                           (owner, 'd' * 64, '{}', now, now, '2 project suggestions are ready.',
                            json.dumps({'proposals': proposals}), 'Java Backend Developer', 'Arun Sample'))
            playwright, browser = browser_or_skip(self)
            try:
                page = browser.new_page(**playwright.devices['iPhone 13'])
                with patch('automation_jobs.AutomationJobs.start'):
                    page.goto(origin)
                    options = page.locator('#auto-role-options option')
                    options.first.wait_for(state='attached')
                    self.assertEqual(options.count(), len(VISIBLE) + 1)
                    self.assertEqual(options.last.get_attribute('value'), 'Custom Role')
                    page.get_by_label('Target role').fill('Custom Role')
                    self.assertTrue(page.locator('#auto-custom-wrap').is_visible())
                    page.get_by_label('Target role').fill('Java Backend Developer')
                    self.assertTrue(page.locator('#auto-custom-wrap').is_hidden())
                    item = page.locator('#auto-job-list li[data-status="needs_input"]')
                    item.wait_for()
                    self.assertIn('Arun Sample', item.locator('.auto-name').inner_text())
                    self.assertIn('Java Backend Developer', item.locator('.auto-role').inner_text())
                    self.assertIn('unconfirmed AI draft', item.inner_text())
                    cards = item.locator('.auto-proposal')
                    # Like the drafted work lines, each suggestion starts included; its background stays folded.
                    for index in range(2):
                        self.assertTrue(cards.nth(index).get_by_label('I built this: include it').is_checked())
                        self.assertTrue(cards.nth(index).locator('dl').is_hidden(), 'background folded on a phone')
                    cards.nth(0).get_by_text('About this project').click()
                    self.assertTrue(cards.nth(0).locator('dl').is_visible())
                    cards.nth(0).locator('[name=name]').fill('Order Tracking Service')
                    cards.nth(1).get_by_label('Skip').check()
                    with page.expect_response(lambda r: r.url.endswith('/api/automation/projects')) as saved:
                        item.get_by_role('button', name='Save project choices').click()
                    self.assertEqual(saved.value.status, 200)
                    with closing(jobs.connection()) as db:
                        checkpoints = json.loads(db.execute('SELECT checkpoints FROM automation').fetchone()[0])
                    self.assertEqual([split_project(p)[0] for p in checkpoints['confirmed_projects']], ['Order Tracking Service'])
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'no sideways scroll on iPhone')
            finally:
                browser.close()
                playwright.stop()
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
