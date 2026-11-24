"""Overleaf's framed marketing message (a Black Friday offer in "#gist-overlay", seen live on 24 Nov) covered the
dashboard and intercepted the New Project click on every resume. dismiss_interruptions must clear it, by its close
button when reachable, else by taking it off the page, and never touch anything else."""
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - the browser checks need Playwright
    sync_playwright = None

from resume_bot.overleaf import dismiss_interruptions

PAGE = """<!doctype html><html><body>
<button id="new" onclick="document.body.dataset.clicked = 'yes'">New Project</button>
<div id="gist-overlay" class="gist-background gist-visible"
     style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:10">
  <iframe class="gist-message gist-center gist-visible" srcdoc="%s" style="width:400px;height:200px"></iframe>
</div></body></html>"""


@unittest.skipUnless(sync_playwright, 'Playwright is not installed')
class MessageOverlayTests(unittest.TestCase):
    def run_page(self, frame_html):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                dismiss_interruptions(page)
                page.set_content(PAGE % frame_html)
                page.get_by_role('button', name='New Project').click(timeout=10000)
                return page.evaluate('document.body.dataset.clicked'), page.locator('#gist-overlay').count()
            finally:
                browser.close()

    def test_a_framed_offer_with_a_close_button_is_closed(self):
        close = ('<button aria-label=&quot;Close&quot; onclick=&quot;parent.document.getElementById('
                 "'gist-overlay').style.display='none'&quot;>x</button><p>Black Friday</p>")
        clicked, _ = self.run_page(close)
        self.assertEqual(clicked, 'yes')

    def test_a_framed_offer_without_a_reachable_close_button_is_removed(self):
        clicked, overlays = self.run_page('<p>Up to 20%% off premium plans</p>'.replace('%%', '%'))
        self.assertEqual(clicked, 'yes')
        self.assertEqual(overlays, 0)


if __name__ == '__main__':
    unittest.main()
