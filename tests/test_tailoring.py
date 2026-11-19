"""AI tailoring must improve wording for the role while never adding facts; experience years must
be verified against dated roles before they are stated or scored."""
import datetime
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from resume_bot.latex import render
from resume_bot.pipeline import process
from resume_bot.polish import grounded, polish, verify_tailored
from resume_bot.profile import validate_profile
from resume_bot.workflow import analyze, experience_evidence

ROOT = Path(__file__).parents[1]
SAMPLE = validate_profile(json.loads((ROOT / 'examples/sample_candidate.json').read_text()))
TODAY = datetime.date(2026, 11, 17)

GOOD = {
    'Developed reusable React components for internal web applications.':
        'Built reusable React components for internal web applications.',
    'Integrated REST APIs and fixed UI bugs reported during testing.':
        'Integrated REST APIs into internal web applications and fixed UI bugs reported during testing.',
    'Worked with product designers to improve keyboard navigation on forms.':
        'Collaborated with product designers to improve keyboard navigation on forms.',
}
BAD = {
    'Maintained HTML, CSS, and JavaScript pages for client websites.':
        'Led a team maintaining HTML, CSS, and JavaScript pages for 12 client websites.',
    'Reviewed code changes with colleagues and documented recurring issues.':
        'Reviewed code changes with Kubernetes and documented recurring issues.',
}


class TailoringModel:
    """Stands in for qwen2.5:7b: rewrites lines the way a tailoring model would, including some
    unsafe rewrites the verifier must reject."""
    model = 'tailoring-test-model'

    def __init__(self, summary=None):
        self.summary = summary
        self.prompts = []

    def generate(self, prompt, schema=None, on_progress=None):
        self.prompts.append(prompt)
        source = json.loads(re.search(r'INPUT ARRAY:\s*(\[.*\])', prompt, re.S).group(1))
        out = [GOOD.get(item, BAD.get(item, item)) for item in source]
        if self.summary is not None:
            out[0] = self.summary
        if on_progress:
            on_progress({'phase': 'waiting', 'chars': 0, 'chunks': 0, 'elapsed': 0.0})
            on_progress({'phase': 'done', 'chars': 100, 'chunks': 10, 'elapsed': 1.0})
        return {'edits': out}


class GroundingTests(unittest.TestCase):
    ctx = 'Example Digital Labs\nSoftware Engineer\n' + '\n'.join(GOOD)

    def test_rephrasing_with_own_facts_is_accepted(self):
        for before, after in GOOD.items():
            self.assertEqual(grounded(before, after, self.ctx), '', after)
        # A skill listed elsewhere in the profile is still not evidence that this job used it.
        self.assertIn('term', grounded(list(GOOD)[0], 'Built reusable React components with TypeScript.', self.ctx))

    def test_added_facts_and_inflated_claims_are_rejected(self):
        cases = [
            ('Supported daily releases.', 'Led daily releases.', 'stronger claim'),
            ('Built a web application.', 'Built a web application and increased sales by 90%.', 'numbers'),
            ('Built reusable UI components.', 'Built reusable UI components with Kubernetes.', 'term'),
            ('Built UI components with React.', 'Built UI components.', 'drops source facts'),
            ('Fixed bugs in 3 releases.', 'Fixed bugs in releases.', 'drops numbers'),
            ('Wrote tests.', 'Wrote comprehensive automated tests boosting quality.', 'wording'),
        ]
        for before, after, why in cases:
            self.assertIn(why, grounded(before, after, before), after)

    def test_years_may_be_stated_only_when_verified(self):
        summary = 'Frontend engineer building accessible web interfaces.'
        self.assertIn('numbers', grounded(summary, 'Frontend engineer with 5 years building accessible web interfaces.', summary))
        self.assertEqual(grounded(summary, 'Frontend engineer with 5 years building accessible web interfaces.',
                                  summary, extra_numbers=(5,)), '')


class PolishTests(unittest.TestCase):
    def test_tailoring_changes_grounded_lines_and_keeps_rejected_originals(self):
        model = TailoringModel()
        result, audit = polish(SAMPLE, model, role='Frontend Developer',
                               keywords=['React', 'TypeScript', 'Redux'], years=3)
        bullets = [b for entry in result['experience'] for b in entry['bullets']]
        for after in GOOD.values():
            self.assertIn(after, bullets)
        for before in BAD:
            self.assertIn(before, bullets)          # unsafe rewrite kept the original
        self.assertEqual(audit['changed'], 3)
        self.assertEqual(audit['rejected'], 2)
        self.assertEqual(len(audit['rejections']), 2)
        prompt = model.prompts[0]
        self.assertIn('Target role: Frontend Developer', prompt)
        self.assertIn('React, TypeScript', prompt)
        self.assertNotIn('Redux', prompt)            # keywords the candidate lacks are never suggested
        self.assertIn('Verified total experience: 3 years', prompt)
        verify_tailored(SAMPLE, result, 3)

    def test_summary_cannot_add_unverified_years_or_skills(self):
        _, audit = polish(SAMPLE, TailoringModel('Frontend engineer with 9 years of React experience.'), years=3)
        self.assertEqual(audit['rejections'][0]['index'], 0)
        _, audit = polish(SAMPLE, TailoringModel('Frontend engineer with Angular experience.'), years=3)
        self.assertEqual(audit['rejections'][0]['index'], 0)
        result, audit = polish(SAMPLE, TailoringModel(
            'Frontend engineer with 3 years of experience building accessible web interfaces and '
            'reusable React components with TypeScript.'), years=3)
        self.assertNotIn(0, [item['index'] for item in audit['rejections']])
        self.assertIn('3 years', result['summary'])

    def test_tampered_saved_output_is_detected(self):
        result, _ = polish(SAMPLE, TailoringModel(), years=3)
        tampered = json.loads(json.dumps(result))
        tampered['experience'][0]['bullets'][0] = 'Architected the React platform for 2 million users.'
        with self.assertRaises(ValueError):
            verify_tailored(SAMPLE, tampered, 3)
        tampered = json.loads(json.dumps(result))
        tampered['experience'][0]['company'] = 'Another Company'
        with self.assertRaises(ValueError):
            verify_tailored(SAMPLE, tampered, 3)


