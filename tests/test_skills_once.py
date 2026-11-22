"""Each skill is shown once, and a shown work line keeps a space after its commas (nothing is reworded)."""
import unittest

from resume_bot.generate import comma_spaced, distinct_skills, present_for_role
from resume_bot.profile import validate_profile
from resume_bot.workflow import load_roles


def facts(skills, bullets):
    return validate_profile({
        'full_name': 'Example Applicant', 'phone': '+1 202 555 0147', 'email': 'candidate@example.test',
        'skills': skills,
        'experience': [{'position': 'Software Developer', 'company': 'Example Works', 'start': '2020', 'end': '2026',
                        'bullets': bullets}],
        'education': [{'degree': 'BSc Computer Science', 'institution': 'Example University', 'end': '2020'}],
    })


class SkillsOnceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_roles()

    def test_a_skill_written_several_ways_is_shown_once_in_the_catalog_spelling(self):
        # Seen live (a Full Stack Developer application): "Node.js" under Frameworks, "node js" twice under Other.
        left = []
        shown = distinct_skills(['react js', 'Node.js', 'next js', 'node js', 'Gen Ai', 'node js.', 'NodeJS'], left)
        self.assertEqual(shown, ['react js', 'Node.js', 'next js', 'Gen Ai'])
        self.assertEqual(len(left), 3)
        self.assertEqual(distinct_skills(['node js', 'Node.js']), ['Node.js'], 'catalog spelling, first place')
        self.assertEqual(distinct_skills(['C', 'C#', 'C++', 'Java', 'JavaScript']), ['C', 'C#', 'C++', 'Java', 'JavaScript'])
        self.assertEqual(distinct_skills(['Gen Ai.']), ['Gen Ai'], 'no trailing full stop')

    def test_the_role_resume_shows_each_skill_once_and_spaced_lines(self):
        target = {'role': 'fullstack-developer', 'keywords': []}
        evidence = facts(['JavaScript', 'Node.js', 'node js', 'React.js', 'react js', 'SQL'],
                         ['Reviewed automation code,suggesting enhancements,and new tools for the team.',
                          'Integrated REST APIs with Node.js and managed 1,200 SQL records.'])
        shown, left = present_for_role(evidence, target, 'Full Stack Developer', keep_own=True)
        self.assertEqual(shown['skills'], ['JavaScript', 'Node.js', 'React.js', 'SQL'])
        self.assertTrue(any('listed twice' in reason for reason in left))
        self.assertEqual(shown['experience'][0]['bullets'][0],
                         'Reviewed automation code, suggesting enhancements, and new tools for the team.')
        self.assertIn('1,200', shown['experience'][0]['bullets'][1], 'numbers keep their commas')
        self.assertEqual(evidence['skills'], ['JavaScript', 'Node.js', 'node js', 'React.js', 'react js', 'SQL'],
                         'the stored facts stay complete')

    def test_comma_spacing_leaves_other_text_alone(self):
        for text in ('Built APIs with Node.js, Express and MongoDB.', 'Managed 1,200 records.', 'Used e.g. Postman.'):
            self.assertEqual(comma_spaced(text), text)
        self.assertEqual(comma_spaced('Tested login,cart;checkout flows.'), 'Tested login, cart; checkout flows.')


if __name__ == '__main__':
    unittest.main()
