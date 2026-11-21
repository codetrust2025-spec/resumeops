"""Browser acceptance for the public multi-candidate intake (fictional candidates; no model runs).

On an iPhone 13 screen: the public form fills itself from a Word resume, takes the rest, and returns a private link;
the candidate's status page (the dashboard's own list and review cards, signed in by the private key) shows the
queue, takes the answers to AI drafts, and offers corrections; the operator's Candidates page lists the candidate.
"""
import io
import json
import re
import sys
import tempfile
import threading
import zipfile
from contextlib import closing
from unittest.mock import patch
from pathlib import Path
from xml.sax.saxutils import escape

from playwright.sync_api import expect, sync_playwright

from resume_dashboard import DashboardServer, Workspace, handler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEXT = """Kiran Example
kiran.example@example.com | +91 90000 77777
Skills: Java, Spring Boot, MySQL, Docker
EMPLOYMENT HISTORY
Company / employer
Employment dates
Example Health Services Pvt Ltd
11 Jul 2022 – 08 Apr 2025
Demo Software Pvt Ltd
23 Aug 2021 – 22 Apr 2022
EDUCATION
Highest Qualification
B.Tech
College / University
Example College, Bengaluru
Education Passing Year
2015
"""


def docx(text):
    body = ''.join(f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>' for line in text.splitlines())
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
                         f'2006/main"><w:body>{body}</w:body></w:document>')
    return data.getvalue()


