"""The order TheSportsDB's queue is worked in, what a page says about the line, and when the match
that follows an answer runs (app/sports_data.py, app/sports_view.py, app/scheduler.py), per
dev/docs/DESIGN-sports-data.md 4.1, dev/changelog/1574 to 1576.

Calls run over real HTTP against tests/support/fake_sportsdb.py. Throwaway temp SQLite DB - never
the live dvr.db.
  python3 -m unittest tests.test_sports_queue_order
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.fake_sportsdb import LEAGUE_ID, FakeSportsDB, game, team_row  # noqa: E402

from app import db, sports_data, sports_view  # noqa: E402
from app.database import (  # noqa: E402
    SPORTS_FETCH_OK, SPORTS_KIND_ROUND, SPORTS_KIND_TEAM, SportsFetch,
)

NOW = datetime(2099, 3, 2, 12, 0)


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.t.sandbox_config({'display': {'timezone': 'America/New_York', 'time_format': '12h'},
                               'sports_data': {'call_spacing_seconds': 60, 'call_jitter_seconds': 0}})
        self.fake = FakeSportsDB()
        self.addCleanup(self.fake.stop)
        p = patch.object(sports_data, 'BASE_URL', self.fake.base_url)
        p.start()
        self.addCleanup(p.stop)
        self.league_id = sports_data.follow_league(
            {'tsdb_id': LEAGUE_ID, 'name': 'Pretend League', 'sport': 'Pretend Ball', 'season': '2099',
             'terms': ['Pretend League']})
        # The follow's own two calls are not what these are about.
        SportsFetch.query.delete()
        db.session.commit()

    def queue(self, target, kind, due, **kw):
        row = SportsFetch(target=target, kind=kind, league_id=self.league_id, next_due_at=due, **kw)
        db.session.add(row)
        db.session.commit()
        return row

    def endpoints(self):
        return [c[0] for c in self.fake.calls]


class OrderTests(_Case):
    """dev/docs/BUGS.md 2026-11-16: a league's round call waited three hours behind team lookups."""

    def test_a_round_call_goes_ahead_of_team_lookups_that_came_due_first(self):
        for n in range(3):
            self.queue(f'team:{20 + n}', SPORTS_KIND_TEAM, NOW - timedelta(hours=2))
        self.queue(f'round:{LEAGUE_ID}:2099:9', SPORTS_KIND_ROUND, NOW - timedelta(minutes=1))
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = [
            game(1, 'Reds', 'Blues', iso(NOW + timedelta(days=1)), home_id=11, away_id=12)]
        sports_data.tick(now=NOW)
        self.assertEqual(self.endpoints(), ['eventsround'])

    def test_a_teams_first_lookup_goes_ahead_of_a_reread_that_came_due_first(self):
        self.fake.teams[31] = team_row(31, 'Pretendville Reds', 'Reds')
        self.fake.teams[32] = team_row(32, 'Madeup City Blues', 'Blues')
        self.queue('team:31', SPORTS_KIND_TEAM, NOW - timedelta(days=1), outcome=SPORTS_FETCH_OK,
                   last_at=NOW - timedelta(days=8))
        self.queue('team:32', SPORTS_KIND_TEAM, NOW - timedelta(minutes=1))
        sports_data.tick(now=NOW)
        self.assertEqual([c[1].get('id') for c in self.fake.calls], ['32'])

    def test_the_line_a_page_reads_is_the_order_the_worker_takes(self):
        self.queue('team:31', SPORTS_KIND_TEAM, NOW - timedelta(days=1), outcome=SPORTS_FETCH_OK,
                   last_at=NOW - timedelta(days=8))
        self.queue('team:32', SPORTS_KIND_TEAM, NOW - timedelta(hours=3))
        self.queue(f'round:{LEAGUE_ID}:2099:9', SPORTS_KIND_ROUND, NOW - timedelta(minutes=1))
        self.queue(f'round:{LEAGUE_ID}:2099:10', SPORTS_KIND_ROUND, NOW + timedelta(days=1))
        self.assertEqual(sports_data.queue_line(NOW), [
            (self.league_id, SPORTS_KIND_ROUND, True), (self.league_id, SPORTS_KIND_TEAM, True),
            (self.league_id, SPORTS_KIND_TEAM, False)])

    def test_a_reread_still_runs_once_nothing_else_is_due(self):
        self.fake.teams[31] = team_row(31, 'Pretendville Reds', 'Reds')
        self.queue('team:31', SPORTS_KIND_TEAM, NOW - timedelta(days=1), outcome=SPORTS_FETCH_OK,
                   last_at=NOW - timedelta(days=8))
        sports_data.tick(now=NOW)
        self.assertEqual(self.endpoints(), ['lookupteam'])


