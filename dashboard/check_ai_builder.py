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
from resume_bot.claim_audit import claim_violations, matches_pdf_text
from resume_bot.pipeline import scan

DRAFT = {
    'professional_summary': 'Software engineering graduate focused on Python.',
    'technical_skills': ['Python', 'Unconfirmed Vendor Tool'],
    'core_competencies': ['Testing'],
    'key_achievements': ['Increased revenue by 75%'],
    'project_ideas': [{'title': 'Unbuilt portfolio idea', 'description': 'Try making a dashboard.'}],
    'certification_recommendations': ['Unearned certification'],
}


def assert_tap_targets(page, selector):
    """Header navigation must be at least 44 x 44 CSS px (WCAG 2.5.5) on phone and desktop."""
    sizes = page.locator(selector).evaluate_all(
        'items => items.filter(item => item.offsetParent).map(item => {'
        'const box = item.getBoundingClientRect(); return [item.textContent.trim(), box.width, box.height]; })')
    assert sizes, selector
    small = [size for size in sizes if size[1] < 44 or size[2] < 44]
    assert not small, small


def verify_completed(page, workspace, candidate, *, education_first, builder):
    job = next(path for path in workspace.inbox.glob('*.json')
               if json.loads(path.read_text(encoding='utf-8')).get('profile', {}).get('full_name') == candidate)
    request = json.loads(job.read_text(encoding='utf-8'))
    assert request['target']['approved'] is True
    # Production runs the watcher with AI polishing on; approved resumes must still bypass it.
    with patch('resume_bot.pipeline.polish', side_effect=AssertionError('approved resume was reworded')):
        scan(workspace.inbox, workspace.output, once=True, polish_enabled=True)
    expect(page.locator('#result-success')).to_be_visible(timeout=15000)
    records = workspace.approvals_for(job.name)
    assert len(records) == 1, records
    assert not claim_violations(request['profile'], records[0]), claim_violations(request['profile'], records[0])
    with page.expect_download() as pdf:
        page.locator('#tile-pdf').click()
    downloaded = workspace.root / 'browser-download.pdf'
    pdf.value.save_as(downloaded)
    text = read_resume_text(downloaded)
    assert candidate in text and 'Python' in text, text
    years = 0 if education_first else request['target']['experience_years']
    assert matches_pdf_text(text, request['profile'], years), text
    for excluded in ('Unconfirmed Vendor Tool', 'Increased revenue', 'Unbuilt portfolio idea',
                     'Unearned certification', 'Testing', DRAFT['professional_summary']):
        assert excluded not in text, (excluded, text)
    if education_first:
        assert 'education' in text.casefold() and 'professional work experience' not in text.casefold()
    else:
        assert text.casefold().index('professional work experience') < text.casefold().index('education')
    page.locator('[data-tab="tab-latex"]').click()
    with page.expect_download() as latex:
        page.locator('#download-latex').click()
    assert b'\\documentclass' in latex.value.path().read_bytes()
    page.locator('#nav-history').click()
    expect(page.locator('#history').get_by_text(candidate, exact=True)).to_be_visible()
    page.goto(page.url.split('/workflow')[0] + builder)
    expect(page.get_by_label('Full name')).to_have_value('')


