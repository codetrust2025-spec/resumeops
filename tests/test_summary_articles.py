"""Composed summaries use "an" before a vowel sound.

Production (a240d5e matrix): "seeking a Angular Developer role" and "moving into a Angular Developer role".
Every candidate here is fictional."""
import unittest

from resume_bot.generate import article, composed_summary, summary_problems
from resume_bot.profile import validate_profile


def person(position=None, bullets=(), skills=('TypeScript', 'Angular', 'RxJS'), degree='B.Tech Computer Science'):
    return validate_profile({
        'full_name': 'Ravi Example (fictional)', 'phone': '+91 90000 22222', 'skills': list(skills),
        'experience': [{'company': 'Example Works Pvt Ltd (fictional)', 'position': position, 'start': 'July 2023',
                        'end': 'Present', 'location': '', 'bullets': list(bullets)}] if position else [],
        'education': [{'institution': 'Example Institute of Technology (fictional)', 'degree': degree,
                       'start': '', 'end': '2022', 'details': ''}]})


class ArticleTests(unittest.TestCase):
    def test_the_article_follows_the_sound(self):
        cases = {'Angular Developer': 'an', 'AWS Cloud Engineer': 'an', 'Azure Data Engineer': 'an',
                 'AI / ML Engineer': 'an', 'Android Developer': 'an', 'iOS Developer': 'an', 'Embedded Engineer': 'an',
                 'M.Tech Computer Science': 'an', 'MCA': 'an', 'MBA': 'an', 'ML Engineer': 'an', 'SRE': 'an',
                 'UI Developer': 'a', '.NET Developer': 'a', 'MERN Stack Developer': 'a', 'Java Backend Developer': 'a',
                 'B.Tech Computer Science': 'a', 'React.js Developer': 'a', 'Node.js Developer': 'a',
                 'QA Automation Engineer': 'a', 'Salesforce Developer': 'a', 'Master of Science': 'a', '': 'a'}
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(article(phrase), expected)

    def test_fresher_summary_reads_correctly(self):
        profile = person()
        text = composed_summary(profile, 'Angular Developer', 0, ['Angular', 'TypeScript'])
        self.assertIn('seeking an Angular Developer role', text)
        self.assertNotIn(' a Angular', text)
        self.assertEqual(summary_problems(text, profile, 'Angular Developer', 0), [])

    def test_career_change_summary_reads_correctly(self):
        profile = person('Frontend Developer', ['Built React.js pages with Redux Toolkit state for an order portal.'],
                         skills=('JavaScript', 'React.js', 'Redux Toolkit'), degree='M.Tech Computer Science')
        text = composed_summary(profile, 'Angular Developer', 3, ['Angular', 'TypeScript'])
        self.assertIn('moving into an Angular Developer role', text)
        self.assertIn('Holds an M.Tech in Computer Science.', text)
        self.assertEqual(summary_problems(text, profile, 'Angular Developer', 3), [])

    def test_consonant_roles_are_unchanged(self):
        text = composed_summary(person(skills=('Java', 'Spring Boot')), 'Java Backend Developer', 0, ['Java'])
        self.assertIn('seeking a Java Backend Developer role', text)


if __name__ == '__main__':
    unittest.main()