class WaitTests(_Case):
    def test_the_wait_is_what_is_left_of_this_one_then_a_spacing_per_call_ahead(self):
        state = sports_data._state()
        state.next_call_at = NOW + timedelta(seconds=30)
        db.session.commit()
        self.assertEqual(sports_data.seconds_until_call(0, NOW), 30)
        self.assertEqual(sports_data.seconds_until_call(100, NOW), 30 + 100 * 60)

    def test_a_backed_off_worker_is_counted_at_its_slower_pace(self):
        state = sports_data._state()
        state.backoff_seconds = 600
        db.session.commit()
        self.assertEqual(sports_data.seconds_until_call(3, NOW), 1800)


class InLineTests(_Case):
    """dev/docs/BUGS.md 2026-11-16: "At least 1 more call" sat still for hours with nothing
    saying about 100 calls were ahead of it."""

    def other_league(self):
        return sports_data.follow_league({'tsdb_id': 9002, 'name': 'Other League', 'sport': 'Pretend Ball',
                                          'season': '2099', 'terms': ['Other League']})

    def notice(self, league_id):
        with self.t.app.test_request_context():
            ctx = sports_view.page_context(league_id)
        return next((n for n in ctx['notices'] if n[1].startswith('Getting this league')), None)

    def test_the_league_page_says_how_many_calls_are_ahead_and_about_when(self):
        other = self.other_league()
        SportsFetch.query.delete()
        for n in range(5):
            db.session.add(SportsFetch(target=f'round:9002:2099:{n}', kind=SPORTS_KIND_ROUND, league_id=other,
                                       next_due_at=NOW - timedelta(hours=1)))
        db.session.commit()
        self.queue(f'round:{LEAGUE_ID}:2099:9', SPORTS_KIND_ROUND, datetime.utcnow() - timedelta(minutes=1))
        SportsFetch.query.filter(SportsFetch.league_id == other).update(
            {SportsFetch.next_due_at: datetime.utcnow() - timedelta(hours=1)})
        db.session.commit()
        _tone, title, text = self.notice(self.league_id)
        self.assertEqual(title, "Getting this league's schedule.")
        self.assertIn('At least 1 more call for its schedule', text)
        self.assertIn('5 other calls are ahead of it in line, so the next one runs in about 5 minutes.', text)

    def test_next_in_line_says_so(self):
        self.queue(f'round:{LEAGUE_ID}:2099:9', SPORTS_KIND_ROUND, datetime.utcnow() - timedelta(minutes=1))
        self.assertIn('It is next in line.', self.notice(self.league_id)[2])

    def test_team_lookups_are_not_called_the_schedule(self):
        for n in range(3):
            self.queue(f'team:{40 + n}', SPORTS_KIND_TEAM, datetime.utcnow() - timedelta(minutes=1))
        _tone, title, text = self.notice(self.league_id)
        self.assertEqual(title, "Getting this league's team names.")
        self.assertIn('Its schedule is in. 3 team lookups still to run', text)
        self.assertIn('The last runs in about 2 minutes.', text)

    def test_the_banner_says_when_the_last_call_runs(self):
        for n in range(4):
            self.queue(f'round:{LEAGUE_ID}:2099:{n}', SPORTS_KIND_ROUND, datetime.utcnow() - timedelta(minutes=1))
        banner = sports_view.list_context()['banner']
        self.assertEqual(banner['title'], 'Getting schedules: at least 4 more calls.')
        self.assertIn('The last of them runs in about 3 minutes.', banner['sub'])


class MatchRunTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-11-16: every TheSportsDB answer moved the queued match run to three
    minutes from then, so a guide import's match waited 33 minutes."""

    def setUp(self):
        self.t = make_test_app(csrf=False, start_scheduler=True)
        self.addCleanup(self.t.cleanup)
        from app import scheduler
        self.scheduler = scheduler

    def armed(self):
        from app.tz_utils import to_naive_utc
        return to_naive_utc(self.scheduler._scheduler.get_job(self.scheduler.SPORTS_MATCH_JOB_ID).next_run_time)

    def test_a_later_ask_does_not_move_a_queued_run(self):
        self.assertTrue(self.scheduler.queue_sports_match(600, 'a guide import'))
        first = self.armed()
        self.assertTrue(self.scheduler.queue_sports_match(1800, 'round refreshed'))
        self.assertEqual(self.armed(), first)

    def test_a_sooner_ask_brings_it_forward(self):
        self.scheduler.queue_sports_match(1800, 'round refreshed')
        first = self.armed()
        self.scheduler.queue_sports_match(600, 'a guide import')
        self.assertLess(self.armed(), first - timedelta(minutes=15))

    def test_with_none_queued_an_ask_queues_one(self):
        self.assertIsNone(self.scheduler._scheduler.get_job(self.scheduler.SPORTS_MATCH_JOB_ID))
        self.scheduler.queue_sports_match(600)
        self.assertGreater(self.armed(), datetime.utcnow() + timedelta(minutes=9))


if __name__ == '__main__':
    unittest.main()