def no_sideways_scroll(page):
    width = page.evaluate('document.documentElement.scrollWidth')
    assert width <= page.viewport_size['width'], (width, page.viewport_size)


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        workspace = Workspace(Path(temp), code_root=ROOT)
        server = DashboardServer(('127.0.0.1', 0), handler(workspace, '', 'token'))
        origin = f'http://127.0.0.1:{server.server_port}'
        server.RequestHandlerClass = handler(workspace, origin, 'token')
        (workspace.root / '.resumeops-queue-paused').write_text('{}', encoding='utf-8')  # saved, not prepared
        threading.Thread(target=server.serve_forever, daemon=True).start()
        database = workspace.root / '.resumeops-automation.sqlite3'
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                phone = browser.new_context(**playwright.devices['iPhone 13'])
                page = phone.new_page()
                page.goto(origin + '/apply')
                no_sideways_scroll(page)
                page.set_input_files('#ap-file', files=[{
                    'name': 'kiran.docx', 'buffer': docx(TEXT),
                    'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}])
                field = lambda path: page.locator(f'[data-field="{path}"]')  # noqa: E731
                expect(field('full_name')).to_have_value('Kiran Example', timeout=20000)
                expect(field('email')).to_have_value('kiran.example@example.com')
                expect(field('phone')).to_have_value('+91 90000 77777')
                expect(field('work_experience.0.company')).to_have_value('Example Health Services Pvt Ltd')
                expect(field('work_experience.1.start')).to_have_value('23 Aug 2021')
                expect(field('education.0.graduation_year')).to_have_value('2015')
                assert 'Spring Boot' in field('skills').input_value()
                # A missing mandatory fact is shown at its field, not after a round trip.
                page.locator('#ap-consent').check()
                page.locator('#ap-submit').click()
                expect(page.locator('#ap-error')).to_contain_text('city')
                field('location').fill('Pune')
                field('role').fill('Java Backend Developer')
                for index, title in ((0, 'Software Engineer'), (1, 'Associate Engineer')):
                    field(f'work_experience.{index}.position').fill(title)
                    field(f'work_experience.{index}.responsibilities').fill(
                        'Built Spring Boot REST APIs for appointment booking with Java and MySQL.')
                field('education.0.degree').fill('B.Tech')
                field('projects.0.name').fill('Clinic Booking API')
                field('projects.0.stack').fill('Java, Spring Boot, MySQL')
                field('projects.0.description').fill('Built booking endpoints with Spring Boot and MySQL.')
                page.reload()  # the draft survives a reload
                expect(field('projects.0.name')).to_have_value('Clinic Booking API')
                page.locator('#ap-consent').check()
                page.locator('#ap-submit').click()
                expect(page.locator('#ap-done')).to_be_visible(timeout=20000)
                expect(page.locator('#ap-form')).to_be_hidden()  # seen live: the form stayed under the receipt
                link = page.locator('#ap-done-link').input_value()
                assert '/apply/status#c=RO-' in link and '&k=' in link, link
                candidate = link.split('#c=')[1].split('&')[0]
                no_sideways_scroll(page)

                # The candidate's own page: the application waits for the operator.
                page.goto(link)
                expect(page.locator('#st-who')).to_contain_text(candidate)
                expect(page.locator('#st-who')).to_contain_text('Kiran Example')
                expect(page.locator('#st-state')).to_contain_text('Received', timeout=15000)
                assert page.locator('#st-files').is_hidden()
                no_sideways_scroll(page)
                with closing(__import__('sqlite3').connect(database)) as db:
                    assert db.execute('SELECT count(*) FROM automation').fetchone()[0] == 0, 'nothing runs before Start'

                # The operator's page: every detail the candidate entered, and Start processing (the local origin
                # is the operator).
                operator = browser.new_page(viewport={'width': 1366, 'height': 900})
                operator.on('dialog', lambda dialog: dialog.accept())
                operator.goto(origin + '/candidates')
                expect(operator.locator('#cd-rows')).to_contain_text(candidate, timeout=15000)
                expect(operator.locator('#cd-rows')).to_contain_text('Kiran Example')
                expect(operator.locator('#cd-rows')).to_contain_text('Not started')
                assert operator.locator('#cd-form-url').input_value() == origin + '/apply'
                operator.get_by_role('button', name='Details').click()
                details = operator.locator('.cd-details')
                expect(details).to_contain_text('Associate Engineer at Demo Software Pvt Ltd')
                expect(details).to_contain_text('Built Spring Boot REST APIs for appointment booking with Java and MySQL.')
                expect(details).to_contain_text('Clinic Booking API')
                expect(details).to_contain_text('Year of completion: 2015')
                expect(details).to_contain_text('Pune')
                operator.get_by_role('button', name='Start processing').click()
                expect(operator.locator('#cd-message')).to_contain_text('Processing started', timeout=15000)
                operator.wait_for_timeout(6000)  # past a list refresh (every 5 s): seen in CI, it erased the message
                expect(operator.locator('#cd-message')).to_contain_text('Processing started')
                expect(operator.locator('#cd-rows')).to_contain_text('Waiting in queue', timeout=15000)
                with closing(__import__('sqlite3').connect(database)) as db:
                    owner, identifier, payload, label = db.execute(
                        'SELECT owner, id, payload, candidate FROM automation').fetchone()
                request = json.loads(payload)
                # The operator's own document-upload workflow: a Word resume of the form, as the operator's resume.
                assert (request['extension'], label) == ('docx', 'Kiran Example'), (request.get('extension'), label)
                assert owner != __import__('intake').candidate_owner(candidate), 'an operator resume, not the candidate'

                # The candidate follows it, and downloads the finished resume; nothing else.
                page.reload()
                expect(page.locator('#st-state')).to_contain_text('Queued for preparation', timeout=15000)
                folder = workspace.private_output / f'private-{owner}' / 'rev1'
                folder.mkdir(parents=True)
                (folder / 'resume.pdf').write_bytes(b'%PDF-1.4 fictional resume')
                revision = f'private-{owner}/rev1'
                with patch('intake.IntakeDesk.candidate_jobs', lambda desk, who: [
                        {'id': identifier, 'status': 'complete', 'percent': 100, 'stage': 'Verified resume ready',
                         'revision': revision, 'ats': 97}] if who == candidate else []):
                    page.reload()
                    expect(page.locator('#st-state')).to_contain_text('Your resume is ready', timeout=15000)
                    with page.expect_download() as download:
                        page.locator('#st-pdf').click()
                    assert download.value.suggested_filename == f'{candidate}-resume.pdf'
                    assert Path(download.value.path()).read_bytes() == b'%PDF-1.4 fictional resume'
                no_sideways_scroll(page)

                # Corrections: the form opens with the candidate's facts and saves a new version; the operator sees
                # that the details changed after processing started.
                page.get_by_role('link', name='Correct my details').click()
                expect(field('location')).to_have_value('Pune', timeout=15000)
                field('location').fill('Mumbai')
                page.locator('#ap-consent').check()
                page.locator('#ap-submit').click()
                expect(page.locator('#ap-done-id')).to_contain_text('corrected details saved', timeout=20000)
                operator.reload()
                expect(operator.get_by_role('button', name='Start again with new details')).to_be_visible(timeout=15000)
                browser.close()
        finally:
            server.shutdown()
            server.RequestHandlerClass.stop_imports()
            server.server_close()
    print('Intake browser acceptance passed: phone form, Word prefill, local draft, private link, nothing processed '
          'before Start, full details and Start processing for the operator (upload workflow as the operator), the '
          "candidate's progress and PDF download, corrections flagged for the operator.")


if __name__ == '__main__':
    main()
