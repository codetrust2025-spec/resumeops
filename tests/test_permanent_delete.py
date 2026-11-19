"""Permanent deletion of resumes: one, all, cancellation and confirmation, account isolation, protected
records, partial failures, the busy builder, and nothing reappearing afterwards.

Every resume here is fictional and is laid out on disk the way the watcher publishes a private build: the
record, the build request in inbox-private, the builder's state entry, the published folder (and an older
build of the same request), the approval records and a retry marker."""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import automation_jobs  # noqa: E402
from automation_jobs import (AutomationJobs, DeleteBusy, DeleteFailed, DeleteRefused,  # noqa: E402
                             PROTECTED_RECORDS)
from resume_dashboard import Workspace, handler  # noqa: E402

FILES = ('resume.pdf', 'overleaf.pdf', 'main.tex', 'overleaf-project.zip', 'result.json', 'profile.json')


class Fixture:
    """A workspace holding fictional resumes for several accounts."""

    def __init__(self, root):
        self.workspace = Workspace(Path(root), code_root=ROOT)
        with patch.object(AutomationJobs, 'start'):
            self.jobs = AutomationJobs(self.workspace)
        self.jobs.start = lambda: None
        self.workspace.private_inbox.mkdir(exist_ok=True)
        self.state_path = self.workspace.private_inbox / '.resume-bot-state.json'
        self.files = {}

    def state(self):
        return json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {}

    def write_state(self, state):
        self.state_path.write_text(json.dumps(state), encoding='utf-8')

    def resume(self, owner, ident, status='handed_off', built=True, candidate='Example Person (fictional)'):
        """One record and, when built, everything the watcher publishes for it."""
        now = time.time()
        job = f'private-{owner}-{hashlib.sha256(ident.encode()).hexdigest()}.json' if built else None
        with closing(self.jobs.connection()) as db, db:
            db.execute('INSERT INTO automation(owner,id,status,stage,percent,created,updated,job,role,candidate,facts) '
                       "VALUES (?,?,?,'stage',50,?,?,?,'Python Backend Developer',?,?)",
                       (owner, ident, status, now, now, job, candidate, json.dumps({'full_name': candidate}) if built else None))
        paths = []
        if built:
            inbox, approvals = self.workspace.private_inbox, self.workspace.approvals
            (inbox / job).write_text('{}', encoding='utf-8')
            (inbox / '.resume-bot-retry').mkdir(exist_ok=True)
            (inbox / '.resume-bot-retry' / job).write_text('', encoding='utf-8')
            (approvals / job[:-5]).mkdir(parents=True)
            (approvals / job[:-5] / 'record.json').write_text('{}', encoding='utf-8')
            folders = []
            for build in ('current', 'older'):
                folder = self.workspace.private_output / f'private-{owner}' / hashlib.sha256((ident + build).encode()).hexdigest()[:12]
                folder.mkdir(parents=True)
                for name in FILES:
                    (folder / name).write_text(f'fictional {name}', encoding='utf-8')
                (folder / 'audit.json').write_text(json.dumps({'source': str(inbox / job)}), encoding='utf-8')
                folders.append(folder)
            state = self.state()
            state[job] = {'status': 'ok', 'percent': 100, 'output': str(folders[0].resolve()), 'candidate': candidate}
            self.write_state(state)
            paths = [inbox / job, inbox / '.resume-bot-retry' / job, approvals / job[:-5], *folders]
        self.files[(owner, ident)] = (job, paths)
        return job, paths

    def row(self, owner, ident):
        with closing(self.jobs.connection()) as db:
            return db.execute('SELECT * FROM automation WHERE owner=? AND id=?', (owner, ident)).fetchone()

    def intact(self, test, owner, ident):
        job, paths = self.files[(owner, ident)]
        test.assertIsNotNone(self.row(owner, ident), 'record kept')
        for path in paths:
            test.assertTrue(path.exists(), f'{path.name} kept')
        if job:
            test.assertIn(job, self.state(), 'builder state entry kept')

    def gone(self, test, owner, ident):
        job, paths = self.files[(owner, ident)]
        test.assertIsNone(self.row(owner, ident), 'record deleted')
        for path in paths:
            test.assertFalse(path.exists(), f'{path} deleted')
        if job:
            test.assertNotIn(job, self.state(), 'builder state entry deleted')

    def holding_folders(self):
        roots = (self.workspace.private_inbox, self.workspace.approvals, self.workspace.private_output)
        return [p for root in roots if root.is_dir() for p in root.iterdir() if p.name.startswith('.deleting-')]


