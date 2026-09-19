"""The account stats on /accounts against DESIGN.md §17.7 (dev/changelog/1029).

What the section must keep doing, each a decision a careless edit would quietly undo:

  * **Two renderings, chosen by account count.** Two or more accounts get the comparison;
    one account gets the account page's Usage card in its place (the same macro), and no
    accounts get nothing under the list's own empty state.
  * **Outside #acct-live.** The list swaps that region every minute; the stats must not be
    recomputed and redrawn with it.
  * **Every windowed number is the ledger's sum for that window**, and the window is
    `?w=`, then the saved pref, then All time. An unknown `?w=` is a 400.
  * **No verdicts.** Facts side by side; the page never tells the user which account to
    keep. A zero is faint and never a link; a pass rate with no checks is "-", never 0%.
  * **The list row's second line** carries the current-state numbers from the same
    `current_stats()` call, with the definitions on its labels.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_accounts_stats_conformance
"""
import json
import os
import re
import sys
import unittest
from datetime import datetime, time, timedelta, timezone
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import account_stats, account_stats_view, db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import UserPref  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Words that turn a fact into a verdict about which account to drop (dev/changelog/1027).
VERDICT_WORDS = ('drop', 'worst', 'best', 'consider', 'keep', 'worth', 'recommend')


class _Regions(HTMLParser):
    """Where things sit: whether #acct-stats opens inside #acct-live, and the text of the
    section on its own."""

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.live_at = None
        self.stats_inside_live = None
        self.stats_at = None
        self.stats_text = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('div', 'section', 'span', 'p', 'table', 'tr', 'td', 'th', 'a', 'thead',
                   'tbody', 'svg', 'button', 'strong', 'i', 'circle', 'h2'):
            if a.get('id') == 'acct-stats':
                # Any copy inside the live region counts, even with another outside it.
                self.stats_inside_live = bool(self.stats_inside_live) or self.live_at is not None
                self.stats_at = self.depth
            if a.get('id') == 'acct-live':
                self.live_at = self.depth
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in ('div', 'section', 'span', 'p', 'table', 'tr', 'td', 'th', 'a', 'thead',
                   'tbody', 'svg', 'button', 'strong', 'i', 'circle', 'h2'):
            self.depth -= 1
            if self.live_at is not None and self.depth == self.live_at:
                self.live_at = None
            if self.stats_at is not None and self.depth == self.stats_at:
                self.stats_at = None

    def handle_data(self, data):
        if self.stats_at is not None:
            self.stats_text.append(data)


def _section(html):
    start = html.index('id="acct-stats"')
    return html[start:html.index('</section>', start)]


class _StatsCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.today = account_stats.today_local()
        self.now = datetime.utcnow()

    def tearDown(self):
        account_stats.wait_for_catch_up(10)
        self.t.cleanup()

    def get(self, url='/accounts'):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:500])
        return resp.get_data(as_text=True)

    def seed_usage(self, account, days_ago, hours=1, passed=0, failed=0, stalls=0):
        """One recording's segment and some health checks on `account`, `days_ago` local
        days back, at noon so no timezone can move it across a day boundary."""
        ch = seed.make_channel(account, name=f'{account.name} ch')
        tz = account_stats.tz_utils.get_display_tz()
        noon = datetime.combine(self.today - timedelta(days=days_ago), time(12), tzinfo=tz)
        when = noon.astimezone(timezone.utc).replace(tzinfo=None)
        if hours:
            rec = seed.make_recording(status='COMPLETED', channel_id=ch.id, start_time=when,
                                      stop_time=when + timedelta(hours=hours))
            seed.make_segment(rec, ch, when, when + timedelta(hours=hours), 0,
                              stall_count=stalls)
        for status, n in (('COMPLETED', passed), ('FAILED', failed)):
            for _ in range(n):
                seed.make_channel_test(ch, status=status, test_started_at=when,
                                       test_ended_at=when + timedelta(seconds=30))
        db.session.commit()
        return ch


