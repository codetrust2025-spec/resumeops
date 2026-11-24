"""Authorized Overleaf ZIP import, recovery, and remote PDF download."""
import json
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .storage import write_json, workspace_lock

ORIGIN = 'https://www.overleaf.com'
EDITOR = re.compile(r'^https://www\.overleaf\.com/project/[A-Za-z0-9_-]{6,}(?:[?#].*)?$')


class OverleafError(RuntimeError):
    pass


class OverleafAuthError(OverleafError):
    """The saved browser session is not signed in to Overleaf (expired, or unreadable by this
    Windows logon). Needs a person to sign in once; retrying alone cannot fix it."""


def _signed_out(url):
    address = urlparse(url or '')
    return address.hostname in ('overleaf.com', 'www.overleaf.com') and bool(
        re.match(r'^/(?:user/)?(?:login|register|sign[-_]?in)(?:/|$)', address.path, re.I))


def _require_session(page, response=None):
    if _signed_out(page.url) or (response is not None and response.status == 401):
        raise OverleafAuthError('Overleaf sign-in required')


def _session_marker(profile):
    return profile.parent / ('.' + profile.name + '.resume-bot-signed-in.json')


def sign_in_time(browser_profile):
    try:
        profile = Path(browser_profile).expanduser().resolve()
        return _session_marker(profile).stat().st_mtime
    except FileNotFoundError:
        return 0


def _record_sign_in(profile):
    write_json(_session_marker(profile), {})


def _editor_url(url):
    parsed = urlparse(url)
    return bool(EDITOR.fullmatch(url)) and parsed.netloc == 'www.overleaf.com'


def _existing_project_url(page, project_name):
    matching = page.get_by_text(project_name, exact=True)
    if not matching.count():
        return None
    matches = page.locator('a[href*="/project/"]').filter(has_text=project_name)
    urls = set()
    for index in range(matches.count()):
        link = matches.nth(index)
        if link.inner_text().strip() == project_name:
            candidate = urljoin(ORIGIN, link.get_attribute('href') or '')
            if _editor_url(candidate):
                urls.add(candidate)
    if len(urls) != 1:
        raise OverleafError('Project title exists without one unique editor link')
    return urls.pop()


def _find_project(page, project_name, timeout_ms):
    from playwright.sync_api import TimeoutError

    page.get_by_role('button', name=re.compile(r'^New Project$', re.I)).wait_for()
    search = page.get_by_role('textbox', name=re.compile('Search in all projects', re.I))
    search.wait_for()
    search.fill(project_name)
    try:
        page.get_by_text(project_name, exact=True).first.wait_for(
            state='visible', timeout=min(timeout_ms, 4500))
    except TimeoutError:
        pass
    return _existing_project_url(page, project_name)


def _compile_and_download(page, destination):
    page.get_by_text('main.tex', exact=True).first.wait_for(state='visible')
    recompile = page.get_by_role('button', name=re.compile(r'^Recompile$', re.I))
    recompile.click()
    # The editor exposes compile-in-progress state in different versions.
    busy = page.get_by_role('button', name=re.compile(r'^(Compiling|Stop compilation)', re.I))
    busy.first.wait_for(state='hidden')
    recompile.wait_for(state='visible')
    errors = page.get_by_text(re.compile(r'^(No PDF|Compile timeout|Compilation error)$', re.I),
                              exact=True)
    if any(errors.nth(index).is_visible() for index in range(errors.count())):
        raise OverleafError('Overleaf reported a compilation failure')
    download = page.get_by_role('button', name=re.compile(r'^Download PDF$', re.I)).or_(
        page.get_by_role('link', name=re.compile(r'^Download PDF$', re.I)))
    with page.expect_download() as event:
        download.first.click()
    event.value.save_as(str(destination))


# Browser steps; on_step(done, current) reports how many finished and what is running now.
OVERLEAF_STEP_COUNT = 6


# Overleaf shows promotional modals (for example a regional discount offer) over the dashboard.
# They intercept every click until dismissed. Only dialogs that offer one of these decline buttons
# are dismissed, by that button; an offer is never accepted, and the upload dialog (which has none
# of these buttons) is never touched.
DECLINE = re.compile(r'^(maybe later|not now|no thanks|no, thanks|remind me later|dismiss)$', re.I)


# A marketing message rendered in its own frame over the whole page (seen live: a Black Friday offer in
# "#gist-overlay"), whose only control is a close button inside the frame. It intercepted the New Project click on
# every resume. It is closed by that button, or removed from the page when the button cannot be reached; nothing in
# it is ever clicked otherwise.
MESSAGE_OVERLAY = '#gist-overlay.gist-visible, #gist-overlay:has(iframe.gist-visible)'


def _close_message_overlay(page):
    frame = page.frame_locator('#gist-overlay iframe')
    close = frame.locator('[aria-label*="close" i], button[title*="close" i], .gist-close, button:has-text("×")')
    try:
        close.first.click(timeout=3000)
        page.locator('#gist-overlay').wait_for(state='hidden', timeout=3000)
    except Exception:  # noqa: BLE001 - the message is not ours to keep: take it off the page
        page.evaluate("document.querySelectorAll('#gist-overlay').forEach(node => node.remove())")


