"""Advanced resume generation rules, applied centrally for every catalog role:

* Technical Skills grouped in recruiter-style categories (the Frontend Developer reference: Frontend Technologies,
  UI Frameworks & Styling, State Management, ... AI-Powered Development Tools), from each role's catalog;
* a skills review that offers the role's whole catalog unticked, so the section can cover what the role's job
  descriptions ask for, while only skills the candidate ticks are ever listed;
* a resume title built from the role and the candidate's leading listed technologies;
* a standards report (six sections, title, keyword coverage, categorized skills, experience depth and variety,
  projects, certifications, education, consistency, ATS, document) that marks a resume verified only when every
  applicable check passes.

All candidates are fictional."""
import re
import unittest

from resume_bot.generate import (SKILL_BREADTH, TITLE_SKILLS, catalog_skill_suggestions, resume_title, skill_groups,
                                 title_problems)
from resume_bot.latex import render
from resume_bot.profile import validate_profile
from resume_bot.standards import standards_report
from resume_bot.workflow import ROLES, load_roles, validate_catalog

FRONTEND_REFERENCE = ['Frontend Technologies', 'UI Frameworks & Styling', 'State Management',
                      'Responsive & Accessibility', 'API Integration & Data Fetching', 'Testing',
                      'Performance Optimization', 'Build Tools & Package Managers', 'Version Control & Collaboration',
                      'Deployment & CI/CD', 'Development Tools', 'AI-Powered Development Tools']


def target(role_id):
    load_roles()
    return {'role': role_id, 'keywords': ROLES[role_id]['keywords']}


def frontend_resume(**extra):
    skills = ['React.js', 'Next.js', 'JavaScript', 'TypeScript', 'HTML', 'CSS', 'Redux', 'Tailwind CSS', 'Jest',
              'React Testing Library', 'Webpack', 'Vite', 'Git', 'Jira', 'REST APIs', 'Axios', 'Figma', 'Material UI']
    jobs = [('Frontend Developer', 'Example Retail Labs (fictional)', 'Jun 2023', 'Present', [
                'Built React.js and Next.js storefront pages with TypeScript for a retail catalogue.',
                'Implemented Redux state for the cart, wishlist and checkout steps.',
                'Integrated REST APIs with Axios for orders, payments and delivery tracking.',
                'Wrote Jest and React Testing Library tests for checkout components.',
                'Styled responsive layouts with Tailwind CSS from Figma designs.',
                'Configured Webpack code splitting for the product listing routes.',
                'Reviewed pull requests in Git and tracked sprint work in Jira.',
                'Added form validation and error states to the address and payment forms.']),
            ('Associate Software Engineer', 'Example Services (fictional)', 'Jul 2021', 'May 2023', [
                'Developed internal admin screens in React.js with HTML and CSS.',
                'Migrated a legacy jQuery report page to React.js function components.',
                'Created reusable table and filter components shared by three admin modules.',
                'Set up Vite for the admin tool to replace a slow custom build script.',
                'Connected search and pagination to REST APIs for the user management module.',
                'Fixed cross-browser layout issues reported by the support team.',
                'Documented component usage for new team members in the project wiki.'])]
    profile = {
        'full_name': 'Ira Example (fictional)', 'email': 'ira@example.com', 'phone': '+91 90000 00077',
        'headline': 'Frontend Developer | React.js | Next.js | JavaScript',
        'summary': 'Frontend Developer focused on dependable, accessible user interfaces and structured '
                   'problem-solving.',
        'skills': skills,
        'experience': [{'company': company, 'position': position, 'start': start, 'end': end, 'location': '',
                        'bullets': bullets} for position, company, start, end, bullets in jobs],
        'projects': [
            'Merchant Service Portal - Tech stack: React.js, Redux, Material UI | Developed a merchant dashboard for '
            'transaction management and account administration. | Handled large datasets with sorting, filtering and '
            'pagination. | Implemented debounced search and CSV export for reporting. | Built role-based access '
            'control with protected routes and session timeout handling.',
            'Dynamic Dashboard Builder - Tech stack: Next.js, TypeScript | Built a builder for charts, tables and KPI '
            'widgets from selected datasets. | Implemented drill-down filters synchronized across widgets. | '
            'Virtualized large data tables for smooth scrolling. | Lazy loaded widget code to shorten the first load.'],
        'certifications': ['Udemy: React - The Complete Guide (incl. Next.js, Redux)'],
        'education': [{'institution': 'Example Institute of Technology (fictional)', 'degree': 'B.Tech - Computer Science',
                       'start': '2017', 'end': '2021', 'details': 'CGPA 8.4'}]}
    profile.update(extra)
    profile['skill_groups'] = skill_groups(profile['skills'], ROLES['frontend-developer'])
    return validate_profile(profile)


