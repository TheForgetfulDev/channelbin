"""Tier 0 - a click that navigates honors Ctrl/Cmd/Shift and the middle button
(static/js/util.js::bindNavClicks / followHref).

Guards dev/docs/BUGS.md 2026-09-16 @ 10:27:44 AM. Rows, tiles and labels that navigate on
click are not links, so they got none of a link's modifiers: every one assigned
`location.href`, and Ctrl-click, Cmd-click and middle-click all replaced the current page.
The helper is the one place that decision is made now (dev/changelog/996).

tests/support/nav_clicks.mjs drives the shipped util.js in jsdom and reports; every assertion
lives here. The page-level half - Search Programs' own rows - is in
tests/test_channel_search_page_js.py::NavClickTests. What jsdom cannot show is that a real
browser puts `window.open(url, '_blank')` in a tab rather than a popup window; that is
browser work.

  python3 -m unittest tests.test_nav_clicks_js
"""
import json
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'nav_clicks.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is None:
        out = subprocess.run(['node', HARNESS, REPO], capture_output=True, text=True,
                             cwd=REPO, timeout=120)
        if out.returncode != 0:
            raise AssertionError(f'nav clicks harness failed: {out.stderr[-2000:]}')
        _RESULT = json.loads(out.stdout)
    return _RESULT


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipUnless(os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class NavClickTests(unittest.TestCase):
    URL = '/recordings/7'

    def setUp(self):
        self.obs = _observe()
        self.assertEqual(self.obs['errors'], [], 'util.js must load cleanly')
        self.assertTrue(self.obs['helper_defined'])

    def assertNewTab(self, key):
        o = self.obs[key]
        self.assertEqual(o['navigated'], 0, f'{key}: must not replace the current page')
        self.assertEqual(o['opened'], [{'url': self.URL, 'target': '_blank', 'features': None}],
                         f'{key}: must open exactly one new tab, with no features string')

    def assertNothing(self, key):
        o = self.obs[key]
        self.assertEqual((o['navigated'], o['opened']), (0, []), f'{key}: must do nothing')

    def test_a_plain_click_navigates_this_tab(self):
        self.assertEqual(self.obs['plain']['navigated'], 1)
        self.assertEqual(self.obs['plain']['opened'], [])

    def test_ctrl_click_opens_a_new_tab(self):
        self.assertNewTab('ctrl')

    def test_cmd_click_opens_a_new_tab(self):
        self.assertNewTab('meta')

    def test_shift_click_opens_a_new_tab(self):
        self.assertNewTab('shift')

    def test_middle_click_opens_a_new_tab_and_claims_the_event(self):
        self.assertNewTab('middle')
        self.assertTrue(self.obs['middle']['defaultPrevented'])

    def test_a_right_button_auxclick_is_not_a_navigation(self):
        self.assertNothing('right_aux')

    def test_the_click_listener_ignores_a_non_primary_button(self):
        """The middle button's navigation belongs to auxclick; a click event carrying it
        must not open a second tab."""
        self.assertNothing('middle_as_click')

    def test_a_click_the_resolver_declines_does_nothing_with_any_modifier(self):
        for key in ('ctrl_on_action', 'middle_on_action', 'plain_on_action'):
            self.assertNothing(key)


if __name__ == '__main__':
    unittest.main(verbosity=2)
