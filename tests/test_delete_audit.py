"""Who deleted what, and when: every Delete and Delete all request is recorded.

Before, the dashboard kept no record, so a Delete all could not be traced to an account or a client. Now each request
that reaches the deletion writes one audit row: the account key, the time, Delete or Delete all, the resume, the
outcome and counts, and the client (access path, address, browser). A deletion and its row commit together; a refusal
or failure is recorded on its own; Delete all never removes the records. Every resume here is fictional."""
import json
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_permanent_delete as base  # noqa: E402  (a module import, so its test classes are not collected twice)
from automation_jobs import AutomationJobs, DeleteFailed  # noqa: E402

ident, B = base.ident, base.B
BROWSER = 'Mozilla/5.0 (fictional test browser)'


class AuditTests(base.DeleteEndpointTests):
    """Runs against the real HTTP route, signed in as the loopback account (inherits the endpoint fixture)."""

    def post(self, body, headers=None):
        return super().post(body, {'User-Agent': BROWSER, **(headers or {})})

    def records(self, everyone=False):
        url = self.origin + '/api/automation/audit' + ('?all=1' if everyone else '')
        with urlopen(Request(url), timeout=5) as response:
            return json.loads(response.read())['records']

    # The inherited endpoint tests run here too; these add the audit records.
    def test_a_deletion_is_recorded_with_account_time_and_client(self):
        self.f.resume(self.me, ident(1))
        before = time.time()
        self.assertEqual(self.post({'id': ident(1), 'confirm': 'DELETE'})[0], 200)
        [record] = self.records()
        self.assertEqual({k: record[k] for k in ('owner', 'action', 'target', 'outcome', 'deleted', 'kept')},
                         {'owner': self.me, 'action': 'delete', 'target': ident(1), 'outcome': 'deleted',
                          'deleted': 1, 'kept': 0})
        self.assertGreaterEqual(record['at'], before)
        self.assertEqual(record['client'], {'via': 'loopback', 'address': '127.0.0.1', 'agent': BROWSER})

    def test_delete_all_is_recorded_and_never_removes_the_records(self):
        self.f.resume(self.me, ident(1))
        self.f.resume(self.me, ident(2))
        self.post({'id': ident(1), 'confirm': 'DELETE'})
        self.assertEqual(self.post({'all': True, 'confirm': 'DELETE ALL'})[0], 200)
        records = self.records()
        self.assertEqual([(r['action'], r['target'], r['deleted']) for r in records],
                         [('delete_all', None, 1), ('delete', ident(1), 1)])

    def test_a_refused_request_is_recorded_too(self):
        self.f.resume(B, ident(3))
        self.assertEqual(self.post({'id': ident(3), 'confirm': 'DELETE'})[0], 404)
        [record] = self.records()
        self.assertEqual((record['action'], record['deleted']), ('delete', 0))
        self.assertTrue(record['outcome'].startswith('refused: '))
        self.f.intact(self, B, ident(3))

    def test_a_forwarded_address_is_believed_only_from_cloudflare(self):
        self.f.resume(self.me, ident(1))
        self.post({'id': ident(1), 'confirm': 'DELETE'}, {'Cf-Connecting-IP': '203.0.113.9'})
        self.assertEqual(self.records()[0]['client']['address'], '127.0.0.1', 'a loopback request cannot claim one')

    def test_an_account_sees_only_its_own_records(self):
        self.f.resume(self.me, ident(1))
        self.post({'id': ident(1), 'confirm': 'DELETE'})
        self.f.jobs.audit(B, None, 'deleted', {'via': 'web'}, deleted=4)
        self.assertEqual([r['owner'] for r in self.records()], [self.me])
        # The loopback session is the operator here: it may ask for every account's records.
        self.assertEqual(sorted(r['owner'] for r in self.records(everyone=True)), sorted([self.me, B]))


class AtomicRecordTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = base.Fixture(self.temp.name)

    def test_without_its_record_nothing_is_deleted(self):
        self.f.resume(B, ident(1))
        real = AutomationJobs.audit

        def failing(jobs, owner, identifier, outcome, client=None, db=None, **counts):
            if db is not None:
                raise sqlite3.OperationalError('disk full')
            return real(jobs, owner, identifier, outcome, client, **counts)
        with patch.object(AutomationJobs, 'audit', failing), self.assertRaises(DeleteFailed):
            self.f.jobs.delete(B, ident(1), client={'via': 'test'})
        self.f.intact(self, B, ident(1))
        with closing(self.f.jobs.connection()) as db:
            [row] = db.execute('SELECT outcome FROM audit').fetchall()
        self.assertTrue(row['outcome'].startswith('failed: '))


if __name__ == '__main__':
    unittest.main()
