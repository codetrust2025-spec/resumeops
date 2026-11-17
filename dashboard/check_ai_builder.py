"""Exercise the AI-first intake, approval gate, and durable watcher handoff."""
import json
import re
import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from resume_dashboard import Workspace, handler
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from resume_bot.ingest import read_resume_text
from resume_bot.pipeline import scan

DRAFT = {
    'professional_summary': 'Software engineering graduate focused on Python.',
    'technical_skills': ['Python', 'Unconfirmed Vendor Tool'],
    'core_competencies': ['Testing'],
    'key_achievements': ['Increased revenue by 75%'],
    'project_ideas': [{'title': 'Unbuilt portfolio idea', 'description': 'Try making a dashboard.'}],
    'certification_recommendations': ['Unearned certification'],
}


def verify_completed(page, workspace, candidate, *, education_first):
    scan(workspace.inbox, workspace.output, once=True, polish_enabled=False)
    expect(page.locator('#result-success')).to_be_visible(timeout=15000)
    with page.expect_download() as pdf:
        page.locator('#tile-pdf').click()
    downloaded = workspace.root / 'browser-download.pdf'
    pdf.value.save_as(downloaded)
    text = read_resume_text(downloaded)
    assert candidate in text and 'Python' in text, text
    for excluded in ('Unconfirmed Vendor Tool', 'Increased revenue', 'Unbuilt portfolio idea',
                     'Unearned certification', 'Testing', DRAFT['professional_summary']):
        assert excluded not in text, (excluded, text)
    if education_first:
        assert 'EDUCATION' in text and 'PROFESSIONAL EXPERIENCE' not in text
    else:
        assert text.index('PROFESSIONAL EXPERIENCE') < text.index('EDUCATION')
    page.locator('[data-tab="tab-latex"]').click()
    with page.expect_download() as latex:
        page.locator('#download-latex').click()
    assert b'\\documentclass' in latex.value.path().read_bytes()
    page.locator('#nav-history').click()
    expect(page.locator('#history').get_by_text(candidate, exact=True)).to_be_visible()
    page.goto(page.url.split('/workflow')[0] + '/')
    expect(page.get_by_label('Full name')).to_have_value('')


