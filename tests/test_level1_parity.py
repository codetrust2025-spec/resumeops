"""Level 1 feature restoration: projects, portfolio, ATS breakdown, job-description keywords, new APIs."""
import copy
import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from resume_bot.latex import render
from resume_bot.profile import ProfileError, validate_profile
from resume_bot.workflow import (analyze, assess, job_description_keywords, keyword_vocabulary,
                                 normalize_request)

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = json.loads((ROOT / 'examples/sample_candidate.json').read_text(encoding='utf-8'))
TARGET = {'role': 'frontend-developer', 'template': 'standard', 'keywords': []}


def with_projects():
    data = copy.deepcopy(SAMPLE)
    data['portfolio'] = 'https://asha.example.com'
    data['projects'] = [{'name': 'Accessible Form Kit', 'link': 'https://example.com/forms',
                         'technologies': ['React', 'TypeScript'],
                         'bullets': ['Built keyboard-accessible form components & docs.']}]
    return data


class BackwardCompatibilityTests(unittest.TestCase):
    def test_existing_profile_normalizes_identically(self):
        self.assertEqual(validate_profile(SAMPLE), SAMPLE)
        self.assertNotIn('projects', validate_profile(SAMPLE))
        self.assertNotIn('portfolio', validate_profile({**SAMPLE, 'portfolio': '', 'projects': []}))

    def test_existing_profile_renders_byte_identical_to_baseline_52a4890(self):
        # The watcher re-renders cached profiles and rejects any byte difference, so this protects
        # every existing candidate output in production.
        profile = validate_profile(SAMPLE)
        for template in ('standard', 'compact'):
            baseline = (ROOT / f'tests/fixtures/baseline-52a4890-{template}.tex').read_text(encoding='utf-8')
            self.assertEqual(render(profile, template), baseline)

    def test_existing_ats_result_unchanged(self):
        result = analyze(validate_profile(SAMPLE), TARGET)
        self.assertEqual(set(result), {'role', 'template', 'score', 'matched', 'missing', 'method'})


class ProfileFieldTests(unittest.TestCase):
    def test_projects_and_portfolio_validate_and_render_escaped(self):
        profile = validate_profile(with_projects())
        self.assertEqual(profile['projects'][0]['technologies'], ['React', 'TypeScript'])
        latex = render(profile)
        self.assertIn(r'\section*{PROJECTS}', latex)
        self.assertIn(r'components \& docs.', latex)
        self.assertIn('https://asha.example.com', latex)

    def test_invalid_projects_rejected(self):
        for projects in ([{'name': ''}], [{'name': 'X', 'invented': 'y'}], ['text'], {'name': 'X'},
                         [{'name': 'X', 'bullets': 'not a list'}]):
            with self.assertRaises(ProfileError):
                validate_profile({**SAMPLE, 'projects': projects})

    def test_projects_count_as_keyword_evidence(self):
        data = with_projects()
        data['projects'][0]['technologies'].append('Jest')
        self.assertIn('Jest', analyze(validate_profile(data), TARGET)['matched'])
        self.assertNotIn('Jest', analyze(validate_profile(SAMPLE), TARGET)['matched'])


