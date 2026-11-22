"""AI drafts never claim results or achievements the candidate's documents do not state."""
import unittest
from unittest.mock import patch

from resume_bot.generate import DRAFT_ACHIEVEMENT, draft_problem, draft_role_content, named_tools
from resume_bot.profile import validate_profile
from resume_bot.workflow import load_roles

# Seen live in owner uploads: claims with no support in the candidate's documents.
CLAIMS = ['Automated deployment pipelines with Docker and Jenkins, achieving zero-downtime deployments.',
          "Integrated Cypress for front-end testing, improving the user interface's reliability.",
          'Configured caching with Redis, resulting in faster page loads for the catalogue.',
          'Refactored queries in PostgreSQL to reduce response time for the order reports.',
          'Tuned the Jenkins pipeline stages, which significantly shortened release cycles.']
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
                self.assertIn('claimed results', draft_problem(line, tools, tools, [], 'past'))
        for line in TASKS:
            with self.subTest(line=line):
                self.assertIsNone(DRAFT_ACHIEVEMENT.search(line))

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
