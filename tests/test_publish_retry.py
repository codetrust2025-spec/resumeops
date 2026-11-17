"""Publishing build outputs waits out brief Windows file locks instead of failing the job."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resume_bot import pipeline


class PublishRetryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / 'result.json'
        self.source.write_text('{}')
        self.target = self.root / 'published.json'

    def test_transient_lock_is_retried(self):
        real = os.replace
        calls = []

        def locked_twice(source, target):
            calls.append(source)
            if len(calls) < 3:
                raise PermissionError(32, 'The process cannot access the file')
            return real(source, target)

        with patch.object(pipeline.os, 'replace', side_effect=locked_twice), \
                patch.object(pipeline.time, 'sleep') as sleep:
            pipeline._replace(self.source, self.target)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(self.target.is_file())

    def test_persistent_lock_still_fails(self):
        with patch.object(pipeline.os, 'replace', side_effect=PermissionError(32, 'locked')), \
                patch.object(pipeline.time, 'sleep'):
            with self.assertRaises(PermissionError):
                pipeline._replace(self.source, self.target, attempts=3)
        self.assertTrue(self.source.is_file())


if __name__ == '__main__':
    unittest.main()