class AtsReportTests(unittest.TestCase):
    def test_breakdown_uses_level1_weights_and_only_candidate_facts(self):
        report = assess(validate_profile(SAMPLE), TARGET)
        self.assertEqual(report['weights'], {'keywords': 50, 'sections': 30, 'content': 20})
        b = report['breakdown']
        self.assertEqual(report['overall'], round(0.5 * b['keywords'] + 0.3 * b['sections'] + 0.2 * b['content']))
        self.assertEqual(b['keywords'], report['score'])
        self.assertEqual(report['passes'], report['overall'] >= 80)
        self.assertIn('linkedin'.replace('linkedin', 'LinkedIn'), report['missing_information'])
        self.assertIn('HTML5', report['priority_gaps']['mustHave'])
        self.assertTrue(report['suggestions'][0].startswith('Only if the candidate has real experience'))

    def test_report_never_alters_profile(self):
        profile = validate_profile(SAMPLE)
        before = copy.deepcopy(profile)
        assess(profile, TARGET)
        self.assertEqual(profile, before)

    def test_job_description_keywords_are_literal_known_terms(self):
        found = job_description_keywords('Looking for React.js, TypeScript and Azure Data Factory skills; '
                                         'Kubernetes a plus. Reactive programming is not React.js.')
        self.assertEqual(found[:3], ['React.js', 'TypeScript', 'Azure Data Factory'])
        self.assertIn('Kubernetes', found)
        self.assertNotIn('Docker', found)
        self.assertEqual(job_description_keywords(''), [])
        with self.assertRaises(ProfileError):
            job_description_keywords('x' * 50001)
        self.assertGreater(len(keyword_vocabulary()), 100)

    def test_extracted_keywords_flow_into_request(self):
        keywords = job_description_keywords('React.js and Jest required')
        _, target = normalize_request({'profile': SAMPLE, 'target': {**TARGET, 'keywords': keywords}})
        self.assertEqual(target['keywords'], ['React.js', 'Jest'])


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / 'dashboard'))
        from resume_dashboard import Workspace, handler
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / 'resume_bot', self.root / 'resume_bot', ignore=shutil.ignore_patterns('__pycache__'))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(Workspace(self.root), '', 'token'))
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.server.RequestHandlerClass = handler(Workspace(self.root), self.origin, 'token')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.headers = {'Origin': self.origin, 'X-Dashboard-Token': 'token', 'Content-Type': 'application/json'}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def get(self, path):
        with urlopen(self.origin + path, timeout=5) as response:
            return json.load(response)

    def post(self, path, body, headers=None):
        request = Request(self.origin + path, data=json.dumps(body).encode(), headers=headers or self.headers)
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_health_settings_and_catalog(self):
        self.assertIn(self.get('/api/health')['status'], ('OK', 'DEGRADED'))
        settings = self.get('/api/settings')
        self.assertEqual(settings['ollama']['model'], 'qwen2.5:7b')
        self.assertEqual(settings['access'], ['Local loopback'])
        self.assertNotIn('uri', json.dumps(settings).lower().replace('url', ''))
        catalog = self.get('/api/catalog')
        self.assertEqual(len(catalog['roles']), 8)
        self.assertIn('frontend-developer', catalog['keywordGroups'])

    def test_preview_returns_latex_and_report(self):
        result = self.post('/api/preview', {'profile': with_projects(), 'target': TARGET})
        self.assertIn(r'\section*{PROJECTS}', result['latex'])
        self.assertEqual(result['report']['score'], result['ats']['score'])
        self.assertFalse(list((self.root / 'inbox').glob('*.json')), 'preview must not queue')

    def test_jd_keywords_requires_token(self):
        self.assertEqual(self.post('/api/jd-keywords', {'text': 'Jest and Git'})['keywords'], ['Jest', 'Git'])
        with self.assertRaises(HTTPError) as denied:
            self.post('/api/jd-keywords', {'text': 'Git'}, {'Content-Type': 'application/json'})
        self.assertEqual(denied.exception.code, 403)
        with self.assertRaises(HTTPError) as invalid:
            self.post('/api/jd-keywords', ['Git'])
        self.assertEqual(invalid.exception.code, 400)

    def test_job_links_to_its_verified_revision_and_pdf_is_frameable_only_by_self(self):
        queued = self.post('/api/queue', SAMPLE)
        output = self.root / 'output/asha-example/abc123'
        output.mkdir(parents=True)
        (output / 'resume.pdf').write_bytes(b'%PDF fixture')
        (output / 'result.json').write_text(json.dumps({'candidate': 'Asha Example'}))
        (self.root / 'inbox/.resume-bot-state.json').write_text(json.dumps({queued['name']: {
            'status': 'ok', 'last_attempt': 4102444800, 'output': str(output), 'percent': 100}}))
        job = self.get('/api/status')['jobs'][0]
        self.assertEqual(job['revision'], 'asha-example/abc123')
        with urlopen(self.origin + '/api/artifact?key=asha-example/abc123&name=resume.pdf', timeout=5) as response:
            self.assertIn("frame-ancestors 'self'", response.headers['Content-Security-Policy'])
        with urlopen(self.origin + '/', timeout=5) as response:
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        # An output path outside the data root is never linked.
        (self.root / 'inbox/.resume-bot-state.json').write_text(json.dumps({queued['name']: {
            'status': 'ok', 'last_attempt': 4102444800, 'output': str(Path(tempfile.gettempdir()))}}))
        self.assertEqual(self.get('/api/status')['jobs'][0]['revision'], '')

    def test_ui_assets_served(self):
        for path in ('/core.js', '/workflow.js', '/dashboard.js'):
            with urlopen(self.origin + path, timeout=5) as response:
                self.assertEqual(response.headers.get_content_type(), 'text/javascript')
        with urlopen(self.origin + '/', timeout=5) as response:
            page = response.read().decode()
        for route in ('dashboard', 'create', 'candidates', 'outputs', 'ats', 'templates', 'logs', 'settings'):
            self.assertIn(f'data-view="{route}"', page)


if __name__ == '__main__':
    unittest.main()
