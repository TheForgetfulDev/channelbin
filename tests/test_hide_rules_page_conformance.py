"""The Hide Rules page against the design that produced it.

The Hide Rules page (dev/changelog/780), built from
the approved dev/mockups/35-hide-rules.html round 4. The page's row-level markup
(the rules table, the account filter pills, every modal) is rendered entirely client-side by
static/js/hide-rules.js from window.HIDE_RULES_CONFIG, so what this file can check from a
server-rendered response is the shell around it - each a decision a careless edit would
quietly undo:

  * The summary tiles are `.dash-pulse`/`.mtile` (style.css), the same component the
    dashboard uses - not a page-local lookalike.
  * The tiles are real `Channel.hidden`/`hidden_deferred` aggregates read once per request,
    not client-recomputed - CLAUDE.md's "Measurements: when a stat earns a column" already
    settled that a stat with a column is read from the column, and re-deriving it in JS
    against a client-side rules snapshot would be a second, driftable copy of the same sum.
  * `window.HIDE_RULES_CONFIG` carries the rules/accounts/target-label data hide-rules.js
    needs to render without a second request - a rule saved a moment ago must show up in
    that blob on the very next load, not just in a follow-up fetch.
  * The page is reached from the sidebar's Setup section - in BOTH nav shells - and is not
    one of the Channels hub's tabs, which are on their way out (dev/changelog/782).

The account-filter/sort/modal behavior itself is covered by hide-rules.js being exercised
directly (Playwright, verified 2026-08-21 against the live database: a real rule's live
preview, save, materialize and delete round-tripped and left the database exactly as it
started).

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_hide_rules_page_conformance
"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import ChannelHideRule, HIDE_TARGET_NAME_GLOB  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


class HideRulesPageTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # One app and one render for the whole class: every case reads the markup and
        # none rewrites the state it was rendered from (dev/changelog/979).
        cls.t = make_test_app()
        cls.app = cls.t.app
        cls.client = cls.app.test_client()
        with cls.app.app_context():
            acct = seed.make_account(name='Test Provider')
            cls.acct_id = acct.id
            # One channel offered, one hidden by hand, one hidden-but-deferred (in the
            # guide) - real Channel.hidden/hidden_deferred values, exactly what
            # app/channel_hiding.py::recompute() would have written.
            seed.make_channel(acct, name='Offered Channel')
            seed.make_channel(acct, name='Hidden Channel', hidden=True,
                              hidden_reason='manual')
            seed.make_channel(acct, name='Deferred Channel', hidden_deferred=True,
                              in_guide=True)
            db.session.add(ChannelHideRule(target=HIDE_TARGET_NAME_GLOB, pattern='TEST*',
                                           account_id=None, enabled=True, match_count=1,
                                           deferred_count=0))
            db.session.commit()
        cls.resp = cls.client.get('/channels/hide-rules')
        cls.html = cls.resp.get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def _config(self):
        m = re.search(r'window\.HIDE_RULES_CONFIG = (\{.*?\});', self.html, re.S)
        self.assertIsNotNone(m, 'HIDE_RULES_CONFIG blob not found in the page')
        # A JS object literal (bare keys, trailing comma before `}`), not strict JSON - quote
        # the four known keys and drop the trailing comma so json.loads can parse it. Each
        # VALUE is still real `| tojson` output, so this only reshapes the wrapper.
        raw = re.sub(r'(rules|accounts|targetLabels|totalChannels):', r'"\1":', m.group(1))
        raw = re.sub(r',(\s*\})', r'\1', raw)
        return json.loads(raw)

    def test_page_loads(self):
        self.assertEqual(self.resp.status_code, 200)

    def test_summary_tiles_are_the_shared_dash_pulse_component(self):
        self.assertIn('dash-pulse', self.html)
        self.assertIn('class="mtile"', self.html)
        # Not a page-local lookalike class.
        self.assertNotIn('hr-summary-tile', self.html)

    def _tile_value(self, label):
        m = re.search(re.escape(label) + r'</div><div class="m-v">([^<]*)</div>', self.html)
        self.assertIsNotNone(m, f'{label!r} tile not found in rendered page')
        return m.group(1).strip()

    def test_summary_tiles_carry_real_aggregates(self):
        # Seed: 3 channels total, 1 hand-hidden, 1 deferred (matched but kept visible by the
        # guide), 1 rule (enabled). These are Channel.hidden/hidden_deferred read straight
        # off the row - not a display-cache or client-side recomputation.
        self.assertEqual(self._tile_value('Hidden now'), '1')
        self.assertEqual(self._tile_value('Kept visible'), '1')
        self.assertEqual(self._tile_value('Still offered'), '2')
        rules_active = self._tile_value_raw('Rules active')
        self.assertTrue(rules_active.startswith('1 '), rules_active)   # enabled_rules
        self.assertIn('/ 1<', rules_active)                            # total rules

    def _tile_value_raw(self, label):
        m = re.search(re.escape(label) + r'</div><div class="m-v">(.*?)</div>', self.html)
        self.assertIsNotNone(m, f'{label!r} tile not found in rendered page')
        return m.group(1)

    def test_reached_from_the_sidebar_not_a_channels_tab(self):
        # base.html renders nav_sections() twice (desktop sidebar + mobile drawer), and
        # BOTH copies must carry the active state - a glyph or state that differed between
        # the two shells is a defect (DESIGN.md 2 / 9.7).
        links = re.findall(r'<a class="nav-link([^"]*)" href="/channels/hide-rules"', self.html)
        self.assertEqual(len(links), 2, f'expected both nav shells to link Hide Rules, got {links}')
        for cls in links:
            self.assertIn('active', cls)
        # Setup, not Channels, and never a hub tab: the Channels tabs are on their way out,
        # so a tab there would be a destination with no menu entry (dev/changelog/782).
        tabs = _read('templates/channels/_tabs.html')
        self.assertNotIn('hide_rules', tabs)
        self.assertNotIn('channels/_tabs.html', _read('templates/channels/hide_rules.html'))

    def test_config_blob_carries_the_seeded_rule(self):
        cfg = self._config()
        patterns = [r['pattern'] for r in cfg['rules']]
        self.assertIn('TEST*', patterns)
        names = [a['name'] for a in cfg['accounts']]
        self.assertIn('Test Provider', names)
        self.assertIn('targetLabels', cfg)
        self.assertEqual(cfg['totalChannels'], 3)

    def test_no_superseded_ui_classes(self):
        # DESIGN.md / CLAUDE.md "UI components and tokens": .table and qbadge-* are frozen
        # legacy systems, never extended by new pages.
        tpl = _read('templates/channels/hide_rules.html')
        self.assertNotRegex(tpl, r'class="[^"]*\btable\b[^"]*"')
        self.assertNotIn('qbadge-', tpl)

    def test_page_scaffold_has_js_mount_points(self):
        # The rules table and account pills are rendered entirely by hide-rules.js - the
        # server ships the mount points, not a pre-filled "nothing active" table that JS
        # would have to reconcile against.
        self.assertIn('id="rules-section"', self.html)
        self.assertIn('id="scope-pills"', self.html)
        self.assertIn('id="hr-add-btn"', self.html)
        self.assertIn("js/hide-rules.js", self.html)


if __name__ == '__main__':
    unittest.main()
