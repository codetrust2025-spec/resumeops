"""AI drafts never claim results or achievements the candidate's documents do not state."""
import unittest
from unittest.mock import patch

from resume_bot.generate import (DRAFT_ACHIEVEMENT, draft_problem, draft_role_content, named_tools,
                                 without_generic_tail)
from resume_bot.profile import validate_profile
from resume_bot.workflow import load_roles

# Seen live in owner uploads: claims with no support in the candidate's documents.
CLAIMS = ['Automated deployment pipelines with Docker and Jenkins, achieving zero-downtime deployments.',
          "Integrated Cypress for front-end testing, improving the user interface's reliability.",
          'Configured caching with Redis, resulting in faster page loads for the catalogue.',
          'Refactored queries in PostgreSQL to reduce response time for the order reports.',
          'Tuned the Jenkins pipeline stages, which significantly shortened release cycles.',
          # Seen live in a proposed project for a candidate with React, Node.js and Git:
          'Deployed application to production server using Git hooks and CI/CD pipeline.',
          'Optimized task assignment logic with dynamic priority queues in the Node.js service.']
TASKS = ['Built Redux reducers and selectors for the cart state in React.',
         'Wrote higher-order components in React for shared form validation on the checkout page.',
         'Designed MongoDB collections for patient records and appointment reminders.',
         'Wrote Selenium and TestNG test cases for the loan approval module of the banking portal.']


class NoClaimedResultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_roles()

    def test_claims_are_rejected_and_plain_work_is_allowed(self):
        tools = named_tools()
        for line in CLAIMS:
            with self.subTest(line=line):
                self.assertTrue(DRAFT_ACHIEVEMENT.search(line))
                problem = draft_problem(line, tools, tools, [], 'past')
                # Rejected, or offered only without its claim (a generic closing clause is cut from fill lines).
                self.assertTrue((problem and 'claimed results' in problem)
                                or not DRAFT_ACHIEVEMENT.search(without_generic_tail(line)), line)
        # A claim inside the work itself is always rejected.
        self.assertIn('claimed results', draft_problem(CLAIMS[0], tools, tools, [], 'past'))
        self.assertIn('claimed results', draft_problem(CLAIMS[3], tools, tools, [], 'past'))
        for line in TASKS:
            with self.subTest(line=line):
                self.assertIsNone(DRAFT_ACHIEVEMENT.search(line))

    def test_a_capitalised_express_is_the_express_tool(self):
        from resume_bot.generate import tools_outside
        tools = named_tools()
        line = 'Configured Express routes for task CRUD operations in the Node.js backend.'
        self.assertEqual(tools_outside(line, ['React', 'Node.js', 'Git'], tools), ['Express.js'],
                         'not stated by the candidate: rejected')
        self.assertEqual(tools_outside(line, ['React', 'Node.js', 'Express'], tools), [], 'stated as "Express"')
        self.assertEqual(tools_outside(line, ['Node.js', 'Express.js'], tools), [], 'stated as "Express.js"')
        self.assertEqual(tools_outside('Wrote tests that express each rule in Node.js.', ['Node.js'], tools), [],
                         'the everyday word is not the tool')

    def test_a_technology_the_facts_do_not_state_is_rejected(self):
        from resume_bot.generate import _accept_project_bullet, unverified_names
        # Seen live: a proposed project for a React and Node.js candidate "Integrated Nodemailer for ... reminders".
        line = 'Sent appointment reminders through Nodemailer from the Node.js service on a daily schedule.'
        self.assertEqual(unverified_names(line, ['React', 'Node.js']), ['Nodemailer'])
        self.assertIn('only the technologies', draft_problem(line, ['React', 'Node.js'], named_tools(), [], 'past'))
        bullets = []
        self.assertFalse(_accept_project_bullet(line, ['React', 'Node.js'], bullets, [], named_tools(), [], []))
        self.assertEqual(unverified_names('Configured Docker Compose files and REST APIs for the order service.',
                                          ['Docker']), [], 'the rest of a stated tool, and generic terms')
        self.assertTrue(_accept_project_bullet('Built the TaskBoard screens with React for planning the weekly work.',
                                               ['React'], bullets, [], named_tools(), [], [], 'TaskBoard'),
                        "a project's bullets may name the project")

    def test_a_job_description_sets_the_career_objective_priorities(self):
        from resume_bot.generate import broad_objective_problems, write_summary
        from resume_bot.profile import validate_profile
        facts = validate_profile({'full_name': 'Example Applicant', 'phone': '+1 202 555 0147', 'email': 'a@example.test',
                                  'skills': ['Selenium', 'Java'],
                                  'experience': [{'position': 'QA Automation Engineer', 'company': 'Example Works',
                                                  'start': '2022', 'end': '2025', 'bullets': ['Wrote Selenium tests.']}],
                                  'education': [{'degree': 'BSc', 'institution': 'Example University', 'end': '2020'}]})
        text, how = write_summary(facts, 'QA Automation Engineer', 3, [], object(),
                                  emphasis=['clear communication', 'attention to detail'])
        self.assertEqual(how, 'job description')
        self.assertIn('clear communication and attention to detail', text)
        self.assertEqual(broad_objective_problems(text, facts, 'QA Automation Engineer'), [], 'the approved template')

    def test_drafted_lines_with_claims_never_reach_the_review_card(self):
        profile = validate_profile({
            'full_name': 'Example Applicant', 'phone': '+1 202 555 0147', 'email': 'candidate@example.test',
            'skills': ['Docker', 'Jenkins', 'PostgreSQL', 'Redis'],
            'experience': [{'position': 'Backend Developer', 'company': 'Example Works', 'start': '2021', 'end': '2024',
                            'bullets': []}],
            'education': [{'degree': 'BSc', 'institution': 'Example University', 'end': '2020'}]})
        plain = ['Configured Docker Compose files for the order service and the PostgreSQL test database.',
                 'Wrote Jenkins pipeline stages that ran the PostgreSQL migration checks before each merge.',
                 'Added Redis caching for the product lookups used by the order service endpoints.']
        reply = {'roles': [{'index': 0, 'bullets': [CLAIMS[0], CLAIMS[2], CLAIMS[3], *plain]}], 'skills': []}
        target = {'role': 'java-backend-developer', 'keywords': ['Docker', 'Jenkins', 'PostgreSQL', 'Redis']}
        with patch('resume_bot.generate.generate', return_value=reply):
            work, _ = draft_role_content(profile, target, 'Backend Developer', 3, [0], False, object(), grounded=True)
        drafted = work.get(0, [])
        self.assertTrue(drafted, 'the plain lines are offered')
        for line in drafted:
            self.assertIsNone(DRAFT_ACHIEVEMENT.search(line), line)


if __name__ == '__main__':
    unittest.main()