A, B = 'a' * 64, 'b' * 64


def ident(n):
    return f'{n:x}' * 64 if n < 16 else hashlib.sha256(str(n).encode()).hexdigest()


class PermanentDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(self.temp.name)

    def test_delete_one_removes_the_record_and_every_file_built_from_it(self):
        self.f.resume(A, ident(1))
        self.f.resume(A, ident(2))
        self.f.resume(B, ident(3))
        self.assertEqual(self.f.jobs.delete(A, ident(1)), {'deleted': 1, 'kept': []})
        self.f.gone(self, A, ident(1))
        self.f.intact(self, A, ident(2))
        self.f.intact(self, B, ident(3))
        self.assertEqual(self.f.holding_folders(), [], 'nothing left aside')
        self.assertEqual([j['id'] for j in self.f.jobs.list(A)], [ident(2)])

    def test_delete_one_of_a_record_that_never_reached_the_builder(self):
        self.f.resume(A, ident(1), status='needs_input', built=False)
        self.assertEqual(self.f.jobs.delete(A, ident(1))['deleted'], 1)
        self.assertIsNone(self.f.row(A, ident(1)))

    def test_delete_all_removes_every_resume_of_the_account_and_its_leftovers(self):
        self.f.resume(A, ident(1))
        self.f.resume(A, ident(2), status='blocked', built=False)
        self.f.resume(A, ident(3), status='needs_input', built=False)
        self.f.resume(A, ident(4), status='queued', built=False)
        self.f.resume(B, ident(5))
        # Leftovers of the account: an interrupted build folder and the output of a request with no record.
        leftover = self.f.workspace.private_output / f'private-{A}' / 'resume-build-x1'
        leftover.mkdir(parents=True)
        orphan_job = f'private-{A}-{"c" * 64}.json'
        orphan = self.f.workspace.private_output / f'private-{A}' / 'c0ffee000000'
        orphan.mkdir()
        (orphan / 'audit.json').write_text(json.dumps({'source': str(self.f.workspace.private_inbox / orphan_job)}))
        (self.f.workspace.private_inbox / orphan_job).write_text('{}')
        result = self.f.jobs.delete(A)
        self.assertEqual(result, {'deleted': 4, 'kept': []})
        for n in (1, 2, 3, 4):
            self.f.gone(self, A, ident(n))
        self.assertFalse(leftover.exists())
        self.assertFalse(orphan.exists())
        self.assertFalse((self.f.workspace.private_inbox / orphan_job).exists())
        self.f.intact(self, B, ident(5))
        self.assertEqual(self.f.jobs.list(A), [])
        self.assertEqual(len(self.f.jobs.list(B)), 1)

    def test_delete_all_keeps_protected_records_and_a_resume_being_prepared(self):
        self.f.resume(A, ident(1))
        self.f.resume(A, ident(2))  # the acceptance reference record
        self.f.resume(A, ident(3), status='processing', built=False)
        (self.f.workspace.root / PROTECTED_RECORDS).write_text(json.dumps({'records': [ident(2)]}))
        result = self.f.jobs.delete(A)
        self.assertEqual(result['deleted'], 1)
        self.assertCountEqual(result['kept'], [{'id': ident(2), 'reason': 'protected'},
                                               {'id': ident(3), 'reason': 'in_progress'}])
        self.f.gone(self, A, ident(1))
        self.f.intact(self, A, ident(2))
        self.assertIsNotNone(self.f.row(A, ident(3)))

    def test_a_protected_record_or_one_being_prepared_cannot_be_deleted_alone(self):
        self.f.resume(A, ident(1))
        self.f.resume(A, ident(2), status='processing', built=False)
        (self.f.workspace.root / PROTECTED_RECORDS).write_text(json.dumps({'records': [ident(1)]}))
        with self.assertRaises(DeleteRefused) as refused:
            self.f.jobs.delete(A, ident(1))
        self.assertEqual(refused.exception.status, 403)
        with self.assertRaises(DeleteRefused) as refused:
            self.f.jobs.delete(A, ident(2))
        self.assertEqual(refused.exception.status, 409)
        self.f.intact(self, A, ident(1))

    def test_an_unreadable_protected_list_stops_every_deletion(self):
        self.f.resume(A, ident(1))
        (self.f.workspace.root / PROTECTED_RECORDS).write_text('{not json')
        with self.assertRaises(DeleteFailed):
            self.f.jobs.delete(A)
        self.f.intact(self, A, ident(1))

    def test_another_account_cannot_delete_or_detect_a_resume(self):
        self.f.resume(A, ident(1))
        with self.assertRaises(DeleteRefused) as refused:
            self.f.jobs.delete(B, ident(1))
        self.assertEqual(refused.exception.status, 404)
        self.assertEqual(self.f.jobs.delete(B), {'deleted': 0, 'kept': []})
        self.f.intact(self, A, ident(1))

    def test_shared_build_files_are_kept_while_another_record_uses_them(self):
        job, paths = self.f.resume(A, ident(1))
        now = time.time()
        with closing(self.f.jobs.connection()) as db, db:  # a second record of the same build request
            db.execute("INSERT INTO automation(owner,id,status,created,updated,job) VALUES (?,?,'handed_off',?,?,?)",
                       (A, ident(2), now, now, job))
        self.f.jobs.delete(A, ident(1))
        self.assertIsNone(self.f.row(A, ident(1)))
        for path in paths:
            self.assertTrue(path.exists(), f'{path.name} still used by the other record')
        self.assertIn(job, self.f.state())

    def test_a_file_that_cannot_be_removed_leaves_everything_in_place(self):
        job, paths = self.f.resume(A, ident(1))
        self.f.resume(A, ident(2))
        before = self.f.state()
        real = automation_jobs._rename
        blocked = paths[2]  # the approval records, after the build request and retry marker moved

        def rename(source, destination, attempts=10):
            if Path(source) == blocked:
                raise PermissionError('in use')
            return real(source, destination, attempts)
        with patch.object(automation_jobs, '_rename', rename):
            with self.assertRaises(DeleteFailed) as failed:
                self.f.jobs.delete(A)
        self.assertIn('Nothing was deleted', str(failed.exception))
        self.f.intact(self, A, ident(1))
        self.f.intact(self, A, ident(2))
        self.assertEqual(self.f.state(), before)
        self.f.jobs.purge_deleted()
        self.assertEqual(self.f.holding_folders(), [])

    def test_a_file_that_cannot_be_put_back_is_never_swept(self):
        job, paths = self.f.resume(A, ident(1))
        real = automation_jobs._rename

        def rename(source, destination, attempts=10):
            if Path(source) == paths[2] or Path(destination) == paths[0]:
                raise PermissionError('in use')  # the approvals cannot move; the build request cannot go back
            return real(source, destination, attempts)
        with patch.object(automation_jobs, '_rename', rename):
            with self.assertRaises(DeleteFailed):
                self.f.jobs.delete(A, ident(1))
        self.assertIsNotNone(self.f.row(A, ident(1)), 'record kept')
        self.f.jobs.purge_deleted()
        kept = [p for p in self.f.workspace.private_inbox.iterdir() if p.name.startswith('.undeleted-')]
        self.assertEqual(len(kept), 1, 'set aside for the operator, not swept')
        self.assertEqual([item.read_text() for item in kept[0].iterdir()], ['{}'])

    def test_a_failure_recording_the_deletion_restores_everything(self):
        self.f.resume(A, ident(1))
        before = self.f.state()
        with patch('resume_bot.storage.write_json', side_effect=OSError('disk full')):
            with self.assertRaises(DeleteFailed):
                self.f.jobs.delete(A, ident(1))
        self.f.intact(self, A, ident(1))
        self.assertEqual(self.f.state(), before)

    def test_a_link_where_resume_files_belong_stops_the_deletion(self):
        _, paths = self.f.resume(A, ident(1))
        outside = Path(self.temp.name) / 'backups'
        outside.mkdir()
        (outside / 'backup.zip').write_text('production backup')
        link = paths[3] / 'linked'
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            try:
                import _winapi
                _winapi.CreateJunction(str(outside), str(link))
            except (ImportError, OSError):
                self.skipTest('links cannot be created here')
        with self.assertRaises(DeleteFailed):
            self.f.jobs.delete(A, ident(1))
        self.assertTrue((outside / 'backup.zip').exists())
        self.f.intact(self, A, ident(1))

    def test_a_busy_builder_deletes_nothing_built_but_unbuilt_records_still_go(self):
        self.f.resume(A, ident(1))
        self.f.resume(A, ident(2), status='needs_input', built=False)
        lock = sqlite3.connect(self.f.workspace.private_inbox / automation_jobs.BUILDER_LOCK, timeout=0)
        lock.execute('BEGIN EXCLUSIVE')
        try:
            with self.assertRaises(DeleteBusy):
                self.f.jobs.delete(A, ident(1), wait=0.2)
            with self.assertRaises(DeleteBusy):
                self.f.jobs.delete(A, wait=0.2)
            self.f.intact(self, A, ident(1))
            self.assertEqual(self.f.jobs.delete(A, ident(2), wait=0.2)['deleted'], 1)
        finally:
            lock.close()
        self.assertEqual(self.f.jobs.delete(A, ident(1))['deleted'], 1)

    def test_a_deleted_resume_cannot_reappear(self):
        from resume_bot.automation import AnswerError
        self.f.resume(A, ident(1))
        self.f.jobs.delete(A, ident(1))
        self.assertEqual(self.f.jobs.list(A), [])
        self.assertEqual(self.f.jobs.list(A, ident(1)), [])
        with self.assertRaises(AnswerError):
            self.f.jobs.regenerate(A, ident(1), 'Python Backend Developer')
        self.assertEqual(self.f.jobs.list(A), [], 'regeneration created nothing')

    def test_records_hidden_by_the_earlier_soft_delete_are_listed_again(self):
        self.f.resume(A, ident(1))
        with closing(self.f.jobs.connection()) as db, db:
            db.execute('ALTER TABLE automation ADD COLUMN hidden_at TEXT')
            db.execute('UPDATE automation SET hidden_at=? WHERE id=?', (str(time.time()), ident(1)))
        self.assertEqual([j['id'] for j in self.f.jobs.list(A)], [ident(1)])
        self.f.intact(self, A, ident(1))

    def test_folders_left_aside_by_an_interrupted_removal_are_cleared_later(self):
        stray = self.f.workspace.private_output / '.deleting-0123456789abcdef' / '0'
        stray.mkdir(parents=True)
        (stray / 'resume.pdf').write_text('fictional')
        self.f.jobs.purge_deleted()
        self.assertEqual(self.f.holding_folders(), [])


