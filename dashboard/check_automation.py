"""Browser and HTTP acceptance for one-submit automation with an isolated watcher."""
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from xml.sax.saxutils import escape
from zipfile import ZipFile
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from resume_dashboard import Workspace, handler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from resume_bot.automation import CONSENT_VERSION, source_profile, tailor_facts, verified_automation, verified_request
from resume_bot.ollama import OllamaError
from resume_bot.pipeline import CODE, scan
from resume_bot.compile import compile_latex
from resume_bot.storage import write_json

PUBLIC = 'https://resumeops.example'
TARGET = 'Azure Data Engineer'
SOURCE = """Synthetic Applicant
synthetic@example.test
Experience
Example Studio
UI Developer
January 2022 - December 2024
Built reporting interfaces using SQL queries.
Education
Example University
BSc Computing
Skills
SQL, JavaScript
Projects
Reporting dashboard
Certifications
Cloud Practitioner (planned)
"""


def make_facts():
    return {'full_name': 'Synthetic Applicant', 'email': 'synthetic@example.test', 'skills': ['SQL', 'JavaScript'],
            'experience': [{'company': 'Example Studio', 'position': 'UI Developer',
                            'start': 'January 2022', 'end': 'December 2024',
                            'bullets': ['Built reporting interfaces using SQL queries.']}],
            'education': [{'institution': 'Example University', 'degree': 'BSc Computing'}],
            'projects': ['Reporting dashboard'],
            'certifications': ['Cloud Practitioner (planned)']}


def evidence_model(*args, **kwargs):
    if 'Rank the evidence' in args[1]:
        # Deliberately include made-up prose and maliciously shaped ranks.
        return {'skills': [1, 1, 0, 'Azure'], 'projects': ['Fabricated project'],
                'work_0': [0, 'Migrated 100 TB to Azure']}
    return make_facts()


class AccessFixture:
    def claims(self, token):
        return {'email': token + '@example.test'} if token in {'alice', 'bob'} else None

    def accepts(self, token):
        return bool(self.claims(token))


class AutomationContract(unittest.TestCase):
    def test_target_changes_only_objective_and_order(self):
        from resume_bot.automation import role_intelligence
        for previous, target in (('Retail Associate', 'Software Engineer'),
                                 ('QA Analyst', 'Full Stack Developer'), ('UI Developer', TARGET)):
            with self.subTest(target=target):
                facts = make_facts()
                facts['experience'][0]['position'] = previous
                intelligence = role_intelligence(target)
                with patch('resume_bot.automation.generate', side_effect=evidence_model):
                    profile = tailor_facts(facts, intelligence['label'], 3, intelligence, object())
                self.assertEqual(profile['experience'][0]['position'], previous)
                self.assertIn(target, profile['headline'])
                self.assertNotIn('100 TB', json.dumps(profile))
                packet, _ = verified_request(profile, target, 3, intelligence)
                owner, identifier = 'a' * 64, 'b' * 64
                packet['target'].update(automation_owner=owner, automation_id=identifier)
                proof = {'automation': {'facts': facts, 'consent': CONSENT_VERSION,
                                         'submission_sha256': identifier},
                         'operation': identifier, 'approved_by': owner}
                self.assertTrue(verified_automation(packet['profile'], packet['target'], proof))
                packet['profile']['skills'].append(target)
                self.assertFalse(verified_automation(packet['profile'], packet['target'], proof))

    def test_role_objective_does_not_count_as_ats_evidence(self):
        from resume_bot.profile import validate_profile
        from resume_bot.workflow import analyze

        profile = validate_profile(make_facts())
        profile['headline'] = 'Target role: Azure Data Engineer'
        target = {'role': 'custom', 'role_title': 'Azure Data Engineer', 'template': 'standard',
                  'automation_owner': 'a' * 64, 'experience_years': 3}
        score = analyze(profile, target, ['Azure', 'SQL'])
        self.assertEqual(score['matched'], ['SQL'])
        self.assertEqual(score['missing'], ['Azure'])

    def test_document_sources_exclude_planned_certificates(self):
        with patch('resume_bot.automation.extract_profile', return_value=make_facts()):
            profile = source_profile(SOURCE, object())
        self.assertEqual(profile['certifications'], [])
        self.assertEqual(profile['projects'], ['Reporting dashboard'])
        self.assertEqual(profile['skills'], ['SQL', 'JavaScript'])

    def test_retry_keeps_source_checkpoint_and_receipt(self):
        import base64
        from contextlib import closing
        from automation_jobs import AutomationJobs

        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(Path(temp), code_root=ROOT)
            jobs = AutomationJobs(workspace)
            payload = {'role': TARGET, 'consent': CONSENT_VERSION, 'extension': 'txt',
                       'document': base64.b64encode(SOURCE.encode()).decode()}
            with patch.object(jobs, 'start'):
                job_id = jobs.enqueue('a' * 64, payload)
                self.assertEqual(jobs.enqueue('a' * 64, payload), job_id)
            with patch.object(workspace, 'read_automation_source', return_value=SOURCE) as reader, \
                    patch('resume_bot.automation.extract_profile', return_value=make_facts()), \
                    patch('resume_bot.automation.generate', side_effect=[OllamaError('transient'), {}]):
                with closing(jobs.connection()) as db:
                    job = db.execute('SELECT * FROM automation').fetchone()
                jobs._prepare(job)
                with closing(jobs.connection()) as db:
                    retry = db.execute('SELECT * FROM automation').fetchone()
                self.assertEqual(retry['status'], 'retrying')
                self.assertEqual(retry['attempts'], 1)
                self.assertGreater(retry['next_retry'], time.time())
                self.assertEqual(reader.call_count, 1)
                jobs._prepare(retry)
                self.assertEqual(reader.call_count, 1)
            with closing(jobs.connection()) as db:
                final = db.execute('SELECT * FROM automation').fetchone()
            self.assertEqual(final['status'], 'handed_off')
            self.assertIsNone(final['payload'])
            self.assertEqual(len(list(workspace.private_inbox.glob('private-*.json'))), 1)

    def test_rejects_cross_employer_title(self):
        facts = make_facts()
        facts['experience'][0]['position'] = 'Principal Engineer'
        with patch('resume_bot.automation.extract_profile', return_value=facts):
            with self.assertRaisesRegex(ValueError, 'job title'):
                source_profile(SOURCE, object())


