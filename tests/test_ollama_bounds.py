"""Every Ollama request must end: token caps, a streaming deadline, and bounded evidence ranking."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from resume_bot.automation import ranking_schema, tailor_facts
from resume_bot.ollama import Ollama, OllamaLimitError

INTELLIGENCE = {'role': 'custom', 'label': 'Backend Software Engineer',
                'terms': ['Python', 'SQL', 'REST APIs'],
                'sources': [{'responsibilities': ['Build and operate REST APIs.']}]}


def form_facts(projects, skills=(), bullets=()):
    """The shape a one-submit form produces: attested lines, usually no skills or bullets."""
    return {'full_name': 'Rowan Hale', 'email': 'rowan.hale@example.com', 'skills': list(skills),
            'experience': [{'company': 'Example Systems', 'position': 'Software Engineer',
                            'start': 'Jan 2022', 'end': 'Present', 'bullets': list(bullets)},
                           {'company': 'Sample Data Labs', 'position': 'Junior Developer',
                            'start': 'Jul 2021', 'end': 'Dec 2021', 'bullets': []}],
            'education': [{'institution': 'Example Institute of Technology', 'degree': 'B.Tech'}],
            'projects': list(projects)}


class FakeOllama(BaseHTTPRequestHandler):
    """Records each request; `mode` decides how the reply behaves."""
    mode = 'runaway'
    requests = []
    disconnected = threading.Event()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        type(self).requests.append(body)
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson' if body['stream'] else 'application/json')
        self.end_headers()
        if self.mode == 'length':
            final = {'response': '{"skills": [0, 1, 2, 3', 'done': True, 'done_reason': 'length'}
            if body['stream']:
                self.wfile.write((json.dumps({'response': '{"skills": [0, 1', 'done': False}) + '\n').encode())
                final['response'] = ', 2, 3'
            self.wfile.write((json.dumps(final) + '\n').encode())
            return
        # Runaway: an endless JSON array, the way the production generation never ended.
        self.wfile.write((json.dumps({'response': '{"skills": [', 'done': False}) + '\n').encode())
        try:
            for index in range(100000):
                self.wfile.write((json.dumps({'response': f'{index}, ', 'done': False}) + '\n').encode())
                self.wfile.flush()
                time.sleep(0.01)
        except OSError:
            type(self).disconnected.set()  # the client hung up, so Ollama would abort the generation

    def log_message(self, *args):
        pass


class OllamaBoundsTests(unittest.TestCase):
    def setUp(self):
        FakeOllama.requests = []
        FakeOllama.disconnected = threading.Event()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeOllama)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_runaway_stream_stops_at_deadline_and_disconnects(self):
        FakeOllama.mode = 'runaway'
        started = time.monotonic()
        with self.assertRaises(OllamaLimitError) as caught:
            Ollama('qwen2.5:7b', self.url, timeout=30, deadline=1).generate('p', on_progress=lambda m: None)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn('1 seconds', str(caught.exception))
        self.assertEqual(len(FakeOllama.requests), 1, 'a limit must not trigger the non-streaming retry')
        self.assertTrue(FakeOllama.disconnected.wait(5), 'the connection must close so Ollama frees its slot')

    def test_token_limit_is_an_error_in_both_modes(self):
        # A cut answer is retried once with twice the budget, then reported; never accepted.
        FakeOllama.mode = 'length'
        client = Ollama('qwen2.5:7b', self.url, timeout=5, max_tokens=64)
        with self.assertRaises(OllamaLimitError):
            client.generate('p', on_progress=lambda m: None)
        with self.assertRaises(OllamaLimitError):
            client.generate('p')
        self.assertEqual(len(FakeOllama.requests), 4)
        self.assertEqual([r['options']['num_predict'] for r in FakeOllama.requests], [64, 128, 64, 128])
        self.assertEqual([r['options']['num_ctx'] for r in FakeOllama.requests], [8192, 16384, 8192, 16384])

    def test_every_request_is_token_capped_by_default(self):
        FakeOllama.mode = 'length'
        with self.assertRaises(OllamaLimitError):
            Ollama('qwen2.5:7b', self.url, timeout=5).generate('p')
        self.assertEqual(FakeOllama.requests[0]['options'], {'temperature': 0, 'num_ctx': 8192, 'num_predict': 4096})


class RecordingModel:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def generate(self, prompt, schema=None, on_progress=None):
        self.calls.append({'prompt': prompt, 'schema': schema})
        return self.reply


class BoundedRankingTests(unittest.TestCase):
    def test_production_form_shape_only_ranks_groups_with_choices(self):
        # The stuck production submission: two projects, no skills, no bullets.
        model = RecordingModel({'projects': [1, 0]})
        profile = tailor_facts(form_facts(['Order Tracking REST API', 'Log Analytics Pipeline']),
                               'Backend Software Engineer', 3, INTELLIGENCE, model)
        self.assertEqual(len(model.calls), 1)
        schema = model.calls[0]['schema']
        self.assertEqual(list(schema['properties']), ['projects'])
        self.assertFalse(schema['additionalProperties'])
        data = json.loads(model.calls[0]['prompt'].split('DATA:\n', 1)[1])
        self.assertEqual(data['groups'], {'projects': ['Order Tracking REST API', 'Log Analytics Pipeline']})
        self.assertEqual(profile['projects'], ['Log Analytics Pipeline', 'Order Tracking REST API'])

    def test_schema_bounds_every_array(self):
        schema = ranking_schema({'skills': ['a', 'b', 'c'], 'work_0': ['x', 'y']})
        self.assertEqual(schema['required'], ['skills', 'work_0'])
        self.assertEqual(schema['properties']['skills'],
                         {'type': 'array', 'maxItems': 3, 'items': {'type': 'integer', 'minimum': 0, 'maximum': 2}})
        self.assertEqual(schema['properties']['work_0']['maxItems'], 2)

    def test_nothing_to_rank_skips_the_model(self):
        class NoCalls:
            def generate(self, *args, **kwargs):
                raise AssertionError('ranking a single line needs no model call')
        facts = form_facts(['Only project'], skills=['SQL'], bullets=['Built one API.'])
        profile = tailor_facts(facts, 'Backend Software Engineer', 3, INTELLIGENCE, NoCalls())
        self.assertEqual(profile['projects'], ['Only project'])
        self.assertEqual(profile['skills'], ['SQL'])
        self.assertEqual(profile['experience'][0]['bullets'], ['Built one API.'])

    def test_ranks_for_unrequested_groups_are_ignored(self):
        model = RecordingModel({'projects': [1, 0], 'skills': [0], 'work_1': [5]})
        profile = tailor_facts(form_facts(['A', 'B'], skills=['SQL']), 'Backend Software Engineer', 3,
                               INTELLIGENCE, model)
        self.assertEqual(profile['projects'], ['B', 'A'])
        self.assertEqual(profile['skills'], ['SQL'])


if __name__ == '__main__':
    unittest.main()
