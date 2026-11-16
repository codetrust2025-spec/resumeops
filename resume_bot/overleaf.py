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


def import_zip(zip_path, project_name, browser_profile, *, pdf_path,
               saved_url=None, headless=True, timeout_ms=90000):
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
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(profile), channel="chrome", headless=headless, accept_downloads=True)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_default_timeout(timeout_ms)
                    status = 'exists'
                    if saved_url:
                        if not _editor_url(saved_url):
                            raise OverleafError('Invalid saved Overleaf editor URL')
                        page.goto(saved_url, wait_until='domcontentloaded')
                    else:
                        page.goto(ORIGIN + '/project', wait_until='domcontentloaded')
                        if page.url.rstrip('/') != ORIGIN + '/project':
                            raise OverleafError('Overleaf dashboard unavailable; check the authorized session')
                        found = _find_project(page, project_name, timeout_ms)
                        if found:
                            page.goto(found, wait_until='domcontentloaded')
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
                    if not _editor_url(page.url):
                        raise OverleafError('Overleaf editor unavailable; check the authorized session')
                    url = page.url
                    write_json(journal, {'project_name': project_name, 'state': 'editor', 'url': url})
                    _compile_and_download(page, pdf_path)
                    return {'status': status, 'project_name': project_name, 'url': url}
                finally:
                    context.close()
        except PlaywrightError as exc:
            raise OverleafError('Overleaf browser step failed; check session, editor, or upload recovery') from exc


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
