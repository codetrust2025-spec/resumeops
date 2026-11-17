"""Browser acceptance: Cloudflare Access session expiry and dashboard restarts never lose work or
create duplicate jobs. Expiry is simulated at the browser edge exactly as Access answers an expired
background request: 401 when X-Requested-With is sent, otherwise a redirect to the team domain."""
import json
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from resume_dashboard import Workspace, handler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    temp = Path(tempfile.mkdtemp(prefix='resumeops-session-'))
    workspace = Workspace(temp, code_root=ROOT)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(workspace, '', 'token-1'))
    url = f'http://127.0.0.1:{server.server_port}'
    server.RequestHandlerClass = handler(workspace, url, 'token-1')
    threading.Thread(target=server.serve_forever, daemon=True).start()
    drafts = []

    def synthetic_draft(extension, data, report=lambda stage, metrics=None: None):
        drafts.append(extension)
        report('Extracting candidate details with Ollama')
        time.sleep(1.5)
        return {'profile': {'full_name': 'Session Import Example', 'email': 'session.import@example.com',
                            'skills': ['Python'], 'experience': [{'company': 'Example Co', 'position': 'Engineer',
                            'start': 'Jan 2020', 'end': 'Present', 'bullets': ['Built Python tools.']}],
                            'education': []}, 'source': data.decode()}
    workspace.draft = synthetic_draft
    expired = {'mode': None}   # None | '401' | 'redirect'
    hits = {'expired': 0}

    def edge(route):
        """Stand-in for the Cloudflare Access edge in front of the dashboard."""
        request = route.request
        if expired['mode'] and '/auth/renewed' not in request.url:
            hits['expired'] += 1
            if expired['mode'] == '401' and request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return route.fulfill(status=401, body='')
            return route.fulfill(status=302, headers={'Location': 'https://example-team.cloudflareaccess.com/cdn-cgi/access/login'})
        if '/auth/renewed' in request.url:
            expired['mode'] = None   # signing in at the team domain renews the session
        return route.continue_()

    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={'width': 1280, 'height': 900})
        context.route(url + '/**', edge)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url)

        # 1. Fill a candidate, then the session expires (Access 401) before Review.
        page.get_by_role('button', name='Create Resume').click()
        page.locator('[name=source][value=manual]').check()
        page.get_by_role('button', name='Continue').click()
        page.locator('[name=role]').select_option('fullstack-developer')
        page.get_by_role('button', name='Continue').click()
        page.get_by_label('Full name').fill('Session Expiry Example')
        page.get_by_label('Email').fill('session.expiry@example.com')
        page.get_by_role('button', name='Continue').click()
        page.get_by_role('tab', name='Skills').click()
        page.get_by_label('Verified skills').fill('Python\nSQL')
        page.get_by_role('tab', name=__import__('re').compile('^Experience')).click()
        page.get_by_role('button', name='Add experience').click()
        page.get_by_label('Employer').fill('Example Labs')
        page.get_by_label('Position', exact=True).fill('Engineer')
        page.get_by_label('Start date', exact=True).fill('Jan 2021')
        page.get_by_label('End date', exact=True).fill('Present')
        page.wait_for_timeout(600)   # draft autosave
        expired['mode'] = '401'
        page.get_by_role('button', name='Continue').click()
        expect(page.locator('#auth-banner')).to_be_visible()
        expect(page.locator('#auth-banner')).to_contain_text('Everything on this page is kept')
        assert page.get_by_label('Employer').input_value() == 'Example Labs'
        # Signing in happens in another tab; this page continues by itself.
        with context.expect_page() as popup:
            page.get_by_role('button', name='Sign in again').click()
        popup.value.wait_for_load_state()
        expect(page.locator('#auth-banner')).to_have_count(0, timeout=15000)
        expect(page.get_by_role('heading', name='Review and confirm facts')).to_be_visible(timeout=15000)
        results['review_after_401_renewal'] = True

        # 2. Generate while expired (redirect flavour): exactly one job after sign-in.
        page.get_by_label('I confirm these candidate facts are accurate.').check()
        page.get_by_role('button', name='Continue').click()
        expired['mode'] = 'redirect'
        page.get_by_role('button', name='Generate resume').click()
        expect(page.locator('#auth-banner')).to_be_visible(timeout=10000)
        assert not list(workspace.inbox.glob('session-expiry-example-*.json')), 'queued while signed out'
        with context.expect_page() as popup:
            page.get_by_role('button', name='Sign in again').click()
        expect(page.locator('#builder-status')).to_have_text('Resume queued.', timeout=15000)
        jobs = list(workspace.inbox.glob('session-expiry-example-*.json'))
        assert len(jobs) == 1, jobs
        assert page.evaluate("sessionStorage.getItem('resumeops.draft')") is None, 'draft kept after queue'
        results['one_job_after_redirect_renewal'] = jobs[0].name

        # 3. Dashboard restarted (new request token) while the page stays open: no reload needed.
        server.RequestHandlerClass = handler(workspace, url, 'token-2')
        page.get_by_role('button', name='Start next candidate').click()
        page.locator('[name=source][value=manual]').check()
        page.get_by_role('button', name='Continue').click()
        page.locator('[name=role]').select_option('fullstack-developer')
        page.get_by_role('button', name='Continue').click()
        page.get_by_label('Full name').fill('Token Rotation Example')
        page.get_by_label('Email').fill('token.rotation@example.com')
        page.get_by_role('button', name='Continue').click()
        page.get_by_role('tab', name='Education (0)').click()
        page.get_by_role('button', name='Add education').click()
        page.get_by_label('Institution').fill('Example University')
        page.get_by_label('Degree').fill('B.Sc')
        page.get_by_role('button', name='Continue').click()
        page.get_by_label('I confirm these candidate facts are accurate.').check()
        page.get_by_role('button', name='Continue').click()
        page.get_by_role('button', name='Generate resume').click()
        expect(page.locator('#builder-status')).to_have_text('Resume queued.', timeout=15000)
        assert len(list(workspace.inbox.glob('token-rotation-example-*.json'))) == 1
        assert page.evaluate('ResumeAuth.token') == 'token-2'
        results['token_rotation'] = 'recovered without reload'

        # 4. A reload (or a sign-in that replaced the tab) restores the unsaved candidate.
        page.get_by_role('button', name='Start next candidate').click()
        page.locator('[name=source][value=manual]').check()
        page.get_by_role('button', name='Continue').click()
        page.locator('[name=role]').select_option('fullstack-developer')
        page.get_by_role('button', name='Continue').click()
        page.get_by_label('Full name').fill('Reload Draft Example')
        page.get_by_label('Phone').fill('+1 555 0199')
        page.wait_for_timeout(600)
        page.reload()
        expect(page.locator('#builder-status')).to_contain_text('Unsaved candidate details were restored')
        page.locator('#steps li[data-target="3"] button').click()
        expect(page.get_by_label('Full name')).to_have_value('Reload Draft Example')
        expect(page.get_by_label('Phone')).to_have_value('+1 555 0199')
        assert page.locator('[name=role]').input_value() == 'fullstack-developer'
        results['draft_restored_after_reload'] = True

        # 5. Session expires during a resume upload: the same file continues after sign-in, once.
        fresh = context.new_page()
        fresh.goto(url)
        # A new tab resumes tracking the job queued above; start this check from a clean builder.
        fresh.evaluate("sessionStorage.clear(); localStorage.removeItem('resumeops.activeJob')")
        fresh.reload()
        fresh.get_by_role('button', name='Create Resume').click()
        fresh.locator('#resume-upload').set_input_files({'name': 'resume.txt', 'mimeType': 'text/plain',
                                                         'buffer': b'Session Import Example resume text. ' * 10})
        expired['mode'] = '401'
        fresh.get_by_role('button', name='Import and prefill').click()
        expect(fresh.locator('#auth-banner')).to_be_visible(timeout=10000)
        assert drafts == [], 'upload processed while signed out'
        with context.expect_page():
            fresh.get_by_role('button', name='Sign in again').click()
        expect(fresh.get_by_role('heading', name='Choose a target role')).to_be_visible(timeout=20000)
        assert fresh.get_by_label('Full name').input_value() == 'Session Import Example'
        assert drafts == ['.txt'], drafts
        results['import_after_expiry'] = 'continued once'

        # 6. Expiry while the import is being polled: the import still completes.
        drafts.clear()
        fresh.evaluate("sessionStorage.clear(); localStorage.removeItem('resumeops.activeJob')")
        fresh.goto(url)
        fresh.get_by_role('button', name='Create Resume').click()
        fresh.locator('#resume-upload').set_input_files({'name': 'resume.txt', 'mimeType': 'text/plain',
                                                         'buffer': b'Session Import Example resume again. ' * 10})
        fresh.get_by_role('button', name='Import and prefill').click()
        fresh.wait_for_timeout(300)
        expired['mode'] = 'redirect'
        expect(fresh.locator('#auth-banner')).to_be_visible(timeout=10000)
        fresh.wait_for_timeout(2500)   # extraction finishes on the server meanwhile
        with context.expect_page():
            fresh.get_by_role('button', name='Sign in again').click()
        expect(fresh.get_by_role('heading', name='Choose a target role')).to_be_visible(timeout=20000)
        assert drafts == ['.txt'], drafts
        results['import_polling_after_expiry'] = 'completed'

        # Downloads open in their own tab, so an Access redirect can never replace the builder.
        script = Path(__file__).with_name('builder.js').read_text()
        assert "link.target = '_blank'" in script and "el('download-latex').target = '_blank'" in script
        assert not errors, errors
        results['expired_requests_answered_by_edge'] = hits['expired']
        browser.close()
    server.shutdown()
    server.server_close()
    print(json.dumps(results, indent=1))
    print('Session acceptance passed: 401 and redirect expiry, token rotation, draft restore, '
          'upload and polling recovery, no duplicates.')


if __name__ == '__main__':
    main()
