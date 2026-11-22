"""Import keeps every work line of the document; Full Stack Developer is a catalog role with HTML and CSS."""
import unittest

from resume_bot.generate import skill_fits_role
from resume_bot.ingest import repair
from resume_bot.workflow import ROLES, load_roles, matched_terms
from resume_bot.generate import named_tools

KIRAN = """Kiran Sample
Phone: +91 90000 00013 | Email: kiran.sample@example.test | Location: Chennai, India

SKILLS
JavaScript, React, HTML, CSS, Git, node js, Node.js

EXPERIENCE
Full Stack Developer, Example Digital Services, Chennai (Apr 2023 - Present)

Web Developer, Illustrative Web Studio, Chennai (Jun 2021 - Mar 2023)
- Built responsive pages in React and HTML for client websites,with reusable form components.
- Fixed layout bugs in CSS for the checkout pages.

EDUCATION
B.Sc. Computer Science, Example Arts and Science College, Chennai (2018 - 2021)
"""


def extracted(bullets_for_web):
    """What the model returned, seen live: the Web Developer job without its lines."""
    return {'full_name': 'Kiran Sample', 'phone': '+91 90000 00013', 'email': 'kiran.sample@example.test',
            'location': 'Chennai, India', 'linkedin': '', 'github': '', 'portfolio': '', 'headline': '', 'summary': '',
            'current_company': '', 'skills': ['JavaScript', 'React'], 'certifications': [], 'projects': [],
            'achievements': [], 'coding_platforms': [], 'competencies': [],
            'experience': [{'company': 'Example Digital Services', 'position': 'Full Stack Developer', 'start': 'Apr 2023',
                            'end': 'Present', 'location': 'Chennai', 'bullets': []},
                           {'company': 'Illustrative Web Studio', 'position': 'Web Developer', 'start': 'Jun 2021',
                            'end': 'Mar 2023', 'location': 'Chennai', 'bullets': list(bullets_for_web)}],
            'education': [{'degree': 'B.Sc. Computer Science', 'institution': 'Example Arts and Science College',
                           'start': '2018', 'end': '2021', 'details': ''}]}


class ImportKeepsWorkLines(unittest.TestCase):
    def test_a_dropped_work_line_is_restored_verbatim_under_its_job(self):
        out = repair(extracted([]), KIRAN)
        self.assertEqual(out['experience'][1]['bullets'], [
            'Built responsive pages in React and HTML for client websites,with reusable form components.',
            'Fixed layout bugs in CSS for the checkout pages.'])
        self.assertEqual(out['experience'][0]['bullets'], [], 'a job without lines in the document gets none')

    def test_lines_already_extracted_or_reworded_are_not_repeated(self):
        kept = ['Built responsive pages in React and HTML for client websites with reusable form components.']
        out = repair(extracted(kept), KIRAN)
        self.assertEqual(out['experience'][1]['bullets'], [kept[0], 'Fixed layout bugs in CSS for the checkout pages.'])


class FullStackRole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_roles(force=True)

    def test_full_stack_developer_is_offered_and_counts_web_basics(self):
        role = ROLES['fullstack-developer']
        self.assertFalse(role['hidden'])
        target = {'role': 'fullstack-developer', 'keywords': []}
        for skill in ('HTML', 'CSS', 'JavaScript', 'React.js', 'Node.js', 'Java', 'Spring Boot'):
            self.assertTrue(skill_fits_role(skill, target), skill)
        self.assertEqual(matched_terms(named_tools(), 'node js'), ['Node.js'])


if __name__ == '__main__':
    unittest.main()
