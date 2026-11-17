"""Exercise the AI-first intake, approval gate, and durable watcher handoff."""
import json
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from resume_dashboard import Workspace, handler

ROOT = Path(__file__).resolve().parents[1]
DRAFT = {
    'professional_summary': 'Software engineering graduate focused on Python.',
    'technical_skills': ['Python', 'Unconfirmed Vendor Tool'],
    'core_competencies': ['Testing'],
    'key_achievements': ['Increased revenue by 75%'],
    'project_ideas': [{'title': 'Unbuilt portfolio idea', 'description': 'Try making a dashboard.'}],
    'certification_recommendations': ['Unearned certification'],
}


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
                    page = browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True)
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
                    page.get_by_label('Skill (unverified AI suggestion)', exact=True).first.check()
                    page.get_by_label('I confirm the candidate details and selected statements are accurate.').check()
                    page.get_by_role('button', name='Preview and check ATS').click()
                    expect(page.get_by_text('ATS readiness:', exact=False)).to_be_visible()
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
                    browser.close()
            finally:
                server.shutdown()
                server.RequestHandlerClass.stop_imports()
    print('AI-first mobile browser, same-tab Access recovery, approved-only ATS, immutable queue, and replay passed.')


if __name__ == '__main__':
    check()
