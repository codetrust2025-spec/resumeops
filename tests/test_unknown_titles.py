"""A target title the catalog does not list still gets role keywords: its own catalog role (Software Tester, Data
Engineer, Software Engineer) or the nearest one. Seen live: a "Software Tester" resume got no keywords, so it scored 0
for keyword coverage and skills alignment and every skill landed in "Other skills"."""
import unittest

from resume_bot.ai_review import nearest_role, role_intelligence
from resume_bot.generate import named_tools, skill_groups
from resume_bot.profile import validate_profile
from resume_bot.workflow import ROLES, analyze, load_roles

COMMON_TITLES = ['Software Engineer', 'Senior Software Engineer', 'Software Developer', 'Software Tester',
                 'Manual Tester', 'QA Engineer', 'Test Engineer', 'QA Analyst', 'Quality Assurance Engineer',
                 'Software Test Engineer', 'Data Engineer', 'Big Data Engineer', 'ETL Developer', 'Business Analyst',
                 'Power BI Developer', 'Database Administrator', 'PHP Developer', 'Golang Developer',
                 'Spring Boot Developer', 'Python Full Stack Developer', 'System Administrator', 'Network Engineer',
                 'Technical Support Engineer', 'UI/UX Designer']
# Outside technology: no catalog terms are assumed (a custom title's own market sources only).
NON_TECHNICAL = ['Warehouse Planning Manager', 'Scrum Master', 'Project Manager', 'Quality Control Manager',
                 'Mobile Sales Executive', 'Customer Service Engineer']


def tester_profile():
    return validate_profile({
        'full_name': 'Example Applicant', 'phone': '+91 90000 00021', 'email': 'tester@example.test',
        'skills': ['Manual Testing', 'Functional Testing', 'Regression Testing', 'Test Case Design', 'Postman',
                   'API Testing', 'SQL', 'JIRA', 'TestRail', 'Agile', 'STLC', 'Defect Tracking'],
        'experience': [{'position': 'Software Tester', 'company': 'Example Works', 'start': 'Jan 2022', 'end': 'Present',
                        'bullets': ['Wrote test cases and ran regression testing for the loan application portal.',
                                    'Checked REST API responses in Postman and logged defects in JIRA.',
                                    'Validated report data with SQL queries before each release.']}],
        'education': [{'degree': 'B.Tech', 'institution': 'Example University', 'end': '2021'}]})


class UnknownTitleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_roles(force=True)

    def test_no_common_title_is_left_without_keywords(self):
        for title in COMMON_TITLES:
            with self.subTest(title=title):
                intelligence = role_intelligence(title)
                self.assertGreaterEqual(len(intelligence['terms']), 10, title)
                if intelligence['role'] == 'custom':
                    self.assertEqual(intelligence['label'], title, 'a related title keeps its own headline')

    def test_software_tester_is_a_catalog_role_for_manual_and_api_testing(self):
        intelligence = role_intelligence('Software Tester')
        self.assertEqual((intelligence['role'], intelligence['label']), ('software-tester', 'Software Tester'))
        for term in ('Manual Testing', 'Test Case Design', 'API Testing', 'Postman', 'SQL', 'JIRA', 'Agile'):
            self.assertIn(term, intelligence['terms'])
        for title in ('Manual Tester', 'Software Test Engineer'):
            self.assertEqual(role_intelligence(title)['role'], 'software-tester', title)
        self.assertEqual(role_intelligence('Data Engineer')['role'], 'data-engineer')
        self.assertEqual(role_intelligence('Software Engineer')['role'], 'software-engineer')

    def test_related_titles_take_the_nearest_role_keywords(self):
        expected = {'QA Engineer': 'software-tester', 'Test Engineer': 'software-tester',
                    'Lead QA Automation Engineer': 'automation-tester', 'Senior SDET': 'automation-tester',
                    'Senior Software Engineer': 'software-engineer', 'Software Developer': 'software-engineer',
                    'Big Data Engineer': 'data-engineer', 'ETL Developer': 'data-engineer',
                    'Senior Azure Data Engineer': 'azure-data-engineer', 'Azure DevOps Engineer': 'azure-cloud-engineer',
                    'Python Full Stack Developer': 'fullstack-developer', 'Business Analyst': 'data-analyst',
                    'JavaScript Developer': 'frontend-developer', 'UI/UX Designer': 'ui-developer',
                    'Golang Developer': 'backend-developer', 'Embedded Software Engineer': 'software-engineer'}
        for title, role in expected.items():
            with self.subTest(title=title):
                self.assertEqual(nearest_role(title), role)
        intelligence = role_intelligence('QA Engineer')
        self.assertEqual(intelligence['role'], 'custom')
        self.assertEqual(intelligence['terms'], ROLES['software-tester']['keywords'][:40])
        self.assertIn('nearest role Software Tester', intelligence['sources'][-1]['label'])

    def test_a_title_outside_technology_assumes_no_catalog_terms(self):
        for title in NON_TECHNICAL:
            with self.subTest(title=title):
                self.assertIsNone(nearest_role(title))
                intelligence = role_intelligence(title)
                self.assertEqual((intelligence['role'], intelligence['label'], intelligence['terms']), ('custom', title, []))

    def test_a_software_tester_resume_scores_keywords_and_skill_areas(self):
        profile = tester_profile()
        intelligence = role_intelligence('Software Tester')
        target = {'role': intelligence['role'], 'template': 'standard', 'keywords': intelligence['terms']}
        report = analyze(profile, target)
        scores = {c['key']: c['points'] for c in report['categories']}
        self.assertGreaterEqual(len(report['matched']), 10, report['matched'])
        self.assertGreater(scores['keyword_coverage'], 10)
        self.assertEqual(scores['skills_alignment'], 15)
        groups = skill_groups(profile['skills'], ROLES['software-tester'])
        names = [g['name'] for g in groups]
        self.assertEqual(names[:3], ['Testing Types', 'Test Design', 'Defect Management'])
        role = ROLES['software-tester']
        self.assertTrue(set(names) <= set(role['resumeGroups']) | set(role['groups']), names)
        self.assertNotIn('Other skills', [g['name'] for g in groups])

    def test_testing_practices_are_not_named_tools(self):
        tools = named_tools()
        for practice in ('Regression Testing', 'Test Case Design', 'Agile', 'STLC', 'Manual Testing'):
            self.assertNotIn(practice, tools)
        self.assertIn('TestRail', tools)


if __name__ == '__main__':
    unittest.main()