class RenderingByAccountCountTests(_StatsCase):

    def test_two_accounts_render_the_comparison(self):
        seed.make_account(name='Alpha')
        seed.make_account(name='Beta')
        db.session.commit()
        html = self.get()
        self.assertIn('id="acct-stats"', html)
        self.assertIn('id="acst-table"', html)
        self.assertNotIn('id="usage-card"', html)
        for title in ('Recorded time', 'Health checks passed', 'In the TV Guide',
                      'In a channel group'):
            self.assertIn(f'<h2>{title}</h2>', _section(html))

    def test_one_account_renders_the_usage_card_in_its_place(self):
        """dev/changelog/1027: with one account there is nothing to compare,
        so /accounts shows that account's Usage card - the same macro the account page uses."""
        seed.make_account(name='Only')
        db.session.commit()
        html = self.get()
        section = _section(html)
        self.assertIn('id="usage-card"', section)
        self.assertNotIn('id="acst-table"', section)
        self.assertNotIn('class="acst-pie"', section)

    def test_no_accounts_renders_no_section(self):
        html = self.get()
        self.assertNotIn('id="acct-stats"', html)
        self.assertIn('No accounts yet', html)

    def test_the_section_is_outside_the_live_region(self):
        seed.make_account(name='Alpha')
        seed.make_account(name='Beta')
        db.session.commit()
        p = _Regions()
        p.feed(self.get())
        self.assertIs(p.stats_inside_live, False,
                      '#acct-stats must not sit inside #acct-live, which swaps every minute')


class WindowTests(_StatsCase):

    def setUp(self):
        super().setUp()
        self.a = seed.make_account(name='Alpha')
        self.b = seed.make_account(name='Beta')
        db.session.commit()
        # Alpha: 1 h and 3/1 checks two days ago, 2 h and 1/4 checks 20 days ago,
        # 4 h 60 days ago. Beta: 1 h, 2 stalls, 50 days ago.
        self.seed_usage(self.a, 2, hours=1, passed=3, failed=1)
        self.seed_usage(self.a, 20, hours=2, passed=1, failed=4)
        self.seed_usage(self.a, 60, hours=4)
        self.seed_usage(self.b, 50, hours=1, stalls=2)

    def rows(self, html):
        """{account name: the row's data-* numbers} from the comparison table."""
        out = {}
        for m in re.finditer(r'<tr data-id="\d+"([^>]*)>', html):
            attrs = dict(re.findall(r'data-([a-z]+)="([^"]*)"', m.group(1)))
            out[attrs['name']] = attrs
        return out

    def test_every_window_shows_the_ledgers_sum_for_that_window(self):
        ids = [self.a.id, self.b.id]
        for window in account_stats.WINDOWS:
            with self.subTest(window=window):
                html = self.get(f'/accounts?w={window}')
                expected = account_stats.windowed_stats(ids, window, self.today)
                rows = self.rows(html)
                for acc in (self.a, self.b):
                    got = rows[acc.name.lower()]
                    want = expected[acc.id]
                    self.assertEqual(float(got['capture']), want['capture_seconds'])
                    self.assertEqual(int(got['recordings']), want['recordings'])
                    self.assertEqual(int(got['stalls']), want['stalls'])
                    self.assertEqual(int(got['passed']), want['checks_passed'])
                    self.assertEqual(int(got['failed']), want['checks_failed'])

    def test_the_windows_really_differ(self):
        """So the test above is not passing on four identical renders."""
        seven = self.rows(self.get('/accounts?w=7d'))['alpha']
        every = self.rows(self.get('/accounts?w=all'))['alpha']
        self.assertEqual(float(seven['capture']), 3600)
        self.assertEqual(float(every['capture']), 7 * 3600)
        self.assertEqual(float(self.rows(self.get('/accounts?w=30d'))['beta']['capture']), 0)

    def test_the_rendered_numbers_are_the_formatted_sums(self):
        html = self.get('/accounts?w=30d')
        section = _section(html)
        self.assertIn('>3h<', section)          # Alpha's 1 h + 2 h
        self.assertIn('>44%<', section)         # Alpha: 4 passed of 9
        self.assertIn('All accounts', section)

    def test_the_active_chip_is_marked_and_the_scope_line_names_the_window(self):
        section = _section(self.get('/accounts?w=90d'))
        active = re.findall(r'<a class="chip active"[^>]*data-win="([a-z0-9]+)"', section)
        self.assertEqual(active, ['90d'])
        self.assertIn('in the last 90 days', section)

    def test_an_unknown_window_is_a_400(self):
        resp = self.client.get('/accounts?w=bogus')
        self.assertEqual(resp.status_code, 400)

    def test_the_saved_window_is_used_without_a_parameter(self):
        db.session.add(UserPref(key=account_stats_view.WINDOW_PREF_KEY, value=json.dumps('7d')))
        db.session.commit()
        section = _section(self.get())
        self.assertRegex(section, r'<a class="chip active"[^>]*data-win="7d"')

    def test_the_parameter_beats_the_saved_window(self):
        db.session.add(UserPref(key=account_stats_view.WINDOW_PREF_KEY, value=json.dumps('7d')))
        db.session.commit()
        section = _section(self.get('/accounts?w=all'))
        self.assertRegex(section, r'<a class="chip active"[^>]*data-win="all"')

    def test_a_garbage_saved_window_falls_back_to_all_time(self):
        db.session.add(UserPref(key=account_stats_view.WINDOW_PREF_KEY, value='"fortnight"'))
        db.session.commit()
        section = _section(self.get())
        self.assertRegex(section, r'<a class="chip active"[^>]*data-win="all"')

    def test_the_default_is_all_time(self):
        section = _section(self.get())
        self.assertRegex(section, r'<a class="chip active"[^>]*data-win="all"')

    def test_an_empty_window_says_so_inside_its_card(self):
        """Nobody passed a check in the last 7 days... except Alpha, two days ago. In the
        last 7 days nothing at all happened to Beta, and a window where nothing was
        recorded by anyone draws a sentence inside the card, never a blank box."""
        seven = _section(self.get('/accounts?w=7d'))
        self.assertIn('class="acst-plot"', seven)
        db.session.query(account_stats.AccountStatDay).delete()
        db.session.commit()
        # The ledger's watermark has passed the rows above, so emptying it leaves the
        # windows empty rather than refolding them.
        empty = _section(self.get('/accounts?w=7d'))
        self.assertIn('Nothing was recorded in the last 7 days.', empty)
        self.assertIn('No health checks in the last 7 days.', empty)
        self.assertIn('No health check passed in the last 7 days.', empty)
        self.assertNotIn('class="acst-plot"', empty)


