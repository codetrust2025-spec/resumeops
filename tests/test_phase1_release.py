"""Phase 1 release: AI builder beside the classic homepage, permanent approval records,
verbatim rendering of approved resumes, and an exhaustive PDF claim audit."""
import json
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import dashboard_supervisor  # noqa: E402
from durable_imports import RETENTION, DurableImports  # noqa: E402
from resume_dashboard import Workspace, handler  # noqa: E402
from resume_bot.claim_audit import claim_violations, unaccounted_pdf_words
from resume_bot.profile import ProfileError
from resume_bot.workflow import normalize_request

PROFILE = {'full_name': 'Phase One Synthetic', 'email': 'phase1@example.com',
           'summary': 'Engineer focused on Python services.', 'skills': ['Python', 'SQL'],
           'experience': [{'company': 'Example Co', 'position': 'Engineer', 'start': 'Jan 2020',
                           'end': 'Present', 'bullets': ['Maintained billing APIs']}],
           'education': [{'institution': 'Example University', 'degree': 'BSc', 'end': '2019'}]}
CLAIMS = [
    {'id': '0', 'section': 'Summary', 'text': 'Engineer focused on Python services.',
     'provenance': 'unverified_suggestion', 'candidate_confirmed': True},
    {'id': '1', 'section': 'Skill', 'text': 'Python', 'provenance': 'verified_from_source', 'candidate_confirmed': True},
    {'id': '2', 'section': 'Skill', 'text': 'SQL', 'provenance': 'unsupported_role_keyword', 'candidate_confirmed': True},
    {'id': '3', 'section': 'Responsibility', 'text': 'Maintained billing APIs',
     'provenance': 'candidate_confirmed', 'candidate_confirmed': True},
]
REQUEST = {'profile': PROFILE, 'target': {'role': 'backend-developer', 'template': 'standard', 'experience_years': 5}}


def approval(claims=CLAIMS):
    return {'operation': 'a' * 32, 'approved_by': 'f' * 64, 'claims': claims}


class ApprovedRequestTests(unittest.TestCase):
    def test_approved_marker_must_be_literal_true(self):
        _, target = normalize_request({**REQUEST, 'target': {**REQUEST['target'], 'approved': True}})
        self.assertIs(target['approved'], True)
        with self.assertRaises(ProfileError):
            normalize_request({**REQUEST, 'target': {**REQUEST['target'], 'approved': 'yes'}})
        self.assertNotIn('approved', normalize_request(REQUEST)[1])

    def test_classic_queue_cannot_mark_a_resume_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory), code_root=ROOT)
            data = json.dumps({**REQUEST, 'target': {**REQUEST['target'], 'approved': True}}).encode()
            with self.assertRaises(ValueError):
                workspace.enqueue(data)
            self.assertFalse(list(workspace.inbox.glob('*.json')))
            self.assertFalse(workspace.approvals.exists())

    def test_approved_resume_is_rendered_without_ai_rewording(self):
        from resume_bot.pipeline import process
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'job.json'
            source.write_text(json.dumps({**REQUEST, 'target': {**REQUEST['target'], 'approved': True}}))
            with patch('resume_bot.pipeline.polish', side_effect=AssertionError('approved resume was reworded')):
                result = process(source, Path(directory) / 'output', polish_enabled=True)
            self.assertEqual(result['ai_model'] if 'ai_model' in result else 'disabled', 'disabled')
            profile = json.loads((Path(result['output']) / 'profile.json').read_text())
            self.assertEqual(profile['summary'], PROFILE['summary'])
            self.assertEqual(profile['experience'][0]['bullets'], PROFILE['experience'][0]['bullets'])


class ApprovalRecordTests(unittest.TestCase):
    def test_record_is_written_with_the_job_and_survives_import_purge(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory), code_root=ROOT)
            durable = DurableImports(Path(directory), draft=lambda *args: {})
            durable.enqueue('owner', 'b' * 32, '.ai', b'{}')
            result = workspace.enqueue(json.dumps(REQUEST).encode(), approval=approval())
            self.assertTrue(result['created'])
            job = json.loads((workspace.inbox / result['name']).read_text())
            self.assertIs(job['target']['approved'], True)
            [record] = workspace.approvals_for(result['name'])
            self.assertEqual(record['job'], result['name'])
            self.assertEqual(record['claims'], CLAIMS)
            self.assertTrue(record['attested'])
            with closing(durable.connection()) as db, db:
                db.execute('UPDATE imports SET activity=?', (time.time() - RETENTION - 1,))
                durable.purge(db, time.time())
                self.assertEqual(db.execute('SELECT COUNT(*) FROM imports').fetchone()[0], 0)
            self.assertEqual(workspace.approvals_for(result['name']), [record])

    def test_replays_do_not_duplicate_and_new_approvals_never_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory), code_root=ROOT)
            first = workspace.enqueue(json.dumps(REQUEST).encode(), approval=approval())
            [original] = workspace.approvals_for(first['name'])
            path = workspace.approvals / Path(first['name']).stem / (original['record_id'] + '.json')
            before = path.read_bytes()
            again = workspace.enqueue(json.dumps(REQUEST).encode(), approval=approval())
            self.assertFalse(again['created'])
            self.assertEqual(len(workspace.approvals_for(first['name'])), 1)
            other = approval([{**claim, 'provenance': 'candidate_confirmed'} for claim in CLAIMS])
            workspace.enqueue(json.dumps(REQUEST).encode(), approval={**other, 'operation': 'c' * 32})
            self.assertEqual(len(workspace.approvals_for(first['name'])), 2)
            self.assertEqual(path.read_bytes(), before)

    def test_approval_lookup_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory), code_root=ROOT)
            self.assertEqual(workspace.approvals_for('../../etc/passwd'), [])