class DeleteEndpointTests(unittest.TestCase):
    """The HTTP route: same-origin token, the typed confirmations, and the signed-in account only."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = Fixture(self.temp.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.f.workspace, '', 'token'))
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.server.RequestHandlerClass = handler(self.f.workspace, self.origin, 'token', automation_first=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.me = hashlib.sha256((self.origin + '\0loopback').encode()).hexdigest()

    def tearDown(self):
        self.server.shutdown()
        self.server.RequestHandlerClass.stop_imports()
        self.server.server_close()
        self.temp.cleanup()

    def post(self, body, headers=None):
        headers = {'Origin': self.origin, 'X-Dashboard-Token': 'token', 'Content-Type': 'application/json',
                   **(headers or {})}
        request = Request(self.origin + '/api/automation/delete', data=json.dumps(body).encode(), headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read() or b'{}')

    def test_confirmation_token_and_origin_are_required(self):
        self.f.resume(self.me, ident(1))
        for body, headers, status in (
                ({'id': ident(1), 'confirm': 'DELETE'}, {'X-Dashboard-Token': 'wrong'}, 403),
                ({'id': ident(1), 'confirm': 'DELETE'}, {'Origin': 'https://attacker.example'}, 403),
                ({'id': ident(1)}, {}, 400),
                ({'id': ident(1), 'confirm': 'yes'}, {}, 400),
                ({'all': True}, {}, 400),
                ({'all': True, 'confirm': 'delete all'}, {}, 400),
                ({'all': True, 'confirm': 'DELETE ALL', 'id': ident(1)}, {}, 400),
                ({'id': 'not-an-id', 'confirm': 'DELETE'}, {}, 400)):
            with self.subTest(body=body, headers=headers):
                self.assertEqual(self.post(body, headers)[0], status)
        self.f.intact(self, self.me, ident(1))

    def test_delete_one_then_its_downloads_and_history_are_gone(self):
        _, paths = self.f.resume(self.me, ident(1))
        self.f.resume(self.me, ident(2))
        key = f'private-{self.me}/{paths[3].name}'
        self.assertEqual(self.post({'id': ident(1), 'confirm': 'DELETE'}), (200, {'deleted': 1, 'kept': []}))
        self.f.gone(self, self.me, ident(1))
        self.f.intact(self, self.me, ident(2))
        with self.assertRaises(HTTPError) as missing:
            urlopen(Request(f'{self.origin}/api/artifact?key={key}&name=resume.pdf'), timeout=5)
        self.assertEqual(missing.exception.code, 404)
        with urlopen(Request(self.origin + '/api/automation'), timeout=5) as response:
            self.assertEqual([j['id'] for j in json.loads(response.read())['jobs']], [ident(2)])
        self.assertEqual(self.post({'id': ident(1), 'confirm': 'DELETE'})[0], 404, 'already deleted')

    def test_delete_all_covers_only_the_signed_in_account(self):
        self.f.resume(self.me, ident(1))
        self.f.resume(self.me, ident(2), status='needs_input', built=False)
        self.f.resume(B, ident(3))
        durable = self.f.workspace.root / '.resumeops-imports.sqlite3'
        with closing(sqlite3.connect(durable)) as db, db:
            for owner, n in ((self.me, 'm'), (B, 'o')):
                db.execute("INSERT INTO imports(owner,id,extension,document,status,created,activity) "
                           "VALUES (?,?,'.pdf',x'00','complete',?,?)", (owner, n * 32, time.time(), time.time()))
        self.assertEqual(self.post({'id': ident(3), 'confirm': 'DELETE'})[0], 404, "another account's resume")
        status, body = self.post({'all': True, 'confirm': 'DELETE ALL'})
        self.assertEqual((status, body), (200, {'deleted': 2, 'kept': [], 'drafts': 1}))
        self.f.gone(self, self.me, ident(1))
        self.f.gone(self, self.me, ident(2))
        self.f.intact(self, B, ident(3))
        with closing(sqlite3.connect(durable)) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT owner FROM imports')], [B])

    def test_a_busy_builder_is_reported_and_nothing_changes(self):
        self.f.resume(self.me, ident(1))
        lock = sqlite3.connect(self.f.workspace.private_inbox / automation_jobs.BUILDER_LOCK, timeout=0)
        lock.execute('BEGIN EXCLUSIVE')
        original = AutomationJobs.delete
        try:
            with patch.object(AutomationJobs, 'delete', lambda jobs, owner, identifier=None:
                              original(jobs, owner, identifier, wait=0.2)):
                status, body = self.post({'id': ident(1), 'confirm': 'DELETE'})
        finally:
            lock.close()
        self.assertEqual(status, 503)
        self.assertIn('Nothing was deleted', body['error'])
        self.f.intact(self, self.me, ident(1))


if __name__ == '__main__':
    unittest.main()
