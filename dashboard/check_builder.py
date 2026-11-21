"""Browser acceptance for the parity wizard, results, and operations routes."""
import argparse
import hashlib
import json
import re
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from resume_dashboard import Workspace, handler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from resume_bot.pipeline import scan


class EvidenceModel:
    model = 'browser-fixture'

    def generate(self, prompt):
        return {'edits': json.loads(prompt.split('INPUT ARRAY:', 1)[1])}


def check(screenshots):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = Workspace(root, code_root=ROOT)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(workspace, '', 'test'))
        url = f'http://127.0.0.1:{server.server_port}'
        server.RequestHandlerClass = handler(workspace, url, 'test')
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                context = browser.new_context(viewport={'width': 1360, 'height': 960},
                                              permissions=['clipboard-read', 'clipboard-write'])
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))

                def picture(name):
                    if screenshots:
                        page.screenshot(path=screenshots / name, full_page=True,
                                        animations='disabled')

                page.goto(url)
                expect(page.get_by_role('heading', name='Start', exact=True)).to_be_visible()
                expect(page.locator('#step-meta')).to_have_text('Step 1 of 9 · Start')
                expect(page.locator('#steps li')).to_have_count(9)
                picture('new-start.png')
                # Primary builder accepts a file and stays on the same screen.
                import_page = context.new_page()
                import_page.goto(url)
                assert import_page.locator('a[href="/operations"]').count() == 0
                import_page.get_by_role('button', name='Create Resume').click()
                expect(import_page.get_by_role('heading', name='Import or enter manually')).to_be_visible()
                # Continue with "import" selected but nothing imported explains why it cannot continue.
                import_page.get_by_role('button', name='Continue').click()
                expect(import_page.locator('.validation-notice')).to_contain_text('Import and prefill')
                expect(import_page.get_by_role('button', name='Import and prefill')).to_be_disabled()
                import_page.locator('#resume-upload').set_input_files({
                    'name':'resume.exe','mimeType':'application/octet-stream','buffer':b'invalid'})
                import_page.get_by_role('button', name='Import and prefill').click()
                expect(import_page.locator('#resume-import-status')).to_contain_text('Unsupported file type')
                # Exactly one import control exists in the whole page.
                expect(import_page.locator('#template-import')).to_have_count(0)
                expect(import_page.locator('input[type=file]')).to_have_count(1)
                expect(import_page.get_by_role('button', name='Import and prefill')).to_have_count(1)
                import_page.locator('#resume-upload').set_input_files({
                    'name':'verified.json','mimeType':'application/json',
                    'buffer':json.dumps({'profile':{
                        'full_name':'Jordan Import Example','email':'jordan@example.com',
                        'headline':'Frontend Developer','skills':['React','TypeScript'],
                        'projects':[{'name':'Campus Events Portal',
                            'description':'React app listing college events'}, 'Expense tracker CLI'],
                        'certifications':[{'name':'Example Cloud Practitioner'}],
                        'experience':[{'company':'Example Studio','position':'Developer',
                            'start':'January 2020','end':'Present',
                            'bullets':['Built frontend dashboards']}],
                        'education':[{'institution':'Sample University','degree':'B.Tech'}]},
                        'target':{'role':'fullstack-developer','template':'compact',
                            'experience_years':5}}).encode('utf8')})
                import_page.get_by_role('button', name='Import and prefill').click()
                expect(import_page.get_by_role('heading', name='Choose a target role')).to_be_visible()
                expect(import_page.locator('#builder-status')).to_contain_text('Imported')
                assert import_page.evaluate('location.pathname') == '/'
                expect(import_page.get_by_label('Target role')).to_have_value('fullstack-developer')
                expect(import_page.get_by_label('Years of experience')).to_have_value('5')
                import_page.get_by_role('button', name='Continue').click()
                expect(import_page.get_by_label('Full name')).to_have_value('Jordan Import Example')
                expect(import_page.get_by_label('Email')).to_have_value('jordan@example.com')
                import_page.get_by_role('button', name='Continue').click()
                expect(import_page.locator('#count-experience')).to_have_text('(1)')
                expect(import_page.get_by_label('Employer')).to_have_value('Example Studio')
                expect(import_page.get_by_label('Verified skills')).to_have_value('React\nTypeScript')
                # Structured projects/certifications must survive import, not be silently dropped.
                expect(import_page.get_by_label('Verified projects')).to_have_value(
                    'Campus Events Portal — React app listing college events\nExpense tracker CLI')
                expect(import_page.get_by_label('Verified certifications')).to_have_value('Example Cloud Practitioner')
                # Complete imported cards stay collapsed with a one-line summary.
                expect(import_page.locator('#experience-entries .entry')).not_to_have_attribute('open', '')
                expect(import_page.locator('#experience-entries .entry summary')).to_contain_text('Developer · Example Studio')
                assert not list(workspace.inbox.glob('jordan-import-example-*.json')), 'Import must not queue'
                import_page.close()

                # Existing document-import API is reused, not redirected to Operations.
                original_draft = workspace.draft
                def synthetic_draft(extension, data, report=lambda stage: None):
                    assert extension == '.txt'
                    report('Reading document')
                    report('Extracting candidate details with Ollama')
                    return {'profile':{'full_name':'Taylor Document Example',
                        'email':'taylor@example.com','skills':['Python'],
                        'experience':[{'company':'Sample Inc','position':'Engineer',
                            'start':'February 2018','end':'January 2021','bullets':['Built automation']},
                            {'company':'Second Example','position':'Analyst','start':'March 2022'}],
                        'education':[{'institution':'Sample College','degree':''}]},'source':data.decode('utf8')}
                workspace.draft = synthetic_draft
                try:
                    upload_context = browser.new_context()
                    upload_page = upload_context.new_page()
                    upload_page.goto(url)
                    upload_page.get_by_role('button', name='Create Resume').click()
                    upload_page.locator('#resume-upload').set_input_files({
                        'name':'candidate.txt','mimeType':'text/plain',
                        'buffer':b'Taylor Document Example resume. Employment Sample Inc. ' * 5})
                    upload_page.get_by_role('button', name='Import and prefill').click()
                    expect(upload_page.get_by_role('heading',name='Choose a target role')).to_be_visible(timeout=15000)
                    assert upload_page.evaluate('location.pathname') == '/'
                    # Experience level is calculated from the dated roles instead of defaulting to 1 year.
                    expect(upload_page.locator('#experience-level')).to_contain_text('calculated from dated roles')
                    # Feb 2018 - Jan 2021 is dated (36 months); the role without an end date is not counted.
                    expect(upload_page.get_by_label('Years of experience')).to_have_value('3')
                    # Role selection stays required and Continue explains it.
                    upload_page.get_by_role('button',name='Continue').click()
                    expect(upload_page.locator('.validation-notice')).to_contain_text('Target role')
                    upload_page.get_by_label('Target role').select_option('fullstack-developer')
                    upload_page.get_by_role('button',name='Continue').click()
                    expect(upload_page.get_by_label('Full name')).to_have_value('Taylor Document Example')
                    upload_page.get_by_role('button',name='Continue').click()
                    # The education card with a missing degree is opened automatically and flagged.
                    expect(upload_page.locator('#education-entries .entry')).to_have_attribute('open', '')
                    expect(upload_page.locator('#education-entries .entry summary')).to_contain_text('Missing: Degree')
                    expect(upload_page.locator('#tab-education')).to_have_attribute('data-missing', '')
                    upload_page.get_by_role('button',name='Continue').click()
                    expect(upload_page.locator('.validation-notice')).to_contain_text('Education 1')
                    expect(upload_page.get_by_role('tab', name='Education (1)')).to_have_attribute('aria-selected','true')
                    upload_page.get_by_label('Degree', exact=True).fill('B.Sc')
                    upload_page.get_by_role('button',name='Continue').click()
                    # Review shows the original text beside the extracted details.
                    expect(upload_page.get_by_role('heading',name='Review and confirm facts')).to_be_visible(timeout=15000)
                    expect(upload_page.locator('#import-source')).to_have_value(re.compile('Employment Sample Inc'))
                    expect(upload_page.locator('#original-col')).to_be_visible()
                    expect(upload_page.locator('#missing-fields')).to_contain_text('Phone')
                    upload_page.locator('#missing-fields button', has_text='Phone').click()
                    expect(upload_page.get_by_label('Phone')).to_be_focused()
                    assert not list(workspace.inbox.glob('taylor-document-example-*.json'))
                    upload_context.close()
                finally:
                    workspace.draft = original_draft

                page.get_by_role('button', name='Create Resume').click()
                page.locator('[name=source][value=manual]').check()
                page.get_by_role('button', name='Continue').click()
                expect(page.get_by_role('heading', name='Choose a target role')).to_be_visible()
                role = page.get_by_label('Target role')
                expect(role.locator('option', has_text=re.compile(r'^Full Stack Developer$'))).to_have_count(1)
                role.select_option('fullstack-developer')
                page.get_by_label('Resume template').select_option('compact')
                expect(page.locator('#template-name')).to_have_text('Compact')
                years = page.get_by_label('Years of experience')
                years.select_option('25')
                expect(page.locator('#experience-level')).to_contain_text('25+ years')
                years.select_option('12')
                expect(page.locator('#experience-level')).to_have_text('12 years · Lead / Principal')
                page.get_by_text('Target keywords', exact=True).click()
                expect(page.locator('#catalog-keywords')).to_contain_text('SQL')
                page.get_by_label('Job description').fill('React.js and SQL with C++')
                page.get_by_role('button', name='Find role keywords').click()
                expect(page.get_by_label('Additional keywords')).to_have_value('React.js\nSQL')
                page.get_by_label('Additional keywords').fill('C++')
                page.get_by_role('button', name='Save role defaults').click()
                expect(page.locator('#builder-status')).to_have_text('Role keyword defaults saved.')
                assert page.request.get(url+'/api/keywords?role=fullstack-developer').json()['additional']==['C++']
                assert page.request.post(url+'/api/keywords/update',data={'role':'fullstack-developer'}).status==403
                picture('new-role.png')

                page.get_by_role('button',name='Continue').click()
                page.get_by_label('Full name').fill('Robin Parity Example')
                page.get_by_label('Email').fill('robin@example.com')
                page.get_by_label('Phone').fill('+1 555 0100')
                page.get_by_label('Current company').fill('Example Labs')
                page.get_by_label('Professional headline').fill('Software Engineer')
                page.get_by_label('Portfolio').fill('https://example.com/portfolio')
                picture('new-personal.png')
                page.get_by_role('button',name='Continue').click()
                page.get_by_role('tab', name='Skills').click()
                page.get_by_label('Professional summary').fill('Builds Python and SQL reporting tools')
                page.get_by_label('Verified skills').fill('Python\nSQL')
                page.get_by_role('tab', name='Projects').click()
                page.get_by_label('Verified projects').fill('Maintained a reporting utility')
                page.get_by_role('tab', name=re.compile('^Experience')).click()
                page.get_by_role('button',name='Add experience').click()
                expect(page.get_by_label('Employer')).to_have_value('Example Labs')
                page.get_by_label('Position',exact=True).fill('Software Engineer')
                page.get_by_label('Start date',exact=True).fill('January 2020')
                page.get_by_label('Verified work contributions').fill('Maintained Python reporting tools')
                # A hand-picked level is never overwritten by dates.
                expect(years).to_have_value('12')
                page.get_by_role('tab', name=re.compile('^Education')).click()
                page.get_by_role('button',name='Add education').click()
                expect(page.get_by_role('tab', name='Education (1)')).to_have_attribute('aria-selected','true')
                page.get_by_label('Institution').fill('Example College')
                page.get_by_label('Degree').fill('Computer Science')
                page.get_by_role('button',name='Remove entry').last.click()
                expect(page.locator('#count-education')).to_have_text('(0)')
                picture('new-experience.png')
                page.get_by_role('button',name='Continue').click()
                expect(page.get_by_role('heading',name='Review and confirm facts')).to_be_visible()
                expect(page.locator('#review-summary')).to_contain_text('ATS readiness')
                expect(page.locator('#review-summary')).to_contain_text('Keyword coverage')
                expect(page.locator('#original-col')).to_be_hidden()
                expect(page.locator('#missing-fields')).to_contain_text('Education (recommended)')
                picture('new-review.png')
                page.get_by_role('button',name='Back').click()
                page.get_by_role('button',name='Continue').click()
                page.get_by_role('button',name='Continue').click()
                expect(page.locator('.validation-notice')).to_contain_text('Confirm the candidate facts')
                page.get_by_label('I confirm these candidate facts are accurate.').check()
                page.get_by_role('button',name='Continue').click()
                expect(page.get_by_role('heading',name='Generate resume')).to_be_visible()
                expect(page.locator('#generate-summary')).to_contain_text('Robin Parity Example')
                expect(page.locator('#generate-summary')).to_contain_text('12 years · Lead / Principal')
                picture('new-generate.png')
                page.get_by_role('button',name='Generate resume').click()
                expect(page.locator('#builder-status')).to_have_text('Resume queued.')
                expect(page.get_by_role('heading',name=re.compile('Waiting to start|Processing Resume'))).to_be_visible()
                [job]=list(workspace.inbox.glob('robin-parity-example-*.json'))
                queued=json.loads(job.read_text(encoding='utf-8'))
                assert queued['target']['experience_years']==12 and queued['target']['template']=='compact'
                # The just-queued job is never reported as missing while the first poll catches up.
                expect(page.locator('#job-stage')).not_to_have_text('This job is no longer in the inbox.')
                # Duplicate generation is blocked: going back and pressing Generate again queues nothing.
                page.locator('#steps li[data-target="6"] button').click()
                page.get_by_role('button',name='Generate resume').click()
                expect(page.locator('#builder-status')).to_contain_text('already being generated')
                assert len(list(workspace.inbox.glob('robin-parity-example-*.json')))==1

                status=scan(workspace.inbox,workspace.output,ai=EvidenceModel(),once=True)[job.name]
                assert status['status']=='ok' and status['percent']==100
                output=Path(status['output'])
                before={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in output.iterdir()}
                record=json.loads((output/'result.json').read_text(encoding='utf-8'))
                assert record['pdf_checks']['pages']==1
                expect(page.get_by_role('heading',name='Resume results')).to_be_visible(timeout=15000)
                expect(page.locator('#result-success')).to_be_visible()
                expect(page.get_by_title('PDF preview')).to_be_visible()
                # The builder was cleared after the verified result; results stay on screen.
                assert page.locator('[name=full_name]').input_value()==''
                assert page.locator('#experience-entries .entry').count()==0
                expect(page.locator('#ats-ring-value')).to_have_text(str(record['ats']['ats_readiness']) if record.get('ats') and 'ats_readiness' in record['ats'] else re.compile(r'\d+'))
                assert 'Maintained a reporting utility' in (output/'main.tex').read_text()
                picture('new-preview.png')
                with page.expect_download() as pdf:
                    page.locator('#tile-pdf').click()
                assert Path(pdf.value.path()).read_bytes().startswith(b'%PDF-')
                with page.expect_download() as pdf:
                    page.get_by_role('link',name='Download PDF',exact=True).click()
                assert Path(pdf.value.path()).read_bytes().startswith(b'%PDF-')
                page.locator('.tile', has_text='LaTeX Code').click()
                expect(page.get_by_role('tab',name='LaTeX Code')).to_have_attribute('aria-selected','true')
                expect(page.locator('#latex-source')).to_contain_text('section*{Projects}')
                page.get_by_role('button',name='Copy LaTeX').click()
                expect(page.locator('#copy-status')).to_have_text('LaTeX copied.')
                assert 'Example Labs' in page.evaluate('navigator.clipboard.readText()')
                with page.expect_download() as tex:
                    page.get_by_role('link',name='Download LaTeX').click()
                assert Path(tex.value.path()).read_bytes()==(output/'main.tex').read_bytes()
                picture('new-latex.png')
                page.get_by_role('tab',name='LaTeX Code').press('ArrowRight')
                expect(page.get_by_role('tab',name='ATS Analysis')).to_have_attribute('aria-selected','true')
                for label in ['Job role relevance','Supported keyword coverage','Resume structure','Project relevance','Evidence completeness',
                              'Matched keywords','Missing keywords','Improvement suggestions']:
                    expect(page.locator('#ats-result')).to_contain_text(label)
                expect(page.locator('#ats-result')).to_contain_text('C++')
                # Every accepted AI rewrite is listed; this echoing test model changes nothing.
                page.get_by_role('tab', name='AI Improvements').click()
                expect(page.locator('#ai-result')).to_contain_text('Ollama rewrote 0 of')
                expect(page.locator('#ai-result')).to_contain_text('No line needed rewording.')
                # 12 years was selected but the only role has no end date, so no years are stated.
                expect(page.locator('#ai-result')).to_contain_text('Experience years omitted: no dated roles')
                picture('new-ats.png')
                page.get_by_role('button',name='Create another resume').click()
                expect(page.get_by_role('heading',name='Start',exact=True)).to_be_visible()
                expect(page.locator('#recent-list')).to_contain_text('Robin Parity Example')
                page.get_by_role('link',name='History').click()
                expect(page.get_by_role('heading',name='Generation history')).to_be_visible()
                page.get_by_label('Find a resume').fill('nothing')
                expect(page.get_by_text('No matching resumes.')).to_be_visible()
                page.get_by_label('Find a resume').fill('Robin')
                page.locator('#revision-list').get_by_role('button',name='View resume').click()
                expect(page.get_by_role('heading',name='Resume results')).to_be_visible()
                expect(page.locator('#result-success')).to_be_hidden()
                after={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in output.iterdir()}
                assert before==after

                mobile=browser.new_page(viewport={'width':390,'height':844})
                mobile.goto(url)
                expect(mobile.get_by_role('heading',name='Start',exact=True)).to_be_visible()
                expect(mobile.locator('details.recent')).not_to_have_attribute('open', '')
                assert mobile.evaluate('document.documentElement.scrollWidth <= innerWidth')
                expect(mobile.locator('#step-meta')).to_be_visible()
                expect(mobile.locator('#steps')).to_be_hidden()
                # Builder and History navigation are at least 44 x 44 CSS px on a phone (WCAG 2.5.5).
                nav=mobile.locator('.masthead nav a').evaluate_all(
                    'items => items.map(item => {const box = item.getBoundingClientRect(); return [item.textContent, box.width, box.height];})')
                assert nav and all(width>=44 and height>=44 for _,width,height in nav), nav
                mobile.get_by_role('link', name='History').click()
                expect(mobile.locator('#history')).to_be_visible()
                mobile.get_by_role('button', name='Back to builder').click()
                expect(mobile.locator('#history')).to_be_hidden()
                mobile.get_by_role('button', name='Create Resume').click()
                expect(mobile.locator('.step-actions')).to_have_css('position', 'sticky')
                mobile.locator('[name=source][value=manual]').check()
                mobile.get_by_role('button', name='Continue').click()
                mobile.get_by_label('Target role').select_option('fullstack-developer')
                mobile.get_by_role('button', name='Continue').click()
                expect(mobile.get_by_role('heading',name='Personal details')).to_be_visible()
                assert not mobile.locator('#more-personal').evaluate('(node) => node.open')
                mobile.get_by_label('Full name').fill('Mobile Example')
                mobile.get_by_label('Email').fill('mobile@example.com')
                mobile.get_by_role('button', name='Continue').click()
                expect(mobile.get_by_role('heading',name='Experience and education')).to_be_visible()
                mobile.get_by_role('button',name='Add experience').click()
                expect(mobile.get_by_label('Employer')).to_be_visible()
                mobile.get_by_label('Employer').fill('Example Mobile Studio')
                mobile.get_by_label('Position',exact=True).fill('Engineer')
                expect(mobile.locator('.entry > summary')).to_contain_text('Engineer · Example Mobile Studio')
                # A required start date in a closed card must never silently block Continue.
                mobile.locator('#experience-entries .entry > summary').click()
                expect(mobile.locator('#experience-entries .entry')).not_to_have_attribute('open', '')
                mobile.get_by_role('button',name='Continue').click()
                expect(mobile.locator('.validation-notice')).to_be_visible()
                expect(mobile.locator('.validation-notice')).to_contain_text('Experience 1')
                expect(mobile.locator('.validation-notice')).to_contain_text('Start date')
                expect(mobile.locator('#experience-entries .entry')).to_have_attribute('open', '')
                expect(mobile.get_by_label('Start date',exact=True)).to_have_attribute('aria-invalid','true')
                mobile.get_by_label('Start date',exact=True).fill('Jan 2020')
                expect(mobile.locator('.validation-notice')).to_be_hidden()
                mobile.get_by_label('End date',exact=True).fill('Present')
                # Dated roles set the experience level automatically when it was not chosen by hand.
                assert int(mobile.locator('[name=years]').input_value()) >= 5
                # The same reveal-and-explain behaviour works for a closed education card in another tab.
                mobile.get_by_role('tab', name=re.compile('^Education')).click()
                mobile.get_by_role('button',name='Add education').click()
                mobile.get_by_label('Institution').fill('Example University')
                mobile.locator('#education-entries .entry > summary').click()
                mobile.get_by_role('tab', name=re.compile('^Experience')).click()
                mobile.get_by_role('button',name='Continue').click()
                expect(mobile.locator('.validation-notice')).to_contain_text('Education 1')
                expect(mobile.locator('.validation-notice')).to_contain_text('Degree')
                expect(mobile.locator('#education-entries .entry')).to_have_attribute('open','')
                mobile.get_by_label('Degree',exact=True).fill('B.Tech')
                expect(mobile.locator('.validation-notice')).to_be_hidden()
                mobile.get_by_role('button',name='Continue').click()
                expect(mobile.get_by_role('heading',name='Review and confirm facts')).to_be_visible(timeout=15000)
                assert mobile.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if screenshots:
                    mobile.screenshot(path=screenshots/'new-mobile.png',full_page=True)
                # Rotation keeps typed values; optional fields open on wide screens.
                mobile.set_viewport_size({'width':1360,'height':960})
                expect(mobile.locator('#more-personal')).to_have_attribute('open', '')
                assert mobile.get_by_label('Full name').input_value()=='Mobile Example'
                assert mobile.get_by_label('Employer').input_value()=='Example Mobile Studio'
                mobile.close()
                # Operations remains compatible as an internal route, not a second UI in navigation.
                ops_page = context.new_page()
                ops_page.goto(url+'/operations')
                expect(ops_page.get_by_role('heading',name='Resume operations')).to_be_visible()
                expect(ops_page.get_by_label('Candidate file')).to_be_visible()
                ops_page.close()
                phone_ops = browser.new_page(viewport={'width':390,'height':844})
                phone_ops.goto(url+'/operations')
                expect(phone_ops.get_by_role('heading',name='Resume operations')).to_be_visible()
                cards = phone_ops.locator('.metrics > div')
                expect(cards).to_have_count(4)
                positions = cards.evaluate_all(
                    'cards => cards.map(card => Math.round(card.getBoundingClientRect().top))')
                assert len(set(positions)) == 1, positions  # four KPIs fit in a single mobile row
                assert phone_ops.evaluate('document.documentElement.scrollWidth <= innerWidth')
                phone_ops.close()
                assert not errors, errors
                browser.close()
                print('Builder browser acceptance passed: wizard, keywords, history, three views, clipboard, actual TeX/PDF, downloads, mobile, and operations.')
        finally:
            server.shutdown()
            server.RequestHandlerClass.stop_imports()
            server.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--screenshots',type=Path)
    args=parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True,exist_ok=True)
    check(args.screenshots)