class ClaimAuditTests(unittest.TestCase):
    record = {'attested': True, 'claims': CLAIMS}

    def test_every_generated_field_must_trace_to_an_approved_claim(self):
        self.assertEqual(claim_violations(PROFILE, self.record), [])
        for change in ({'summary': 'Award-winning architect.'}, {'skills': ['Python', 'Kubernetes']},
                       {'experience': [{**PROFILE['experience'][0], 'bullets': ['Led 40 engineers']}]},
                       {'certifications': ['AWS Certified Developer']},
                       {'projects': ['Ledger - Scaled to a million users']}):
            with self.subTest(change=change):
                self.assertTrue(claim_violations({**PROFILE, **change}, self.record))
        self.assertTrue(claim_violations(PROFILE, {**self.record, 'attested': False}))
        unapproved = [{**CLAIMS[0], 'candidate_confirmed': False}, *CLAIMS[1:]]
        self.assertTrue(claim_violations(PROFILE, {'attested': True, 'claims': unapproved}))

    def test_pdf_text_outside_the_approved_profile_is_reported(self):
        pdf = ('Phase One Synthetic\nphase1@example.com\nPROFESSIONAL SUMMARY\nEngineer focused on Python\n'
               'services.\nTECHNICAL SKILLS\nSkills: Python • SQL\nPROFESSIONAL EXPERIENCE\nEngineer | Example Co\n'
               'Jan 2020 – Present\n• Maintained billing APIs\nEDUCATION\nBSc | Example University\n2019')
        self.assertEqual(unaccounted_pdf_words(pdf, PROFILE), [])
        self.assertEqual(unaccounted_pdf_words(pdf + '\n• Led a team of 40 engineers', PROFILE),
                         ['led', 'team', 'of', '40', 'engineers'])
        self.assertEqual(unaccounted_pdf_words(pdf.replace('Python •', 'Pythonic •'), PROFILE), ['pythonic'])


class AccessStub:
    def accepts(self, token):
        return False

    def claims(self, token):
        return None


class SupervisorHealthTests(unittest.TestCase):
    def serve(self, ai_first):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        workspace = Workspace(Path(directory.name), code_root=ROOT)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(workspace, '', 'token'))
        origin = f'http://127.0.0.1:{server.server_port}'
        server.RequestHandlerClass = handler(workspace, origin, 'token', web_origin='https://resumeops.tech',
                                             access_verifier=AccessStub(), ai_first=ai_first)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port

    def test_both_homepage_modes_pass(self):
        for ai_first in (False, True):
            with self.subTest(ai_first=ai_first):
                dashboard_supervisor.health(self.serve(ai_first), {'web_url': 'https://resumeops.tech'})

    def test_classic_homepage_with_broken_ai_builder_fails(self):
        port = self.serve(False)
        real = dashboard_supervisor.fetch

        def broken(port_, path, host=None):
            mime, body = real(port_, path, host)
            return (mime, body.replace(b'id="ai-form"', b'')) if path == '/ai-builder' else (mime, body)

        with patch.object(dashboard_supervisor, 'fetch', side_effect=broken):
            with self.assertRaisesRegex(RuntimeError, '/ai-builder'):
                dashboard_supervisor.health(port, {'web_url': 'https://resumeops.tech'})

    def test_ai_first_is_opt_in_through_config(self):
        captured = {}

        class Popen:
            def __init__(self, arguments, **kwargs):
                captured['arguments'] = arguments

        config = {'project_root': str(ROOT), 'tool_paths': [], 'model': 'm', 'web_url': 'https://resumeops.tech',
                  'team': 't', 'audience': 'a'}
        with patch.object(dashboard_supervisor, 'release', return_value=ROOT / 'dashboard'), \
                patch.object(dashboard_supervisor.subprocess, 'Popen', Popen):
            dashboard_supervisor.spawn('x', 1, config, None)
            self.assertNotIn('--ai-first', captured['arguments'])
            dashboard_supervisor.spawn('x', 1, {**config, 'ai_first': True}, None)
            self.assertIn('--ai-first', captured['arguments'])


if __name__ == '__main__':
    unittest.main()