class FactsNotVerdictsTests(_StatsCase):

    def setUp(self):
        super().setUp()
        self.a = seed.make_account(name='Alpha')
        self.b = seed.make_account(name='Beta')
        db.session.commit()
        self.seed_usage(self.a, 1, hours=1, passed=2, failed=2)

    def test_a_zero_is_faint_and_never_a_link(self):
        section = _section(self.get())
        beta = section[section.index('<tr data-id="%d"' % self.b.id):]
        beta = beta[:beta.index('</tr>')]
        cells = re.findall(r'<td class="num"[^>]*>(.*?)</td>', beta, re.S)
        self.assertEqual(len(cells), 7)
        for cell in cells[:-1]:
            self.assertIn('<span class="text-faint">0</span>', cell)
            self.assertNotIn('<a ', cell)

    def test_no_checks_is_a_dash_not_zero_percent(self):
        section = _section(self.get())
        beta = section[section.index('<tr data-id="%d"' % self.b.id):]
        beta = beta[:beta.index('</tr>')]
        rate = re.findall(r'<td class="num" data-label="Pass rate">(.*?)</td>', beta, re.S)[0]
        self.assertIn('<span class="text-faint">-</span>', rate)
        self.assertNotIn('0%', rate)

    def test_the_section_carries_no_verdict_and_no_em_dash(self):
        p = _Regions()
        p.feed(self.get())
        text = ' '.join(p.stats_text).lower()
        self.assertTrue(text.strip())
        for word in VERDICT_WORDS:
            self.assertNotRegex(text, rf'\b{word}\b', f'verdict word {word!r} on the page')
        self.assertNotIn('—', text)

    def test_the_template_and_script_carry_no_em_dash(self):
        for path in ('templates/_account_stats.html', 'static/js/account-stats.js',
                     'app/account_stats_view.py'):
            with open(os.path.join(REPO, path), encoding='utf-8') as f:
                self.assertNotIn('—', f.read(), path)

    def test_every_number_in_the_table_carries_its_definition(self):
        section = _section(self.get())
        alpha = section[section.index('<tr data-id="%d"' % self.a.id):]
        alpha = alpha[:alpha.index('</tr>')]
        self.assertEqual(alpha.count('data-tip='), 7)

    def test_the_totals_row_is_last(self):
        section = _section(self.get())
        body = section[section.index('<tbody>'):section.index('</tbody>')]
        rows = re.findall(r'<tr[^>]*>', body)
        self.assertIn('acst-total', rows[-1])