class CatalogTests(unittest.TestCase):
    def test_every_role_has_recruiter_style_categories(self):
        load_roles()
        for role_id, role in ROLES.items():
            with self.subTest(role=role_id):
                groups = role['resumeGroups']
                self.assertTrue(6 <= len(groups) <= 12, len(groups))
                self.assertTrue(all(len(items) >= 1 for items in groups.values()))
                self.assertNotIn('Concepts', groups)  # categories are named as job descriptions name them
        self.assertEqual(list(ROLES['frontend-developer']['resumeGroups']), FRONTEND_REFERENCE)

    def test_catalog_validation_rejects_too_many_categories(self):
        role = {'id': 'x-role', 'name': 'X Role', 'groups': {'Languages': ['Python']},
                'resumeGroups': {f'Group {i}': ['Python'] for i in range(13)}}
        with self.assertRaisesRegex(ValueError, 'resumeGroups'):
            validate_catalog({'roles': [role]})
        role['resumeGroups'] = {'Programming Languages': ['Python']}
        roles, _ = validate_catalog({'roles': [role]})
        self.assertEqual(roles['x-role']['resumeGroups'], {'Programming Languages': ['Python']})


class SkillsSectionTests(unittest.TestCase):
    def test_frontend_skills_use_the_reference_categories_in_order(self):
        groups = skill_groups(['Jest', 'React.js', 'Git', 'Redux', 'TypeScript', 'Tailwind CSS', 'Vite', 'ChatGPT',
                               'Axios', 'Lazy Loading', 'Figma', 'Vercel', 'Flexbox'], ROLES['frontend-developer'])
        names = [g['name'] for g in groups]
        self.assertEqual(names, [n for n in FRONTEND_REFERENCE if n in names])
        found = {g['name']: g['items'] for g in groups}
        self.assertEqual(found['Frontend Technologies'], ['React.js', 'TypeScript'])
        self.assertEqual(found['State Management'], ['Redux'])
        self.assertEqual(found['AI-Powered Development Tools'], ['ChatGPT'])
        self.assertEqual(len([s for g in groups for s in g['items']]), 13)  # every skill exactly once

    def test_exact_entries_win_and_close_spellings_find_their_category(self):
        groups = {g['name']: g['items'] for g in skill_groups(['SQL', 'Spark SQL', 'Python'],
                                                              ROLES['azure-data-engineer'])}
        self.assertIn('SQL', groups['Programming Languages'])
        java = {g['name']: g['items'] for g in skill_groups(['Java', 'JavaScript'], ROLES['java-fullstack-developer'])}
        self.assertEqual(java['Programming Languages'], ['Java', 'JavaScript'])

    def test_every_role_groups_its_own_catalog_without_leftovers(self):
        for role_id, role in ROLES.items():
            with self.subTest(role=role_id):
                skills = list(dict.fromkeys(i for items in role['resumeGroups'].values() for i in items))
                groups = skill_groups(skills, role)
                self.assertNotIn('Other skills', [g['name'] for g in groups])
                self.assertEqual(sorted(i for g in groups for i in g['items']), sorted(skills))

    def test_the_section_is_typeset_with_bold_category_names(self):
        tex = render(frontend_resume(), years=3, complete_sections=True, career_objective=True)
        skills = tex[tex.index(r'\section*{Technical Skills}'):tex.index(r'\section*{Professional Work Experience}')]
        self.assertIn(r'\textbf{Frontend Technologies:} React.js, Next.js, JavaScript, TypeScript, HTML, CSS', skills)
        self.assertIn(r'\textbf{UI Frameworks \& Styling:} Tailwind CSS', skills)


class SkillsReviewTests(unittest.TestCase):
    def test_suggestions_cover_the_role_and_skip_what_the_candidate_has(self):
        have = ['React.js', 'JavaScript', 'git']
        offered = catalog_skill_suggestions(target('frontend-developer'), have)
        self.assertLessEqual(len(offered), 30)
        self.assertFalse({'React.js', 'Git', 'JavaScript (ES6+)'} & set(offered))
        technical = [n for n in FRONTEND_REFERENCE if n not in ('Version Control & Collaboration', 'Development Tools',
                                                                 'AI-Powered Development Tools')]
        # The first suggestions span the technical categories before any working tool.
        categories = {name for name in technical for term in offered[:len(technical)]
                      if term in ROLES['frontend-developer']['resumeGroups'][name]}
        self.assertGreaterEqual(len(categories), len(technical) - 1)
        tools = ROLES['frontend-developer']['resumeGroups']['AI-Powered Development Tools']
        self.assertFalse(set(tools) & set(offered[:20]))

    def test_every_role_offers_suggestions_to_a_thin_list(self):
        for role_id in ROLES:
            with self.subTest(role=role_id):
                self.assertGreaterEqual(len(catalog_skill_suggestions(target(role_id), [])), SKILL_BREADTH)


