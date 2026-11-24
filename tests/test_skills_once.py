"""Each skill is shown once, and a shown work line keeps a space after its commas (nothing is reworded)."""
import unittest

from resume_bot.generate import comma_spaced, distinct_skills, present_for_role, skill_groups
from resume_bot.profile import MAX_SKILL_GROUPS, ProfileError, validate_profile
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

    def test_group_overflow_is_normalized_without_losing_skills(self):
        skills = [f'Tool {index:02}' for index in range(15)]
        categories = {f'Category {index:02}': [skill] for index, skill in enumerate(skills)}
        legacy = [{'name': name, 'items': items} for name, items in categories.items()]
        generated = skill_groups(skills, {'resumeGroups': categories})
        profile = {**facts(skills, []), 'skill_groups': legacy}
        normalized = validate_profile(profile)
        self.assertEqual(normalized['skills'], skills)
        self.assertEqual(normalized['skill_groups'], generated)
        self.assertEqual(generated[:11], legacy[:11])
        self.assertEqual(generated[11], {'name': 'Other skills', 'items': skills[11:]})
        self.assertEqual(len(generated), MAX_SKILL_GROUPS)
        self.assertEqual(validate_profile(normalized), normalized)
        self.assertEqual(len(profile['skill_groups']), 15)
        with self.assertRaisesRegex(ProfileError, 'contain every listed skill exactly once'):
            validate_profile({**profile, 'skill_groups': [*legacy, legacy[-1]]})


if __name__ == '__main__':
    unittest.main()
