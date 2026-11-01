"""The Readiness check page against the decisions that produced it.

Design record: dev/mockups/40-readiness.html, dev/changelog/945, 948, 950; its own page
since dev/changelog/1366, which moved the card off Maintenance unchanged and gave it the
menu link that carries its counts. Each case is a call a careless edit would quietly undo.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_readiness_page_conformance
"""
import re
import unittest

from tests.support.app import make_test_app


class ReadinessPageConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One app and one render for the whole class: every case reads the markup and
        # none rewrites the state it was rendered from (dev/changelog/979).
        cls.t = make_test_app()
        cls.client = cls.t.app.test_client()
        cls.resp = cls.client.get('/readiness')
        cls.html = cls.resp.get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def _head(self):
        return self.html[self.html.index('class="page-head"'):self.html.index('id="rd-content"')]

    def test_the_page_renders(self):
        self.assertEqual(self.resp.status_code, 200)

    def test_page_has_exactly_one_h1_and_it_carries_the_state_pill(self):
        """The pill beside the heading says whether the install is ready before the verdict
        has been read."""
        h1s = re.findall(r'<h1[^>]*>(.*?)</h1>', self.html, re.S)
        self.assertEqual(len(h1s), 1)
        self.assertRegex(h1s[0], r'^Readiness check <span class="badge [^"]*" id="rd-head-pill"')

    def test_the_first_paint_is_the_nothing_active_state(self):
        """Server-rendered initial state must equal the nothing-active state - JS may
        upgrade it, never be required to calm it down (CLAUDE.md frontend rule). So the
        page ships a neutral pill and no verdict at all until /api/readiness answers."""
        self.assertIn('b-abort', self._head(), 'the head pill starts neutral, never Ready or Not ready')
        markup = self.html[self.html.index('<div id="readiness">'):self.html.index('js/readiness.js')]
        self.assertNotIn('rd-verdict', markup, 'the verdict is never server-rendered')

    def test_the_head_carries_the_actions_and_the_way_back_to_get_started(self):
        """DESIGN.md 16.7: once the setup steps are done the banner and the Get started menu
        link are gone, and this is the way back to them. The page's actions sit in its
        header, where a list page's actions go (DESIGN.md 4)."""
        head = self._head()
        self.assertIn('href="/get-started">Get started <span class="arw">', head)
        self.assertIn('id="rd-copy"', head)
        self.assertIn('id="rd-runall"', head)

    def test_the_head_and_the_list_share_the_scripts_root(self):
        """readiness.js delegates every click on #readiness, and Copy report and Run sit in
        the head - a root that held only the list would leave both buttons dead."""
        root = self.html.index('id="readiness"')
        self.assertLess(root, self.html.index('id="rd-runall"'))
        self.assertLess(root, self.html.index('id="rd-content"'))

    def test_the_page_loads_its_own_script(self):
        self.assertIn('js/readiness.js', self.html)
        self.assertNotIn('js/maintenance.js', self.html)

    def _menu_link(self):
        link = re.search(r'<a class="nav-link[^"]*" href="/readiness">.*?</a>', self.html, re.S)
        self.assertIsNotNone(link, 'the Readiness menu link is gone')
        return link.group(0)

    def test_the_menu_link_is_active_here(self):
        self.assertRegex(self._menu_link(), r'^<a class="nav-link active"')

    def test_the_menu_link_carries_both_counts(self):
        """The amber count sits beside the red one, the way Alerts shows both.

        Their own class names, deliberately: static/js/nav-alerts.js queries
        .nav-count-bad / .nav-count-warn app-wide, so reusing those would write the ALERT
        numbers into this badge.
        """
        link = self._menu_link()
        self.assertIn('nav-count-ready-bad', link)
        self.assertIn('nav-count-ready-warn', link)
        self.assertNotIn('nav-count-bad', link)
        self.assertNotIn('nav-count-warn', link)

    def test_the_counts_start_hidden(self):
        link = self._menu_link()
        for cls in ('nav-count-ready-bad', 'nav-count-ready-warn', 'pip-ready'):
            self.assertRegex(link, rf'{cls}"[^>]*style="display:none"',
                             f'{cls} is rendered visible before anything has been measured')

    def test_the_link_sits_second_under_system_beside_alerts(self):
        """The two links that carry red and amber counts sit together (dev/changelog/1366)."""
        sidebar = self.html[self.html.index('<aside class="sidebar">'):]
        order = re.findall(r'href="(/alerts|/readiness|/jobs)"', sidebar)
        self.assertEqual(order[:3], ['/alerts', '/readiness', '/jobs'])


if __name__ == '__main__':
    unittest.main()