def staging_documents(root):
    folder = root / 'fixtures'
    folder.mkdir()
    text, docx = folder / 'resume.txt', folder / 'resume.docx'
    text.write_text(SOURCE, encoding='utf-8')
    with ZipFile(docx, 'w') as archive:
        archive.writestr('word/document.xml',
                         '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                         '<w:body>' + ''.join('<w:p><w:r><w:t>' + escape(line) + '</w:t></w:r></w:p>'
                                              for line in SOURCE.splitlines()) + '</w:body></w:document>')
    (folder / 'main.tex').write_text(r'\documentclass{article}\begin{document}' + '\n' +
                                    '\n\n'.join(SOURCE.splitlines()) + r'\end{document}', encoding='utf-8')
    return text, docx, compile_latex(folder)


def fake_overleaf(zip_path, project_name, _profile, *, pdf_path, saved_url, on_step):
    shutil.copyfile(zip_path.parent / 'resume.pdf', pdf_path)
    return {'status': 'uploaded', 'url': 'https://www.overleaf.com/project/fixture', 'project_name': project_name}


def check():
    with tempfile.TemporaryDirectory() as temp:
        workspace = Workspace(Path(temp), code_root=ROOT)
        write_json(workspace.inbox / '.resume-bot-health.json', {'last_scan': time.time(), 'code': CODE})
        with ThreadingHTTPServer(('127.0.0.1', 0), handler(workspace, '', 'test')) as server:
            origin = f'http://127.0.0.1:{server.server_port}'
            server.RequestHandlerClass = handler(workspace, origin, 'test', web_origin=PUBLIC,
                                                 access_verifier=AccessFixture(), automation_first=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with patch('resume_bot.automation.generate', side_effect=evidence_model), \
                        patch('resume_bot.automation.extract_profile', return_value=make_facts()), \
                        patch('resume_bot.pipeline.import_zip', side_effect=fake_overleaf), sync_playwright() as pw:
                    browser = pw.chromium.launch()
                    page = browser.new_page(**pw.devices['iPhone 13'])
                    page.goto(origin)
                    target = page.get_by_label('Target role')
                    target.fill('Full Stack Developer')
                    page.get_by_label('Enter details').check()
                    page.get_by_label('Full name').fill('Fresher Synthetic')
                    page.get_by_label('Email').fill('fresher@example.test')
                    page.get_by_label('Years of experience').fill('0')
                    education = page.locator('details').filter(has=page.get_by_text('Education', exact=True))
                    education.locator('summary').click()
                    education.get_by_role('button', name='Add entry').click()
                    page.get_by_label('Institution').fill('Example University')
                    page.get_by_label('Degree', exact=True).fill('BSc')
                    page.locator('#auto-consent').check()
                    with page.expect_response(lambda response: response.url.endswith('/api/automation') and
                                              response.request.method == 'POST') as accepted:
                        page.get_by_role('button', name='Create verified resume').click()
                    assert accepted.value.status == 202, accepted.value.text()
                    expect(page.get_by_text('Waiting for the resume watcher', exact=True)).to_be_visible()
                    auth = {'interrupted': False, 'renewed': False}

                    def expire_once(route):
                        if not auth['interrupted']:
                            auth['interrupted'] = True
                            route.fulfill(status=401)
                        else:
                            auth['renewed'] = True
                            route.continue_()

                    page.route('**/api/automation', expire_once)
                    page.reload()
                    expect(page.get_by_text('Waiting for the resume watcher', exact=True)).to_be_visible()
                    assert auth['interrupted'] and auth['renewed']
                    page.unroute('**/api/automation', expire_once)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert not [path for path in workspace.inbox.glob('*.json') if not path.name.startswith('.')]
                    scan(workspace.inbox, workspace.output, once=True, automate=True, overleaf=True,
                         browser_profile=workspace.root / 'fixture-profile')
                    expect(page.get_by_role('link', name='Download PDF', exact=True)).to_be_visible()
                    expect(page.locator('#auto-percent')).to_have_text('100%')
                    with page.expect_download() as downloaded:
                        page.get_by_role('link', name='Download PDF', exact=True).click()
                    assert downloaded.value.path().read_bytes().startswith(b'%PDF')
                    assert len(list(workspace.private_inbox.glob('private-*.json'))) == 1

                    def request(path, account, payload=None):
                        headers = {'Host': 'resumeops.example', 'Cf-Access-Jwt-Assertion': account,
                                   'Origin': PUBLIC, 'X-Dashboard-Token': 'test', 'Content-Type': 'application/json'}
                        data = json.dumps(payload).encode() if payload is not None else None
                        with urlopen(Request(origin + path, headers=headers, data=data), timeout=12) as response:
                            return json.load(response)

                    import base64
                    files = staging_documents(workspace.root)
                    docs = [{'role': TARGET, 'consent': CONSENT_VERSION, 'extension': path.suffix[1:],
                             'document': base64.b64encode(path.read_bytes()).decode()} for path in files]
                    jobs = [request('/api/automation', 'alice', document) for document in docs]
                    assert jobs[0] == request('/api/automation', 'alice', docs[0])
                    assert len({job['id'] for job in jobs}) == 3
                    assert request('/api/automation', 'bob')['jobs'] == []
                    deadline = time.monotonic() + 25
                    while time.monotonic() < deadline:
                        entries = request('/api/automation', 'alice')['jobs']
                        if len(entries) == 3 and all(entry['percent'] == 55 for entry in entries):
                            break
                        assert not any(entry['status'] == 'blocked' for entry in entries), entries
                        time.sleep(.15)
                    else:
                        raise AssertionError(entries)
                    scan(workspace.inbox, workspace.output, once=True, automate=True, overleaf=True,
                         browser_profile=workspace.root / 'fixture-profile')
                    results = request('/api/automation', 'alice')['jobs']
                    assert len(results) == 3 and all(item['status'] == 'complete' and item['percent'] == 100
                                                     for item in results), results
                    result = results[0]
                    artifact = '/api/revision?' + urlencode({'key': result['revision']})
                    assert request(artifact, 'alice')['profile']['experience'][0]['position'] == 'UI Developer'
                    for path in (artifact, '/api/artifact?' + urlencode({
                            'key': result['revision'], 'name': 'resume.pdf'})):
                        try:
                            request(path, 'bob')
                        except HTTPError as error:
                            assert error.code == 404
                        else:
                            raise AssertionError('Cross-account artifact was readable')
                    assert not request('/api/status', 'alice')['revisions']
                    assert not list(workspace.output.glob('*/*/result.json'))
                    browser.close()
            finally:
                server.shutdown()
                server.RequestHandlerClass.stop_imports()


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AutomationContract)
    if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
        sys.exit(1)
    check()
    print('Automation acceptance passed: one submit, mobile, refresh, real PDF, download, deduplication and account isolation.')
