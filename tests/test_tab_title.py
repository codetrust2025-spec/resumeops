"""23 Nov 2026 (owner): every ResumeOps browser tab reads just "ResumeOps" with the brand's document icon,
matching TeleAutomation's clean tabs."""
import re
import unittest
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1] / 'dashboard'
PAGES = ['index', 'ai-builder', 'automation', 'candidates', 'operations', 'roles', 'apply', 'apply-status']


class TabTitleTest(unittest.TestCase):
    def test_every_page_has_short_title_and_icon(self):
        for page in PAGES:
            html = (DASHBOARD / f'{page}.html').read_text(encoding='utf-8')
            self.assertEqual(re.findall(r'<title>[^<]*</title>', html), ['<title>ResumeOps</title>'], page)
            icon = '/apply/assets/favicon.svg' if page.startswith('apply') else '/favicon.svg'
            self.assertIn(f'<link rel="icon" type="image/svg+xml" href="{icon}">', html, page)

    def test_icon_is_served_on_both_origins(self):
        source = (DASHBOARD / 'resume_dashboard.py').read_text(encoding='utf-8')
        self.assertIn("'/favicon.svg': ('favicon.svg', 'image/svg+xml')", source)
        self.assertIn("'favicon.svg': 'image/svg+xml'", source)
        self.assertIn('<title>ResumeOps</title>', source)
        self.assertNotIn('Signed in \xc2\xb7 ResumeOps', source)
        self.assertTrue((DASHBOARD / 'favicon.svg').read_text(encoding='utf-8').startswith('<svg'))

    def test_candidate_page_uses_public_icon(self):
        import sys
        sys.path.insert(0, str(DASHBOARD.parent))
        from dashboard.intake import candidate_page
        html = candidate_page((DASHBOARD / 'automation.html').read_bytes())
        self.assertIn(b'href="/apply/assets/favicon.svg"', html)
        self.assertNotIn(b'href="/favicon.svg"', html)


if __name__ == '__main__':
    unittest.main()
