"""The account page's Usage section and its Content card against DESIGN.md §17.3/§17.7
(dev/changelog/1031).

What the page must keep doing:

  * **Five sections**, Usage after Content, and a layout saved before Usage existed still
    shows it there - not dropped, not buried at the end.
  * **The Usage card is the one component** `/accounts` draws for a one-account install,
    narrowed to this account even when there are others.
  * **Its numbers are the ledger's sums for the window**, and the window is the same
    `?w=` / saved-pref / All time pick the list page uses, under the same pref key. An
    unknown `?w=` is a 400 here too.
  * **Two Recorded times on one page say which is which**: the Content card's is all time,
    the Usage card's follows its window, and each tooltip says so.
  * **The Content card carries the "right now" numbers** from `current_stats()`, the twin
    of the list row's second line, with the health-band bar.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_account_page_usage
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import seed  # noqa: E402
from tests.test_accounts_stats_conformance import _StatsCase  # noqa: E402
from app import account_stats_view, db  # noqa: E402
from app.database import UserPref  # noqa: E402
from app.routes.accounts import ACCOUNT_SECTIONS  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')


def _card(html, section):
    """The markup of one `data-section` card, up to the next section's card."""
    start = html.index(f'data-section="{section}"')
    nxt = html.find('data-section="', start + 20)
    return html[start:nxt if nxt != -1 else len(html)]


def _srow(html, label):
    m = re.search(r'<div class="srow[^"]*"><span class="sk">' + re.escape(label)
                  + r'</span>(.*?)</div>', html, re.S)
    return m.group(1) if m else ''


def _text(fragment):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', fragment)).strip()


def _boot(html):
    """window.ACCOUNT_DETAIL's sections and sectionPref, as the page hands them to JS."""
    sections = json.loads(re.search(r'sections: (\[.*?\]),', html).group(1))
    pref = json.loads(re.search(r'sectionPref: (.*?),\n', html).group(1))
    return sections, pref


class SectionTests(_StatsCase):

    def test_usage_is_the_fifth_section_after_content(self):
        self.assertEqual(ACCOUNT_SECTIONS, ['details', 'content', 'usage', 'history', 'activity'])
        acc = seed.make_account(name='Alpha')
        db.session.commit()
        html = self.get(f'/accounts/{acc.id}')
        self.assertEqual(_boot(html)[0], ACCOUNT_SECTIONS)
        for section in ACCOUNT_SECTIONS:
            self.assertEqual(html.count(f'data-section="{section}"'), 1, section)
        self.assertLess(html.index('data-section="content"'), html.index('data-section="usage"'))
        self.assertLess(html.index('data-section="usage"'), html.index('data-section="history"'))
        # A card with a head, like its siblings - never a bare block in the reorderable list.
        self.assertIn('<h2>Usage</h2>', _card(html, 'usage'))

    @unittest.skipIf(shutil.which('node') is None or not os.path.isdir(JSDOM),
                     'node or jsdom not installed')
    def test_a_layout_saved_before_usage_existed_shows_it_after_content(self):
        """dev/changelog/455's layouts were saved with four sections. The shipped util.js,
        fed the page's own boot config, must put Usage at its default place rather than
        dropping it or appending it below Activity."""
        acc = seed.make_account(name='Alpha')
        db.session.add(UserPref(key='account_detail_sections', value=json.dumps(
            {'order': ['details', 'history', 'content', 'activity'], 'hidden': ['activity']})))
        db.session.commit()
        sections, pref = _boot(self.get(f'/accounts/{acc.id}'))
        self.assertNotIn('usage', pref['order'])
        script = (
            "const { JSDOM } = require(process.argv[1]);"
            "const fs = require('fs');"
            "const dom = new JSDOM('<!doctype html><head></head><body></body>', "
            "{ runScripts: 'outside-only' });"
            "dom.window.eval(fs.readFileSync(process.argv[2], 'utf8'));"
            "dom.window.initSectionLayout({ config: JSON.parse(process.argv[3]), "
            "saveUrl: '/x', names: {} });"
            "const css = Array.from(dom.window.document.querySelectorAll('style'))"
            ".map((s) => s.textContent).join('\\n');"
            "console.log(JSON.stringify(css));")
        proc = subprocess.run(
            ['node', '-e', script, JSDOM, os.path.join(REPO, 'static', 'js', 'util.js'),
             json.dumps({'sections': sections, 'sectionPref': pref})],
            capture_output=True, text=True, cwd=REPO, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        css = json.loads(proc.stdout)
        order = {m.group(1): int(m.group(2))
                 for m in re.finditer(r'\[data-section="(\w+)"\] \{ order: (\d+);', css)}
        # The saved order is kept; Usage goes in at its default index (third).
        self.assertEqual(sorted(order, key=order.get),
                         ['details', 'history', 'usage', 'content', 'activity'])
        self.assertNotIn('display: none', re.search(r'\[data-section="usage"\][^}]*}', css).group(0))


class WindowTests(_StatsCase):

    def setUp(self):
        super().setUp()
        self.acc = seed.make_account(name='Alpha')
        self.other = seed.make_account(name='Beta')
        self.seed_usage(self.acc, 1, hours=1, passed=3, failed=1)
        self.seed_usage(self.acc, 40, hours=2, passed=5)
        self.seed_usage(self.other, 1, hours=4)

    def usage(self, window=None):
        url = f'/accounts/{self.acc.id}' + (f'?w={window}' if window else '')
        return self.get(url)

    def test_the_usage_numbers_are_the_ledgers_sums_for_the_window(self):
        for window, capture, passed, failed in (('7d', '1h', '3', '1'),
                                                ('30d', '1h', '3', '1'),
                                                ('all', '3h', '8', '1')):
            with self.subTest(window=window):
                card = _card(self.usage(window), 'usage')
                self.assertEqual(_text(_srow(card, 'Recorded time')), capture)
                self.assertEqual(_text(_srow(card, 'Health checks passed')), passed)
                self.assertEqual(_text(_srow(card, 'Health checks failed')), failed)

    def test_the_card_is_this_accounts_only(self):
        """Beta recorded 4h. The account page is narrowed to its own account, so it gets
        the one-account Usage card and none of Beta's time - never the comparison."""
        html = self.usage('all')
        self.assertNotIn('id="acst-table"', html)
        self.assertEqual(_text(_srow(_card(html, 'usage'), 'Recorded time')), '3h')

    def test_the_content_cards_recorded_time_is_all_time_whatever_the_window(self):
        content = _card(self.usage('7d'), 'content')
        self.assertEqual(_text(_srow(content, 'Recorded time')), '3h')
        self.assertEqual(_text(_srow(content, 'Recordings')), '2')

    def test_each_recorded_time_says_which_one_it_is(self):
        # Rendered into an attribute, where the tooltip's `&#10;` line break is escaped.
        all_time = account_stats_view.ALL_TIME_NOTE.replace('&', '&amp;')
        window = account_stats_view.WINDOW_NOTE.replace('&', '&amp;')
        html = self.usage('7d')
        content = _srow(_card(html, 'content'), 'Recorded time')
        usage = _srow(_card(html, 'usage'), 'Recorded time')
        self.assertIn(all_time, content)
        self.assertNotIn(window, content)
        self.assertIn(window, usage)
        self.assertNotIn(all_time, usage)
        self.assertIn(all_time,
                      _srow(_card(html, 'content'), 'Recordings'))

    def test_an_unknown_window_is_a_400(self):
        self.assertEqual(self.client.get(f'/accounts/{self.acc.id}?w=bogus').status_code, 400)

    def test_the_saved_window_is_the_one_the_list_page_saves(self):
        db.session.add(UserPref(key=account_stats_view.WINDOW_PREF_KEY, value=json.dumps('7d')))
        db.session.commit()
        card = _card(self.usage(), 'usage')
        self.assertRegex(card, r'class="chip active"[^>]*data-win="7d"')
        self.assertEqual(_text(_srow(card, 'Recorded time')), '1h')

    def test_the_chips_land_back_on_their_section(self):
        """A chip reloads the page; without the fragment it lands at the top, two cards
        above the Usage card (and on /accounts, a whole list above the stats)."""
        self.assertIn('href="?w=7d#usage-card"', self.usage())
        self.assertIn('href="?w=7d#acct-stats"', self.get('/accounts'))

    def test_a_missing_account_still_goes_back_to_the_list(self):
        resp = self.client.get('/accounts/9999')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers['Location'].endswith('/accounts'))