def check():
    with tempfile.TemporaryDirectory(prefix='resumeops-ai-') as directory:
        workspace = Workspace(Path(directory), code_root=ROOT)
        with ThreadingHTTPServer(('127.0.0.1', 0), handler(workspace, '', 'test-token')) as server:
            origin = f'http://127.0.0.1:{server.server_port}'
            server.RequestHandlerClass = handler(workspace, origin, 'test-token', ai_first=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with patch('resume_bot.ollama.Ollama.generate', return_value=DRAFT), sync_playwright() as pw:
                    browser = pw.chromium.launch()
                    context = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True)
                    page = context.new_page()
                    access = {'armed': True, 'expired': False, 'renewed': False}

                    def route_access(route):
                        url = route.request.url
                        if '/auth/renewed?' in url:
                            access.update(expired=False, renewed=True)
                        elif access['armed'] and '/api/import-progress?' in url:
                            access.update(armed=False, expired=True)
                        if access['expired'] and '/api/' in url:
                            return route.fulfill(status=401, body='')
                        return route.continue_()

                    page.context.route(origin + '/**', route_access)
                    page.add_init_script('window.open = () => null')
                    page.goto(origin + '/')
                    page.get_by_label('Full name').fill('Previous Candidate')
                    page.get_by_label('Phone').fill('555-0101')
                    second = page.context.new_page()
                    second.goto(origin + '/')
                    expect(second.get_by_role('status')).to_contain_text('open in another tab')
                    assert not second.get_by_label('Full name').count()
                    second.close()

                    fixture = {'profile': {'full_name': 'AI First Synthetic', 'email': 'ai-first@example.com'},
                               'source': 'AI First Synthetic, ai-first@example.com\nSkills: Python'}
                    access['armed'] = False
                    with patch.object(workspace, 'draft', return_value=fixture):
                        page.locator('#ai-file').set_input_files({'name': 'synthetic.txt', 'mimeType': 'text/plain',
                                                                  'buffer': b'Synthetic resume'})
                        page.get_by_role('button', name='Import details').click()
                        expect(page.get_by_role('status')).to_contain_text('Details imported')
                    expect(page.get_by_label('Full name')).to_have_value('AI First Synthetic')
                    expect(page.get_by_label('Phone')).to_have_value('')
                    access['armed'] = True
                    page.get_by_label('Target role').select_option('fullstack-developer')
                    page.get_by_label('Total years of experience').fill('0')
                    page.get_by_label('Full name').fill('AI First Synthetic')
                    page.get_by_label('Email').fill('ai-first@example.com')
                    page.get_by_role('button', name='Add education').click()
                    page.get_by_label('Degree', exact=True).fill('BSc')
                    page.get_by_label('University', exact=True).fill('Example University')
                    assert not page.get_by_role('textbox', name='skills').count()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.get_by_role('button', name='Draft resume with Ollama').click()
                    expect(page.locator('#auth-banner')).to_be_visible(timeout=10000)
                    page.get_by_role('button', name='Sign in again').click()
                    expect(page.get_by_role('heading', name='Review AI suggestions')).to_be_visible(timeout=15000)
                    assert access['renewed']
                    assert not list(workspace.inbox.glob('*.json'))
                    assert page.get_by_text('ResumeOps role catalog (curated)', exact=False).count()
                    assert not page.get_by_text('Naukri', exact=False).count()
                    supported = page.locator('[data-provenance="verified_from_source"]').filter(has_text=re.compile(r'^Skill '))
                    unsupported = page.locator('[data-provenance="unsupported_role_keyword"]').filter(has_text=re.compile(r'^Skill '))
                    expect(supported).to_have_count(1)
                    expect(unsupported).to_have_count(1)
                    expect(unsupported.get_by_role('checkbox')).not_to_be_checked()
                    supported.get_by_role('checkbox').check()
                    page.get_by_label('I confirm the candidate details and selected statements are accurate.').check()
                    page.get_by_role('button', name='Preview and check ATS').click()
                    expect(page.get_by_text('ATS readiness:', exact=False)).to_be_visible()
                    expect(page.get_by_text('BSc | Example University', exact=True)).to_be_visible()
                    assert not list(workspace.inbox.glob('*.json'))
                    request = page.evaluate("""async () => {
                      const saved = await ResumeRecovery.read('ai-builder');
                      return {id:saved.draft.id, approval:saved.approval, token:ResumeAuth.token};
                    }""")
                    page.get_by_role('button', name='Generate resume').click()
                    page.wait_for_url(origin + '/workflow')
                    files = list(workspace.inbox.glob('*.json'))
                    assert len(files) == 1
                    profile = json.loads(files[0].read_text(encoding='utf-8'))['profile']
                    assert profile['skills'] == ['Python']
                    assert 'Unbuilt portfolio' not in str(profile)
                    assert not profile['certifications']
                    headers = {'Origin': origin, 'X-Dashboard-Token': request.pop('token')}
                    again = page.request.post(origin + '/api/ai-queue', data=request, headers=headers)
                    assert again.status == 200, again.text()
                    assert again.json()['created'] is False
                    request['approval']['claims']['1'] = 'Confirmed edited claim'
                    changed = page.request.post(origin + '/api/ai-queue', data=request, headers=headers)
                    assert changed.status == 409, changed.text()
                    request['approval']['attested'] = False
                    rejected = page.request.post(origin + '/api/ai-queue', data=request, headers=headers)
                    assert rejected.status == 400, rejected.text()
                    assert len(list(workspace.inbox.glob('*.json'))) == 1
                    with closing(sqlite3.connect(workspace.root / '.resumeops-imports.sqlite3')) as db:
                        saved = json.loads(db.execute('SELECT result FROM imports WHERE id=?', (request['id'],)).fetchone()[0])
                    assert saved['_queued_review']['claims'][0]['provenance'] == 'verified_from_source'
                    verify_completed(page, workspace, 'AI First Synthetic', education_first=True)

                    desktop = browser.new_context(viewport={'width': 1360, 'height': 960})
                    senior = desktop.new_page()
                    senior.goto(origin + '/')
                    senior.get_by_label('Target role').select_option('fullstack-developer')
                    senior.get_by_label('Total years of experience').fill('9')
                    senior.get_by_label('Full name').fill('Senior Synthetic')
                    senior.get_by_label('Email').fill('senior@example.com')
                    senior.get_by_role('button', name='Add work experience').click()
                    senior.get_by_label('Employer').fill('Example Engineering')
                    senior.get_by_label('Designation').fill('Software Engineer')
                    senior.get_by_label('Start date').fill('Jan 2017')
                    senior.get_by_label('End date').fill('Present')
                    senior.get_by_role('button', name='Add education').click()
                    senior.get_by_label('Degree', exact=True).fill('BSc')
                    senior.get_by_label('University', exact=True).fill('Example University')
                    senior.get_by_role('button', name='Draft resume with Ollama').click()
                    expect(senior.get_by_role('heading', name='Review AI suggestions')).to_be_visible()
                    skills = senior.locator('[data-provenance="unsupported_role_keyword"]').filter(has_text=re.compile(r'^Skill '))
                    expect(skills).to_have_count(2)
                    skills.first.get_by_role('checkbox').check()
                    senior.locator('#ai-attest').check()
                    senior.get_by_role('button', name='Preview and check ATS').click()
                    senior.get_by_role('button', name='Generate resume').click()
                    senior.wait_for_url(origin + '/workflow')
                    verify_completed(senior, workspace, 'Senior Synthetic', education_first=False)
                    browser.close()
            finally:
                server.shutdown()
                server.RequestHandlerClass.stop_imports()
    print('AI-first mobile browser, same-tab Access recovery, approved-only ATS, immutable queue, and replay passed.')


if __name__ == '__main__':
    check()
