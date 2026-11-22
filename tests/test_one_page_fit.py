"""One-page fit: a resume that spills only a few lines onto page 2 is built on one page with tighter vertical spacing
(every word kept); resumes built before stay exactly as they are. Seen live: a QA Automation Engineer resume printed
only its last project lines and Education on page 2."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resume_bot.latex import FIT_LEVELS, expected_text, render
from resume_bot.pipeline import process
from resume_bot.profile import validate_profile

LINE = ('Automated regression checks for the {area} workflow with Selenium WebDriver and TestNG, and reported each '
        'defect with clear steps in JIRA for the team.')
AREAS = ['payroll', 'leave', 'billing', 'invoice', 'onboarding', 'attendance', 'claims', 'expense', 'reporting', 'audit']


def profile(bullets):
    """A fictional QA resume with three jobs of ``bullets`` lines each. With the standard layout: 4 spills 5 lines in
    the larger 11pt type, 6 spills 7 lines, 7 spills 12 lines and 9 spills 23 lines onto page 2."""
    jobs = [{'company': f'Example Systems {n} (fictional)', 'position': 'QA Automation Engineer',
             'start': f'Jan {2015 + 3 * n}', 'end': f'Dec {2017 + 3 * n}', 'location': 'Bengaluru',
             'bullets': [LINE.format(area=AREAS[(i + n) % len(AREAS)]) + f' Cycle {i + 1}.' for i in range(bullets)]}
            for n in range(3)]
    return validate_profile({
        'full_name': 'Meera Example', 'headline': 'QA Automation Engineer', 'email': 'meera.example@example.com',
        'phone': '+91 90000 00077', 'location': 'Bengaluru, India', 'linkedin': '', 'github': '',
        'summary': 'QA automation engineer who builds dependable regression suites for business workflows.',
        'skills': ['Java', 'Selenium WebDriver', 'TestNG', 'Cucumber', 'Jenkins', 'Git', 'JIRA', 'Maven', 'Postman'],
        'experience': jobs,
        'education': [{'institution': 'Example Engineering College', 'degree': 'B.E. Mechanical Engineering',
                       'start': '2010', 'end': '2014', 'details': ''}],
        'projects': [f'Example Portal {n} - Tech stack: Java, Selenium WebDriver, TestNG | Automated the portal checks.'
                     for n in range(4)],
        'certifications': []})


def body(tex):
    return tex.split('\\begin{document}', 1)[1]


class FitLayoutTests(unittest.TestCase):
    def test_a_fit_changes_only_vertical_spacing(self):
        facts = profile(6)
        standard = render(facts)
        for level in FIT_LEVELS:
            fitted = render(facts, fit=level)
            self.assertNotEqual(fitted, standard)
            self.assertEqual(body(fitted), body(standard), 'every word and line of the resume is the same')
            self.assertIn('[10pt,a4paper]', fitted)
            self.assertIn('margin=1.45cm', fitted, 'line width unchanged, so every line breaks where it did')
        self.assertEqual(expected_text(facts), expected_text(facts), 'the strict PDF text check is layout-free')

    def test_without_a_fit_the_layouts_are_unchanged(self):
        short, long = profile(2), profile(40)
        self.assertIn('[11pt,a4paper]', render(short), 'short resumes keep the larger type')
        self.assertIn(r'\setlength{\parskip}{1pt}', render(long), 'long resumes keep the compact spacing')
        self.assertEqual(render(long, fit='tight'), render(long), 'the compact layout is never fitted')
        self.assertIn('[10pt,a4paper]', render(short, fit='snug'), 'a fitted short resume uses the normal type')


TOOLS = all(shutil.which(tool) for tool in ('pdflatex', 'pdfinfo', 'pdftotext'))


def words(pdf):
    return ' '.join(subprocess.run(['pdftotext', str(pdf), '-'], capture_output=True, encoding='utf-8',
                                   check=True).stdout.split())


@unittest.skipUnless(TOOLS, 'TeX/PDF tools missing')
class FitBuildTests(unittest.TestCase):
    def build(self, facts, root, **patches):
        root.mkdir(parents=True, exist_ok=True)
        source = root / 'candidate.json'
        source.write_text(json.dumps(facts), encoding='utf-8')
        with patch.multiple('resume_bot.pipeline', **patches) if patches else patch.dict({}):
            return process(source, root / 'output', polish_enabled=False)

    def test_a_short_spill_is_built_on_one_page_with_every_word(self):
        for bullets, level in ((6, 'tight'), (4, 'snug')):
            with self.subTest(bullets=bullets), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                facts = profile(bullets)
                result = self.build(facts, root)
                folder = Path(result['output'])
                self.assertEqual((result['pdf_checks']['pages'], result.get('layout_fit')), (1, level))
                self.assertEqual((folder / 'main.tex').read_text(encoding='utf-8'), render(facts, fit=level))
                # The same words as the standard two-page build of the same resume.
                standard = self.build(facts, root / 'standard', FIT_LEVELS={})
                self.assertEqual(standard['pdf_checks']['pages'], 2)
                self.assertEqual(words(folder / 'resume.pdf'), words(Path(standard['output']) / 'resume.pdf'))
                # Built again: the saved one-page revision verifies and is reused as it is.
                self.assertEqual(self.build(facts, root), result)

    def test_a_longer_spill_keeps_two_pages(self):
        for bullets in (7, 9):  # the fits do not reach one page / page 2 is too full to try
            with self.subTest(bullets=bullets), tempfile.TemporaryDirectory() as temp:
                result = self.build(profile(bullets), Path(temp))
                self.assertEqual((result['pdf_checks']['pages'], result.get('layout_fit')), (2, None))

    def test_resumes_built_before_the_fit_stay_exactly_as_they_are(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            facts = profile(6)
            before = self.build(facts, root, FIT_LEVELS={})  # a two-page resume saved by the earlier builder
            folder = Path(before['output'])
            saved = {name: (folder / name).read_bytes() for name in ('main.tex', 'resume.pdf', 'overleaf-project.zip')}
            again = self.build(facts, root)
            self.assertEqual(again, before, 'the saved revision verifies and is returned unchanged')
            self.assertEqual(again['pdf_checks']['pages'], 2)
            self.assertEqual({name: (folder / name).read_bytes() for name in saved}, saved, 'no file rewritten')


if __name__ == '__main__':
    unittest.main()
