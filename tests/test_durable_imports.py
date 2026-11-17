import tempfile
import json
import time
import unittest
from contextlib import closing
from pathlib import Path

from dashboard.durable_imports import DurableImports, ImportConflict, RETENTION


class DurableImportTests(unittest.TestCase):
    def test_interrupted_job_is_recovered_and_scoped_to_its_owner(self):
        calls = []

        def draft(extension, document, report):
            calls.append((extension, document))
            report('Extracting candidate details with Ollama')
            return {'profile': {'full_name': 'Synthetic Example'}}

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = DurableImports(root, draft)
            original.stop()
            original.enqueue('alice', 'request', '.txt', b'sample')
            with closing(original.connection()) as db, db:
                db.execute("UPDATE imports SET status='processing'")
            restarted = DurableImports(root, draft)
            restarted.start()
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if restarted.get('alice', 'request')['status'] == 'complete':
                        break
                    time.sleep(0.05)
                same = restarted.enqueue('alice', 'request', '.txt', b'sample')
                self.assertEqual(same['status'], 'complete')
                self.assertEqual(same['profile']['full_name'], 'Synthetic Example')
                self.assertIsNone(restarted.get('bob', 'request'))
                with self.assertRaises(ImportConflict):
                    restarted.enqueue('alice', 'request', '.txt', b'different')
                self.assertEqual(calls, [('.txt', b'sample')])
                with closing(restarted.connection()) as db:
                    self.assertIsNone(db.execute('SELECT document FROM imports').fetchone()[0])
            finally:
                restarted.stop()


    def test_progress_api_reports_measured_fields_without_time_estimates(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = DurableImports(Path(folder), lambda *args: None)
            queue.stop()
            queue.enqueue('alice', 'progress', '.pdf', b'synthetic')
            with closing(queue.connection()) as db, db:
                db.execute("UPDATE imports SET status='processing',stage=?,metrics=? WHERE id=?",
                           ('Extracting candidate details with Ollama',
                            json.dumps({'fields_emitted': 8, 'fields_total': 15, 'chars': 2407}),
                            'progress'))
            read = queue.get('alice', 'progress')
            self.assertEqual(read['progress']['fields_emitted'], 8)
            self.assertEqual(read['progress']['fields_total'], 15)
            self.assertGreater(read['progress']['percent'], 35)
            self.assertLess(read['progress']['percent'], 80)
            self.assertEqual(read['metrics']['chars'], 2407)
            self.assertIsNone(queue.get('bob', 'progress'))
            queue.stop()

    def test_stale_queued_document_is_removed_before_inference(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = DurableImports(Path(folder), lambda *args: self.fail('Expired input was processed'))
            queue.stop()
            queue.enqueue('alice', 'old', '.txt', b'abandoned document')
            with closing(queue.connection()) as db, db:
                db.execute('UPDATE imports SET activity=?', (time.time() - RETENTION - 1,))
            restarted = DurableImports(Path(folder), queue.draft)
            restarted.start()
            try:
                deadline = time.monotonic() + 5
                while restarted.get('alice', 'old') is not None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIsNone(restarted.get('alice', 'old'))
            finally:
                restarted.stop()
