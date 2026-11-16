"""Browser end-to-end check of the ResumeOps dashboard in an isolated staging workspace.

Runs the real dashboard server and the real watcher pipeline (real LaTeX → PDF compilation and PDF
text verification) against a temporary data root. Ollama is replaced by a local HTTP stand-in that
returns the candidate's own text unchanged, and the Overleaf upload is stubbed, because neither the
GPU model nor an Overleaf session exists in CI. Usage:

    python dashboard/check_ui.py <source checkout> [screenshot directory]
"""
import json
import re
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(sys.argv[1]).resolve()
SHOTS = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
sys.path.insert(0, str(SOURCE / 'dashboard'))
sys.path.insert(0, str(SOURCE))

from playwright.sync_api import expect, sync_playwright  # noqa: E402

from resume_dashboard import Workspace, handler  # noqa: E402

VIEWPORTS = {'desktop': (1440, 900), 'tablet': (820, 1180), 'mobile': (390, 844)}
VIEWS = ['dashboard', 'create', 'candidates', 'outputs', 'ats', 'templates', 'logs', 'settings']
JOB_DESCRIPTION = ('We need a Frontend Developer with strong React.js, TypeScript and Next.js skills, '
                   'experience with REST API integration, Jest testing and Git. Agile team.')


class EchoOllama(BaseHTTPRequestHandler):
    """Ollama-compatible stand-in: returns the input strings unchanged (no fabrication possible)."""
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        match = re.search(r'INPUT ARRAY:\s*(\[.*\])', body['prompt'], re.S)
        data = json.dumps({'response': json.dumps({'edits': json.loads(match.group(1))})}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def no_horizontal_overflow(page, label):
    width = page.evaluate('() => [document.documentElement.scrollWidth, window.innerWidth]')
    assert width[0] <= width[1], f'{label}: page scrolls horizontally ({width[0]} > {width[1]})'


def main():
    from resume_bot.ollama import Ollama
    from resume_bot.pipeline import scan

    with tempfile.TemporaryDirectory(prefix='resumeops-staging-') as temp:
        root = Path(temp)
        shutil.copytree(SOURCE / 'resume_bot', root / 'resume_bot', ignore=shutil.ignore_patterns('__pycache__'))
        (root / 'logs').mkdir()
        (root / 'logs/watcher.log').write_text('OK staging watcher started\n', encoding='utf-8')
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(Workspace(root), '', 'token'))
        origin = f'http://127.0.0.1:{server.server_port}'
        server.RequestHandlerClass = handler(Workspace(root), origin, 'staging-token')
        ollama = ThreadingHTTPServer(('127.0.0.1', 0), EchoOllama)
        threads = [serve(server), serve(ollama)]
        errors = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(viewport={'width': 1440, 'height': 900}, accept_downloads=True)
                context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=origin)
                page = context.new_page()
                page.on('console', lambda message: message.type == 'error' and errors.append(message.text))
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(origin + '/#/create')
                expect(page.locator('#role-select option')).to_have_count(9)

                # Step 1: personal details, with a validation error first.
                page.click('#next')
                expect(page.locator('#wizard-errors li').first).to_contain_text('Full name is required')
                fill = lambda bind, value: page.fill(f'[data-bind="{bind}"]', value)
                fill('profile.full_name', 'Morgan Example')
                fill('profile.headline', 'Frontend Software Engineer')
                fill('profile.email', 'morgan.example@example.com')
                fill('profile.phone', '+91 90000 00001')
                fill('profile.location', 'Hyderabad, India')
                fill('profile.linkedin', 'https://linkedin.com/in/morgan-example')
                fill('profile.portfolio', 'https://morgan.example.com')
                fill('profile.summary', 'Frontend engineer building accessible React and TypeScript interfaces.')
                expect(page.locator('#paper')).to_contain_text('Morgan Example')
                page.click('#next')

                # Step 2: experience, education, skills, projects, certifications.
                expect(page.locator('[data-step="2"]')).to_be_visible()
                expect(page.locator('[data-step="1"]')).to_be_hidden()
                page.click('[data-add="experience"]')
                entry = lambda name: page.locator(f'[data-entry="{name}"]')
                entry('experience.0.position').fill('Software Engineer')
                entry('experience.0.company').fill('Example Digital Labs')
                entry('experience.0.start').fill('Jun 2021')
                entry('experience.0.end').fill('Present')
                entry('experience.0.bullets').fill('Built reusable React.js components used by 4 internal apps.\n'
                                                   'Integrated REST API endpoints with TypeScript clients.')
                page.click('[data-add="education"]')
                entry('education.0.degree').fill('B.Tech in Information Technology')
                entry('education.0.institution').fill('Example Institute of Technology')
                entry('education.0.end').fill('2021')
                fill('skills', 'React.js\nTypeScript\nJavaScript\nGit\nJest')
                fill('certifications', 'Example Frontend Certificate')
                page.click('[data-add="projects"]')
                entry('projects.0.name').fill('Accessible Form Kit')
                entry('projects.0.technologies').fill('React.js, TypeScript')
                entry('projects.0.bullets').fill('Built keyboard-accessible form components.')
                expect(page.locator('#experience-summary')).to_contain_text('Senior')
                expect(page.locator('#paper')).to_contain_text('PROJECTS')
                page.click('#next')

                # Step 3: role, template, job-description keyword extraction.
                page.select_option('#role-select', 'frontend-developer')
                page.select_option('#template-select', 'compact')
                expect(page.locator('#role-detail')).to_contain_text('Must-have terms')
                fill('jobDescription', JOB_DESCRIPTION)
                page.click('#extract-keywords')
                expect(page.locator('#jd-feedback')).to_contain_text('Found')
                expect(page.locator('#keyword-chips .chip').first).to_be_visible()
                page.fill('#keyword-input', 'Accessibility')
                page.press('#keyword-input', 'Enter')
                expect(page.locator('#keyword-chips')).to_contain_text('Accessibility')
                expect(page.locator('#coverage-score')).to_have_text(re.compile(r'\d+%'), timeout=10000)
                page.click('#tab-latex')
                expect(page.locator('#latex-code')).to_contain_text('\\section*{PROJECTS}')
                page.click('#copy-latex')
                expect(page.locator('#latex-feedback')).to_contain_text('copied')
                assert '\\documentclass' in page.evaluate('navigator.clipboard.readText()')
                with page.expect_download() as download:
                    page.click('#download-latex')
                assert download.value.suggested_filename == 'morgan-example-frontend-developer.tex'
                page.click('#tab-ats')
                expect(page.locator('#preview-ats')).to_contain_text('Keyword coverage')
                page.click('#tab-resume')
                page.click('#next')

                # Step 4: review, ATS, explicit confirmation.
                expect(page.locator('#review-summary')).to_contain_text('Frontend Developer')
                expect(page.locator('#source-compare')).to_contain_text('Entered manually')
                expect(page.locator('#review-ats')).to_contain_text('Overall')
                page.click('#generate')
                expect(page.locator('#wizard-errors')).to_contain_text('Confirm the candidate facts')
                page.check('[data-bind="verified"]')
                expect(page.locator('#wizard-errors li')).to_have_count(0)
                if SHOTS:
                    SHOTS.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(SHOTS / 'desktop-create-review.png'), full_page=True)
                page.click('#generate')

                # Step 5: queued; run the real watcher once and watch progress reach 100%.
                expect(page.locator('#gen-stage')).to_contain_text('Queued')
                inbox = root / 'inbox'
                assert len(list(inbox.glob('*.json'))) == 1

                def fake_overleaf(zip_path, title, profile, *, pdf_path, saved_url):
                    shutil.copyfile(zip_path.parent / 'resume.pdf', pdf_path)
                    return {'status': 'uploaded', 'project_name': title,
                            'url': 'https://www.overleaf.com/project/stagingfixture01'}

                with patch('resume_bot.pipeline.import_zip', side_effect=fake_overleaf):
                    watcher = threading.Thread(target=scan, args=(inbox, root / 'output'), kwargs={
                        'ai': Ollama('qwen2.5:7b', f'http://127.0.0.1:{ollama.server_port}', timeout=10),
                        'overleaf': True, 'browser_profile': root / 'browser', 'once': True})
                    watcher.start()
                    watcher.join(180)
                assert not watcher.is_alive(), 'watcher did not finish'
                state = json.loads((inbox / '.resume-bot-state.json').read_text())
                record = next(iter(state.values()))
                assert record['status'] == 'ok', record
                expect(page.locator('#gen-percent')).to_have_text('100%', timeout=10000)
                expect(page.locator('#gen-stage')).to_contain_text('Verified PDF ready')
                expect(page.locator('#gen-outputs')).to_contain_text('Open Overleaf project')
                frame_src = page.locator('#pdf-panel iframe').get_attribute('src')
                pdf = context.request.get(origin + frame_src)
                assert pdf.status == 200 and pdf.body().startswith(b'%PDF'), 'PDF preview is not served'
                assert "frame-ancestors 'self'" in pdf.headers['content-security-policy']
                result = json.loads(next((root / 'output').glob('*/*/result.json')).read_text())
                assert result['ats']['role'] == 'frontend-developer' and result['pdf_checks']['pages'] == 1
                profile = json.loads(next((root / 'output').glob('*/*/profile.json')).read_text())
                assert profile['projects'][0]['name'] == 'Accessible Form Kit'
                assert profile['experience'][0]['bullets'][0].startswith('Built reusable React.js')
                expect(page.locator('#draft-state')).to_have_text('Generated')
                if SHOTS:
                    page.screenshot(path=str(SHOTS / 'desktop-create-generated.png'), full_page=True)
                    import subprocess
                    pdf_file = next((root / 'output').glob('*/*/resume.pdf'))
                    subprocess.run(['pdftoppm', '-png', '-r', '80', '-singlefile', str(pdf_file),
                                    str(SHOTS / 'generated-resume-pdf')], check=True)

                # Every view at every viewport: renders, no horizontal overflow, screenshot.
                for size, (width, height) in VIEWPORTS.items():
                    page.set_viewport_size({'width': width, 'height': height})
                    for view in VIEWS:
                        page.goto(f'{origin}/#/{view}')
                        page.wait_for_selector(f'[data-view="{view}"]:not([hidden])')
                        if view == 'outputs':
                            expect(page.locator('#revisions')).to_contain_text('Morgan Example')
                        if view == 'candidates':
                            expect(page.locator('#jobs')).to_contain_text('Completed')
                        if view == 'ats':
                            expect(page.locator('#ats-rows')).to_contain_text('Frontend Developer')
                        if view == 'templates':
                            expect(page.locator('#template-cards .code').first).to_contain_text('documentclass', timeout=10000)
                        if view == 'settings':
                            expect(page.locator('#settings-grid')).to_contain_text('qwen2.5:7b')
                        if view == 'logs':
                            expect(page.locator('#log-content')).to_contain_text('staging watcher')
                        page.wait_for_timeout(250)
                        visible = page.evaluate("() => [...document.querySelectorAll('[data-view]')]"
                                                ".filter(v => v.offsetParent !== null).map(v => v.dataset.view)")
                        assert visible == [view], f'{size}/{view}: visible views {visible}'
                        no_horizontal_overflow(page, f'{size}/{view}')
                        if SHOTS:
                            page.screenshot(path=str(SHOTS / f'{size}-{view}.png'), full_page=True)
                    if size == 'mobile':
                        page.goto(origin + '/#/dashboard')
                        expect(page.locator('#sidebar')).to_be_hidden()
                        page.click('#menu-toggle')
                        expect(page.locator('#sidebar')).to_be_visible()
                        page.click('#sidebar [data-route="outputs"]')
                        expect(page.locator('[data-view="outputs"]')).to_be_visible()
                        expect(page.locator('#sidebar')).to_be_hidden()
                browser.close()
        finally:
            for item in (server, ollama):
                item.shutdown()
                item.server_close()
            for thread in threads:
                thread.join()
        assert not errors, 'Browser console errors: ' + '; '.join(errors)
    print('OK ResumeOps UI end-to-end check passed')


if __name__ == '__main__':
    main()