def dismiss_interruptions(page):
    """Register handlers that clear promotional modals and framed marketing messages whenever one blocks an
    action."""
    blocking = page.locator('[role="dialog"], dialog').filter(has=page.get_by_role('button', name=DECLINE))
    page.add_locator_handler(blocking, lambda dialog: dialog.get_by_role('button', name=DECLINE).first.click())
    page.add_locator_handler(page.locator(MESSAGE_OVERLAY), lambda overlay: _close_message_overlay(page))


def import_zip(zip_path, project_name, browser_profile, *, pdf_path,
               saved_url=None, headless=True, timeout_ms=90000, on_step=lambda done, label: None):
    """``on_step(done, current)``: steps finished out of OVERLEAF_STEP_COUNT and the running step.
    Compiling and downloading happen inside one editor action, so they finish together."""
    try:
        from playwright.sync_api import sync_playwright, Error as PlaywrightError
    except ImportError as exc:
        raise OverleafError('Playwright is required for Overleaf') from exc
    profile = Path(browser_profile).expanduser().resolve()
    with workspace_lock(profile.parent, '.' + profile.name + '.resume-bot.sqlite3'):
        journal_root = profile.parent / ('.' + profile.name + '.resume-bot-uploads')
        journal_root.mkdir(exist_ok=True)
        if not re.fullmatch(r'[A-Za-z0-9-]{1,100}', project_name):
            raise OverleafError('Invalid managed Overleaf project name')
        journal = journal_root / (project_name + '.json')
        if journal.exists() and not saved_url:
            saved_url = json.loads(journal.read_text(encoding='utf-8')).get('url')
        try:
            on_step(0, 'Opening authorized browser session')
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(profile), channel="chrome", headless=headless, accept_downloads=True)
                on_step(1, 'Opening Overleaf project')
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_default_timeout(timeout_ms)
                    dismiss_interruptions(page)
                    status = 'exists'
                    if saved_url:
                        if not _editor_url(saved_url):
                            raise OverleafError('Invalid saved Overleaf editor URL')
                        _require_session(page, page.goto(saved_url, wait_until='domcontentloaded'))
                    else:
                        _require_session(page, page.goto(ORIGIN + '/project', wait_until='domcontentloaded'))
                        if page.url.rstrip('/') != ORIGIN + '/project':
                            raise OverleafError('Overleaf dashboard unavailable; check the authorized session')
                        found = _find_project(page, project_name, timeout_ms)
                        if found:
                            _require_session(page, page.goto(found, wait_until='domcontentloaded'))
                        else:
                            if journal.exists():
                                raise OverleafError('Prior upload has an unknown outcome; reconcile the exact title on Overleaf')
                            page.get_by_role('button', name=re.compile(r'^New Project$', re.I)).click()
                            page.get_by_role('menuitem', name='Existing project (.zip)', exact=True).click()
                            upload = page.locator(':is(dialog, [role="dialog"]) input[type="file"]:not([webkitdirectory])')
                            upload.wait_for(state='attached')
                            with tempfile.TemporaryDirectory(prefix='overleaf-upload-') as staging:
                                named = Path(staging) / (project_name + '.zip')
                                shutil.copyfile(zip_path, named)
                                write_json(journal, {'project_name': project_name, 'state': 'submitting'})
                                upload.set_input_files(str(named))
                                page.wait_for_url(EDITOR, wait_until='domcontentloaded')
                            status = 'uploaded'
                    on_step(3, 'Waiting for the Overleaf editor')
                    _require_session(page)
                    if not _editor_url(page.url):
                        raise OverleafError('Overleaf editor unavailable; check the authorized session')
                    url = page.url
                    write_json(journal, {'project_name': project_name, 'state': 'editor', 'url': url})
                    on_step(4, 'Compiling on Overleaf and downloading the PDF')
                    _compile_and_download(page, pdf_path)
                    on_step(6, 'Overleaf PDF downloaded')
                    _record_sign_in(profile)
                    return {'status': status, 'project_name': project_name, 'url': url}
                except PlaywrightError:
                    _require_session(page)
                    raise
                finally:
                    context.close()
        except OverleafError:
            raise
        except PlaywrightError as exc:
            # Keep the browser's own first line (timeout, intercepted click, ...) for diagnosis.
            reason = ' '.join(str(exc).split())[:240]
            raise OverleafError(f'Overleaf browser step failed; check session, editor, or upload recovery ({reason})') from exc


def login_setup(browser_profile):
    from playwright.sync_api import sync_playwright

    profile = Path(browser_profile).expanduser().resolve()
    with workspace_lock(profile.parent, '.' + profile.name + '.resume-bot.sqlite3'):
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile), channel="chrome", headless=False)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(ORIGIN + '/project', wait_until='domcontentloaded')
                input('Sign in to Overleaf in the opened browser, then press Enter: ')
                page.get_by_role('button', name=re.compile(r'^New Project$', re.I)).wait_for(timeout=5000)
            finally:
                context.close()
            # Reopen the same saved profile the way the unattended worker will use it.
            context = playwright.chromium.launch_persistent_context(
                str(profile), channel="chrome", headless=True)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                _require_session(page, page.goto(ORIGIN + '/project', wait_until='domcontentloaded'))
                page.get_by_role('button', name=re.compile(r'^New Project$', re.I)).wait_for(timeout=5000)
            finally:
                context.close()
            _record_sign_in(profile)
            print('Overleaf sign-in verified. Waiting jobs can resume.')
