"""The IPTV Output page against the design that produced it (mockup 53 round 3,
dev/docs/DESIGN.md 3 and 11, dev/changelog/1526). Each case is a decision a careless edit
would quietly undo:

  * The page is the league page's shell and the shared components, not page-local lookalikes:
    `.page-head`, the closed About, `.gd-statezone`, `.gd-sections` of `.card`s, and every
    table a `table.tbl.tbl-cards` inside `.table-scroll` whose cells carry `data-label`, so a
    phone gets cards.
  * The cards come in the approved order.
  * The Settings card is the Settings page's own row (`.frow`) and names the same keys.
  * The script asks with `confirmModal`, never the native dialog, and leaves the page only
    through links.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_iptv_output_page_conformance
"""
import os
import re
import sys
import unittest
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.output_feed import FeedCase, feed_cfg  # noqa: E402

import app.output_feed as feed  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Tables(HTMLParser):
    """Every <table>'s classes, whether a .table-scroll wraps it, and its body cells'
    data-label attributes."""

    def __init__(self):
        super().__init__()
        self.tables = []
        self._stack = []
        self._in_body = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'div':
            self._stack.append(attrs.get('class') or '')
        elif tag == 'table':
            self.tables.append({'class': attrs.get('class') or '',
                                'scroll': bool(self._stack) and 'table-scroll' in self._stack[-1],
                                'cells': []})
        elif tag == 'tbody':
            self._in_body = True
        elif tag == 'td' and self._in_body and 'colspan' not in attrs:
            self.tables[-1]['cells'].append(attrs.get('data-label'))

    def handle_endtag(self, tag):
        if tag == 'div' and self._stack:
            self._stack.pop()
        elif tag == 'tbody':
            self._in_body = False


class IptvOutputPageConformanceTests(FeedCase):
    def render(self, **block):
        if block:
            self.t.sandbox_config(feed_cfg(**block))
        return self.client.get('/iptv-output').get_data(as_text=True)

    def test_the_shell_is_the_shared_one(self):
        html = self.render()
        for needle in ('<div class="page-head">', '<details class="page-about">',
                       'class="gd-statezone" id="of-state"', 'class="gd-sections" id="of-sections"',
                       'class="gd-actionbar"'):
            self.assertIn(needle, html)
        # The Beta mark after the name, still being built (dev/changelog/1635).
        self.assertRegex(html, r'<h1>IPTV Output <span class="badge b-sched tip-plain beta-mark"[^>]*>Beta</span></h1>')
        self.assertNotIn('page-sub', html)

    def test_the_cards_come_in_the_approved_order(self):
        self.session(feed.feed_id_for_channel(self.alpha.id))
        html = self.render()
        cards = re.findall(r'<div class="card[^"]*" id="(of-[a-z]+)"', html)
        self.assertEqual(cards, ['of-connect', 'of-watching', 'of-channels', 'of-recent',
                                 'of-accounts', 'of-settings'])

    def test_every_table_is_the_shared_table_with_its_phone_form(self):
        self.session(feed.feed_id_for_channel(self.alpha.id))
        for block in ({}, {'mode': 'redirect'}):
            parser = _Tables()
            parser.feed(self.render(**block))
            self.assertGreaterEqual(len(parser.tables), 2)
            for table in parser.tables:
                self.assertIn('tbl', table['class'].split())
                self.assertIn('tbl-cards', table['class'].split())
                self.assertTrue(table['scroll'], 'a table outside .table-scroll')
                self.assertTrue(table['cells'])
                self.assertNotIn(None, table['cells'], 'a cell with no data-label')

    def test_the_connection_details_are_two_tabs_xtream_first(self):
        html = self.render()
        tabs = re.findall(r'data-act="tab" data-tab="(\w+)"', html)
        self.assertEqual(tabs, ['xtream', 'playlist'])
        self.assertRegex(html, r'class="tab active"[^>]*data-tab="xtream"')
        self.assertRegex(html, r'data-pane="playlist" hidden')

    def test_the_settings_card_is_the_settings_pages_own_row(self):
        html = self.render()
        settings = html[html.index('id="of-settings"'):]
        self.assertEqual(settings.count('<div class="frow"'), 9)
        self.assertEqual(settings.count('class="fl-desc"'), 9)
        self.assertNotIn('data-tip', settings.split('<script')[0])

    def test_off_is_a_banner_with_the_one_primary_action(self):
        html = self.render(enabled=False)
        self.assertIn('notice-banner notice-banner-warn of-banner', html)
        self.assertEqual(html.count('btn btn-primary'), 1)
        self.assertNotIn('gd-actionbar', html)

    def test_the_script_uses_the_shared_dialog_and_never_navigates_by_hand(self):
        with open(os.path.join(REPO, 'static', 'js', 'iptv-output.js'), encoding='utf-8') as fh:
            js = fh.read()
        self.assertIn('confirmModal(', js)
        self.assertNotRegex(js, r'(?<![A-Za-z])confirm\(')
        self.assertNotRegex(js, r'\balert\(')
        self.assertNotIn('location.href =', js)
        self.assertNotIn('innerHTML = \'\'', js)

    def test_no_emoji_or_raw_glyph_stands_in_for_an_icon(self):
        html = self.render()
        body = html[html.index('id="of-state"'):html.rindex('<script src')]
        self.assertNotRegex(body, '[←-⇿✀-➿\U0001f300-\U0001faff]')


if __name__ == '__main__':
    unittest.main()
