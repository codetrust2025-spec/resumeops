"""Full-screen dashboard: both intake modes at desktop resolutions without page scrolling, a usable single
column on phones, mode switching that keeps entered data, and the history panel's search, filters,
pagination, actions, regeneration and progress.

The page and its scripts are the real dashboard served by the real handler; only the history list is a
fictional fixture (request interception), so every layout case has the same rows. End-to-end flows
(builder, real downloads, session renewal) are covered by dashboard/check_automation.py."""
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
sys.path.insert(0, str(ROOT / 'tests'))
from test_extraction_recovery import browser_or_skip  # noqa: E402

ROLES = ['React.js Developer', 'Angular Developer', 'Java Backend Developer', 'Python Backend Developer', 'Node.js Developer']
CATEGORIES = [('Job role relevance', 15), ('Supported keyword coverage', 20), ('Skills alignment', 15),
              ('Professional experience', 15), ('Project relevance', 10), ('Resume structure', 10),
              ('Content quality', 10), ('Evidence completeness', 5)]


def fake_jobs(count=15):
    """Fictional history: one waiting for input, one running, one stopped, the rest complete."""
    now = time.time()
    jobs = []
    for i in range(count):
        job = {'id': f'{i:02d}' + 'f' * 62, 'candidate': f'Candidate {i:02d} Example', 'role': ROLES[i % len(ROLES)],
               'created': now - i * 3600, 'updated': now - i * 3600, 'notice': [], 'attempts': 0, 'next_retry': 0,
               'error': None, 'regenerable': False, 'status': 'complete', 'stage': 'Verified resume ready', 'percent': 100}
        if i == 0:
            job.update(status='needs_input', stage='Confirming essential career facts', percent=30,
                       error='A client-ready resume needs real work and technology evidence.',
                       questions=[{'id': 'experience.0.bullets', 'field': 'bullets', 'label': 'Actual work at Example Works',
                                   'explanation': 'In your own simple words, what did you do?', 'value': ''}], proposals=[])
        elif i == 1:
            job.update(status='processing', stage='Compiling LaTeX with pdfLaTeX', percent=72)
        elif i == 2:
            job.update(status='blocked', stage='Needs operator attention', percent=0,
                       error='The resume did not pass the final fact check, so nothing was published.')
        else:
            job.update(revision=f'candidate-{i:02d}/abc{i:02d}', overleaf=f'https://www.overleaf.com/project/abc{i:02d}',
                       regenerable=True,
                       ats={'ats_readiness': 60 + i, 'categories': [{'label': n, 'points': m - 1, 'max': m} for n, m in CATEGORIES],
                            'recommendations': ['Add a project that uses the role\'s core tools.'],
                            'role_relevant_skills': ['TypeScript'], 'suggested_skills': ['GraphQL']})
        jobs.append(job)
    return jobs


class DashboardLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from resume_bot.pipeline import CODE
        from resume_bot.storage import write_json
        from resume_dashboard import Workspace, handler
        cls.temp = tempfile.TemporaryDirectory()
        workspace = Workspace(Path(cls.temp.name), code_root=ROOT)
        write_json(workspace.inbox / '.resume-bot-health.json', {'last_scan': time.time(), 'code': CODE})
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(workspace, '', 't'))
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'
        cls.server.RequestHandlerClass = handler(workspace, cls.origin, 't', automation_first=True)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    def setUp(self):
        self.playwright, self.browser = browser_or_skip(self)
        self.addCleanup(self.playwright.stop)
        self.addCleanup(self.browser.close)
        self.posts = []
        self.delete_failures = []

    def open(self, jobs=None, **context):
        page = self.browser.new_page(**context)
        jobs = jobs if jobs is not None else fake_jobs()

        def answer(route):
            self.posts.append(json.loads(route.request.post_data))
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'id': 'a' * 64, 'status': 'queued'}))
        page.route('**/api/automation/answer', answer)

        def history(route):
            if route.request.method == 'GET':
                route.fulfill(status=200, content_type='application/json', body=json.dumps({'jobs': jobs}))
            else:
                route.continue_()

        def regenerate(route):
            self.posts.append(json.loads(route.request.post_data))
            route.fulfill(status=202, content_type='application/json', body=json.dumps({'id': 'r' * 64}))

        def delete(route):
            # Behaves like the server: deletes what it was asked to, keeps a resume being prepared.
            body = json.loads(route.request.post_data)
            self.posts.append(body)
            if self.delete_failures:
                error = self.delete_failures.pop(0)
                return route.fulfill(status=503, content_type='application/json', body=json.dumps({'error': error}))
            chosen = [j for j in jobs if body.get('all') or j['id'] == body.get('id')]
            kept = [{'id': j['id'], 'reason': 'in_progress'} for j in chosen if j['status'] == 'processing']
            removed = [j for j in chosen if j['status'] != 'processing']
            for job in removed:
                jobs.remove(job)
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'deleted': len(removed), 'kept': kept}))
        page.route('**/api/automation', history)
        page.route('**/api/automation/regenerate', regenerate)
        page.route('**/api/automation/delete', delete)
        page.goto(self.origin)
        page.locator('#auto-job-list > li').first.wait_for()
        page.locator('#auto-role-options option').first.wait_for(state='attached')
        return page

    def measure(self, page):
        return page.evaluate('''() => {
            const de = document.documentElement, box = s => document.querySelector(s).getBoundingClientRect();
            return {pageY: de.scrollHeight - innerHeight, pageX: de.scrollWidth - innerWidth, width: innerWidth,
                    dash: box('.auto-dash').width, create: box('.auto-create').width, history: box('#auto-jobs').width,
                    createTop: box('.auto-create').top, createBottom: box('.auto-create').bottom, historyTop: box('#auto-jobs').top,
                    body: parseFloat(getComputedStyle(document.body).fontSize),
                    label: parseFloat(getComputedStyle(document.querySelector('#ai-form label')).fontSize),
                    submit: box('#auto-submit').height};
        }''')

    def test_desktop_uses_the_full_screen_without_page_scrolling_in_both_modes(self):
        for width, height in ((1366, 768), (1440, 900), (1920, 1080)):
            page = self.open(viewport={'width': width, 'height': height})
            for mode in ('AI-first: name and phone', 'Upload candidate documents'):
                with self.subTest(resolution=f'{width}x{height}', mode=mode):
                    page.get_by_label(mode).check()
                    m = self.measure(page)
                    self.assertLessEqual(m['pageY'], 0, 'no page-level vertical scrolling')
                    self.assertLessEqual(m['pageX'], 0, 'no horizontal scrolling')
                    self.assertGreaterEqual(m['dash'], 0.95 * m['width'], 'the dashboard spans the screen')
                    self.assertAlmostEqual(m['create'] / (m['create'] + m['history']), 0.4, delta=0.03)
                    self.assertGreaterEqual(m['body'], 14, 'readable text, no scaling tricks')
                    self.assertGreaterEqual(m['label'], 13)
                    self.assertGreaterEqual(m['submit'], 40)
                    self.assertEqual(page.locator('#auto-job-list > li').count(), 6, 'one bounded page of history')
            with self.subTest(resolution=f'{width}x{height}', state='expanded details'):
                page.get_by_label('AI-first: name and phone').check()
                page.get_by_text('Optional details and profiles', exact=True).click()
                self.assertLessEqual(self.measure(page)['pageY'], 0, 'expanded details scroll inside the form panel')
            page.close()

    def test_phone_layout_is_one_column_without_sideways_scrolling(self):
        page = self.open(**self.playwright.devices['iPhone 13'])
        m = self.measure(page)
        self.assertLessEqual(m['pageX'], 0)
        self.assertGreaterEqual(m['historyTop'], m['createBottom'] - 1, 'My resumes follows the form')
        self.assertGreaterEqual(m['submit'], 44, 'touch-friendly')
        page.get_by_label('Upload candidate documents').check()
        self.assertLessEqual(self.measure(page)['pageX'], 0)
        page.close()

    def test_switching_modes_keeps_what_was_entered(self):
        page = self.open(viewport={'width': 1366, 'height': 768})
        page.get_by_label('Target role').fill('React.js Developer')
        page.get_by_label('Full name').fill('Kiran Example (fictional)')
        page.get_by_label('Phone', exact=True).fill('+91 90000 00000')
        page.locator('#auto-background').fill('Worked at Example Works as Frontend Developer from 2023.')
        page.get_by_label('Upload candidate documents').check()
        with tempfile.TemporaryDirectory() as folder:
            resume = Path(folder) / 'resume.txt'
            resume.write_text('Kiran Example (fictional)\n+91 90000 00000\n', encoding='utf-8')
            page.locator('#auto-file').set_input_files(str(resume))
        page.get_by_label('AI-first: name and phone').check()
        self.assertEqual(page.get_by_label('Full name').input_value(), 'Kiran Example (fictional)')
        self.assertEqual(page.get_by_label('Phone', exact=True).input_value(), '+91 90000 00000')
        self.assertIn('Example Works', page.locator('#auto-background').input_value())
        page.get_by_label('Upload candidate documents').check()
        self.assertEqual(page.evaluate("document.querySelector('#auto-file').files.length"), 1, 'the chosen file stays')
        self.assertEqual(page.get_by_label('Target role').input_value(), 'React.js Developer')
        page.close()

    def test_multiple_evidence_documents_are_sent_in_one_submission(self):
        page = self.open(viewport={'width': 1366, 'height': 768})

        def submit(route):
            if route.request.method == 'POST':
                route.fulfill(status=202, content_type='application/json',
                              body=json.dumps({'id': 'd' * 64, 'status': 'queued'}))
            else:
                route.fulfill(status=200, content_type='application/json', body=json.dumps({'jobs': []}))

        page.route('**/api/automation', submit)
        page.get_by_label('Target role').fill('Python Backend Developer')
        page.get_by_label('Upload candidate documents').check()
        page.locator('#auto-file').set_input_files([
            {'name': 'education.txt', 'mimeType': 'text/plain', 'buffer': b'Example College diploma'},
            {'name': 'certificate.png', 'mimeType': 'image/png', 'buffer': b'fixture image bytes'}])
        page.locator('#auto-consent').check()
        with page.expect_request(lambda request: request.method == 'POST' and request.url.endswith('/api/automation')) as sent:
            page.get_by_role('button', name='Create verified resume', exact=True).click()
        payload = sent.value.post_data_json
        self.assertEqual([item['extension'] for item in payload['documents']], ['txt', 'png'])
        self.assertNotIn('candidate', payload)
        page.close()

    def test_search_filters_and_pagination(self):
        page = self.open(viewport={'width': 1440, 'height': 900})
        rows = page.locator('#auto-job-list > li')
        self.assertEqual(page.locator('#auto-page').inner_text(), 'Page 1 of 3 · 1–6 of 15')
        self.assertEqual(rows.first.get_attribute('data-status'), 'needs_input', 'rows needing input come first')
        first_page = rows.evaluate_all('items => items.map(item => item.dataset.id)')
        page.get_by_role('button', name='Next page').click()
        self.assertEqual(page.locator('#auto-page').inner_text(), 'Page 2 of 3 · 7–12 of 15')
        self.assertFalse(set(first_page) & set(rows.evaluate_all('items => items.map(item => item.dataset.id)')))
        page.get_by_role('button', name='Next page').click()
        self.assertEqual(rows.count(), 3)
        self.assertTrue(page.get_by_role('button', name='Next page').is_disabled())
        page.get_by_role('button', name='Previous page').click()
        self.assertEqual(page.locator('#auto-page').inner_text(), 'Page 2 of 3 · 7–12 of 15')
        page.get_by_label('Search resumes').fill('Candidate 07')
        self.assertEqual(rows.count(), 1)
        self.assertIn('Candidate 07 Example · Java Backend Developer', rows.first.inner_text())
        page.get_by_label('Search resumes').fill('')
        page.get_by_label('Filter by job role').select_option('Angular Developer')
        titles = rows.evaluate_all('items => items.map(item => item.querySelector(".auto-meta").textContent)')
        self.assertTrue(titles and all('Angular Developer' in title for title in titles), titles)
        page.get_by_label('Filter by job role').select_option('')
        page.get_by_label('Filter by status').select_option('needs_input')
        self.assertEqual(rows.count(), 1)
        page.get_by_label('Filter by status').select_option('complete')
        self.assertEqual(page.locator('#auto-page').inner_text(), 'Page 1 of 2 · 1–6 of 12')
        page.close()

    def test_every_action_and_regeneration(self):
        page = self.open(viewport={'width': 1366, 'height': 768})
        row = page.locator('#auto-job-list > li[data-status="complete"]').first
        for name, fragment in (('Download PDF', 'name=resume.pdf&download=1'), ('Download LaTeX', 'name=main.tex&download=1'),
                               ('Download Overleaf PDF', 'name=overleaf.pdf&download=1'), ('Preview PDF', 'name=resume.pdf'),
                               ('Open Overleaf project', 'https://www.overleaf.com/project/abc03')):
            with self.subTest(action=name):
                link = row.get_by_role('link', name=name, exact=True)
                self.assertTrue(link.is_visible())
                self.assertIn(fragment, link.get_attribute('href'))
        self.assertIn('ATS 63', row.locator('.auto-score').inner_text())
        details = row.get_by_role('button', name='Details')
        details.click()
        self.assertTrue(row.locator('.auto-ats').is_visible())
        row.locator('.auto-ats summary').click()
        self.assertEqual(row.locator('.auto-ats tr').count(), 8, 'the explainable ATS breakdown')
        row.get_by_role('button', name='Hide details').click()
        self.assertTrue(row.locator('.auto-ats').is_hidden())
        row.get_by_role('button', name='Regenerate', exact=True).click()
        field = row.get_by_label('Regenerate for another role')
        self.assertTrue(field.evaluate('el => el === document.activeElement'), 'Regenerate opens and focuses the role')
        field.fill('Node.js Developer')
        with page.expect_request('**/api/automation/regenerate'):
            row.locator('.auto-regenerate').get_by_role('button', name='Regenerate').click()
        self.assertEqual(self.posts[-1], {'id': '03' + 'f' * 62, 'role': 'Node.js Developer'})
        page.close()

    def test_ai_drafts_and_projects_are_reviewed_in_one_card(self):
        from playwright.sync_api import expect
        jobs = fake_jobs(4)
        jobs[0].update(stage='Review AI drafts for GCP Cloud Engineer', role='GCP Cloud Engineer',
                       error='AI drafted GCP Cloud Engineer content from your job titles and dates. Keep what you '
                             'actually did, edit anything, then confirm once.',
                       questions=[{'id': 'experience.0.bullets', 'field': 'bullets', 'drafted': True,
                                   'label': 'Work as Cloud Engineer at Example Health (AI draft)', 'explanation': 'Keep only what you did.',
                                   'value': 'Deployed services to Cloud Run with Terraform.\nProvisioned Compute Engine instances.'},
                                  {'id': 'experience.1.bullets', 'field': 'bullets', 'drafted': True,
                                   'label': 'Work as Associate Engineer at Sample Systems (AI draft)', 'explanation': 'Keep only what you did.',
                                   'value': 'Maintained Docker images on Google Cloud.\nWrote Terraform modules.'},
                                  {'id': 'skills', 'field': 'skills', 'drafted': True, 'label': 'Skills for GCP Cloud Engineer (AI draft)',
                                   'explanation': 'Keep only tools you have used.', 'value': 'Google Cloud, Terraform, Cloud Run'}],
                       proposals=[{'name': 'Practice API on Cloud Run', 'problem': 'Fictional.', 'architecture': 'Cloud Run.',
                                   'features': [], 'responsibilities': 'Build it.', 'stack': ['Cloud Run', 'Terraform'],
                                   'bullets': ['Deployed a practice API to Cloud Run with Terraform.']}])
        page = self.open(jobs=jobs, viewport={'width': 1366, 'height': 768})
        row = page.locator('#auto-job-list > li[data-status="needs_input"]')
        work = row.get_by_label('Work as Cloud Engineer at Example Health (AI draft)')
        expect(work).to_have_value('Deployed services to Cloud Run with Terraform.\nProvisioned Compute Engine instances.')
        self.assertEqual(work.evaluate('el => el.tagName'), 'TEXTAREA')
        self.assertEqual(row.locator('form').count(), 1, 'drafts and projects share one card')
        self.assertEqual(row.get_by_role('button', name='Confirm and continue').count(), 1)
        self.assertTrue(row.locator('label.auto-drafted').first.is_visible(), 'drafts are marked as AI drafts')
        work.fill('Deployed services to Cloud Run with Terraform.')
        row.get_by_label('Work as Associate Engineer at Sample Systems (AI draft)').fill('')  # leave this job without lines
        # A suggested project starts included as an AI draft (one confirmation); the candidate skips this one.
        expect(row.get_by_label('I built this: include it')).to_be_checked()
        row.get_by_label('Skip').check()
        with page.expect_request('**/api/automation/answer'):
            row.get_by_role('button', name='Confirm and continue').click()
        self.assertEqual(self.posts[-1], {'id': jobs[0]['id'], 'answers': {
            'experience.0.bullets': 'Deployed services to Cloud Run with Terraform.', 'experience.1.bullets': '',
            'skills': 'Google Cloud, Terraform, Cloud Run'}, 'decisions': [{'include': False}]})
        page.close()

    def test_delete_one_and_delete_all_need_explicit_confirmation(self):
        from playwright.sync_api import expect
        page = self.open(viewport={'width': 1366, 'height': 768})
        dialogs, answers = [], iter([False, True])
        page.on('dialog', lambda d: (dialogs.append(d.message), d.accept() if next(answers) else d.dismiss()))
        rows = page.locator('#auto-job-list > li')
        for status, has_delete in (('needs_input', True), ('processing', False), ('blocked', True), ('complete', True)):
            with self.subTest(status=status):
                row = page.locator(f'#auto-job-list > li[data-status="{status}"]').first
                self.assertEqual(row.get_by_role('button', name='Delete', exact=False).count() > 0, has_delete)
        delete_03 = page.locator('#auto-job-list > li[data-id^="03"]').get_by_role('button', name='Delete Candidate 03 Example')
        delete_03.click()  # cancelled in the browser's confirmation: no request at all
        page.wait_for_timeout(300)
        self.assertEqual(self.posts, [])
        self.assertIn('Permanently delete Candidate 03 Example', dialogs[0])
        self.assertIn('cannot be undone', dialogs[0])
        with page.expect_request('**/api/automation/delete'):
            delete_03.click()  # confirmed
        expect(page.locator('#auto-delete-status')).to_have_text('Permanently deleted 1 resume.')
        self.assertEqual(self.posts, [{'id': '03' + 'f' * 62, 'confirm': 'DELETE'}])
        expect(page.locator('#auto-job-list > li[data-id^="03"]')).to_have_count(0)
        expect(page.locator('#auto-count')).to_have_text('14 resumes')

        dialog = page.locator('#auto-delete-dialog')
        confirm = dialog.get_by_role('button', name='Permanently delete all')
        phrase = dialog.get_by_label('Type DELETE ALL to confirm')
        page.get_by_role('button', name='Delete all', exact=True).click()
        expect(dialog).to_be_visible()
        for typed in ('', 'delete all', 'DELETE AL', 'DELETE ALL '):
            phrase.fill(typed)
            expect(confirm).to_be_disabled()
        phrase.press('Enter')  # submitting without the exact phrase does nothing
        dialog.get_by_role('button', name='Cancel').click()
        expect(dialog).to_be_hidden()
        self.assertEqual(len(self.posts), 1, 'cancel sends nothing')
        page.get_by_role('button', name='Delete all', exact=True).click()
        expect(phrase).to_have_value('')
        phrase.fill('DELETE ALL')
        expect(confirm).to_be_enabled()
        self.delete_failures.append('ResumeOps is building a resume right now. Nothing was deleted; try again in a minute.')
        confirm.click()
        expect(dialog.locator('#auto-delete-error')).to_have_text(
            'ResumeOps is building a resume right now. Nothing was deleted; try again in a minute.')
        expect(dialog).to_be_visible()
        expect(page.locator('#auto-count')).to_have_text('14 resumes')
        with page.expect_request('**/api/automation/delete'):
            confirm.click()
        expect(dialog).to_be_hidden()
        self.assertEqual(self.posts[-1], {'all': True, 'confirm': 'DELETE ALL'})
        expect(page.locator('#auto-delete-status')).to_have_text(
            'Permanently deleted 13 resumes. 1 resume being prepared was kept; delete it when finished.')
        expect(rows).to_have_count(1)
        expect(rows.first).to_have_attribute('data-status', 'processing')
        page.close()

    def test_progress_shows_the_real_stage_and_percent(self):
        page = self.open(viewport={'width': 1920, 'height': 1080})
        status = page.locator('#auto-status')
        status.wait_for()
        self.assertEqual(page.locator('#auto-stage').inner_text(), 'Compiling LaTeX with pdfLaTeX')
        self.assertEqual(page.locator('#auto-percent').inner_text(), '72%')
        running = page.locator('#auto-job-list > li[data-status="processing"]')
        self.assertIn('Compiling LaTeX with pdfLaTeX · 72%', running.inner_text())
        self.assertEqual(running.locator('progress').evaluate('el => el.value'), 72)
        page.close()


if __name__ == '__main__':
    unittest.main()