class ContentCardTests(_StatsCase):

    def test_the_content_card_carries_the_current_numbers(self):
        acc = seed.make_account(name='Alpha')
        good = seed.make_channel(acc, name='good')
        bad = seed.make_channel(acc, name='bad')
        good.health_score = 95.0
        bad.health_score = 10.0
        seed.make_channel(acc, name='untested')
        seed.make_group(name='G', members=[good, bad])
        db.session.commit()
        from app.config import load_config
        from app import account_stats
        cur = account_stats.current_stats([acc.id], load_config())[acc.id]
        content = _card(self.get(f'/accounts/{acc.id}'), 'content')
        self.assertEqual(_text(_srow(content, 'In a channel group')), str(cur['in_group']))
        self.assertEqual(_text(_srow(content, 'Recording on')), str(cur['recording_memberships']))
        self.assertEqual(_text(_srow(content, 'Guide rows it can feed')),
                         f"{cur['guide_rows_fed']} of {cur['guide_rows_total']}")
        self.assertEqual(_text(_srow(content, 'Average health score')),
                         str(round(cur['avg_score'])))
        self.assertEqual(_text(_srow(content, 'Failing right now')), str(cur['failing']))
        self.assertGreater(cur['failing'], 0)
        self.assertIn('class="acst-bandbar"', content)
        self.assertIn('Tested channels by health band', content)

    def test_in_a_group_links_to_the_search_and_failing_does_not(self):
        """Failing counts losing streaks as well as the failing band, and no channel-search
        filter finds exactly that set - a link would open a different number."""
        acc = seed.make_account(name='Alpha')
        ch = seed.make_channel(acc, name='bad')
        ch.health_score = 5.0
        seed.make_group(name='G', members=[ch])
        db.session.commit()
        content = _card(self.get(f'/accounts/{acc.id}'), 'content')
        group_row = _srow(content, 'In a channel group')
        self.assertIn('f.group=__any__', group_row)
        self.assertIn(f'f.acct={acc.id}', group_row)
        self.assertNotIn('<a ', _srow(content, 'Failing right now'))

    def test_an_untested_account_says_so_and_zeros_are_faint(self):
        acc = seed.make_account(name='Alpha')
        seed.make_channel(acc, name='untested')
        db.session.commit()
        content = _card(self.get(f'/accounts/{acc.id}'), 'content')
        self.assertIn('Not tested yet', content)
        self.assertIn('text-faint">-', _srow(content, 'Average health score'))
        for label in ('In a channel group', 'Recording on', 'Failing right now'):
            row = _srow(content, label)
            self.assertIn('text-faint">0', row, label)
            self.assertNotIn('<a ', row, label)


if __name__ == '__main__':
    unittest.main()
