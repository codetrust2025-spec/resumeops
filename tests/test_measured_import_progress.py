"""Completion percentage must describe real work milestones and emitted JSON fields."""
import unittest
import json
from dashboard.durable_imports import measured_progress
from resume_bot.ollama import _TopLevelJsonFields


class MeasuredProgressTests(unittest.TestCase):
    def test_stream_counts_only_finished_top_level_json_fields(self):
        text = json.dumps({'name': 'Smith, Jane', 'skills': ['C++, Rust'],
                           'job': {'a': 1, 'b': [1, 2]}, 'extra': 'quoted "comma"'})
        meter = _TopLevelJsonFields()
        counts = [meter.feed(part) for part in text]
        self.assertEqual(counts[-1], 4)
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(_TopLevelJsonFields().feed('{}'), 0)
        self.assertEqual(_TopLevelJsonFields().feed('{"a":1,"b":[1,2]}'), 2)

    def test_real_resume_import_milestones_progress_and_never_100_early(self):
        phase = lambda status, stage, metrics=None: measured_progress(
            status, stage, metrics, '.pdf')
        queued = phase('queued', 'Reading document')
        reading = phase('processing', 'Reading document')
        ready = phase('processing', 'Document text ready')
        generating = phase('processing', 'Extracting candidate details with Ollama',
                           {'fields_emitted': 4, 'fields_total': 15})
        growing = phase('processing', 'Extracting candidate details with Ollama',
                        {'fields_emitted': 13, 'fields_total': 15})
        emitted = phase('processing', 'Ollama output received')
        validated = phase('processing', 'Candidate fields validated')
        complete = phase('complete', 'Draft ready for review')
        values = [v['percent'] for v in
                  (queued, reading, ready, generating, growing, emitted, validated, complete)]
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[-1], 100)
        self.assertTrue(all(n < 100 for n in values[:-1]))
        self.assertEqual(generating['steps_completed'], 2)
        self.assertEqual(generating['fields_emitted'], 4)
        self.assertEqual(complete['steps_completed'], 5)
        self.assertEqual(complete['steps_total'], 5)

    def test_ai_draft_has_own_eight_field_total(self):
        stage = measured_progress('processing', 'Drafting suggestions with Ollama',
                                  {'fields_emitted': 4}, '.ai')
        self.assertEqual(stage['fields_total'], 8)
        self.assertGreater(stage['percent'], 35)
        self.assertLess(stage['percent'], 80)
        self.assertEqual(measured_progress(
            'complete', 'Draft ready for review', extension='.ai')['percent'], 100)

    def test_fallback_and_errors_never_claim_completion(self):
        fallback = measured_progress('processing', 'Ollama retrying without streaming', {}, '.pdf')
        self.assertLess(fallback['percent'], 100)
        self.assertLess(measured_progress('error', 'Import failed')['percent'], 100)


if __name__ == '__main__':
    unittest.main()
