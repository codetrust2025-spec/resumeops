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
from urllib.parse import unquote
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


def png(width=60, height=80):
    """A small solid-colour PNG: the fictional candidate's passport photo."""
    import struct
    import zlib
    rows = b''.join(b'\x00' + b'\x9c\xb4\xd8' * width for _ in range(height))
    chunk = lambda kind, data: (struct.pack('>I', len(data)) + kind + data  # noqa: E731
                                + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


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
        server.RequestHandlerClass = handler(workspace, origin, 'token', automation_first=True)
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
                # Details for recruiters (never on the resume): all mandatory; the photo is scaled in the browser.
                page.locator('#ap-consent').check()
                page.locator('#ap-submit').click()
                expect(page.locator('#ap-error')).to_contain_text('date of birth')
                field('dob').fill('1996-04-12')
                field('preferred_location').fill('Bengaluru, Pune')
                field('current_salary').fill('7.5 LPA')
                field('salary_type').select_option('Fixed + variable')
                page.locator('#ap-submit').click()
                expect(page.locator('#ap-error')).to_contain_text('passport size photo')
                page.set_input_files('#ap-photo', files=[{'name': 'photo.png', 'mimeType': 'image/png', 'buffer': png()}])
                expect(page.locator('#ap-photo-status')).to_contain_text('Photo added')
                # Specialization and CGPA / percentage are mandatory (marked *); skills and projects are optional.
                for path in ('education.0.specialization', 'education.0.cgpa'):
                    expect(page.locator(f'label:has([data-field="{path}"]) .ap-req')).to_have_count(1)
                expect(page.locator('#ap-skills-title .ap-req')).to_have_count(0)
                expect(page.locator('#ap-projects-title .ap-opt')).to_have_text('(optional)')
                expect(page.locator('label:has([data-field="work_experience.0.responsibilities"]) .ap-req')).to_have_count(0)
                field('education.0.specialization').fill('')
                page.locator('#ap-submit').click()
                expect(page.locator('#ap-error')).to_contain_text('specialization')
                field('education.0.specialization').fill('Mechanical Engineering')
                field('education.0.cgpa').fill('8.1')
                field('projects.0.name').fill('Clinic Booking API')
                field('projects.0.stack').fill('Java, Spring Boot, MySQL')
                field('projects.0.description').fill('Built booking endpoints with Spring Boot and MySQL.')
                page.reload()  # the draft survives a reload
                expect(field('projects.0.name')).to_have_value('Clinic Booking API')
                expect(page.locator('#ap-photo-status')).to_contain_text('Photo added')  # the photo is in the draft
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
                # Candidates sits beside My Resumes, with the number of applications not started yet, on a desktop
                # and on a phone (a full-width tab bar there).
                operator.goto(origin + '/')
                expect(operator.locator('#auto-candidates-link')).to_be_visible(timeout=15000)
                expect(operator.locator('#auto-candidates-badge')).to_have_text('1')
                handset = browser.new_context(**playwright.devices['iPhone 13']).new_page()
                handset.goto(origin + '/')
                expect(handset.locator('#auto-candidates-link')).to_be_visible(timeout=15000)
                expect(handset.locator('#auto-candidates-badge')).to_have_text('1')
                box = handset.locator('#auto-candidates-link').bounding_box()
                assert box and box['height'] >= 44 and box['x'] + box['width'] <= 390, box  # a whole, tappable tab
                no_sideways_scroll(handset)
                handset.goto(origin + '/candidates')
                expect(handset.locator('#cd-rows')).to_contain_text('Kiran Example', timeout=15000)
                start_box = handset.get_by_role('button', name='Start processing').bounding_box()
                assert start_box and start_box['height'] >= 44, start_box
                # Share via WhatsApp: visible on a phone, a whole-width touch target.
                share = handset.get_by_role('link', name='Share via WhatsApp')
                expect(share).to_be_visible()
                share_box = share.bounding_box()
                assert share_box and share_box['height'] >= 44 and share_box['width'] >= 300, share_box
                no_sideways_scroll(handset)
                operator.goto(origin + '/candidates')
                expect(operator.locator('#cd-rows')).to_contain_text(candidate, timeout=15000)
                expect(operator.locator('#cd-rows')).to_contain_text('Kiran Example')
                expect(operator.locator('#cd-rows')).to_contain_text('Not started')
                assert operator.locator('#cd-form-url').input_value() == origin + '/apply'
                # ...and on a computer: one tap opens WhatsApp with a prefilled invitation holding the public form link.
                share = operator.get_by_role('link', name='Share via WhatsApp')
                expect(share).to_be_visible()
                href = share.get_attribute('href')
                assert href.startswith('https://wa.me/?text='), href
                invitation = unquote(href.split('?text=', 1)[1])
                assert origin + '/apply' in invitation and 'no login' in invitation, invitation
                assert share.get_attribute('target') == '_blank', 'opens WhatsApp beside the dashboard'
                operator.get_by_role('button', name='Details').click()
                details = operator.locator('.cd-details')
                expect(details).to_contain_text('Associate Engineer at Demo Software Pvt Ltd')
                expect(details).to_contain_text('Built Spring Boot REST APIs for appointment booking with Java and MySQL.')
                expect(details).to_contain_text('Clinic Booking API')
                expect(details).to_contain_text('Year of completion: 2015')
                expect(details).to_contain_text('Pune')
                recruiter = details.locator('section', has=operator.get_by_role('heading', name='Recruiter details'))
                for term, value in (('Date of birth', '1996-04-12'), ('Preferred location', 'Bengaluru, Pune'),
                                    ('Current salary', '7.5 LPA'), ('Salary type', 'Fixed + variable')):
                    expect(recruiter).to_contain_text(term + value)
                photo = details.locator('.cand-photo img')
                expect(photo).to_be_visible()
                operator.wait_for_function('img => img.complete && img.naturalWidth > 0', arg=photo.element_handle())
                operator.get_by_role('button', name='Start processing').click()
                expect(operator.locator('#cd-message')).to_contain_text('Processing started', timeout=15000)
                operator.wait_for_timeout(6000)  # past a list refresh (every 5 s): seen in CI, it erased the message
                expect(operator.locator('#cd-message')).to_contain_text('Processing started')
                expect(operator.locator('#cd-rows')).to_contain_text('Waiting in queue', timeout=15000)
                with closing(__import__('sqlite3').connect(database)) as db:
                    owner, identifier, payload, label = db.execute(
                        'SELECT owner, id, payload, candidate FROM automation').fetchone()
                request = json.loads(payload)
                # The operator's own resume workflow on the details as typed: the project keeps its name, technologies
                # and description, and the education its year (as the operator's resume, labelled with the name).
                from resume_bot.generate import split_project
                profile = request['profile']
                assert label == 'Kiran Example', label
                assert [split_project(p)[0] for p in profile['projects']] == ['Clinic Booking API'], profile['projects']
                assert split_project(profile['projects'][0])[1] == ['Java', 'Spring Boot', 'MySQL'], profile['projects']
                assert [e['end'] for e in profile['education']] == ['2015'], profile['education']
                assert owner != __import__('intake').candidate_owner(candidate), 'an operator resume, not the candidate'

                # Records link both ways. My Resumes names the application; its link opens that candidate's details.
                operator.goto(origin + '/')
                expect(operator.locator('#auto-candidates-badge')).to_be_hidden(timeout=15000)  # none waiting now
                chip = operator.locator(f'li[data-id="{identifier}"] .auto-application')
                expect(chip).to_have_text(f'Application {candidate}', timeout=15000)
                chip.click()
                operator.wait_for_url(f'**/candidates#{candidate}')
                expect(operator.locator(f'#cand-{candidate}')).to_be_visible(timeout=15000)
                expect(operator.locator('.cd-details')).to_contain_text('Associate Engineer at Demo Software Pvt Ltd')
                # Waiting for review: "Review in My Resumes" opens exactly that resume.
                with closing(__import__('sqlite3').connect(database)) as db, db:
                    db.execute("UPDATE automation SET status='needs_input', stage='Review AI drafts' WHERE id=?",
                               (identifier,))
                operator.reload()
                review = operator.get_by_role('link', name='Review in My Resumes')
                expect(review).to_have_attribute('href', f'/#resume-{identifier}', timeout=15000)
                review.click()
                expect(operator.locator(f'li[data-id="{identifier}"].auto-focus')).to_be_visible(timeout=15000)
                with closing(__import__('sqlite3').connect(database)) as db, db:
                    db.execute("UPDATE automation SET status='complete', stage='Verified resume ready' WHERE id=?",
                               (identifier,))
                operator.goto(origin + '/candidates')
                # A finished resume can be prepared again from the same details (the operator's choice): a new version,
                # its own resume linked to the application; the first one stays.
                operator.get_by_role('button', name='Process again').click(timeout=15000)
                expect(operator.locator('#cd-message')).to_contain_text('Processing started', timeout=15000)
                with closing(__import__('sqlite3').connect(database)) as db:
                    runs = db.execute('SELECT job_id FROM intake_runs WHERE candidate=? ORDER BY started',
                                      (candidate,)).fetchall()
                    statuses = dict(db.execute('SELECT id, status FROM automation').fetchall())
                assert len(runs) == 2 and runs[0][0] == identifier and runs[1][0] != identifier, runs
                assert statuses[identifier] == 'complete' and statuses[runs[1][0]] == 'queued', statuses
                operator.reload()

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
          'before Start, Candidates beside My Resumes with its badge (desktop and phone), Share via WhatsApp with the form link, full details, Start processing '
          'and Process again (a new linked version of the typed details), resume and application linked both ways, the candidate '
          'progress and PDF download, corrections flagged for the operator.')


if __name__ == '__main__':
    main()
