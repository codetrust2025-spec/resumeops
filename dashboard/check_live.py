"""Browser end-to-end check of live progress and automatic reset.

Real dashboard server and real watcher (pdfLaTeX, PDF checks, retries, heartbeat). Ollama is an
NDJSON-streaming stand-in that echoes the candidate's text slowly; the Overleaf browser upload is a
stand-in that reports the same browser steps with delays. Scenarios: success with monotonic
progress and automatic reset; page refresh mid-job; failure that must not reset, then Retry now;
two candidates queued together; desktop and iPhone-sized layouts.

    python dashboard/check_live.py [screenshot directory]
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
sys.path.insert(0, str(ROOT))
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from resume_dashboard import Workspace, handler  # noqa: E402
from resume_bot import pipeline  # noqa: E402
from resume_bot.ollama import Ollama  # noqa: E402
from resume_bot.overleaf import OverleafError  # noqa: E402

SHOTS = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None


class SlowOllama(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        source = json.loads(re.search(r'INPUT ARRAY:\s*(\[.*\])', body['prompt'], re.S).group(1))
        text = json.dumps({'edits': source})
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson')
        self.end_headers()
        time.sleep(1.0)  # model load / prompt evaluation: no output yet
        for i in range(0, len(text), 6):
            self.wfile.write((json.dumps({'response': text[i:i + 6], 'done': False}) + '\n').encode())
            self.wfile.flush()
            time.sleep(0.12)
        self.wfile.write((json.dumps({'response': '', 'done': True, 'eval_count': 1}) + '\n').encode())

    def log_message(self, *args):
        pass


FAIL_OVERLEAF = {'on': False}


def overleaf(zip_path, title, browser, *, pdf_path, saved_url, on_step):
    on_step(0, 'Opening authorized browser session')
    time.sleep(0.6)
    if FAIL_OVERLEAF['on']:
        raise OverleafError('Overleaf session expired')
    on_step(1, 'Opening Overleaf project')
    time.sleep(0.4)
    on_step(3, 'Waiting for the Overleaf editor')
    on_step(4, 'Compiling on Overleaf and downloading the PDF')
    time.sleep(1.2)
    shutil.copyfile(zip_path.parent / 'resume.pdf', pdf_path)
    on_step(6, 'Overleaf PDF downloaded')
    return {'status': 'uploaded', 'project_name': title,
            'url': 'https://www.overleaf.com/project/live' + title[-8:].lower()}


class Watcher:
    """Runs scan(once=True) in a loop like the scheduled watcher, until stopped."""
    def __init__(self, inbox, output, ai, browser):
        self.args = (inbox, output)
        self.kwargs = dict(ai=ai, overleaf=True, browser_profile=browser, once=True)
        self.stop = threading.Event()
        self.paused = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            if not self.paused.is_set():
                pipeline.scan(*self.args, **self.kwargs)
            self.stop.wait(0.5)


def fill_candidate(page, name, email):
    page.get_by_label('Target role').select_option('fullstack-developer')
    page.get_by_role('button', name='Continue').click()
    page.get_by_label('Full name').fill(name)
    page.get_by_label('Email').fill(email)
    page.get_by_role('button', name='Continue').click()
    page.get_by_label('Professional summary').fill('Builds Python and SQL reporting tools for operations teams.')
    page.get_by_label('Verified skills').fill('Python\nSQL')
    page.get_by_role('button', name='Add experience').click()
    page.get_by_label('Employer').fill('Example Labs')
    page.get_by_label('Position', exact=True).fill('Software Engineer')
    page.get_by_label('Start date', exact=True).fill('January 2020')
    page.get_by_label('Verified work contributions').fill(
        'Maintained Python reporting tools used by four teams\nAutomated weekly SQL data checks')
    page.get_by_role('button', name='Continue').click()
    page.get_by_label('I confirm these candidate facts are accurate.').check()
    page.get_by_role('button', name='Generate resume').click()
    expect(page.locator('#builder-status')).to_have_text('Resume queued.')


def sample(page):
    return page.evaluate("""() => ({percent: document.getElementById('job-progress').value,
        title: document.getElementById('job-title').textContent,
        stage: document.getElementById('job-stage').textContent,
        elapsed: document.getElementById('job-elapsed').textContent,
        status: document.getElementById('job-status').textContent,
        visible: !document.getElementById('generation').hidden})""")


def watch_until(page, done, timeout=90, samples=None):
    end = time.time() + timeout
    while time.time() < end:
        current = sample(page)
        if samples is not None and current['visible']:
            samples.append(current)
        if done(current):
            return current
        page.wait_for_timeout(250)
    raise AssertionError('Timed out; last sample ' + json.dumps(sample(page)))


def main():
    temp = Path(tempfile.mkdtemp(prefix='resumeops-live-'))
    workspace = Workspace(temp, code_root=ROOT)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(workspace, '', 't'))
    url = f'http://127.0.0.1:{server.server_port}'
    server.RequestHandlerClass = handler(workspace, url, 't')
    ollama = ThreadingHTTPServer(('127.0.0.1', 0), SlowOllama)
    for item in (server, ollama):
        threading.Thread(target=item.serve_forever, daemon=True).start()
    ai = Ollama('qwen2.5:7b', f'http://127.0.0.1:{ollama.server_port}', timeout=30)
    watcher = Watcher(workspace.inbox, workspace.output, ai, temp / 'browser')
    results = {}
    with patch('resume_bot.pipeline.import_zip', side_effect=overleaf), \
            patch('resume_bot.pipeline.HEARTBEAT_SECONDS', 1), sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={'width': 1360, 'height': 960})
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url)

        # 1. Success: queue, watch progress, refresh mid-job, verify reset after verified 100%.
        fill_candidate(page, 'Live Progress Example', 'live@example.com')
        watcher.thread.start()
        samples = []
        watch_until(page, lambda s: 'Ollama' in s['stage'] and s['percent'] > 21, samples=samples)
        if SHOTS:
            SHOTS.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SHOTS / 'desktop-processing.png'), full_page=False)
        inbox_before = sorted(p.name for p in workspace.inbox.glob('*.json') if not p.name.startswith('.'))
        page.reload()  # refresh mid-job: the active job must be restored, not restarted
        expect(page.locator('#generation')).to_be_visible()
        restored = watch_until(page, lambda s: s['percent'] > 0 and s['visible'])
        assert sorted(p.name for p in workspace.inbox.glob('*.json') if not p.name.startswith('.')) == inbox_before, \
            'refresh created work'
        results['restored_after_refresh'] = restored
        watch_until(page, lambda s: not s['visible'] and not page.locator('#results').is_hidden(),
                    timeout=120, samples=samples)
        percents = [s['percent'] for s in samples]
        assert percents == sorted(percents), 'displayed progress decreased: %s' % percents
        assert 100 not in percents[:-1] or all(p == 100 for p in percents[percents.index(100):])
        elapsed = [s['elapsed'] for s in samples]
        assert len(set(elapsed)) > 5, 'elapsed time did not advance'
        stages = list(dict.fromkeys(s['stage'].split(' — ')[0] for s in samples))
        results['displayed_percents'] = sorted(set(percents))
        results['stages_seen'] = stages
        expect(page.locator('#result-success')).to_be_visible()
        expect(page.locator('#result-success')).to_contain_text('Resume completed successfully')
        expect(page.locator('#pdf-actions')).to_contain_text('Download PDF')
        expect(page.locator('#pdf-actions')).to_contain_text('Open Overleaf project')
        expect(page.locator('#download-latex')).to_have_attribute('href', re.compile('main.tex'))
        expect(page.locator('#ats-result')).not_to_be_empty()
        expect(page.get_by_role('heading', name='Choose a target role')).to_be_visible()
        assert page.get_by_label('Target role').input_value() == ''
        assert page.locator('[name=full_name]').input_value() == ''
        assert page.locator('#experience-entries fieldset').count() == 0
        assert page.evaluate("localStorage.getItem('resumeops.activeJob')") is None
        page.locator('#history-toggle').evaluate('n => n.click()') if page.locator('#history-toggle').is_visible() else None
        expect(page.locator('#revision-list')).to_contain_text('Live Progress Example')
        if SHOTS:
            page.screenshot(path=str(SHOTS / 'desktop-completed-reset.png'), full_page=True)
        outputs_after_first = {p.relative_to(workspace.output).as_posix() for p in workspace.output.rglob('*') if p.is_file()}

        # 2. Failure: must stay visible, must not reset, Retry now runs it again.
        FAIL_OVERLEAF['on'] = True
        fill_candidate(page, 'Retry Example', 'retry@example.com')
        failed = watch_until(page, lambda s: s['title'].startswith(('Retry scheduled', 'Failed')), timeout=90)
        assert failed['percent'] < 100
        expect(page.locator('#job-retry')).to_be_visible()
        expect(page.locator('#job-detail')).to_contain_text('Overleaf is unavailable')
        expect(page.locator('#job-stages li[data-state=failed]')).to_have_text('Overleaf')
        assert page.locator('[name=full_name]').input_value() == 'Retry Example', 'failure reset the form'
        results['failure_view'] = failed
        if SHOTS:
            page.screenshot(path=str(SHOTS / 'desktop-failed.png'), full_page=False)
        FAIL_OVERLEAF['on'] = False
        page.get_by_role('button', name='Retry now').click()
        expect(page.locator('#job-status')).to_contain_text(re.compile('Retry requested|attempt 2'))
        retry = watch_until(page, lambda s: 'attempt 2' in s['status'], timeout=60)
        results['retry_view'] = retry
        watch_until(page, lambda s: not s['visible'] and not page.locator('#result-success').is_hidden(), timeout=120)
        assert page.locator('[name=full_name]').input_value() == ''

        # 3. Two candidates queued while the watcher is busy elsewhere (paused here): the second
        # waits behind the first, and the panel names the other job.
        watcher.paused.set()
        page.wait_for_timeout(1500)
        fill_candidate(page, 'Queue First Example', 'first@example.com')
        page.get_by_role('button', name='Start next candidate').click()
        expect(page.get_by_role('heading', name='Choose a target role')).to_be_visible()
        fill_candidate(page, 'Queue Second Example', 'second@example.com')
        waiting = watch_until(page, lambda s: s['title'].startswith('Waiting'), timeout=20)
        results['queued_view'] = waiting
        expect(page.locator('#job-others')).to_contain_text('Queue First Example (waiting)')
        watcher.paused.clear()
        expect(page.locator('#job-others')).to_contain_text(re.compile(r'Queue First Example \(\d+%\)'), timeout=30000)
        watch_until(page, lambda s: not s['visible'] and not page.locator('#result-success').is_hidden(), timeout=180)
        expect(page.locator('#result-name')).to_have_text('Queue Second Example')
        all_outputs = {p.relative_to(workspace.output).as_posix() for p in workspace.output.rglob('*') if p.is_file()}
        assert outputs_after_first <= all_outputs, 'earlier outputs were removed'
        state = json.loads((workspace.inbox / '.resume-bot-state.json').read_text())
        assert all(v['status'] == 'ok' for v in state.values()), state
        jobs = [p.name for p in workspace.inbox.glob('*.json') if not p.name.startswith('.')]
        assert len(jobs) == 4, jobs  # four candidates, no duplicate work from refreshes or retries

        # 4. iPhone-sized layout: compact panel, no horizontal overflow, live during processing.
        phone = browser.new_context(viewport={'width': 390, 'height': 844}, device_scale_factor=3,
                                    is_mobile=True, has_touch=True)
        mobile = phone.new_page()
        mobile.on('pageerror', lambda error: errors.append(str(error)))
        mobile.goto(url)
        fill_candidate(mobile, 'Mobile Live Example', 'mobile@example.com')
        box = mobile.locator('#generation').bounding_box()
        assert 0 <= box['y'] < 844, 'live panel not in view after Generate on a phone: %s' % box
        watch_until(mobile, lambda s: 'Ollama' in s['stage'] and s['percent'] > 21)
        box = mobile.locator('#generation').bounding_box()
        results['mobile_panel_height_px'] = round(box['height'])
        assert box['height'] < 520, box
        assert mobile.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if SHOTS:
            mobile.screenshot(path=str(SHOTS / 'mobile-processing.png'))
        watch_until(mobile, lambda s: not s['visible'] and not mobile.locator('#result-success').is_hidden(), timeout=120)
        assert mobile.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if SHOTS:
            mobile.screenshot(path=str(SHOTS / 'mobile-completed.png'), full_page=True)
        browser.close()
    watcher.stop.set()
    watcher.thread.join(30)
    for item in (server, ollama):
        item.shutdown()
        item.server_close()
    assert not errors, errors
    print(json.dumps(results, indent=1))
    print('Live progress acceptance passed: success, refresh, failure, retry, queue, desktop and mobile.')


if __name__ == '__main__':
    main()
