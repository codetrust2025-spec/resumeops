"""Browser acceptance for the parity wizard, results, and operations routes."""
import argparse
import hashlib
import json
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
                expect(page.get_by_role('heading', name='Create a resume')).to_be_visible()
                role = page.get_by_label('Target role')
                expect(role.locator('option', has_text='Full Stack Developer')).to_have_count(1)
                picture('new-role.png')
                page.get_by_role('button', name='Continue').click()
                expect(page.get_by_role('heading', name='Choose a target role')).to_be_visible()
                role.select_option('fullstack-developer')
                page.get_by_label('Resume template').select_option('compact')
                expect(page.locator('#template-name')).to_have_text('Compact')
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

                page.get_by_role('button',name='Continue').click()
                page.get_by_label('Full name').fill('Robin Parity Example')
                page.get_by_label('Email').fill('robin@example.com')
                page.get_by_label('Phone').fill('+1 555 0100')
                page.get_by_label('Current company').fill('Example Labs')
                page.get_by_label('Professional headline').fill('Software Engineer')
                page.get_by_label('Portfolio').fill('https://example.com/portfolio')
                picture('new-personal.png')
                page.get_by_role('button',name='Continue').click()
                slider=page.get_by_role('slider',name='Years of experience')
                slider.fill('12')
                expect(page.locator('#experience-level')).to_have_text('12 years · Lead / Principal')
                slider.fill('25')
                expect(page.locator('#experience-level')).to_contain_text('25+ years')
                slider.fill('12')
                page.get_by_label('Professional summary').fill('Builds Python and SQL reporting tools')
                page.get_by_label('Verified skills').fill('Python\nSQL')
                page.get_by_label('Verified projects').fill('Maintained a reporting utility')
                page.get_by_role('button',name='Add experience').click()
                expect(page.get_by_label('Employer')).to_have_value('Example Labs')
                page.get_by_label('Position',exact=True).fill('Software Engineer')
                page.get_by_label('Start date',exact=True).fill('January 2020')
                page.get_by_label('Verified work contributions').fill('Maintained Python reporting tools')
                page.get_by_role('button',name='Add education').click()
                page.get_by_label('Institution').fill('Example College')
                page.get_by_label('Degree').fill('Computer Science')
                page.get_by_role('button',name='Remove entry').last.click()
                picture('new-experience.png')
                page.get_by_role('button',name='Continue').click()
                expect(page.get_by_role('heading',name='Review and generate')).to_be_visible()
                expect(page.locator('#review-summary')).to_contain_text('ATS readiness')
                expect(page.locator('#review-summary')).to_contain_text('Keyword coverage')
                picture('new-review.png')
                page.get_by_role('button',name='Back').click()
                expect(slider).to_have_value('12')
                page.get_by_role('button',name='Continue').click()
                page.get_by_role('button',name='Generate resume').click()
                expect(page.locator('#builder-status')).to_contain_text('Confirm the candidate facts')
                page.get_by_label('I confirm these candidate facts are accurate.').check()
                page.get_by_role('button',name='Generate resume').click()
                expect(page.locator('#builder-status')).to_have_text('Resume queued.')
                [job]=list(workspace.inbox.glob('robin-parity-example-*.json'))
                queued=json.loads(job.read_text(encoding='utf-8'))
                assert queued['target']['experience_years']==12 and queued['target']['template']=='compact'

                status=scan(workspace.inbox,workspace.output,ai=EvidenceModel(),once=True)[job.name]
                assert status['status']=='ok' and status['percent']==100
                output=Path(status['output'])
                before={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in output.iterdir()}
                record=json.loads((output/'result.json').read_text(encoding='utf-8'))
                assert record['pdf_checks']['pages']==1
                expect(page.get_by_role('heading',name='Resume results')).to_be_visible(timeout=15000)
                expect(page.get_by_title('PDF preview')).to_be_visible()
                assert 'Maintained a reporting utility' in (output/'main.tex').read_text()
                picture('new-preview.png')
                with page.expect_download() as pdf:
                    page.get_by_role('link',name='Download PDF',exact=True).click()
                assert Path(pdf.value.path()).read_bytes().startswith(b'%PDF-')
                page.get_by_role('tab',name='LaTeX Code').click()
                expect(page.locator('#latex-source')).to_contain_text('PROJECTS')
                page.get_by_role('button',name='Copy LaTeX').click()
                expect(page.locator('#copy-status')).to_have_text('LaTeX copied.')
                assert 'Example Labs' in page.evaluate('navigator.clipboard.readText()')
                with page.expect_download() as tex:
                    page.get_by_role('link',name='Download LaTeX').click()
                assert Path(tex.value.path()).read_bytes()==(output/'main.tex').read_bytes()
                picture('new-latex.png')
                page.get_by_role('tab',name='LaTeX Code').press('ArrowRight')
                expect(page.get_by_role('tab',name='ATS Analysis')).to_have_attribute('aria-selected','true')
                for label in ['Role keyword alignment','Section structure','Evidence completeness',
                              'Matched keywords','Missing keywords','Improvement suggestions']:
                    expect(page.locator('#ats-result')).to_contain_text(label)
                expect(page.locator('#ats-result')).to_contain_text('C++')
                picture('new-ats.png')
                page.get_by_role('button',name='Generate another').click()
                expect(page.get_by_role('heading',name='Choose a target role')).to_be_visible()
                page.get_by_label('Find a resume').fill('nothing')
                expect(page.get_by_text('No matching resumes.')).to_be_visible()
                page.get_by_label('Find a resume').fill('Robin')
                page.get_by_role('button',name='View resume').click()
                expect(page.get_by_role('heading',name='Resume results')).to_be_visible()
                after={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in output.iterdir()}
                assert before==after

                mobile=browser.new_page(viewport={'width':390,'height':844})
                mobile.goto(url)
                expect(mobile.get_by_role('heading',name='Create a resume')).to_be_visible()
                assert mobile.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if screenshots:
                    mobile.screenshot(path=screenshots/'new-mobile.png',full_page=True)
                page.get_by_role('link',name='Operations ↗').click()
                expect(page.get_by_role('heading',name='Resume operations')).to_be_visible()
                expect(page.get_by_label('Candidate file')).to_be_visible()
                assert not errors, errors
                browser.close()
                print('Builder browser acceptance passed: wizard, keywords, history, three views, clipboard, actual TeX/PDF, downloads, mobile, and operations.')
        finally:
            server.shutdown()
            server.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--screenshots',type=Path)
    args=parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True,exist_ok=True)
    check(args.screenshots)
