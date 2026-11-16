"""Regression coverage for targeted jobs and dependency retries."""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from resume_bot.pipeline import scan
from resume_bot.workflow import DatabaseUnavailable, analyze, normalize_request

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / 'examples' / 'sample_candidate.json'


def targeted():
    profile = json.loads(SAMPLE.read_text(encoding='utf-8'))
    profile['skills'] = ['Python', 'SQL']
    return {'profile': profile, 'target': {
        'role': 'fullstack-developer', 'template': 'compact', 'keywords': ['C++', 'Python'],
    }}


class WorkflowTests(unittest.TestCase):
    def test_keywords_are_literal_and_never_inserted_into_profile(self):
        job = targeted()
        profile, target = normalize_request(job)
        stats = analyze(profile, target)
        self.assertIn('C++', stats['missing'])
        self.assertIn('Python', stats['matched'])
        self.assertNotIn('C++', profile['skills'])
        self.assertEqual(stats['template'], 'compact')
        self.assertIsNone(normalize_request(job['profile'])[1])

    def test_reject_unknown_template(self):
        job = targeted()
        job['target']['template'] = '../../../private'
        with self.assertRaisesRegex(ValueError, 'supported resume template'):
            normalize_request(job)

    @unittest.skipUnless(all(shutil.which(program) for program in
                         ('pdflatex', 'pdfinfo', 'pdftotext')), 'TeX/PDF tools missing')
    def test_database_block_retries_from_existing_verified_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / 'inbox'
            inbox.mkdir()
            (inbox / 'job.json').write_text(json.dumps(targeted()), encoding='utf-8')
            opts = dict(polish_enabled=False, once=True)
            with patch('resume_bot.pipeline.persist_analysis',
                       side_effect=DatabaseUnavailable('Database is unavailable')):
                result = scan(inbox, root / 'output', **opts)['job.json']
            self.assertTrue(result['blocked'])
            self.assertEqual(result['percent'], 95)
            output = next((root / 'output').glob('*/*/resume.pdf'))
            previous = output.read_bytes()
            with patch('resume_bot.pipeline.time.time', return_value=time.time() + 121), patch(
                'resume_bot.pipeline.compile_latex', side_effect=AssertionError('Unexpected rebuild')
            ):
                result = scan(inbox, root / 'output', **opts)['job.json']
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['percent'], 100)
            self.assertEqual(output.read_bytes(), previous)
