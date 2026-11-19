"""A league page a page at a time: Upcoming games and On your IPTV output (mockup 57).

A page of games ends at the end of a day once it holds sports_view.PAGE_SIZE, so no day is split
and today's games are all on page 1. The IPTV output card is filtered by team first and then paged,
with the league's own row on every page. Every link keeps the other card's page and filter.
Throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_league_paging
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_recording  # noqa: E402

from app import db, sports_data, sports_view  # noqa: E402
from app.database import (  # noqa: E402
    RecordingSports, SportsEvent, SportsFollow, SportsLeague, SportsTeam,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'sports_league_paging.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
ZONE = ZoneInfo('America/New_York')
#: Games on the next four days, by day: 30 + (3 + 24) + 10, so whole-day pages hold 30, 27 and 10.
PER_DAY = (30, 3, 24, 10)
#: 60 teams: page 25, 25, 10. Two have "oh" in them in different cases.
TEAM_NAMES = {1000: 'Ohio State Buckeyes', 1001: 'Miami (OH) RedHawks',
              **{1000 + k: f'Team {k:02d}' for k in range(2, 60)}}


def section(html, name):
    start = html.index(f'data-section="{name}"')
    m = re.compile(r'<div class="card[ "]').search(html, start)
    return html[start:m.start()]


def rows(part):
    return re.findall(r'<tr>(.*?)</tr>', part, re.S)[1:]


def days(part):
    return re.findall(r'<span class="day">([^<]+)</span>', part)


def games(part):
    """The TheSportsDB event id of each game row, from its View on TheSportsDB link."""
    return [int(x) for x in re.findall(r'thesportsdb\.com/event/(\d+)"', part)]


def pager_text(part):
    m = re.search(r'sp-pager-n">([^<]+)<', part)
    return m.group(1) if m else None


def next_link(part):
    m = re.search(r'<a class="btn btn-sm sp-next" href="([^"]+)"', part)
    return m.group(1).replace('&amp;', '&') if m else None


class DayPagesTests(unittest.TestCase):
    def test_a_page_runs_on_to_the_end_of_its_day(self):
        items = ['a'] * 30 + ['b'] * 3 + ['c'] * 24 + ['d'] * 10
        pages = sports_view.day_pages(items, lambda x: x, size=25)
        self.assertEqual([len(p) for p in pages], [30, 27, 10])

    def test_no_day_is_on_two_pages_and_every_item_is_on_one(self):
        items = [d for d, n in zip('abcdefg', (5, 40, 1, 1, 26, 7, 30)) for _ in range(n)]
        pages = sports_view.day_pages(items, lambda x: x, size=25)
        self.assertEqual(sum(pages, []), items)
        for before, after in zip(pages, pages[1:]):
            self.assertNotEqual(before[-1], after[0])
        self.assertTrue(all(len(p) >= 25 for p in pages[:-1]))

    def test_nothing_is_one_empty_page(self):
        self.assertEqual(sports_view.day_pages([], lambda x: x), [[]])


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.t.sandbox_config({'display': {'timezone': 'America/New_York', 'time_format': '12h'}})
        self.client = self.t.client
        self.league = SportsLeague(tsdb_id=9001, name='Pretend League', sport='Pretend Ball',
                                   current_season='2099', match_terms=json.dumps(['Pretend League']))
        db.session.add(self.league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=self.league.id, team_tsdb_id=0, rule_id=0))
        for tsdb_id, name in TEAM_NAMES.items():
            db.session.add(SportsTeam(tsdb_id=tsdb_id, full_name=name, names=json.dumps([name])))
        tomorrow = datetime.now(ZONE).date() + timedelta(days=1)
        self.events = []
        n = 0
        for d, count in enumerate(PER_DAY):
            for i in range(count):
                local = datetime.combine(tomorrow + timedelta(days=d), time(12, 0), tzinfo=ZONE) + timedelta(minutes=i)
                start = local.astimezone(ZoneInfo('UTC')).replace(tzinfo=None)
                home, away = 1000 + (2 * n) % 60, 1001 + (2 * n) % 60
                e = SportsEvent(tsdb_id=7000 + n, league_id=self.league.id, name=f'Game {n:03d}', season='2099',
                                home_team_id=home, away_team_id=away, home_name=TEAM_NAMES[home],
                                away_name=TEAM_NAMES[away], start_at=start, local_date=local.date())
                db.session.add(e)
                self.events.append(e)
                n += 1
        db.session.commit()

    def page(self, query=''):
        r = self.client.get(f'/sports/{self.league.id}{query}')
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)


class UpcomingPagingTests(_Case):
    def test_page_one_is_the_first_day_whole_and_the_heading_counts_every_game(self):
        html = self.page()
        up = section(html, 'upcoming')
        self.assertEqual(len(rows(up)), 30)
        self.assertEqual(set(days(up)), {'Tomorrow'})
        self.assertIn(f'<span class="cnt">{sum(PER_DAY)}</span>', up)
        self.assertEqual(pager_text(up), 'Page 1 of 3: Tomorrow')
        self.assertIn('<button class="btn btn-sm sp-prev" type="button" disabled>', up)

    def test_walking_next_shows_every_game_once_and_splits_no_day(self):
        query, seen, pages = '', [], []
        for _ in range(10):
            up = section(self.page(query), 'upcoming')
            seen += games(up)
            pages.append(days(up))
            link = next_link(up)
            if not link:
                break
            query = link[link.index('?'):]
        self.assertEqual([len(p) for p in pages], [30, 27, 10])
        self.assertEqual(sorted(seen), sorted(e.tsdb_id for e in self.events))
        for before, after in zip(pages, pages[1:]):
            self.assertNotIn(before[-1], after)
        self.assertRegex(pager_text(up), r'^Page 3 of 3: \w{3} \w{3} \d+$')

    def test_a_page_past_the_end_is_the_last_page_and_a_bad_number_is_the_first(self):
        self.assertEqual(len(rows(section(self.page('?page=99'), 'upcoming'))), 10)
        self.assertEqual(len(rows(section(self.page('?page=x'), 'upcoming'))), 30)

    def test_a_game_on_a_later_page_keeps_its_recording_and_the_rest_keep_record(self):
        later = self.events[40]
        rec = make_recording(status='SCHEDULED', name='Game 040', start_time=later.start_at,
                             stop_time=later.start_at + timedelta(hours=3))
        db.session.add(RecordingSports(recording_id=rec.id, tsdb_event_id=later.tsdb_id, league_name=self.league.name,
                                       event_name=later.name, strength='strong', airing='first', reason='x',
                                       linked_at=datetime.utcnow(), updated_at=datetime.utcnow()))
        db.session.commit()
        up = section(self.page('?page=2'), 'upcoming')
        self.assertIn(later.tsdb_id, games(up))
        self.assertIn(f'data-act="edit-rec" data-rec="{rec.id}"', up)
        self.assertEqual(up.count('Manage recording'), 1)
        self.assertEqual(up.count('data-act="record"'), 26)


class FeedPagingTests(_Case):
    def test_the_league_row_is_on_every_page_with_25_teams_a_page(self):
        # The league's Up next row and its league row lead every page (dev/changelog/1614).
        feed = section(self.page(), 'feed')
        self.assertEqual(len(rows(feed)), 27)
        self.assertIn('data-act="up-next"', rows(feed)[0])
        self.assertIn('data-team="0"', rows(feed)[1])
        self.assertEqual(pager_text(feed), 'Page 1 of 3: Miami (OH) RedHawks to Team 24')
        feed = section(self.page('?feed_page=3'), 'feed')
        self.assertEqual(len(rows(feed)), 12)
        self.assertIn('data-act="up-next"', rows(feed)[0])
        self.assertIn('data-team="0"', rows(feed)[1])

    def test_the_filter_comes_before_the_pages_and_ignores_case(self):
        feed = section(self.page('?feed_page=3&feed_q=oH'), 'feed')
        self.assertEqual(re.findall(r'class="rl-prog"><span class="t">([^<]+)<', feed),
                         ['Miami (OH) RedHawks', 'Ohio State Buckeyes'])
        self.assertNotIn('data-team="0"', feed)
        self.assertIsNone(pager_text(feed))
        self.assertIn('id="sp-feed-q" value="oH"', feed)

    def test_the_league_row_stays_when_the_filter_names_the_league(self):
        feed = section(self.page('?feed_q=pretend'), 'feed')
        self.assertEqual(len(rows(feed)), 2, 'the league row and its Up next row both carry its name')
        self.assertIn('data-team="0"', feed)
        self.assertIn('data-act="up-next"', feed)

    def test_a_filter_nothing_matches_says_so(self):
        feed = section(self.page('?feed_q=zzz'), 'feed')
        self.assertIn('Nothing here for that filter.', feed)
        self.assertIn('id="sp-feed-q"', feed)

    def test_each_card_keeps_the_others_page_and_filter(self):
        html = self.page('?page=2&feed_page=2&feed_q=team')
        self.assertEqual(next_link(section(html, 'upcoming')),
                         f'/sports/{self.league.id}?page=3&feed_page=2&feed_q=team')
        self.assertEqual(next_link(section(html, 'feed')),
                         f'/sports/{self.league.id}?page=2&feed_page=3&feed_q=team')


class ShortListTests(_Case):
    def test_a_league_that_fits_on_a_page_has_no_pager_and_no_filter(self):
        SportsEvent.query.filter(SportsEvent.id.notin_([e.id for e in self.events[:4]])).delete(synchronize_session=False)
        db.session.commit()
        html = self.page()
        self.assertNotIn('sp-pager', html)
        self.assertNotIn('id="sp-feed-q"', html)

    def test_the_scope_chips_go_to_page_one_and_keep_the_feed_card(self):
        sports_data.set_following(self.league.id, [1000 + k for k in range(2, 40)])
        html = self.page('?scope=all&page=2&feed_page=2&feed_q=team')
        self.assertIn(f'href="/sports/{self.league.id}?feed_page=2&amp;feed_q=team">Your teams', html)
        self.assertIn(f'href="/sports/{self.league.id}?scope=all&amp;feed_page=2&amp;feed_q=team">All', html)


@unittest.skipIf(shutil.which('node') is None or not os.path.isdir(JSDOM), 'node or jsdom not installed')
class PagerScriptTests(unittest.TestCase):
    """static/js/sports.js over the rendered page: a pager link or the filter puts the address in the
    bar and swaps only that card's list, so the timed refresh, which reads the address, keeps it."""

    @classmethod
    def setUpClass(cls):
        case = _Case('run')
        case.setUp()
        try:
            league_id = case.league.id
            start = f'/sports/{league_id}?feed_page=2'
            html = case.page('?feed_page=2')
        finally:
            case.doCleanups()
        cls.base = f'/sports/{league_id}'
        with tempfile.TemporaryDirectory() as tmp:
            for name, body in (('league.html', html), ('url.txt', start)):
                with open(os.path.join(tmp, name), 'w', encoding='utf-8') as fh:
                    fh.write(body)
            proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO], capture_output=True, text=True,
                                  cwd=REPO, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
        cls.obs = json.loads(proc.stdout)

    def test_no_script_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_next_swaps_only_its_card_from_the_new_address(self):
        self.assertTrue(self.obs['games_next_prevented'])
        self.assertEqual(self.obs['games_next']['url'], f'{self.base}?feed_page=2&page=2')
        self.assertEqual(self.obs['games_next']['swaps'],
                         [{'selectors': ['#sp-upcoming'], 'url': f'{self.base}?feed_page=2&page=2'}])
        self.assertEqual(self.obs['feed_next']['url'], f'{self.base}?feed_page=3&page=2')
        self.assertEqual(self.obs['feed_next']['swaps'][0]['selectors'], ['#sp-feed-list'])

    def test_a_new_tab_click_is_left_to_the_browser(self):
        self.assertFalse(self.obs['ctrl_click_prevented'])
        self.assertEqual(self.obs['ctrl_click_swaps'], 0)

    def test_typing_filters_once_after_a_pause_from_page_one(self):
        self.assertEqual(self.obs['typing_before_pause'], 0)
        f = self.obs['filtered']
        self.assertEqual(f['url'], f'{self.base}?feed_q=ohi&page=2')
        self.assertEqual(f['swaps'], [{'selectors': ['#sp-feed-list'], 'url': f'{self.base}?feed_q=ohi&page=2'}])
        self.assertTrue(f['has_text'])
        self.assertTrue(f['default_matches'], 'the field must stop holding the timed refresh off once it is applied')

    def test_clear_empties_the_field_and_drops_the_filter(self):
        c = self.obs['cleared']
        self.assertEqual(c['url'], f'{self.base}?page=2')
        self.assertEqual([s['selectors'] for s in c['swaps']], [['#sp-feed-list']])
        self.assertEqual(c['value'], '')
        self.assertFalse(c['has_text'])


if __name__ == '__main__':
    unittest.main()
