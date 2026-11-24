"""Full-screen dashboard: both intake modes at desktop resolutions without page scrolling, a usable single
column on phones, mode switching that keeps entered data, and the history panel's search, filters,
pagination, actions, regeneration and progress.

The page and its scripts are the real dashboard served by the real handler; only the history list is a
fictional fixture (request interception), so every layout case has the same rows. End-to-end flows
(builder, real downloads, session renewal) are covered by dashboard/check_automation.py."""
import re
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
    def test_all_pages_fit_phone_and_desktop_widths(self):
        for width in (390, 1440):
            page = self.browser.new_page(viewport={'width': width, 'height': 900})
            try:
                for route in ('/', '/ai-builder', '/workflow', '/operations', '/candidates',
                              '/roles', '/apply', '/apply/status'):
                    with self.subTest(width=width, route=route):
                        response = page.goto(self.origin + route)
                        self.assertEqual(response.status, 200)
                        if route == '/roles':
                            page.locator('#roles-list li').first.wait_for()
                        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth - innerWidth'), 0)
            finally:
                page.close()

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
                    createScroll: document.querySelector('.auto-create').scrollHeight - document.querySelector('.auto-create').clientHeight,
                    body: parseFloat(getComputedStyle(document.body).fontSize),
                    label: parseFloat(getComputedStyle(document.querySelector('#ai-form label')).fontSize),
                    submit: box('#auto-submit').height};
        }''')

    def test_desktop_uses_the_full_screen_without_page_scrolling_in_both_modes(self):
        for width, height in ((1366, 768), (1440, 900), (1920, 1080)):
            page = self.open(viewport={'width': width, 'height': height})
            for mode in ('Enter Manually', 'Upload Files'):
                with self.subTest(resolution=f'{width}x{height}', mode=mode):
                    page.get_by_label(mode).check()
                    m = self.measure(page)
                    self.assertLessEqual(m['pageY'], 0, 'no page-level vertical scrolling')
                    self.assertLessEqual(m['pageX'], 0, 'no horizontal scrolling')
                    self.assertGreaterEqual(m['dash'], 0.95 * m['width'], 'the dashboard spans the screen')
                    self.assertAlmostEqual(m['create'] / (m['create'] + m['history']), 0.35, delta=0.02)
                    self.assertGreaterEqual(m['body'], 14, 'readable text, no scaling tricks')
                    self.assertGreaterEqual(m['label'], 13)
                    self.assertGreaterEqual(m['submit'], 40)
                    if mode.startswith('Enter Manually'):
                        self.assertLessEqual(m['createScroll'], 0, 'the whole form fits, Create verified resume included')
                    rows = page.locator('#auto-job-list > li').count()
                    self.assertTrue(3 <= rows <= 12, 'one bounded page of history, sized to the panel')
                    self.assertEqual(page.locator('#auto-page').inner_text(), f'Showing 1 – {rows} of 15 resumes')
                    self.assertTrue(page.locator('#auto-pager').is_visible(), 'page numbers when there is more than one page')
            with self.subTest(resolution=f'{width}x{height}', state='expanded details'):
                page.get_by_label('Enter Manually').check()
                page.get_by_text('More details', exact=True).click()
                self.assertLessEqual(self.measure(page)['pageY'], 0, 'expanded details scroll inside the form panel')
            page.close()

    def test_phone_layout_is_one_column_without_sideways_scrolling(self):
        for device in ('iPhone 13', 'Galaxy S9+'):
            with self.subTest(device=device):
                page = self.open(**self.playwright.devices[device])
                m = self.measure(page)
                self.assertLessEqual(m['pageX'], 0)
                self.assertGreaterEqual(m['historyTop'], m['createBottom'] - 1, 'My resumes follows the form')
                self.assertGreaterEqual(m['submit'], 44, 'touch-friendly')
                page.get_by_label('Enter Manually').check()
                name, phone = page.get_by_label('Full name').bounding_box(), page.get_by_label('Phone', exact=True).bounding_box()
                self.assertGreaterEqual(phone['y'], name['y'] + name['height'], 'Full name and Phone stack on a phone')
                self.assertGreaterEqual(phone['width'], 250, 'room for the whole "Enter phone number" placeholder')
                page.get_by_label('Upload Files').check()
                self.assertLessEqual(self.measure(page)['pageX'], 0)
                # Every control of a complete card stays inside the card: nothing overlaps or spills sideways.
                row = page.locator('#auto-job-list > li[data-status="complete"]').first
                outside = row.evaluate('''li => { const card = li.getBoundingClientRect();
                    return [...li.querySelectorAll('.auto-main > *:not(.auto-menu), .auto-actions > *')].filter(el => {
                        const box = el.getBoundingClientRect();
                        return box.left < card.left - 0.5 || box.right > card.right + 0.5; }).length; }''')
                self.assertEqual(outside, 0)
                for name in ('Download PDF', 'Preview PDF', 'Details'):
                    self.assertTrue(row.get_by_role('link' if name != 'Details' else 'button', name=name, exact=True).is_visible())
                row.get_by_role('button', name='More actions for Candidate 03 Example').click()
                menu = row.locator('.auto-menu')
                self.assertTrue(menu.is_visible())
                box, width = menu.bounding_box(), page.evaluate('innerWidth')
                self.assertTrue(box['x'] >= 0 and box['x'] + box['width'] <= width, 'the menu stays on screen')
                self.assertLessEqual(self.measure(page)['pageX'], 0)
                # Scrolling the page a little keeps the menu open and attached to its button.
                page.mouse.wheel(0, 60)
                page.wait_for_timeout(250)
                kebab = row.locator('.auto-kebab').bounding_box()
                box = menu.bounding_box()
                self.assertTrue(menu.is_visible())
                self.assertAlmostEqual(box['x'] + box['width'], kebab['x'] + kebab['width'], delta=1.5)
                self.assertTrue(abs(box['y'] - (kebab['y'] + kebab['height'])) < 12 or box['y'] + box['height'] < kebab['y'] + 1)
                page.close()

    def test_three_dot_menu_holds_the_other_actions_and_opens_and_closes(self):
        from playwright.sync_api import expect
        page = self.open(viewport={'width': 1440, 'height': 900})
        page.get_by_label('Filter by status').select_option('complete')
        row = page.locator('#auto-job-list > li[data-status="complete"]').first
        kebab = row.get_by_role('button', name='More actions for Candidate 03 Example')
        menu = row.locator('.auto-menu')
        self.assertTrue(menu.is_hidden(), 'closed until asked for')
        for name in ('Download LaTeX', 'Download Overleaf PDF', 'Open Overleaf project'):
            self.assertEqual(row.get_by_role('link', name=name, exact=True).count(), 0, f'{name} is in the menu')
        kebab.click()
        self.assertTrue(menu.is_visible())
        self.assertEqual(kebab.get_attribute('aria-expanded'), 'true')
        self.assertEqual(menu.locator('a, button').evaluate_all('items => items.map(i => i.textContent.trim())'),
                         ['LaTeX', 'Overleaf PDF', 'Open Overleaf', 'Regenerate', 'Delete'])
        k, m = kebab.bounding_box(), menu.bounding_box()
        self.assertAlmostEqual(m['x'] + m['width'], k['x'] + k['width'], delta=1.5, msg='right-aligned with its button')
        self.assertGreater(m['y'], k['y'] + k['height'] - 1, 'opens below its button')
        expect(menu.locator('a').first).to_be_focused()  # focus moves into the menu
        page.keyboard.press('ArrowDown')
        self.assertEqual(page.evaluate('document.activeElement.textContent.trim()'), 'Overleaf PDF')
        page.keyboard.press('Escape')
        self.assertTrue(menu.is_hidden(), 'Escape closes it')
        self.assertEqual(kebab.get_attribute('aria-expanded'), 'false')
        kebab.click()
        page.mouse.click(5, 300)
        self.assertTrue(menu.is_hidden(), 'a click elsewhere closes it')
        kebab.click()
        kebab.click()
        self.assertTrue(menu.is_hidden(), 'the button toggles it')
        # Only one menu is open at a time.
        page.locator('#auto-job-list > li .auto-kebab').nth(1).click()
        kebab.click()
        expect(page.locator('.auto-menu:popover-open')).to_have_count(1)
        self.assertTrue(menu.is_visible())
        page.keyboard.press('Escape')
        # A card near the bottom of a short window opens its menu upward, fully on screen.
        page.set_viewport_size({'width': 1366, 'height': 768})
        last = page.locator('#auto-job-list > li .auto-kebab').last
        last.click()
        opened = page.locator('.auto-menu:popover-open')
        expect(opened).to_have_count(1)
        o, k = opened.bounding_box(), last.bounding_box()
        self.assertTrue(o['y'] >= 0 and o['y'] + o['height'] <= 768, 'on screen')
        if k['y'] + k['height'] + o['height'] + 6 > 768:
            self.assertLess(o['y'] + o['height'], k['y'] + 1, 'opens upward when there is no room below')
        page.close()

    def test_pagination_is_hidden_when_everything_fits_on_one_page(self):
        page = self.open(jobs=fake_jobs(4), viewport={'width': 1920, 'height': 1080})
        self.assertEqual(page.locator('#auto-page').inner_text(), 'Showing 1 – 4 of 4 resumes')
        self.assertTrue(page.locator('#auto-pager').is_hidden())
        self.assertEqual(page.locator('#auto-count').inner_text(), '4')
        self.assertEqual(page.locator('#auto-done').inner_text(), '1')
        stats = page.locator('#auto-stats').inner_text()
        for label in ('Total Resumes', 'Completed', 'In Progress'):
            self.assertIn(label, stats)
        self.assertEqual(page.locator('#auto-done-percent').count(), 0, 'no summary progress bar')
        page.close()

    def test_switching_modes_keeps_what_was_entered(self):
        page = self.open(viewport={'width': 1366, 'height': 768})
        self.assertTrue(page.get_by_label('Upload Files').is_checked(), 'upload is the default')
        self.assertLess(page.locator('#auto-upload').bounding_box()['y'], page.locator('#auto-role').bounding_box()['y'],
                        'documents come before the target role')
        # Upload Files: only the files, Target Role, consent and Create Resume.
        self.assertFalse(page.locator('#auto-manual').is_visible(), 'no typed fields with Upload Files')
        for text in ('Full name', 'Phone', 'More details'):
            self.assertFalse(page.get_by_text(text, exact=True).is_visible(), text)
        self.assertTrue(page.get_by_text('Choose Files').is_visible())
        self.assertTrue(page.locator('#auto-consent').is_visible())
        page.get_by_label('Target Role').fill('React.js Developer')
        page.get_by_label('Enter Manually').check()
        self.assertTrue(page.get_by_placeholder('Enter full name').is_visible())
        self.assertTrue(page.get_by_placeholder('Enter phone number').is_visible())
        page.get_by_label('Full name').fill('Kiran Example (fictional)')
        page.get_by_label('Phone', exact=True).fill('+91 90000 00000')
        page.locator('#auto-background').fill('Worked at Example Works as Frontend Developer from 2023.')
        page.get_by_label('Upload Files').check()
        self.assertFalse(page.locator('#auto-manual').is_visible(), 'typed fields hide again with Upload Files')
        with tempfile.TemporaryDirectory() as folder:
            resume = Path(folder) / 'resume.txt'
            resume.write_text('Kiran Example (fictional)\n+91 90000 00000\n', encoding='utf-8')
            page.locator('#auto-file').set_input_files(str(resume))
        page.get_by_label('Enter Manually').check()
        self.assertEqual(page.get_by_label('Full name').input_value(), 'Kiran Example (fictional)')
        self.assertEqual(page.get_by_label('Phone', exact=True).input_value(), '+91 90000 00000')
        self.assertIn('Example Works', page.locator('#auto-background').input_value())
        page.get_by_label('Upload Files').check()
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
        page.get_by_label('Upload Files').check()
        page.locator('#auto-file').set_input_files([
            {'name': 'education.txt', 'mimeType': 'text/plain', 'buffer': b'Example College diploma'},
            {'name': 'certificate.png', 'mimeType': 'image/png', 'buffer': b'fixture image bytes'}])
        # The upload area names the chosen files, centred under Choose Files (approved mockup).
        self.assertEqual(page.locator('#auto-file-name').inner_text(), '2 files: education.txt, certificate.png')
        page.locator('#auto-consent').check()
        with page.expect_request(lambda request: request.method == 'POST' and request.url.endswith('/api/automation')) as sent:
            page.get_by_role('button', name=re.compile(r'^(?:Create|Generate) Resume$')).click()
        payload = sent.value.post_data_json
        self.assertEqual([item['extension'] for item in payload['documents']], ['txt', 'png'])
        self.assertNotIn('candidate', payload)
        page.close()

    def test_search_filters_and_pagination(self):
        page = self.open(viewport={'width': 1440, 'height': 900})
        rows = page.locator('#auto-job-list > li')
        size = rows.count()
        pages = -(-15 // size)
        self.assertEqual(page.locator('#auto-page').inner_text(), f'Showing 1 – {size} of 15 resumes')
        self.assertEqual(rows.first.get_attribute('data-status'), 'needs_input', 'rows needing input come first')
        self.assertEqual(page.locator('#auto-pages button').count(), min(pages, 5))
        self.assertEqual(page.locator('#auto-pages [aria-current=page]').inner_text(), '1')
        first_page = rows.evaluate_all('items => items.map(item => item.dataset.id)')
        page.get_by_role('button', name='Next page').click()
        self.assertEqual(page.locator('#auto-page').inner_text(), f'Showing {size + 1} – {min(15, 2 * size)} of 15 resumes')
        self.assertEqual(rows.count(), min(size, 15 - size), 'every page holds the same number of cards')
        self.assertFalse(set(first_page) & set(rows.evaluate_all('items => items.map(item => item.dataset.id)')))
        page.get_by_role('button', name=f'Page {pages}', exact=True).click()
        self.assertEqual(rows.count(), 15 - (pages - 1) * size)
        self.assertTrue(page.get_by_role('button', name='Next page').is_disabled())
        page.get_by_role('button', name='Page 2', exact=True).click()
        self.assertEqual(page.locator('#auto-page').inner_text(), f'Showing {size + 1} – {min(15, 2 * size)} of 15 resumes')
        page.get_by_label('Search resumes').fill('Candidate 07')
        self.assertEqual(rows.count(), 1)
        self.assertIn('Candidate 07 Example', rows.first.inner_text())
        self.assertIn('Java Backend Developer', rows.first.inner_text())
        self.assertTrue(page.locator('#auto-pager').is_hidden(), 'no page numbers for one page')
        page.get_by_label('Search resumes').fill('')
        page.get_by_label('Filter by job role').select_option('Angular Developer')
        titles = rows.evaluate_all('items => items.map(item => item.querySelector(".auto-meta").textContent)')
        self.assertTrue(titles and all('Angular Developer' in title for title in titles), titles)
        page.get_by_label('Filter by job role').select_option('')
        page.get_by_label('Filter by status').select_option('needs_input')
        self.assertEqual(rows.count(), 1)
        page.get_by_label('Filter by status').select_option('complete')
        self.assertEqual(page.locator('#auto-page').inner_text(), f'Showing 1 – {min(size, 12)} of 12 resumes')
        page.close()

    def test_every_action_and_regeneration(self):
        page = self.open(viewport={'width': 1366, 'height': 768})
        page.get_by_label('Filter by status').select_option('complete')
        row = page.locator('#auto-job-list > li[data-status="complete"]').first
        # Download PDF, Preview and Details are on the card; the rest are in its three-dot menu.
        actions = (('Download PDF', 'name=resume.pdf&download=1'), ('Preview PDF', 'name=resume.pdf'),
                   ('Download LaTeX', 'name=main.tex&download=1'), ('Download Overleaf PDF', 'name=overleaf.pdf&download=1'),
                   ('Open Overleaf project', 'https://www.overleaf.com/project/abc03'))
        for name, fragment in actions:
            with self.subTest(action=name):
                if name == 'Download LaTeX':
                    row.get_by_role('button', name='More actions for Candidate 03 Example').click()
                link = row.get_by_role('link', name=name, exact=True)
                self.assertTrue(link.is_visible())
                self.assertIn(fragment, link.get_attribute('href'))
        page.keyboard.press('Escape')
        self.assertIn('ATS 63', row.locator('.auto-score').inner_text())
        self.assertIn('Complete', row.locator('strong').inner_text())
        details = row.get_by_role('button', name='Details')
        details.click()
        self.assertTrue(row.locator('.auto-ats').is_visible())
        row.locator('.auto-ats summary').click()
        self.assertEqual(row.locator('.auto-ats tr').count(), 8, 'the explainable ATS breakdown')
        row.get_by_role('button', name='Hide details').click()
        self.assertTrue(row.locator('.auto-ats').is_hidden())
        row.get_by_role('button', name='More actions for Candidate 03 Example').click()
        row.locator('.auto-menu').get_by_role('button', name='Regenerate', exact=True).click()
        self.assertTrue(row.locator('.auto-menu').is_hidden(), 'choosing an item closes the menu')
        field = row.get_by_label('Regenerate for another role')
        self.assertTrue(field.evaluate('el => el === document.activeElement'), 'Regenerate opens and focuses the role')
        field.fill('Node.js Developer')
        with page.expect_request('**/api/automation/regenerate'):
            row.locator('.auto-regenerate').get_by_role('button', name='Regenerate').click()
        self.assertEqual(self.posts[-1], {'id': '03' + 'f' * 62, 'role': 'Node.js Developer'})
        page.close()

    def test_ai_edit_previews_changes_and_applies_only_the_ticked_ones(self):
        from playwright.sync_api import expect
        page = self.open(viewport={'width': 1440, 'height': 900})
        sent = {}
        proposal = {'id': 'c' * 64, 'candidate': 'Candidate 03 Example', 'role': 'React.js Developer', 'keywords': ['React'],
                    'gaps': ['Kafka'], 'edits': [
                        {'path': 'education.0.end', 'label': 'End (Example University)', 'old': '2024', 'new': '2026'},
                        {'path': 'skills.+', 'label': 'Skill (added)', 'old': '', 'new': 'Docker'}]}

        def preview(route):
            sent['preview'] = json.loads(route.request.post_data)
            route.fulfill(status=200, content_type='application/json', body=json.dumps(proposal))

        def apply(route):
            sent['apply'] = json.loads(route.request.post_data)
            route.fulfill(status=202, content_type='application/json', body=json.dumps({'id': 'd' * 64, 'status': 'queued'}))
        page.route('**/api/automation/edit-preview', preview)
        page.route('**/api/automation/edit-apply', apply)
        row = page.locator('#auto-job-list > li[data-status="complete"]').first
        row.get_by_role('button', name=re.compile('^AI Edit')).click()
        dialog = page.locator('#auto-edit-dialog')
        expect(dialog).to_be_visible()
        dialog.get_by_label('Correction').fill("Change the graduation year from 2024 to 2026 and add Docker to skills")
        dialog.get_by_role('button', name='Preview changes').click()
        first = dialog.locator('.auto-compare-table tr').nth(1)
        self.assertEqual([first.locator('td').nth(i).inner_text() for i in (1, 2, 3)],
                         ['End (Example University)', '2024', '2026'], 'original vs edited, before approval')
        expect(dialog).to_contain_text('Skill gaps (not in the verified facts, so not added): Kafka')
        self.assertEqual(sent['preview']['id'], row.get_attribute('data-id'), 'opened from the card: that resume')
        dialog.locator('.auto-edit-changes input[type=checkbox]').nth(1).uncheck()
        dialog.get_by_role('button', name='Apply and regenerate').click()
        expect(dialog).to_be_hidden()
        self.assertEqual(sent['apply'], {'id': 'c' * 64, 'edits': [proposal['edits'][0]], 'keywords': ['React'],
                                         'strengths': []})
        expect(page.locator('#auto-delete-status')).to_contain_text('The original is kept')
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth - innerWidth'), 0)
        page.close()

    def test_ai_edit_is_a_full_screen_sheet_on_a_phone(self):
        page = self.open(**self.playwright.devices['iPhone 13'])
        row = page.locator('#auto-job-list > li[data-status="complete"]').first
        row.get_by_role('button', name=re.compile('^AI Edit')).click()
        dialog = page.locator('#auto-edit-dialog')
        box, width = dialog.bounding_box(), page.evaluate('innerWidth')
        self.assertLessEqual(abs(box['width'] - width), 1, 'the sheet spans the phone')
        for name in ('Preview changes', 'Cancel'):
            self.assertGreaterEqual(dialog.get_by_role('button', name=name).bounding_box()['height'], 44)
        self.assertRegex(page.locator('#auto-edit-for').inner_text(), r'^Editing Candidate \d+ Example', 'that resume, no choice')
        self.assertTrue(page.locator('#auto-edit-choose').is_hidden())
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth - innerWidth'), 0)
        page.close()

    def test_an_edited_version_is_compared_with_its_original_on_a_phone(self):
        from playwright.sync_api import expect
        jobs = fake_jobs(4)
        done = next(job for job in jobs if job['status'] == 'complete')
        done['edited_from'] = 'f' * 64
        page = self.open(jobs, **self.playwright.devices['iPhone 13'])
        page.route('**/api/automation/compare', lambda route: route.fulfill(status=200, content_type='application/json', body=json.dumps({
            'original': {'id': 'f' * 64, 'role': 'Java Developer', 'updated': 1, 'ats': 88},
            'edited': {'id': done['id'], 'role': 'Java Backend Developer', 'updated': 2, 'ats': 94},
            'notes': ['Edited by you: End (Example University): 2024 -> 2026.'],
            'changes': [{'field': 'End (Example University)', 'original': '2024', 'edited': '2026'}]})))
        row = page.locator(f'#auto-job-list > li[data-id="{done["id"]}"]')
        row.locator('.auto-kebab').click()
        row.get_by_role('button', name=re.compile('^Compare with original')).click()
        dialog = page.locator('#auto-compare-dialog')
        expect(dialog).to_contain_text('End (Example University)')
        expect(dialog).to_contain_text('ATS 88')
        expect(dialog).to_contain_text('ATS 94')
        expect(dialog).to_contain_text('Java Backend Developer')
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth - innerWidth'), 0)
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
                                   'bullets': ['Deployed a practice API to Cloud Run with Terraform.']}],
                       certifications=['Google Cloud Associate Cloud Engineer', 'HashiCorp Certified: Terraform Associate'])
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
        # Certifications are recommendations: unticked until the candidate says they completed one.
        expect(row.get_by_label('Google Cloud Associate Cloud Engineer')).not_to_be_checked()
        row.get_by_label('Google Cloud Associate Cloud Engineer').check()
        row.get_by_label('Other certifications (one per line)').fill('Udemy: Cloud Run Fundamentals')
        with page.expect_request('**/api/automation/answer'):
            row.get_by_role('button', name='Confirm and continue').click()
        self.assertEqual(self.posts[-1], {'id': jobs[0]['id'], 'answers': {
            'experience.0.bullets': 'Deployed services to Cloud Run with Terraform.', 'experience.1.bullets': '',
            'skills': 'Google Cloud, Terraform, Cloud Run'}, 'decisions': [{'include': False}],
            'certifications': ['Google Cloud Associate Cloud Engineer', 'Udemy: Cloud Run Fundamentals']})
        page.close()

    def test_delete_one_and_delete_all_need_explicit_confirmation(self):
        from playwright.sync_api import expect
        page = self.open(viewport={'width': 1366, 'height': 768}, permissions=['clipboard-read', 'clipboard-write'])
        dialogs, answers = [], iter([False, True])
        page.on('dialog', lambda d: (dialogs.append(d.message), d.accept() if next(answers) else d.dismiss()))
        rows = page.locator('#auto-job-list > li')
        status_filter = page.get_by_label('Filter by status')
        for status, choice, has_delete in (('needs_input', 'needs_input', True), ('processing', 'active', False),
                                           ('blocked', 'blocked', True), ('complete', 'complete', True)):
            with self.subTest(status=status):
                status_filter.select_option(choice)
                row = page.locator(f'#auto-job-list > li[data-status="{status}"]').first
                menu = row.locator('.auto-kebab')
                self.assertEqual(menu.count(), int(has_delete), 'a resume being prepared has no menu')
                if has_delete:
                    menu.click()
                    self.assertEqual(row.get_by_role('button', name='Delete', exact=False).count(), 1)
                    page.keyboard.press('Escape')
        card_03 = page.locator('#auto-job-list > li[data-id^="03"]')
        delete_03 = card_03.get_by_role('button', name='Delete Candidate 03 Example')
        card_03.locator('.auto-kebab').click()
        delete_03.click()  # cancelled in the browser's confirmation: no request at all
        page.wait_for_timeout(300)
        self.assertEqual(self.posts, [])
        self.assertIn('Permanently delete Candidate 03 Example', dialogs[0])
        self.assertIn('cannot be undone', dialogs[0])
        card_03.locator('.auto-kebab').click()
        with page.expect_request('**/api/automation/delete'):
            delete_03.click()  # confirmed
        expect(page.locator('#auto-delete-status')).to_have_text('Permanently deleted 1 resume.')
        self.assertEqual(self.posts, [{'id': '03' + 'f' * 62, 'confirm': 'DELETE'}])
        expect(page.locator('#auto-job-list > li[data-id^="03"]')).to_have_count(0)
        expect(page.locator('#auto-count')).to_have_text('14')
        status_filter.select_option('')

        dialog = page.locator('#auto-delete-dialog')
        confirm = dialog.get_by_role('button', name='Permanently delete all')
        phrase = dialog.get_by_label('Type DELETE ALL to confirm')
        page.get_by_role('button', name='Delete All', exact=True).click()
        expect(dialog).to_be_visible()
        for typed in ('', 'delete all', 'DELETE AL', 'DELETE ALL '):
            phrase.fill(typed)
            expect(confirm).to_be_disabled()
        phrase.press('Enter')  # submitting without the exact phrase does nothing
        dialog.get_by_role('button', name='Cancel').click()
        expect(dialog).to_be_hidden()
        self.assertEqual(len(self.posts), 1, 'cancel sends nothing')
        page.get_by_role('button', name='Delete All', exact=True).click()
        expect(phrase).to_have_value('')
        # The Copy button beside the phrase copies exactly "DELETE ALL" and says so, but never types it:
        # the field stays empty and Delete stays disabled until the phrase is entered.
        copy = dialog.get_by_role('button', name='Copy DELETE ALL')
        expect(copy).to_be_visible()
        copy_box, label_box = copy.bounding_box(), dialog.locator('.auto-delete-code').bounding_box()
        self.assertLess(abs((copy_box['y'] + copy_box['height'] / 2) - (label_box['y'] + label_box['height'] / 2)), 12)
        self.assertGreater(copy_box['x'], label_box['x'])
        copy.click()
        expect(dialog.locator('#auto-delete-copied')).to_have_text('Copied!')
        self.assertEqual(page.evaluate('navigator.clipboard.readText()'), 'DELETE ALL')
        expect(phrase).to_have_value('')
        expect(confirm).to_be_disabled()
        expect(dialog).to_be_visible()
        self.assertEqual(len(self.posts), 1, 'copying sends nothing')
        expect(dialog.locator('#auto-delete-copied')).to_have_text('', timeout=4000)
        phrase.focus()
        page.keyboard.press('Control+V')  # paste what the button copied
        expect(phrase).to_have_value('DELETE ALL')
        expect(confirm).to_be_enabled()
        self.delete_failures.append('ResumeOps is building a resume right now. Nothing was deleted; try again in a minute.')
        confirm.click()
        expect(dialog.locator('#auto-delete-error')).to_have_text(
            'ResumeOps is building a resume right now. Nothing was deleted; try again in a minute.')
        expect(dialog).to_be_visible()
        expect(page.locator('#auto-count')).to_have_text('14')
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
        running = page.locator('#auto-job-list > li[data-status="processing"]')
        running.wait_for()
        # The live stage and percent are in the resume's card only; no banner above the list repeats them.
        self.assertTrue(page.locator('#auto-status').is_hidden())
        self.assertIn('Compiling LaTeX with pdfLaTeX · 72%', running.inner_text())
        self.assertEqual(running.locator('progress').evaluate('el => el.value'), 72)
        page.close()


if __name__ == '__main__':
    unittest.main()