class ResumeTitleTests(unittest.TestCase):
    def test_title_names_the_role_then_leading_listed_technologies(self):
        skills = ['JavaScript', 'TypeScript', 'React.js', 'Next.js', 'Redux', 'Git', 'HTML']
        title = resume_title('Frontend Developer', skills, target('frontend-developer'))
        self.assertEqual(title, 'Frontend Developer | React.js | Next.js | JavaScript')
        self.assertEqual(title_problems(title, 'Frontend Developer', skills), [])
        java = resume_title('Java Backend Developer', ['Java', 'SQL', 'Git', 'Spring Boot', 'Hibernate', 'MySQL'],
                            target('java-backend-developer'))
        self.assertEqual(java, 'Java Backend Developer | Spring Boot | Hibernate | MySQL')  # Java is in the role name

    def test_title_never_claims_an_unlisted_skill(self):
        for role_id, role in ROLES.items():
            with self.subTest(role=role_id):
                title = resume_title(role['name'], ['Git'], target(role_id))
                self.assertEqual(title, role['name'])
        self.assertEqual(resume_title('Data Analyst', [], target('data-analyst')), 'Data Analyst')
        self.assertTrue(title_problems('Frontend Developer | Vue.js', 'Frontend Developer', ['React.js']))
        self.assertTrue(title_problems('React.js | Frontend Developer', 'Frontend Developer', ['React.js']))
        too_many = 'Frontend Developer | ' + ' | '.join(['A', 'B', 'C', 'D'][:TITLE_SKILLS + 1])
        self.assertTrue(title_problems(too_many, 'Frontend Developer', ['A', 'B', 'C', 'D']))

    def test_objective_bolds_the_role_not_the_title_skills(self):
        tex = render(frontend_resume(summary='Frontend Developer focused on dependable, accessible user interfaces.'),
                     years=3, complete_sections=True, career_objective=True)
        objective = tex[tex.index(r'\section*{Career Objective}'):tex.index(r'\section*{Technical Skills}')]
        self.assertIn(r'\textbf{Frontend Developer}', objective)
        self.assertIn('Frontend Developer | React.js | Next.js | JavaScript', tex)  # the title under the name


class StandardsReportTests(unittest.TestCase):
    def report(self, profile, years=3, ats=None):
        ats = {'score': 72, 'ats_readiness': 88, 'matched': []} if ats is None else ats
        return standards_report(profile, target('frontend-developer'), years, ats, {'pages': 1, 'searchable': True})

    def test_a_complete_resume_meets_every_standard(self):
        report = self.report(frontend_resume())
        failed = [c for c in report['checks'] if c['ok'] is False]
        self.assertEqual(failed, [])
        self.assertTrue(report['verified'])
        self.assertEqual(report['met'], report['applicable'])
        self.assertEqual(len(report['checks']), 11)

    def test_gaps_are_named_and_the_resume_is_not_marked_verified(self):
        profile = frontend_resume(certifications=[], skills=['React.js', 'JavaScript', 'Git'])
        profile['experience'][0]['bullets'] = profile['experience'][0]['bullets'][:3]
        report = self.report(profile, ats={'score': 30, 'ats_readiness': 61, 'matched': []})
        failed = {c['id']: c['detail'] for c in report['checks'] if c['ok'] is False}
        self.assertFalse(report['verified'])
        self.assertIn('Certifications', failed['sections'])
        self.assertIn('None confirmed', failed['certifications'])
        self.assertIn('Example Retail Labs (fictional) (3 lines)', failed['experience'])
        self.assertIn(f'at least {SKILL_BREADTH}', failed['skills'])
        self.assertIn('30%', failed['keywords'])
        self.assertIn('61/100', failed['ats'])
        self.assertIn('title', failed)  # Next.js is no longer a listed skill

    def test_repeated_work_and_project_lines_are_flagged(self):
        profile = frontend_resume()
        profile['experience'][1]['bullets'][0] = profile['experience'][0]['bullets'][0]
        profile['projects'][0] = ('Merchant Service Portal - Tech stack: React.js, Redux | '
                                  + profile['experience'][0]['bullets'][1] + ' | Two. | Three. | Four.')
        failed = {c['id']: c['detail'] for c in self.report(profile)['checks'] if c['ok'] is False}
        self.assertIn('repeat another job', failed['experience'])
        self.assertIn('Repeats work lines: Merchant Service Portal', failed['projects'])

    def test_a_fresher_without_jobs_needs_no_experience_section(self):
        report = self.report(frontend_resume(experience=[]), years=0)
        checks = {c['id']: c for c in report['checks']}
        self.assertTrue(checks['sections']['ok'])
        self.assertIsNone(checks['experience']['ok'])
        self.assertEqual(report['applicable'], 10)
        self.assertTrue(re.search(r'\d+ project', checks['projects']['detail']))


if __name__ == '__main__':
    unittest.main()
