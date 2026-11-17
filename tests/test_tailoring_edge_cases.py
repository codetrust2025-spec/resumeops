"""Regression for the production 45% failure (candidates with no summary and no work bullets):
qwen2.5:7b answered [""] with two strings (adding an invented bullet), polish() raised ValueError,
and the deterministic error was retried forever. Also covers the retry policy that followed."""
import json
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from resume_bot import pipeline
from resume_bot.ollama import OllamaError
from resume_bot.polish import polish, verify_tailored
from resume_bot.profile import validate_profile

# Same shape as the failing production jobs (synthetic values): empty summary, one dated role
# without bullets, education, no skills.
NO_LINES = {'full_name': 'Edge Case Example', 'email': 'edge.case@example.com', 'headline': 'Frontend Developer',
            'summary': '', 'skills': [],
            'experience': [{'company': 'Example Web Co', 'position': 'Frontend Developer', 'start': 'Jan 2019',
                            'end': 'Present', 'location': '', 'bullets': []}],
            'education': [{'institution': 'Example University', 'degree': 'B.Tech', 'start': '', 'end': '', 'details': ''}],
            'certifications': []}
REQUEST = {'profile': NO_LINES, 'target': {'role': 'frontend-developer', 'template': 'standard',
                                           'keywords': [], 'experience_years': 5}}
# The exact reply qwen2.5:7b gave three times out of three for the production prompt.
PRODUCTION_REPLY = {'edits': ['', 'Built responsive user interfaces using React and Redux.']}


class Model:
    model = 'edge-case-model'

    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def generate(self, prompt, schema=None, on_progress=None):
        self.prompts.append(prompt)
        return self.reply(prompt) if callable(self.reply) else self.reply


class EmptyInputTests(unittest.TestCase):
    def test_nothing_to_tailor_skips_the_model_and_never_fails(self):
        profile = validate_profile(NO_LINES)
        model = Model(PRODUCTION_REPLY)
        result, audit = polish(profile, model, role='Frontend Developer', years=5)
        self.assertEqual(model.prompts, [])                     # the model is not asked to rewrite nothing
        self.assertEqual(result, profile)                       # and nothing invented reaches the resume
        self.assertIn('No summary or work bullets', audit['skipped'])
        verify_tailored(profile, result, 5)

    def test_wrong_number_of_lines_keeps_every_original(self):
        profile = validate_profile({**NO_LINES, 'experience': [{**NO_LINES['experience'][0],
                                    'bullets': ['Worked on React pages for the store front.']}]})
        model = Model({'edits': ['Built React pages for the store front.', 'Led a team of 6 engineers.']})
        result, audit = polish(profile, model, role='Frontend Developer', years=5)
        self.assertEqual(result, profile)
        self.assertIn('model returned 2 lines for 1', audit['model_output_rejected'])
        prompt = model.prompts[0]
        self.assertIn('INPUT ARRAY:\n["Worked on React pages for the store front."]', prompt)  # no empty summary sent
        self.assertIn('There is no summary in this array', prompt)
        self.assertIn('Return exactly 1 string:', prompt)
        self.assertNotIn('5 years', prompt)                     # years are only for a summary

    def test_malformed_replies_never_fail_the_resume(self):
        profile = validate_profile({**NO_LINES, 'summary': 'Frontend developer building React pages.'})
        for reply in ({'edits': 'not a list'}, {'other': []}, {'edits': [None]}, {'edits': [42]}):
            result, audit = polish(profile, Model(reply), years=5)
            self.assertEqual(result['summary'], profile['summary'], reply)
            self.assertEqual(audit['changed'], 0)

    def test_summary_only_profile_is_still_tailored(self):
        profile = validate_profile({**NO_LINES, 'summary': 'I am a frontend developer who builds React pages.'})
        model = Model(lambda prompt: {'edits': ['Frontend developer with 5 years building React pages.']})
        result, audit = polish(profile, model, role='Frontend Developer', years=5)
        self.assertEqual(result['summary'], 'Frontend developer with 5 years building React pages.')
        self.assertEqual(audit['changed'], 1)


@unittest.skipUnless(shutil.which('pdflatex') and shutil.which('pdftotext'), 'TeX/PDF tools missing')
class PipelineTests(unittest.TestCase):
    def test_failing_production_shape_now_reaches_100(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / 'inbox'
            inbox.mkdir()
            (inbox / 'edge.json').write_text(json.dumps(REQUEST))
            job = pipeline.scan(inbox, root / 'output', ai=Model(PRODUCTION_REPLY), once=True)['edge.json']
            self.assertEqual((job['status'], job['percent']), ('ok', 100))
            result = json.loads((Path(job['output']) / 'result.json').read_text())
            self.assertIn('skipped', result['tailoring'])
            self.assertNotIn('Redux', (Path(job['output']) / 'main.tex').read_text())


class RetryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / 'inbox'
        self.inbox.mkdir()
        (self.inbox / 'job.json').write_text(json.dumps(REQUEST))

    def tearDown(self):
        self.temp.cleanup()

    def scan(self, error, now):
        with patch('resume_bot.pipeline.process', side_effect=error) as call, \
                patch('resume_bot.pipeline.time.time', return_value=now):
            state = pipeline.scan(self.inbox, self.root / 'output', polish_enabled=False, once=True)
        return state['job.json'], call.call_count

    def test_deterministic_error_is_not_retried_until_code_changes_or_retry_now(self):
        job, calls = self.scan(ValueError('shape mismatch'), 1000)
        self.assertEqual((job['status'], job['retryable'], job['next_retry'], calls), ('failed', False, None, 1))
        self.assertEqual(job['error_detail'], 'ValueError: shape mismatch')   # the message is kept for diagnosis
        _, calls = self.scan(ValueError('shape mismatch'), 1000 + 6 * 3600)
        self.assertEqual(calls, 0)                                             # hours later: still not retried
        pipeline.request_retry(self.inbox, 'job.json')
        _, calls = self.scan(ValueError('shape mismatch'), 1000 + 6 * 3600)
        self.assertEqual(calls, 1)                                             # a person asked
        with patch('resume_bot.pipeline.CODE_VERSION', 'fixed-code'):
            _, calls = self.scan(ValueError('shape mismatch'), 1000 + 6 * 3600)
        self.assertEqual(calls, 1)                                             # deployed fix: tried once at once

    def test_transient_errors_retry_with_backoff_and_stop_after_the_limit(self):
        now = 1000
        for attempt in range(1, pipeline.MAX_AUTO_ATTEMPTS + 1):
            job, calls = self.scan(OllamaError('connection refused'), now)
            self.assertEqual(calls, 1, attempt)
            self.assertTrue(job['retryable'])
            now += pipeline._backoff(attempt) + 1
        self.assertIsNone(job['next_retry'])
        _, calls = self.scan(OllamaError('connection refused'), now + 10 * 3600)
        self.assertEqual(calls, 0)


if __name__ == '__main__':
    unittest.main()