class ExperienceTests(unittest.TestCase):
    profile = {'experience': [{'start': 'Jun 2019', 'end': 'February 2021'},
                              {'start': 'March 2021', 'end': 'Present'}]}

    def test_claim_is_checked_against_dated_roles(self):
        self.assertEqual(experience_evidence(self.profile, {'experience_years': 5}, TODAY)['used'], 5)
        over = experience_evidence(self.profile, {'experience_years': 12}, TODAY)
        self.assertEqual((over['used'], over['supported'], over['dated_years']), (7, False, 7))
        undated = experience_evidence({'experience': []}, {'experience_years': 3}, TODAY)
        self.assertEqual((undated['used'], undated['supported']), (None, False))
        self.assertTrue(experience_evidence({'experience': []}, {'experience_years': 0}, TODAY)['supported'])

    def test_ats_scores_the_experience_level(self):
        target = {'role': 'frontend-developer', 'template': 'standard', 'keywords': []}
        honest = analyze(SAMPLE, {**target, 'experience_years': 5}, today=TODAY)
        inflated = analyze(SAMPLE, {**target, 'experience_years': 15}, today=TODAY)
        self.assertEqual(honest['ats_readiness'] - inflated['ats_readiness'], 4)  # V3 rubric: 4 experience points
        self.assertTrue(any('dated roles show 5 years' in tip for tip in inflated['suggestions']))
        self.assertEqual(inflated['experience']['used'], 5)

    def test_fresher_layout_puts_education_first(self):
        fresher, experienced = render(SAMPLE, years=0), render(SAMPLE, years=4)
        self.assertLess(fresher.index(r'\section*{Education}'), fresher.index(r'\section*{Professional Experience}'))
        self.assertLess(experienced.index(r'\section*{Professional Experience}'), experienced.index(r'\section*{Education}'))


@unittest.skipUnless(shutil.which('pdflatex') and shutil.which('pdftotext'), 'TeX/PDF tools missing')
class TailoredBuildTests(unittest.TestCase):
    def test_tailored_resume_is_built_verified_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = root / 'job.json'
            request.write_text(json.dumps({'profile': json.loads((ROOT / 'examples/sample_candidate.json').read_text()),
                                           'target': {'role': 'frontend-developer', 'template': 'standard',
                                                      'keywords': [], 'experience_years': 3}}))
            model = TailoringModel()
            result = process(request, root / 'output', ai=model)
            out = Path(result['output'])
            saved = json.loads((out / 'profile.json').read_text())
            self.assertIn(GOOD['Developed reusable React components for internal web applications.'],
                          saved['experience'][0]['bullets'])
            self.assertEqual(result['tailoring']['changed'], 3)
            self.assertEqual(result['experience']['used'], 3)
            import subprocess
            text = ' '.join(subprocess.run(['pdftotext', str(out / 'resume.pdf'), '-'], capture_output=True,
                                           text=True, check=True).stdout.split())
            self.assertIn('Collaborated with product designers', text)
            # A second run reuses and re-verifies the saved tailored revision without calling the model.
            again = process(request, root / 'output', ai=model)
            self.assertEqual(len(model.prompts), 1)
            self.assertEqual(again['output'], result['output'])


if __name__ == '__main__':
    unittest.main()


class RealModelOutputTests(unittest.TestCase):
    """Output captured from qwen2.5:7b on the RTX laptop (17 Nov 2026) for a weak synthetic resume.
    Every line is a genuine rewrite and must pass grounding; the original facts stay intact."""
    WEAK = ['Was responsible for writing Python scripts that load sales data into SQL tables every night.',
            'Worked on a Flask API that the reporting team used for dashboards.',
            'Helped with moving the services to Docker containers.',
            'Did code reviews for other developers in Git.']
    REAL = ['Loaded sales data into SQL tables nightly with Python scripts.',
            "Developed a Flask API for the reporting team's dashboards.",
            'Collaborated on moving services to Docker containers.',
            'Conducted code reviews for other developers in Git.']

    def test_captured_rewrites_are_grounded(self):
        context = '\n'.join(self.WEAK)
        for before, after in zip(self.WEAK, self.REAL):
            self.assertEqual(grounded(before, after, context), '', after)
