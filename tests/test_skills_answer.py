"""A candidate's whole skills list can be confirmed, with group labels removed."""
import unittest

from resume_bot.automation import AnswerError, _drafted_answer

# As in a live QA application: grouped skills, more than 20 of them.
LIST = ('Programming: Java, TypeScript, Automation Testing: Playwright, Codegen, Page Object Model (POM), '
        'Data-Driven Testing, Manual Testing: Requirement Analysis, Test Scenario Design, Test Case Design, '
        'Test Planning, Test Execution, Test Data Management, Requirement Traceability Matrix (RTM), '
        'Boundary Value Analysis, Equivalence Partitioning, Testing Types: Functional Testing, Regression Testing, '
        'Smoke Testing, Sanity Testing, Integration Testing, System Testing, End-to-End (E2E) Testing, '
        'User Acceptance Testing (UAT), Cross-Browser Testing, Cross-Platform Testing, Responsive UI Testing, '
        'Exploratory Testing, Tools: JIRA, Postman, Git, GitHub, Chrome DevTools, SQL')


class SkillsAnswerTests(unittest.TestCase):
    def test_a_long_grouped_list_is_accepted_without_its_labels(self):
        skills = _drafted_answer(LIST, 'skills', 'skills')
        self.assertGreater(len(skills), 20)
        self.assertEqual(skills[:3], ['Java', 'TypeScript', 'Playwright'])
        self.assertIn('Requirement Analysis', skills)
        self.assertIn('Functional Testing', skills)
        self.assertIn('JIRA', skills)
        self.assertFalse(any(':' in skill for skill in skills), skills)

    def test_limits_still_apply(self):
        with self.assertRaises(AnswerError):
            _drafted_answer(', '.join(f'Skill {n}' for n in range(61)), 'skills', 'skills')
        with self.assertRaises(AnswerError):
            _drafted_answer('x' * 3001, 'skills', 'skills')
        self.assertEqual(_drafted_answer('C++, C#, Node.js, ASP.NET', 'skills', 'skills'), ['C++', 'C#', 'Node.js', 'ASP.NET'])


if __name__ == '__main__':
    unittest.main()