def check(ai_first=False):
    builder = '/' if ai_first else '/ai-builder'
    with tempfile.TemporaryDirectory(prefix='resumeops-ai-') as directory:
        workspace = Workspace(Path(directory), code_root=ROOT)
        with ThreadingHTTPServer(('127.0.0.1', 0), handler(workspace, '', 'test-token')) as server:
            origin = f'http://127.0.0.1:{server.server_port}'
            server.RequestHandlerClass = handler(workspace, origin, 'test-token', ai_first=ai_first)
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
                    home = page.request.get(origin + '/').text()
                    assert ('id="ai-form"' in home) is ai_first and ('id="candidate"' in home) is not ai_first
                    assert 'id="candidate"' in page.request.get(origin + '/workflow').text()
                    page.goto(origin + builder)
                    assert_tap_targets(page, '.ai-nav-actions a, .ai-nav-actions button')
                    # The compact view surfaces essential fields but hides long optional lists.
                    expect(page.locator('#ai-contact')).not_to_have_attribute('open', '')
                    expect(page.locator('#ai-section-work_experience')).not_to_have_attribute('open', '')
                    expect(page.locator('#ai-section-education')).not_to_have_attribute('open', '')
                    expect(page.locator('#ai-section-projects')).not_to_have_attribute('open', '')
                    expect(page.locator('#ai-section-coding_platforms')).not_to_have_attribute('open', '')
                    expect(page.locator('#ai-basics [name=role]')).to_be_visible()
                    expect(page.locator('#ai-basics [name=phone]')).to_be_hidden()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    # The import spinner is directly under the title, never a bottom sticky banner.
                    expect(page.locator('#ai-progress')).to_be_hidden()
                    page.evaluate("""() => updateProgress({
                      status:'processing', stage:'Extracting candidate details with Ollama',
                      progress:{percent:47,steps_completed:2,steps_total:5,
                                fields_emitted:4,fields_total:15},
                      metrics:{chars:1280,tokens:27,elapsed:9.3}}, 'Importing resume')""")
                    expect(page.locator('#ai-progress')).to_be_visible()
                    expect(page.get_by_role('status')).to_contain_text('Extracting candidate details')
                    expect(page.locator('#ai-progress-metrics')).to_contain_text('1,280 characters generated')
                    expect(page.locator('#ai-progress-metrics')).to_contain_text('27 stream chunks')
                    expect(page.locator('#ai-progress-value')).to_have_text('47%')
                    expect(page.get_by_role('progressbar')).to_have_attribute('aria-valuenow','47')
                    expect(page.locator('#ai-progress-steps')).to_contain_text('4/15 AI fields emitted')
                    page.evaluate("""() => updateProgress({
                      status:'processing', stage:'Extracting candidate details with Ollama',
                      progress:{percent:74,steps_completed:2,steps_total:5,
                                fields_emitted:13,fields_total:15},
                      metrics:{chars:3380,tokens:120,elapsed:31}}, 'Importing resume')""")
                    expect(page.locator('#ai-progress-value')).to_have_text('74%')
                    expect(page.get_by_role('progressbar')).to_have_attribute('aria-valuenow','74')
                    assert float(page.locator('.ai-ring-fill').evaluate(
                        "(el) => el.style.strokeDashoffset")) < 175.93 * .5
                    assert page.locator('#ai-progress').evaluate(
                        "(item) => getComputedStyle(item).position !== 'sticky' && "
                        "item.getBoundingClientRect().top < innerHeight")
                    page.get_by_label('Full name').fill('Previous Candidate')
                    page.locator('#ai-contact > summary').click()
                    page.get_by_label('Phone').fill('555-0101')
                    expect(page.locator('#ai-contact-summary')).to_contain_text('1 added')
                    page.get_by_label('Target role').fill('Full Stack Developer')
                    page.get_by_label('Total years of experience').fill('0')
                    page.get_by_label('Email').fill('synthetic@example.test')
                    portfolio = page.get_by_label('Portfolio URL')
                    portfolio.fill('invalid-url')
                    page.locator('#ai-contact > summary').click()
                    page.locator('#ai-form button[type=submit]').click()
                    expect(page.locator('#ai-contact')).to_have_attribute('open', '')
                    expect(portfolio).to_be_focused()
                    portfolio.fill('')
                    page.get_by_label('Target role').fill('')
                    page.get_by_label('Total years of experience').fill('')
                    page.get_by_label('Email').fill('')
                    page.wait_for_function("""async () =>
                      (await ResumeRecovery.read('ai-builder'))?.intake?.phone === '555-0101'""")
                    # Dismissing the confirmation must not destroy a saved draft.
                    page.once('dialog', lambda dialog: dialog.dismiss())
                    page.get_by_role('button', name='Clear saved draft').click()
                    expect(page.get_by_label('Full name')).to_have_value('Previous Candidate')
                    # Explicitly confirming clears only this browser's in-progress AI draft.
                    page.once('dialog', lambda dialog: dialog.accept())
                    page.get_by_role('button', name='Clear saved draft').click()
                    expect(page.get_by_label('Full name')).to_have_value('')
                    page.reload()
                    expect(page.get_by_label('Full name')).to_have_value('')
                    second = page.context.new_page()
                    second.goto(origin + builder)
                    expect(second.get_by_role('status')).to_contain_text('open in another tab')
                    assert not second.get_by_label('Full name').count()
                    second.close()

                    fixture = {'profile': {'full_name': 'AI First Synthetic', 'email': 'ai-first@example.com',
                                           'phone': '+91 90000 00001',
                                           'education': [{'degree': 'B.Tech, Computer Science',
                                                          'institution': 'Imported University',
                                                          'start': '2022', 'end': ''}]},
                               'source': 'AI First Synthetic, ai-first@example.com, +91 90000 00001\nSkills: Python'}
                    access['armed'] = False
                    with patch.object(workspace, 'draft', return_value=fixture):
                        page.locator('#ai-file').set_input_files({'name': 'synthetic.txt', 'mimeType': 'text/plain',
                                                                  'buffer': b'Synthetic resume'})
                        page.get_by_role('button', name='Import details').click()
                        expect(page.get_by_role('status')).to_contain_text('Details imported')
                        expect(page.locator('#ai-progress-value')).to_have_text('100%')
                        expect(page.get_by_role('progressbar')).to_have_attribute('aria-valuenow','100')
                    expect(page.locator('#ai-upload-options')).not_to_have_attribute('open', '')
                    expect(page.locator('#ai-section-education > summary')).to_contain_text('(1)')
                    expect(page.get_by_label('Full name')).to_have_value('AI First Synthetic')
                    expect(page.get_by_label('Phone')).to_have_value('+91 90000 00001')
                    # A lone year extracted as the start date is the graduation year.
                    expect(page.locator('#ai-education [name=graduation_year]')).to_have_value('2022')
                    # Degree and branch arrive as one verbatim span and are split into their own fields.
                    expect(page.locator('#ai-education [name=degree]')).to_have_value('B.Tech')
                    expect(page.locator('#ai-education [name=specialization]')).to_have_value('Computer Science')
                    expect(page.locator('#ai-section-education > summary')).to_contain_text('(1)')
                    page.locator('#ai-section-education > summary').click()
                    page.locator('#ai-education').get_by_role('button', name='Remove entry').click()
                    expect(page.locator('#ai-section-education > summary')).to_contain_text('(0)')
                    access['armed'] = True
                    page.get_by_label('Target role').fill('Full Stack Developer')
                    page.get_by_label('Total years of experience').fill('0')
                    page.get_by_label('Full name').fill('AI First Synthetic')
                    page.locator('#ai-contact > summary').click()
                    expect(page.locator('#ai-contact')).to_have_attribute('open', '')
                    page.get_by_label('Phone').fill('+91 90000 00002')
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
                    supported.locator('xpath=ancestor::details[1]').locator('summary').click()
                    expect(unsupported.get_by_role('checkbox')).not_to_be_checked()
                    supported.get_by_role('checkbox').check()
                    expect(supported.locator('xpath=ancestor::details[1]').locator('summary')).to_contain_text('1/')
                    page.get_by_label('I confirm the candidate details and selected statements are accurate.').check()
                    page.get_by_role('button', name='Preview and check ATS').click()
                    expect(page.get_by_text('ATS readiness:', exact=False)).to_be_visible()
                    expect(page.locator('#ai-review')).to_be_hidden()
                    page.get_by_role('button', name='Back to review').click()
                    expect(page.locator('#ai-review')).to_be_visible()
                    page.get_by_role('button', name='Preview and check ATS').click()
                    expect(page.locator('#ai-review')).to_be_hidden()
                    page.locator('#ai-preview-content details').filter(
                        has_text='BSc | Example University').locator('summary').click()
                    expect(page.get_by_text('BSc | Example University', exact=True)).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert page.locator('#ai-preview-content details').count() >= 3
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
                    verify_completed(page, workspace, 'AI First Synthetic', education_first=True, builder=builder)

                    desktop = browser.new_context(viewport={'width': 1360, 'height': 960})
                    senior = desktop.new_page()
                    senior.goto(origin + builder)
                    assert_tap_targets(senior, '.ai-nav-actions a, .ai-nav-actions button')
                    senior.get_by_label('Target role').fill('Principal Platform Engineer')
                    senior.get_by_label('Total years of experience').fill('9')
                    senior.get_by_label('Full name').fill('Senior Synthetic')
                    senior.locator('#ai-contact > summary').click()
                    expect(senior.locator('#ai-contact')).to_have_attribute('open', '')
                    senior.get_by_label('Phone').fill('+91 90000 00003')
                    senior.get_by_label('Email').fill('senior@example.com')
                    senior.locator('#ai-section-work_experience > summary').click()
                    senior.get_by_role('button', name='Add work experience').click()
                    senior.get_by_label('Employer').fill('Example Engineering')
                    senior.get_by_label('Designation').fill('Software Engineer')
                    senior.get_by_label('Start date').fill('Jan 2017')
                    senior.get_by_label('End date').fill('Present')
                    senior.locator('#ai-section-education > summary').click()
                    senior.get_by_role('button', name='Add education').click()
                    senior.get_by_label('Degree', exact=True).fill('BSc')
                    senior.get_by_label('University', exact=True).fill('Example University')
                    senior.get_by_role('button', name='Draft resume with Ollama').click()
                    expect(senior.get_by_role('heading', name='Review AI suggestions')).to_be_visible()
                    skills = senior.locator('[data-provenance="unsupported_role_keyword"]').filter(has_text=re.compile(r'^Skill '))
                    expect(skills).to_have_count(2)
                    skills.first.locator('xpath=ancestor::details[1]').evaluate('(item) => item.open = true')
                    skills.first.get_by_role('checkbox').check()
                    senior.locator('#ai-attest').check()
                    senior.get_by_role('button', name='Preview and check ATS').click()
                    senior.get_by_role('button', name='Generate resume').click()
                    senior.wait_for_url(origin + '/workflow')
                    verify_completed(senior, workspace, 'Senior Synthetic', education_first=False, builder=builder)
                    browser.close()
            finally:
                server.shutdown()
                server.RequestHandlerClass.stop_imports()
    print(f"AI builder at {builder}: mobile, same-tab Access recovery, approved-only ATS, immutable queue, replay, "
          "verbatim approved render, permanent approval record, and full PDF claim audit passed.")


if __name__ == '__main__':
    check(ai_first=False)  # Phase 1: classic builder at /, AI builder at /ai-builder
    check(ai_first=True)   # Phase 2: AI builder at /, classic builder at /workflow
