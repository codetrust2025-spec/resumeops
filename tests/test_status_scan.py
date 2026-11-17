"""The status endpoint is polled by every open page and by the supervisor's 5-second health
probe. It must stay cheap as outputs grow: no per-file Path.resolve() (a handle open on Windows,
which real-time scanning makes slow) and no re-parsing of unchanged result.json files."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import resume_dashboard  # noqa: E402
from resume_dashboard import ARTIFACTS, Workspace  # noqa: E402


def link_directory(target, link):
    if os.name == 'nt':
        subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(target)],
                       check=True, capture_output=True, timeout=10)
    else:
        os.symlink(target, link, target_is_directory=True)


def make_revision(output, candidate, revision, files=ARTIFACTS, result=None):
    folder = output / candidate / revision
    folder.mkdir(parents=True)
    for name in files:
        (folder / name).write_text(json.dumps(result or {'candidate': candidate.title(), 'model': 'm',
                                                         'pdf_checks': {'pages': 1}}))
    return folder


class StatusScanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = Workspace(self.root, code_root=ROOT)
        self.workspace.output.mkdir()

    def test_revisions_listed_with_their_artifacts(self):
        make_revision(self.workspace.output, 'alpha', 'aaa111')
        make_revision(self.workspace.output, 'beta', 'bbb222', files=('result.json', 'resume.pdf'))
        make_revision(self.workspace.output, 'gamma', 'ccc333', files=('resume.pdf',))  # no result.json
        revisions = {item['key']: item for item in self.workspace.status()['revisions']}
        self.assertEqual(set(revisions), {'alpha/aaa111', 'beta/bbb222'})
        self.assertEqual(revisions['alpha/aaa111']['files'], list(ARTIFACTS))
        self.assertEqual(revisions['beta/bbb222']['files'], ['resume.pdf', 'result.json'])
        self.assertEqual(revisions['beta/bbb222']['pages'], 1)

    def test_status_opens_no_handles_and_parses_unchanged_results_once(self):
        for index in range(30):
            make_revision(self.workspace.output, f'candidate-{index}', f'{index:012x}')
        real_read = resume_dashboard.read_json
        reads = []
        with patch.object(Path, 'resolve', side_effect=AssertionError('status must not resolve paths')), \
                patch.object(resume_dashboard, 'read_json', side_effect=lambda path: reads.append(path) or real_read(path)):
            first = self.workspace.status()
            parsed = sum(1 for path in reads if path.name == 'result.json')
            second = self.workspace.status()
            reparsed = sum(1 for path in reads if path.name == 'result.json') - parsed
        self.assertEqual(len(first['revisions']), 30)
        self.assertEqual(parsed, 30)
        self.assertEqual(reparsed, 0)
        self.assertEqual(first['revisions'], second['revisions'])

    def test_changed_result_is_reparsed(self):
        folder = make_revision(self.workspace.output, 'alpha', 'aaa111')
        self.assertEqual(self.workspace.status()['revisions'][0]['candidate'], 'Alpha')
        (folder / 'result.json').write_text(json.dumps({'candidate': 'Alpha Renamed', 'padding': 'x' * 10}))
        self.assertEqual(self.workspace.status()['revisions'][0]['candidate'], 'Alpha Renamed')

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlinks unavailable')
    def test_symlinked_folders_outside_output_are_ignored(self):
        outside = Path(tempfile.mkdtemp(dir=self.root))
        make_revision(outside, 'escape', 'eee555')
        link_directory(outside / 'escape', self.workspace.output / 'escape')
        target = make_revision(self.workspace.output, 'real', 'rrr666', files=('result.json',))
        if os.name != 'nt':
            os.symlink(outside / 'escape' / 'eee555' / 'resume.pdf', target / 'resume.pdf')
        revisions = self.workspace.status()['revisions']
        self.assertEqual([item['key'] for item in revisions], ['real/rrr666'])
        self.assertEqual(revisions[0]['files'], ['result.json'])

    def test_revision_key_is_path_arithmetic(self):
        output = self.workspace.output
        self.assertEqual(self.workspace.revision_key(str(output / 'alpha' / 'aaa111')), 'alpha/aaa111')
        self.assertEqual(self.workspace.revision_key(str(output / 'alpha')), '')
        self.assertEqual(self.workspace.revision_key(str(output.parent / 'elsewhere' / 'x')), '')
        self.assertEqual(self.workspace.revision_key(str(output / 'alpha' / '..' / '..' / 'x' / 'y')), '')
        self.assertEqual(self.workspace.revision_key(''), '')


class StatusScanReviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_failed_result_read_is_retried_on_the_next_poll(self):
        workspace = Workspace(self.root, code_root=ROOT)
        workspace.output.mkdir()
        make_revision(workspace.output, 'cand', 'abc123def456')
        real = resume_dashboard.read_json
        with patch.object(resume_dashboard, 'read_json',
                          side_effect=lambda path: None if path.name == 'result.json' else real(path)):
            self.assertEqual(workspace.status()['revisions'], [])
        self.assertEqual([item['key'] for item in workspace.status()['revisions']], ['cand/abc123def456'])

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlinks unavailable')
    def test_revision_key_when_output_is_a_link(self):
        storage = self.root / 'storage'
        storage.mkdir()
        data = self.root / 'data'
        data.mkdir()
        link_directory(storage, data / 'output')
        workspace = Workspace(data, code_root=ROOT)
        real_output = storage / 'cand' / 'abc123def456'
        self.assertEqual(workspace.revision_key(str(real_output)), 'cand/abc123def456')
        self.assertEqual(workspace.revision_key(str(data / 'output' / 'cand' / 'abc123def456')), 'cand/abc123def456')
        self.assertEqual(workspace.revision_key(str(storage / 'cand')), '')

    def test_revision_key_windows_path_forms(self):
        import ntpath
        from pathlib import PureWindowsPath
        workspace = Workspace(self.root, code_root=ROOT)
        workspace._output_bases = [ntpath.normcase(r'C:\Users\codet\AI_Resume_Builder_v2_Windows\output'),
                                   ntpath.normcase(r'D:\store\output')]
        cases = {
            r'C:\Users\codet\AI_Resume_Builder_v2_Windows\output\cand\abc': 'cand/abc',
            r'c:\users\CODET\AI_Resume_Builder_v2_Windows\OUTPUT\cand\abc': 'cand/abc',
            r'\\?\C:\Users\codet\AI_Resume_Builder_v2_Windows\output\cand\abc': 'cand/abc',
            r'D:\store\output\cand\abc': 'cand/abc',
            r'E:\elsewhere\cand\abc': '',
            r'C:\Users\codet\AI_Resume_Builder_v2_Windows\output\cand': '',
            r'C:\Users\codet\AI_Resume_Builder_v2_Windows\output\..\x\y': '',
            r'C:\Users\codet\AI_Resume_Builder_v2_Windows\output\Cand\ABC': 'Cand/ABC',
            r'\\?\UNC\server\share\output\cand\abc': '',
        }
        with patch.object(resume_dashboard.os, 'path', ntpath), patch.object(resume_dashboard, 'Path', PureWindowsPath):
            for raw, expected in cases.items():
                with self.subTest(raw=raw):
                    self.assertEqual(workspace.revision_key(raw), expected)

    def test_reparse_points_are_skipped_without_opening_them(self):
        class Info:
            st_file_attributes = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT

        class Entry:
            def is_symlink(self):
                return False

            def is_junction(self):
                return False

            def stat(self, follow_symlinks=True):
                return Info()

        with patch.object(resume_dashboard.stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400, create=True):
            self.assertTrue(resume_dashboard.linked(Entry()))
            Info.st_file_attributes = 0x10
            self.assertFalse(resume_dashboard.linked(Entry()))

    def test_status_with_finished_jobs_opens_no_handles(self):
        workspace = Workspace(self.root, code_root=ROOT)
        workspace.output.mkdir()
        state = {}
        for index in range(10):
            folder = make_revision(workspace.output, f'cand-{index}', f'{index:012x}')
            name = f'cand-{index}-{index:064x}.json'
            (workspace.inbox / name).write_text('{}')
            state[name] = {'status': 'ok', 'last_attempt': 4102444800, 'output': str(folder)}
        (workspace.inbox / '.resume-bot-state.json').write_text(json.dumps(state))
        with patch.object(Path, 'resolve', side_effect=AssertionError('status must not resolve paths')):
            jobs = workspace.status()['jobs']
        self.assertEqual(sorted(job['revision'] for job in jobs), sorted(f'cand-{i}/{i:012x}' for i in range(10)))


class SupervisorToleranceTests(unittest.TestCase):
    def test_single_slow_probe_does_not_restart_the_dashboard(self):
        import dashboard_supervisor as supervisor
        probes = iter([RuntimeError('slow'), RuntimeError('slow'), None, RuntimeError('slow'),
                       RuntimeError('slow'), RuntimeError('slow'), KeyboardInterrupt()])
        stops = []

        class Process:
            pid = 1

            def poll(self):
                return None

        def health(port, config):
            outcome = next(probes)
            if outcome:
                raise outcome

        with patch.object(supervisor, 'read', return_value={'active': 'r'}), \
                patch.object(supervisor, 'listen', return_value=Process()), \
                patch.object(supervisor, 'health', side_effect=health), \
                patch.object(supervisor, 'stop', side_effect=lambda process: stops.append(process)), \
                patch.object(supervisor.time, 'sleep'):
            with self.assertRaises(KeyboardInterrupt):
                supervisor.service(1, {}, None)
        # Two failures, a success, then three in a row: exactly one restart (plus final cleanup).
        self.assertEqual(len([process for process in stops if process is not None]), 2)


if __name__ == '__main__':
    unittest.main()