class RowSecondLineTests(_StatsCase):

    def test_the_row_carries_the_current_numbers_from_current_stats(self):
        a = seed.make_account(name='Alpha')
        ch1 = seed.make_channel(a, name='One', health_score=95.0)
        ch2 = seed.make_channel(a, name='Two', health_score=20.0)
        seed.make_channel(a, name='Three')
        seed.make_group(name='G', members=[ch1, ch2])
        db.session.commit()
        cur = account_stats.current_stats([a.id], load_config())[a.id]
        html = self.get()
        row = html[html.index('data-id="%d"' % a.id):html.index('id="acct-stats"')]
        more = row[row.index('class="a-more"'):]
        self.assertIn(f'In a group</span>{cur["in_group"]}', more)
        self.assertIn(f'Recording on</span>{cur["recording_memberships"]}', more)
        self.assertIn('Failing now', more)
        self.assertEqual(cur['tested'], 2)
        # Two tested channels: one Great, one Poor, each drawn and named in the band bar.
        self.assertIn('class="stackbar-seg hb-great"', more)
        self.assertIn('class="stackbar-seg hb-poor"', more)
        self.assertEqual(cur['failing'], 1)
        self.assertRegex(more, r'Failing now</span><span class="hb-poor hb-text">1</span>')

    def test_the_second_line_labels_carry_definitions_and_sit_inside_the_live_region(self):
        seed.make_account(name='Alpha')
        db.session.commit()
        html = self.get()
        live = html[html.index('id="acct-live"'):html.index('id="acct-stats"')]
        self.assertIn('class="a-more"', live)
        self.assertIn('class="a-bands"', live)
        labels = re.findall(r'<span class="a-k" data-tip="[^"]+">([^<]+)</span>', live)
        self.assertEqual(labels, ['In a group', 'Recording on', 'Guide rows', 'With EPG',
                                  'Avg score', 'Failing now', 'Health bands'])

    def test_an_untested_account_says_so_rather_than_drawing_an_empty_bar(self):
        seed.make_account(name='Alpha')
        db.session.commit()
        html = self.get()
        self.assertIn('Not tested yet', html)
        self.assertNotIn('class="stackbar-seg', html)


class ViewHelperTests(unittest.TestCase):
    """The pieces section_context() is built from, without a page around them."""

    def test_axis_labels_per_unit(self):
        from datetime import date
        d = date(2026, 8, 9)
        self.assertEqual(account_stats_view._bucket_labels('day', d), ('Aug 9', 'Aug 9'))
        self.assertEqual(account_stats_view._bucket_labels('week', d), ('Aug 9', 'Week of Aug 9'))
        self.assertEqual(account_stats_view._bucket_labels('month', d), ('Aug', 'Aug 2026'))
        self.assertEqual(account_stats_view._bucket_labels('quarter', date(2026, 7, 1)),
                         ('Q3 26', 'Q3 2026'))
        self.assertEqual(account_stats_view._bucket_labels('year', d), ('2026', '2026'))
        with self.assertRaises(ValueError):
            account_stats_view._bucket_labels('fortnight', d)

    def _trend(self, n):
        from datetime import date
        return {'unit': 'week', 'buckets': [
            {'start': date(2026, 1, 4) + timedelta(weeks=i), 'end': None,
             'values': {1: {'capture_seconds': 3600 * (i % 3)}}} for i in range(n)]}

    def test_columns_thin_the_axis_past_eight_and_always_label_the_last(self):
        trend = self._trend(20)
        c = account_stats_view.columns(
            trend, [{'name': 'A', 'color': '#fff',
                     'values': [b['values'][1]['capture_seconds'] for b in trend['buckets']]}],
            account_stats_view.fmt_hours, 'empty')
        labelled = [i for i, label in enumerate(c['axis']) if label]
        self.assertLessEqual(len(labelled), 9)
        self.assertEqual(labelled[-1], 19)
        self.assertTrue(c['cols'][0]['zero'], 'a bucket with nothing is drawn as a stub')
        self.assertEqual(c['peak'], '2.0 h')

    def test_columns_with_nothing_in_them_are_the_empty_sentence(self):
        trend = self._trend(3)
        c = account_stats_view.columns(trend, [{'name': 'A', 'color': '#fff',
                                                'values': [0, 0, 0]}],
                                       account_stats_view.fmt_hours, 'Nothing was recorded yet.')
        self.assertEqual(c, {'empty': 'Nothing was recorded yet.'})

    def test_a_tiny_share_reads_under_one_percent_not_zero(self):
        class A:
            def __init__(self, i):
                self.id, self.name, self.color = i, f'a{i}', '#123456'
        p = account_stats_view.pie('T', 'now', [A(1), A(2)], {1: 1, 2: 999},
                                   account_stats_view.fmt_count, 'none')
        self.assertEqual([x['share'] for x in p['parts']], ['<1%', '100%'])
        self.assertEqual(len(p['arcs']), 2)


if __name__ == '__main__':
    unittest.main()
