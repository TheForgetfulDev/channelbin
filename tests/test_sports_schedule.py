"""The local schedule from TheSportsDB (app/sports_data.py, app/sports_view.py, the Sports Schedules
pages), per dev/docs/DESIGN-sports-data.md and mockup 50, dev/changelog/1444.

The client runs over real HTTP against tests/support/fake_sportsdb.py: TheSportsDB's field names and
odd shapes, with made-up data. Throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_schedule
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.fake_sportsdb import (  # noqa: E402
    FIELD_LEAGUE_ID, LEAGUE_ID, FakeSportsDB, game, race, team_row,
)

from app import db  # noqa: E402
from app import sports_data, sports_view  # noqa: E402
from app.database import (  # noqa: E402
    SPORTS_FETCH_EMPTY, SPORTS_FETCH_OK, SPORTS_FETCH_RATE_LIMITED, SPORTS_FETCH_REFUSED,
    SPORTS_FETCH_SHRUNK, SportsEvent, SportsFetch, SportsFollow, SportsLeague, SportsState, SportsTeam,
)


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


class SportsCase(unittest.TestCase):
    display = 'America/New_York'

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.t.sandbox_config({'display': {'timezone': self.display, 'time_format': '12h'}})
        self.fake = FakeSportsDB()
        self.addCleanup(self.fake.stop)
        patcher = patch.object(sports_data, 'BASE_URL', self.fake.base_url)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.t.client
        self.now = datetime(2099, 3, 2, 12, 0)

    def add_league(self, tsdb_id=LEAGUE_ID, **kw):
        payload = {'tsdb_id': tsdb_id, 'name': kw.get('name', 'Pretend League'), 'sport': kw.get('sport', 'Pretend Ball'),
                   'season': '2099', 'terms': ['Pretend League']}
        return sports_data.follow_league(payload, kw.get('team'))

    def drain(self, start=None, step_seconds=120, limit=60):
        """Run the worker until nothing is due, moving the clock past each wait."""
        now = start or self.now
        for _ in range(limit):
            db.session.expire_all()
            result = sports_data.tick(now=now)
            if result == 'idle':
                return now
            now += timedelta(seconds=step_seconds)
        self.fail('the worker never ran out of due rows')

    def fetch(self, target):
        db.session.expire_all()
        return SportsFetch.query.filter_by(target=target).first()

    def ask_now(self, target, at):
        """Make this row the one the next tick calls, whatever else is due and whatever the pace."""
        row = self.fetch(target)
        row.next_due_at = at - timedelta(days=1)
        state = db.session.get(SportsState, 1)
        state.next_call_at = state.last_call_at = None
        db.session.commit()
        return sports_data.tick(now=at)


class NothingFollowedTests(SportsCase):
    def test_nothing_followed_makes_no_calls(self):
        self.assertEqual(sports_data.tick(now=self.now), 'idle')
        self.assertEqual(self.fake.calls, [])

    def test_nothing_followed_means_no_job(self):
        self.assertFalse(sports_data.worker_needed())
        self.add_league()
        self.assertTrue(sports_data.worker_needed())

    def test_the_page_says_no_calls_are_being_made(self):
        text = self.client.get('/sports').get_data(as_text=True)
        self.assertIn('No leagues followed', text)
        self.assertIn('making no calls', text)
        self.assertEqual(self.fake.calls, [])


class AnswerShapeTests(SportsCase):
    def test_a_429_is_not_read_as_no_games(self):
        self.fake.fault = 'rate_limited'
        answer = sports_data._call('eventsround', {'id': 1}, 'events')
        self.assertEqual(answer.outcome, SPORTS_FETCH_RATE_LIMITED)

    def test_a_503_is_a_refusal_and_names_the_status(self):
        self.fake.fault = 'refused'
        answer = sports_data._call('eventsround', {'id': 1, 's': '2099', 'r': 1}, 'events')
        self.assertEqual(answer.outcome, SPORTS_FETCH_REFUSED)
        self.assertIn('503', answer.reason)

    def test_a_200_that_is_not_json_is_a_refusal(self):
        self.fake.fault = 'not_json'
        answer = sports_data._call('eventsround', {'id': 1, 's': '2099', 'r': 1}, 'events')
        self.assertEqual(answer.outcome, SPORTS_FETCH_REFUSED)

    def test_null_events_is_an_empty_answer_not_a_failure(self):
        answer = sports_data._call('eventsround', {'id': LEAGUE_ID, 's': '2099', 'r': 77}, 'events')
        self.assertEqual(answer.outcome, SPORTS_FETCH_OK)
        self.assertEqual(answer.rows, [])
        self.assertTrue(answer.empty)

    def test_no_reason_text_carries_the_url(self):
        self.fake.fault = 'refused'
        answer = sports_data._call('eventsround', {'id': 1, 's': '2099', 'r': 1}, 'events')
        self.assertNotIn('/json/', answer.reason)
        self.assertNotIn(sports_data.FREE_KEY + '/', answer.reason)


class FirstFillTests(SportsCase):
    def world(self):
        self.fake.next_event[LEAGUE_ID] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)), home_id=11, away_id=12)]
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = [
            game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)), home_id=11, away_id=12),
            game(2, 'Greens', 'Golds', iso(self.now + timedelta(days=2)), home_id=13, away_id=14),
        ]
        self.fake.rounds[(LEAGUE_ID, '2099', 10)] = [
            game(3, 'Reds', 'Greens', iso(self.now + timedelta(days=30)), round_no=10, home_id=11, away_id=13),
        ]
        self.fake.teams[11] = team_row(11, 'Pretendville Reds', 'Reds')

    def test_what_a_follow_obliges_is_recorded_before_any_call(self):
        self.add_league()
        self.assertEqual(self.fake.calls, [])
        self.assertIsNotNone(self.fetch(f'league:{LEAGUE_ID}'))
        self.assertIsNotNone(self.fetch(f'next:{LEAGUE_ID}'))

    def test_one_call_a_tick(self):
        self.world()
        self.add_league()
        sports_data.tick(now=self.now)
        self.assertEqual(len(self.fake.calls), 1)

    def test_a_tick_waits_for_the_stored_pace_so_a_restart_cannot_burst(self):
        self.world()
        self.add_league()
        sports_data.tick(now=self.now)
        self.assertEqual(sports_data.tick(now=self.now + timedelta(seconds=5)), 'waiting')
        self.assertEqual(len(self.fake.calls), 1)
        state = db.session.get(SportsState, 1)
        wait = (state.next_call_at - self.now).total_seconds()
        self.assertGreaterEqual(wait, 40)
        self.assertLessEqual(wait, 80)

    def test_the_floor_holds_whatever_the_config_says(self):
        self.t.sandbox_config({'sports_data': {'call_spacing_seconds': 1, 'call_jitter_seconds': 0}})
        self.world()
        self.add_league()
        sports_data.tick(now=self.now)
        state = db.session.get(SportsState, 1)
        self.assertGreaterEqual((state.next_call_at - self.now).total_seconds(), sports_data.MIN_WAIT_SECONDS)

    def test_the_next_event_finds_its_round_and_the_walk_stops_at_the_horizon(self):
        self.world()
        self.add_league()
        self.drain()
        db.session.expire_all()
        self.assertEqual(SportsEvent.query.count(), 3)
        self.assertEqual(len(self.fake.calls_to('eventsround')), 2)  # 9 and 10; 10 is past the horizon
        self.assertIsNone(self.fetch(f'round:{LEAGUE_ID}:2099:11'))

    def test_a_round_number_that_is_not_consecutive_is_still_found(self):
        """NFL playoffs are rounds 160, 125, 150 and 200, so counting upward would miss them."""
        self.fake.next_event[LEAGUE_ID] = [game(5, 'Reds', 'Blues', iso(self.now + timedelta(days=1)), round_no=160)]
        self.fake.rounds[(LEAGUE_ID, '2099', 160)] = [game(5, 'Reds', 'Blues', iso(self.now + timedelta(days=1)), round_no=160)]
        self.add_league()
        self.drain()
        self.assertEqual(SportsEvent.query.filter_by(tsdb_id=5).first().round, 160)

    def test_an_empty_round_stops_the_walk_and_deletes_nothing(self):
        self.fake.next_event[LEAGUE_ID] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)))]
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)))]
        self.add_league()
        self.drain()
        row = self.fetch(f'round:{LEAGUE_ID}:2099:10')
        self.assertEqual(row.outcome, SPORTS_FETCH_EMPTY)
        self.assertIsNone(self.fetch(f'round:{LEAGUE_ID}:2099:11'))
        self.assertEqual(SportsEvent.query.count(), 1)

    def test_team_names_are_read_once_and_a_city_is_the_name_less_its_nickname(self):
        self.world()
        self.add_league()
        self.drain()
        team = SportsTeam.query.filter_by(tsdb_id=11).first()
        self.assertEqual(team.full_name, 'Pretendville Reds')
        self.assertEqual(team.city, 'Pretendville')
        self.assertIn('Reds', json.loads(team.names))
        before = len(self.fake.calls_to('lookupteam'))
        self.drain(start=self.now + timedelta(days=2))
        self.assertEqual(len(self.fake.calls_to('lookupteam')), before)

    def test_the_league_call_fills_in_terms_leaving_out_the_country(self):
        self.world()
        self.add_league()
        self.drain()
        league = SportsLeague.query.filter_by(tsdb_id=LEAGUE_ID).first()
        terms = json.loads(league.match_terms)
        self.assertIn('PL', terms)
        self.assertNotIn('Pretendia', terms)

    def test_a_row_with_no_date_is_dropped_counted_and_named(self):
        self.fake.next_event[LEAGUE_ID] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)))]
        bad = game(2, 'Greens', 'Golds', None)
        bad['dateEvent'] = bad['dateEventLocal'] = None
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1))), bad]
        self.add_league()
        self.drain()
        row = self.fetch(f'round:{LEAGUE_ID}:2099:9')
        self.assertEqual(row.dropped_count, 1)
        self.assertIn('no date', row.reason)
        self.assertEqual(SportsEvent.query.count(), 1)

    def test_a_row_for_another_league_is_dropped(self):
        self.fake.next_event[LEAGUE_ID] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)))]
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1))),
                                                    game(2, 'Wigan', 'Bolton', iso(self.now), league=4242)]
        self.add_league()
        self.drain()
        self.assertIsNone(SportsEvent.query.filter_by(tsdb_id=2).first())


class FailureTests(SportsCase):
    def seeded(self):
        self.fake.next_event[LEAGUE_ID] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)))]
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1))),
                                                    game(2, 'Greens', 'Golds', iso(self.now + timedelta(days=2)))]
        self.add_league()
        return self.drain()

    def test_a_429_doubles_the_wait_changes_nothing_stored_and_asks_again(self):
        self.seeded()
        row = self.fetch(f'round:{LEAGUE_ID}:2099:9')
        later = self.now + timedelta(hours=6)
        self.fake.fault = 'rate_limited'
        self.assertEqual(self.ask_now(row.target, later), SPORTS_FETCH_RATE_LIMITED)
        db.session.expire_all()
        state = db.session.get(SportsState, 1)
        self.assertEqual(state.backoff_seconds, 120)
        self.assertGreaterEqual((state.next_call_at - later).total_seconds(), 100)
        self.assertEqual(SportsEvent.query.count(), 2)
        self.assertLessEqual(self.fetch(row.target).next_due_at, later)

    def test_clean_calls_bring_a_backoff_back_down(self):
        self.seeded()
        with db.session.begin_nested():
            db.session.get(SportsState, 1).backoff_seconds = 240
        db.session.commit()
        settings = sports_data._settings()
        state = db.session.get(SportsState, 1)
        for _ in range(sports_data.CLEAN_CALLS_TO_EASE):
            sports_data._note_call(state, settings, sports_data.Answer(SPORTS_FETCH_OK), self.now)
        self.assertEqual(state.backoff_seconds, 120)
        for _ in range(sports_data.CLEAN_CALLS_TO_EASE):
            sports_data._note_call(state, settings, sports_data.Answer(SPORTS_FETCH_OK), self.now)
        self.assertEqual(state.backoff_seconds, 0)

    def test_a_refusal_keeps_what_is_stored_and_says_when_it_started(self):
        self.seeded()
        target = f'round:{LEAGUE_ID}:2099:9'
        later = self.now + timedelta(hours=6)
        self.fake.fault = 'refused'
        self.ask_now(target, later)
        row = self.fetch(target)
        self.assertEqual(row.outcome, SPORTS_FETCH_REFUSED)
        self.assertEqual(row.failing_since, later)
        self.assertEqual(SportsEvent.query.count(), 2)
        self.assertEqual(row.next_due_at, later + timedelta(hours=sports_data.REFUSED_RETRY_HOURS))

    def test_a_smaller_answer_is_named_once_and_deletes_nothing(self):
        self.seeded()
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)))]
        target = f'round:{LEAGUE_ID}:2099:9'
        later = self.now + timedelta(hours=6)
        self.assertEqual(self.ask_now(target, later), SPORTS_FETCH_OK)  # the call itself was answered
        row = self.fetch(target)
        self.assertEqual(row.outcome, SPORTS_FETCH_SHRUNK)
        self.assertEqual((row.row_count, row.prev_row_count), (1, 2))
        self.assertEqual(SportsEvent.query.count(), 2)
        missing = SportsEvent.query.filter_by(tsdb_id=2).first()
        self.assertEqual(missing.missing_since, later)
        self.ask_now(target, later + timedelta(hours=1))
        self.assertEqual(self.fetch(target).outcome, SPORTS_FETCH_OK)

    def test_an_empty_answer_for_a_round_that_had_games_is_shrunk_not_no_games(self):
        self.seeded()
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = []
        target = f'round:{LEAGUE_ID}:2099:9'
        self.ask_now(target, self.now + timedelta(hours=6))
        self.assertEqual(self.fetch(target).outcome, SPORTS_FETCH_SHRUNK)
        self.assertEqual(SportsEvent.query.count(), 2)
        self.assertIsNone(SportsEvent.query.filter_by(tsdb_id=2).first().missing_since)

    def test_a_game_back_in_the_answer_is_no_longer_missing(self):
        self.seeded()
        full = self.fake.rounds[(LEAGUE_ID, '2099', 9)]
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = full[:1]
        target = f'round:{LEAGUE_ID}:2099:9'
        self.ask_now(target, self.now + timedelta(hours=6))
        self.assertIsNotNone(SportsEvent.query.filter_by(tsdb_id=2).first().missing_since)
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = full
        self.ask_now(target, self.now + timedelta(hours=7))
        db.session.expire_all()
        self.assertIsNone(SportsEvent.query.filter_by(tsdb_id=2).first().missing_since)

    def test_a_moved_game_keeps_its_old_start(self):
        self.seeded()
        moved = iso(self.now + timedelta(days=1, hours=2))
        self.fake.rounds[(LEAGUE_ID, '2099', 9)][0]['strTimestamp'] = moved
        self.ask_now(f'round:{LEAGUE_ID}:2099:9', self.now + timedelta(hours=6))
        db.session.expire_all()
        event = SportsEvent.query.filter_by(tsdb_id=1).first()
        self.assertEqual(event.previous_start, self.now + timedelta(days=1))
        self.assertEqual(event.start_at, self.now + timedelta(days=1, hours=2))


class ReviewedFailureTests(SportsCase):
    """An answer that cannot be stored, or a state the worker could report wrongly, must not pass as
    healthy or defeat the pace."""
    seeded = FailureTests.seeded

    def test_an_answer_that_cannot_be_stored_keeps_the_pace_and_is_named(self):
        self.seeded()
        target = f'round:{LEAGUE_ID}:2099:9'
        with patch.object(sports_data, '_apply_round', side_effect=RuntimeError('boom')):
            at = self.now + timedelta(hours=6)
            self.assertEqual(self.ask_now(target, at), SPORTS_FETCH_REFUSED)
        row = self.fetch(target)
        self.assertEqual(row.outcome, SPORTS_FETCH_REFUSED)
        self.assertIn('could not be stored', row.reason)
        self.assertEqual(row.next_due_at, at + timedelta(hours=sports_data.REFUSED_RETRY_HOURS))
        state = db.session.get(SportsState, 1)
        self.assertEqual(state.last_call_at, at, 'the pace survives the rollback')
        calls = len(self.fake.calls)
        self.assertEqual(sports_data.tick(now=at + timedelta(seconds=15)), 'waiting')
        self.assertEqual(len(self.fake.calls), calls)

    def test_a_repeated_event_id_in_one_answer_is_dropped_not_fatal(self):
        when = iso(self.now + timedelta(days=1))
        self.fake.next_event[LEAGUE_ID] = [game(1, 'Reds', 'Blues', when)]
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = [game(1, 'Reds', 'Blues', when), game(1, 'Reds', 'Blues', when)]
        self.add_league()
        self.drain()
        row = self.fetch(f'round:{LEAGUE_ID}:2099:9')
        self.assertEqual(row.dropped_count, 1)
        self.assertIn('repeated id', row.reason)
        self.assertEqual(SportsEvent.query.count(), 1)

    def test_a_postponed_field_that_is_not_text_does_not_raise(self):
        parsed, _ = sports_data.parse_event(game(1, 'Reds', 'Blues', '2099-01-01T00:00:00', strPostponed=1), LEAGUE_ID)
        self.assertFalse(parsed['postponed'])

    def test_following_again_resumes_refreshing_rounds(self):
        self.seeded()
        league_id = SportsLeague.query.filter_by(tsdb_id=LEAGUE_ID).one().id
        sports_data.stop_following(league_id)
        before = len(self.fake.calls_to('eventsround'))
        sports_data.set_following(league_id)
        self.drain(start=self.now + timedelta(hours=1))
        self.assertGreater(len(self.fake.calls_to('eventsround')), before, 'an already stored next game still queues its round')
        self.assertIsNotNone(self.fetch(f'round:{LEAGUE_ID}:2099:9').next_due_at)

    def test_a_next_event_with_no_round_is_named_not_healthy(self):
        self.fake.next_event[LEAGUE_ID] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)), intRound='0')]
        self.add_league()
        self.drain()
        row = self.fetch(f'next:{LEAGUE_ID}')
        self.assertIn('no season or round', row.reason)
        self.assertEqual(sports_view.league_status([object()], [row], 0), sports_view.STATUS_LOOK)

    def test_a_league_no_longer_listed_is_named_not_healthy(self):
        self.fake.leagues.pop(LEAGUE_ID)
        self.add_league()
        self.drain()
        row = self.fetch(f'league:{LEAGUE_ID}')
        self.assertEqual(row.outcome, SPORTS_FETCH_EMPTY)
        self.assertEqual(sports_view.league_status([object()], [row], 0), sports_view.STATUS_LOOK)

    def test_retention_keeps_running_after_the_last_follow(self):
        self.seeded()
        sports_data.stop_following(SportsLeague.query.filter_by(tsdb_id=LEAGUE_ID).one().id)
        self.assertEqual(SportsFetch.query.count(), 0)
        self.assertTrue(sports_data.worker_needed(), 'stored events still have retention to run')
        SportsEvent.query.delete()
        db.session.commit()
        self.assertFalse(sports_data.worker_needed())

    def test_an_empty_round_of_a_past_season_stops_asking(self):
        self.fake.next_event[LEAGUE_ID] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)), season='2098', round_no=18)]
        self.fake.rounds[(LEAGUE_ID, '2098', 18)] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)), season='2098', round_no=18)]
        self.add_league()
        self.drain()
        self.assertIsNone(self.fetch(f'round:{LEAGUE_ID}:2098:19').next_due_at)

    def test_an_empty_round_of_this_season_is_asked_again(self):
        self.fake.next_event[LEAGUE_ID] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)))]
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)))]
        self.add_league()
        end = self.drain()
        row = self.fetch(f'round:{LEAGUE_ID}:2099:10')
        self.assertGreater(row.next_due_at, end)

    def test_delete_takes_a_team_added_before_any_game_arrived(self):
        self.fake.team_search['reds'] = []
        league_id = self.add_league(team={'team_id': 77, 'team': 'Early Team'})
        self.assertIsNotNone(SportsTeam.query.filter_by(tsdb_id=77).first())
        sports_data.delete_league(league_id)
        db.session.expire_all()
        self.assertIsNone(SportsTeam.query.filter_by(tsdb_id=77).first())


class CadenceTests(unittest.TestCase):
    NOW = datetime(2099, 3, 2, 12, 0)
    SET = {'horizon_days': 21}

    def due(self, *deltas):
        return sports_data._round_next_due([self.NOW + d for d in deltas], self.NOW, self.SET, 3600)

    def test_all_played_means_nothing_left_to_ask(self):
        self.assertIsNone(self.due(timedelta(days=-1)))

    def test_within_a_week_is_daily_at_this_installs_time(self):
        self.assertEqual(self.due(timedelta(days=3)), datetime(2099, 3, 3, 1, 0))

    def test_eight_to_twentyone_days_out_is_every_three_days(self):
        self.assertEqual(self.due(timedelta(days=15)), datetime(2099, 3, 5, 1, 0))

    def test_past_the_horizon_waits_until_it_comes_in(self):
        self.assertEqual(self.due(timedelta(days=30)), self.NOW + timedelta(days=9))

    def test_once_more_six_hours_before_the_start(self):
        self.assertEqual(self.due(timedelta(hours=10)), self.NOW + timedelta(hours=4))


class TimeDisplayTests(SportsCase):
    """A game's time is strTimestamp, which is UTC, converted to the display timezone, since an
    event's own local time could be anywhere in the world (dev/changelog/1444)."""

    def row_for(self, when, **kw):
        event = SportsEvent(tsdb_id=1, league_id=1, name='x', start_at=when, local_date=when.date(),
                            home_team_id=kw.get('home', 0), away_team_id=kw.get('away', 0))
        return event

    def when(self, event, zone, now=datetime(2099, 1, 1)):
        from zoneinfo import ZoneInfo
        return sports_view.event_when(event, now, ZoneInfo(zone), False)

    def test_utc_is_converted_not_taken_as_local(self):
        self.assertEqual(self.when(self.row_for(datetime(2099, 11, 6, 11, 30), home=1), 'America/New_York')[1], '6:30 AM')

    def test_another_zone_gets_its_own_time(self):
        self.assertEqual(self.when(self.row_for(datetime(2099, 11, 6, 11, 30), home=1), 'Asia/Tokyo')[1], '8:30 PM')

    def test_a_dst_change_moves_the_clock(self):
        self.assertEqual(self.when(self.row_for(datetime(2099, 7, 6, 11, 30), home=1), 'America/New_York')[1], '7:30 AM')

    def test_strtimelocal_is_never_read(self):
        row = game(1, 'Reds', 'Blues', '2099-11-06T11:30:00', strTimeLocal='23:59:00', strTime='22:22:00')
        parsed, _ = sports_data.parse_event(row, LEAGUE_ID)
        self.assertEqual(parsed['start_at'], datetime(2099, 11, 6, 11, 30))

    def test_a_timestamp_with_an_offset_is_stored_as_naive_utc(self):
        self.assertEqual(sports_data._parse_when('2099-11-06T11:30:00+02:00'), datetime(2099, 11, 6, 9, 30))

    def test_a_matchup_at_midnight_utc_is_a_real_time(self):
        self.assertEqual(self.when(self.row_for(datetime(2099, 11, 6, 0, 0), home=1), 'America/New_York')[1], '7:00 PM')

    def test_a_race_at_midnight_utc_has_no_time_and_keeps_its_own_day(self):
        """Moved into a time zone, the placeholder lands on the wrong day: the race is Nov 7 by its
        own date and 7pm on Nov 6 converted."""
        event = SportsEvent(tsdb_id=1, league_id=1, name='x', start_at=datetime(2099, 11, 7, 0, 0),
                            local_date=datetime(2099, 11, 7).date())
        day, clock = self.when(event, 'America/New_York', now=datetime(2099, 11, 1))
        self.assertEqual(clock, '')
        self.assertIn('Nov 7', day)

    def test_a_race_at_a_real_time_is_converted(self):
        event = SportsEvent(tsdb_id=1, league_id=1, name='x', start_at=datetime(2099, 11, 6, 11, 30),
                            local_date=datetime(2099, 11, 6).date())
        self.assertEqual(self.when(event, 'America/New_York')[1], '6:30 AM')

    def test_no_timestamp_at_all_says_so(self):
        event = SportsEvent(tsdb_id=1, league_id=1, name='x', start_at=None, local_date=datetime(2099, 11, 7).date())
        self.assertEqual(self.when(event, 'America/New_York')[1], '')


class FollowingTests(SportsCase):
    def stored(self):
        self.fake.next_event[LEAGUE_ID] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)), home_id=11, away_id=12)]
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)), home_id=11, away_id=12)]
        league_id = self.add_league()
        self.drain()
        return league_id

    def test_stopping_removes_the_calls_and_keeps_the_events_and_league(self):
        league_id = self.stored()
        sports_data.stop_following(league_id)
        db.session.expire_all()
        self.assertEqual(SportsFetch.query.count(), 0)
        self.assertEqual(SportsEvent.query.count(), 1)
        self.assertIsNotNone(db.session.get(SportsLeague, league_id))
        calls = len(self.fake.calls)
        self.assertEqual(sports_data.tick(now=self.now + timedelta(days=30)), 'idle')
        self.assertEqual(len(self.fake.calls), calls)

    def test_a_rule_owned_follow_keeps_the_calls_when_a_person_stops(self):
        league_id = self.stored()
        db.session.add(SportsFollow(league_id=league_id, team_tsdb_id=0, rule_id=7))
        db.session.commit()
        sports_data.stop_following(league_id)
        db.session.expire_all()
        self.assertGreater(SportsFetch.query.count(), 0)
        self.assertEqual(SportsFollow.query.filter_by(league_id=league_id, rule_id=7).count(), 1)

    def test_deleting_a_league_takes_only_what_is_its_own(self):
        league_id = self.stored()
        other = self.add_league(FIELD_LEAGUE_ID, name='Pretend Racing', sport='Motorsport')
        db.session.add(SportsEvent(tsdb_id=99, league_id=other, name='Shared', home_team_id=11, away_team_id=0,
                                   local_date=self.now.date()))
        db.session.commit()
        sports_data.delete_league(league_id)
        db.session.expire_all()
        self.assertIsNone(db.session.get(SportsLeague, league_id))
        self.assertEqual(SportsFollow.query.filter_by(league_id=league_id).count(), 0)
        self.assertEqual(SportsFetch.query.filter_by(league_id=league_id).count(), 0)
        self.assertEqual(SportsEvent.query.filter_by(league_id=league_id).count(), 0)
        self.assertIsNotNone(SportsTeam.query.filter_by(tsdb_id=11).first(), 'another league still has its events')
        self.assertIsNone(SportsTeam.query.filter_by(tsdb_id=12).first())

    def test_retention_prunes_past_and_long_missing_events(self):
        league_id = self.stored()
        db.session.add_all([
            SportsEvent(tsdb_id=50, league_id=league_id, name='Old', start_at=self.now - timedelta(days=31),
                        local_date=self.now.date()),
            SportsEvent(tsdb_id=51, league_id=league_id, name='Gone', start_at=self.now + timedelta(days=3),
                        local_date=self.now.date(), missing_since=self.now - timedelta(days=8)),
            SportsEvent(tsdb_id=52, league_id=league_id, name='Recent', start_at=self.now - timedelta(days=29),
                        local_date=self.now.date()),
        ])
        db.session.commit()
        db.session.get(SportsState, 1).last_pruned_at = None
        db.session.commit()
        sports_data.tick(now=self.now)
        db.session.expire_all()
        self.assertIsNone(SportsEvent.query.filter_by(tsdb_id=50).first())
        self.assertIsNone(SportsEvent.query.filter_by(tsdb_id=51).first())
        self.assertIsNotNone(SportsEvent.query.filter_by(tsdb_id=52).first())


class AddingALeagueTests(SportsCase):
    def post(self, **body):
        return self.client.post('/api/sports/lookup', json=body)

    def clear_pace(self):
        state = db.session.get(SportsState, 1)
        if state is not None:
            state.last_call_at = None
            db.session.commit()

    def test_entry_parsing(self):
        self.assertEqual(sports_data.parse_entry('https://www.thesportsdb.com/league/4391-NFL'), ('league', 4391))
        self.assertEqual(sports_data.parse_entry(' 4387 '), ('league', 4387))
        self.assertEqual(sports_data.parse_entry('Boston Bruins'), ('team', 'Boston Bruins'))
        self.assertEqual(sports_data.parse_entry('https://www.thesportsdb.com/team/1-X')[0], 'badlink')
        self.assertEqual(sports_data.parse_entry('https://example.test/x')[0], 'badlink')

    def test_a_link_to_something_else_says_so_with_no_call(self):
        r = self.post(text='https://www.thesportsdb.com/team/1-X')
        self.assertEqual(r.status_code, 400)
        self.assertIn('not a league', r.get_json()['error'])
        self.assertEqual(self.fake.calls, [])

    def test_a_number_finds_its_league_and_saves_nothing(self):
        body = self.post(text=str(LEAGUE_ID)).get_json()
        self.assertEqual(body['league']['name'], 'Pretend League')
        self.assertEqual(SportsLeague.query.count(), 0)

    def test_an_unknown_number_says_so(self):
        r = self.post(text='424242')
        self.assertEqual(r.status_code, 404)
        self.assertIn('no league with the number 424242', r.get_json()['error'])

    def test_a_second_lookup_too_soon_says_when_and_never_waits(self):
        self.post(text=str(LEAGUE_ID))
        calls = len(self.fake.calls)
        r = self.post(text=str(FIELD_LEAGUE_ID))
        self.assertEqual(r.status_code, 409)
        self.assertIn('Try again in', r.get_json()['error'])
        self.assertLessEqual(r.get_json()['retry_after'], sports_data.MIN_WAIT_SECONDS)
        self.assertEqual(len(self.fake.calls), calls)

    def test_a_lookup_counts_toward_the_workers_pace(self):
        self.post(text=str(LEAGUE_ID))
        state = db.session.get(SportsState, 1)
        self.assertGreater(state.next_call_at, datetime.utcnow() + timedelta(seconds=20))

    def test_thesportsdb_being_down_saves_nothing_and_says_so(self):
        self.fake.fault = 'refused'
        r = self.post(text=str(LEAGUE_ID))
        self.assertEqual(r.status_code, 502)
        self.assertIn('Nothing was saved', r.get_json()['error'])

    def test_a_team_name_finds_its_league_in_one_call(self):
        self.fake.team_search['pretendville reds'] = [team_row(11, 'Pretendville Reds', 'Reds')]
        body = self.post(text='Pretendville Reds').get_json()
        self.assertEqual(body['team']['team_id'], 11)
        self.assertEqual(body['league']['tsdb_id'], LEAGUE_ID)
        self.assertEqual(len(self.fake.calls), 1)

    def test_a_name_with_several_teams_asks_which(self):
        self.fake.team_search['reds'] = [team_row(11, 'Pretendville Reds'), team_row(12, 'Reds Reserves')]
        body = self.post(text='Reds').get_json()
        self.assertEqual(len(body['choices']), 2)

    def test_a_team_that_is_not_found_points_at_the_other_ways_in(self):
        r = self.post(text='xyzzy')
        self.assertEqual(r.status_code, 404)
        self.assertIn('link to its page on TheSportsDB', r.get_json()['error'])

    def test_a_follow_must_name_something_just_checked(self):
        r = self.client.post('/api/sports/leagues', json={'tsdb_id': 31337})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(SportsLeague.query.count(), 0)

    def test_following_a_checked_league_queues_it(self):
        self.post(text=str(LEAGUE_ID))
        r = self.client.post('/api/sports/leagues', json={'tsdb_id': LEAGUE_ID})
        self.assertTrue(r.get_json()['success'])
        league = SportsLeague.query.filter_by(tsdb_id=LEAGUE_ID).one()
        self.assertEqual(SportsFollow.query.filter_by(league_id=league.id, team_tsdb_id=0).count(), 1)
        self.assertIsNotNone(self.fetch(f'next:{LEAGUE_ID}'))

    def test_following_one_team_from_a_search(self):
        self.fake.team_search['pretendville reds'] = [team_row(11, 'Pretendville Reds', 'Reds')]
        self.post(text='Pretendville Reds')
        self.client.post('/api/sports/leagues', json={'tsdb_id': LEAGUE_ID, 'team_id': 11})
        league = SportsLeague.query.filter_by(tsdb_id=LEAGUE_ID).one()
        self.assertEqual([f.team_tsdb_id for f in SportsFollow.query.filter_by(league_id=league.id)], [11])
        self.assertEqual(SportsTeam.query.filter_by(tsdb_id=11).one().full_name, 'Pretendville Reds')

    def test_changing_teams_makes_no_calls(self):
        league_id = self.add_league()
        calls = len(self.fake.calls)
        r = self.client.put(f'/api/sports/leagues/{league_id}/following', json={'team_ids': [11, 12]})
        self.assertTrue(r.get_json()['success'])
        self.assertEqual(sorted(f.team_tsdb_id for f in SportsFollow.query.all()), [11, 12])
        self.assertEqual(len(self.fake.calls), calls)
        self.client.put(f'/api/sports/leagues/{league_id}/following', json={'team_ids': []})
        db.session.expire_all()
        self.assertEqual([f.team_tsdb_id for f in SportsFollow.query.all()], [0])

    def test_the_dialog_offers_the_six_default_leagues_alphabetically_and_follows_none(self):
        """The shipped picks (dev/changelog/1446): offered, never followed until a click."""
        shipped = sports_view.add_dialog_config()['shipped']
        names = [s['name'] for s in shipped]
        self.assertEqual(names, ['English Premier League', 'Formula 1', 'NASCAR Cup Series', 'NBA', 'NFL', 'NHL'])
        self.assertEqual(names, sorted(names, key=str.lower))
        self.assertFalse(any(s['followed'] for s in shipped))
        self.assertEqual(SportsFollow.query.count(), 0)

    def test_stop_and_delete_routes_answer_404_for_a_league_gone(self):
        self.assertEqual(self.client.delete('/api/sports/leagues/999/following').status_code, 404)
        self.assertEqual(self.client.delete('/api/sports/leagues/999').status_code, 404)


class PaceSettingTests(SportsCase):
    def save(self, path, value):
        return self.client.post('/api/settings/field', json={'path': path, 'value': value})

    def test_pace_error_is_about_the_shortest_wait(self):
        self.assertEqual(sports_data.pace_error(60, 20), '')
        self.assertEqual(sports_data.pace_error(32, 20), '')
        self.assertIn('12', sports_data.pace_error(31, 20))

    def test_a_spacing_too_fast_for_the_jitter_is_refused(self):
        r = self.save('sports_data.call_spacing_seconds', 25)
        self.assertEqual(r.status_code, 400)
        self.assertIn('at least 12', r.get_json()['error'])

    def test_a_jitter_too_wide_for_the_spacing_is_refused(self):
        r = self.save('sports_data.call_jitter_seconds', 50)
        self.assertEqual(r.status_code, 400)

    def test_a_fine_pace_saves(self):
        self.assertEqual(self.save('sports_data.call_spacing_seconds', 90).status_code, 200)

    def test_the_settings_page_carries_the_four_advanced_fields_and_no_key(self):
        text = self.client.get('/settings').get_data(as_text=True)
        for key in ('call_spacing_seconds', 'call_jitter_seconds', 'horizon_days', 'history_keep_days'):
            self.assertIn(f'sports_data.{key}', text)
        self.assertNotIn('sports_data.api_key', text)


class PageTests(SportsCase):
    """The list is the Auto-record list's anatomy and the league page its rule page's (mockup 50)."""

    def seed(self):
        now = datetime.utcnow().replace(microsecond=0)
        self.real_now = now
        league = SportsLeague(tsdb_id=LEAGUE_ID, name='Pretend League', sport='Pretend Ball', current_season='2099')
        db.session.add(league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0, rule_id=0))
        db.session.add(SportsFetch(target=f'round:{LEAGUE_ID}:2099:11', kind='round', league_id=league.id,
                                   last_at=now, next_due_at=now + timedelta(hours=5), outcome=SPORTS_FETCH_OK,
                                   row_count=2))
        db.session.add(SportsEvent(tsdb_id=1, league_id=league.id, season='2099', round=11, name='x',
                                   home_team_id=11, away_team_id=12, home_name='Reds', away_name='Blues',
                                   venue='Reds Arena', city='Pretendville', start_at=now + timedelta(hours=3),
                                   local_date=(now + timedelta(hours=3)).date()))
        db.session.add(SportsEvent(tsdb_id=2, league_id=league.id, season='2099', round=11, name='y',
                                   home_team_id=13, away_team_id=14, home_name='Greens', away_name='Golds',
                                   venue='Greens Park', start_at=now + timedelta(days=2),
                                   local_date=(now + timedelta(days=2)).date()))
        db.session.commit()
        return league.id

    def list_html(self):
        r = self.client.get('/sports')
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def league_html(self, league_id, **query):
        r = self.client.get(f'/sports/{league_id}', query_string=query)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def test_the_menu_lists_it_under_channels_and_guide(self):
        text = self.list_html()
        self.assertRegex(text, r'nav-link active" href="/sports"')
        self.assertIn('data-icon="nav-sports"', text)

    def test_the_list_says_who_when_and_where(self):
        self.seed()
        text = self.list_html()
        self.assertIn('Blues at Reds', text)
        self.assertIn('Reds Arena, Pretendville', text)
        self.assertIn('class="rl-list sp-list"', text)
        self.assertIn('rs-on', text)

    def test_the_list_never_says_round(self):
        self.seed()
        main = self.list_html().split('<main', 1)[1]
        self.assertNotRegex(main.split('sports-boot')[0], r'(?i)\bround\b')

    def test_the_list_sorts_on_its_column_headers(self):
        self.seed()
        text = self.list_html()
        for key in ('name', 'status', 'next', 'count', 'updated'):
            self.assertIn(f'data-sort="{key}"', text)

    def test_the_list_has_no_reason_text_on_a_failed_row(self):
        """A status is a badge and an edge, with the reason on the league's page (DESIGN.md 17.1)."""
        self.seed()
        row = SportsFetch.query.first()
        row.outcome, row.reason = SPORTS_FETCH_REFUSED, 'HTTP 503, and the answer was not a schedule.'
        db.session.commit()
        text = self.list_html()
        self.assertIn("Couldn&#39;t update", text)
        self.assertNotIn('HTTP 503', text)

    def test_credit_on_both_pages(self):
        league_id = self.seed()
        for text in (self.list_html(), self.league_html(league_id)):
            self.assertIn('href="https://www.thesportsdb.com/" target="_blank"', text)

    def test_the_league_page_order_and_shell(self):
        league_id = self.seed()
        text = self.league_html(league_id)
        order = [text.index(f'data-section="{s}"') for s in ('about', 'upcoming', 'updates')]
        self.assertEqual(order, sorted(order), 'This league sits above Upcoming games by default')
        self.assertIn('All Leagues', text)
        self.assertIn('class="gd-actionbar"', text)
        self.assertEqual(text.count('<h1'), 1)

    def test_every_table_cell_is_labeled_for_the_phone_card(self):
        league_id = self.seed()
        text = self.league_html(league_id)
        self.assertIn('tbl-cards', text)
        self.assertNotRegex(text.split('<main', 1)[1].split('</main>')[0], r'<td(?![^>]*data-label)')

    def test_schedule_updates_says_what_dates_it_asked_for_and_explains_round(self):
        league_id = self.seed()
        text = self.league_html(league_id)
        self.assertRegex(text, r'Games from \w+ \d+ to \w+ \d+')
        self.assertIn('TheSportsDB calls this round 11', text)
        self.assertIn('which TheSportsDB calls a round', text)

    def test_a_team_follow_shows_your_teams_or_all(self):
        league_id = self.seed()
        follow = SportsFollow.query.first()
        follow.team_tsdb_id = 11
        db.session.commit()
        mine = self.league_html(league_id)
        self.assertIn('Blues at Reds', mine)
        self.assertNotIn('Golds at Greens', mine)
        everything = self.league_html(league_id, scope='all')
        self.assertIn('Golds at Greens', everything)

    def test_a_refusal_says_why_on_the_league_page(self):
        league_id = self.seed()
        row = SportsFetch.query.first()
        row.outcome, row.reason, row.failing_since = SPORTS_FETCH_REFUSED, 'HTTP 503, and the answer was not a schedule.', self.real_now
        db.session.commit()
        text = self.league_html(league_id)
        self.assertIn('The last update failed.', text)
        self.assertIn('HTTP 503', text)

    def test_a_failed_first_fill_never_reads_as_no_games(self):
        league_id = self.seed()
        SportsEvent.query.delete()
        row = SportsFetch.query.first()
        row.outcome, row.reason, row.failing_since = SPORTS_FETCH_REFUSED, 'HTTP 503.', self.real_now
        db.session.commit()
        text = self.league_html(league_id)
        self.assertNotIn('lists nothing', text)
        self.assertIn('says nothing about what is coming up', text)
        listing = self.list_html()
        self.assertNotIn('Nothing listed', listing)
        self.assertIn('Not known', listing)

    def test_up_to_date_is_only_said_when_every_league_is(self):
        self.seed()
        self.assertIn('Schedules are up to date', self.list_html())
        other = SportsLeague(tsdb_id=FIELD_LEAGUE_ID, name='Other League', current_season='2099')
        db.session.add(other)
        db.session.flush()
        db.session.add(SportsFollow(league_id=other.id, team_tsdb_id=0, rule_id=0))
        db.session.add(SportsFetch(target=f'round:{FIELD_LEAGUE_ID}:2099:1', kind='round', league_id=other.id,
                                   last_at=self.real_now, outcome=SPORTS_FETCH_REFUSED, reason='HTTP 503.'))
        db.session.commit()
        text = self.list_html()
        self.assertNotIn('Schedules are up to date', text)
        self.assertIn('1 league could not update', text)

    def test_the_round_call_going_away_is_named_while_others_still_answer(self):
        self.seed()
        row = SportsFetch.query.first()
        row.outcome, row.failing_since = SPORTS_FETCH_REFUSED, self.real_now
        db.session.add(SportsFetch(target=f'next:{LEAGUE_ID}', kind='next', league_id=row.league_id,
                                   last_at=self.real_now, outcome=SPORTS_FETCH_OK))
        db.session.commit()
        self.assertIn('refused schedule calls', self.list_html())

    def test_a_race_that_got_a_real_time_is_not_moved_from_its_placeholder(self):
        league_id = self.seed()
        event = SportsEvent.query.filter_by(tsdb_id=2).first()
        event.home_team_id = event.away_team_id = 0
        event.previous_start = self.real_now
        db.session.commit()
        self.assertNotIn('Moved from', self.league_html(league_id))
        event = SportsEvent.query.filter_by(tsdb_id=1).first()
        event.previous_start = self.real_now
        db.session.commit()
        self.assertIn('Moved from', self.league_html(league_id))

    def test_keep_days_in_the_copy_follows_the_setting(self):
        league_id = self.seed()
        self.t.sandbox_config({'sports_data': {'history_keep_days': 9}, 'display': {'timezone': self.display, 'time_format': '12h'}})
        SportsFollow.query.delete()
        db.session.commit()
        self.assertIn('stay until 9 days after', self.league_html(league_id))

    def test_a_refusal_everywhere_gets_a_banner_on_the_list(self):
        self.seed()
        row = SportsFetch.query.first()
        row.outcome, row.failing_since = SPORTS_FETCH_REFUSED, self.real_now
        db.session.commit()
        self.assertIn('refused schedule calls', self.list_html())

    def test_a_backoff_gets_a_banner_on_the_list(self):
        self.seed()
        db.session.add(SportsState(id=1, backoff_seconds=240))
        db.session.commit()
        self.assertIn('asked ChannelBin to slow down', self.list_html())

    def test_a_missing_game_is_named(self):
        league_id = self.seed()
        event = SportsEvent.query.filter_by(tsdb_id=2).first()
        event.missing_since = self.real_now
        db.session.commit()
        text = self.league_html(league_id)
        self.assertIn('A game is missing.', text)
        self.assertIn('Golds at Greens', text)
        self.assertIn('kept for 7 days', text)

    def test_a_league_not_followed_says_no_calls(self):
        league_id = self.seed()
        SportsFollow.query.delete()
        db.session.commit()
        text = self.league_html(league_id)
        self.assertIn('Not followed.', text)
        self.assertIn('Follow again', text)
        self.assertIn('No calls are being made', self.list_html())

    def test_a_race_with_a_placeholder_time_arrives_and_says_so(self):
        """Through the worker over HTTP, to the page: its day is its own, and its time is not invented."""
        when = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=3)
        self.fake.next_event[FIELD_LEAGUE_ID] = [race(7, 'Pretend 500', iso(when))]
        self.fake.rounds[(FIELD_LEAGUE_ID, '2099', 30)] = [race(7, 'Pretend 500', iso(when))]
        league_id = self.add_league(FIELD_LEAGUE_ID, name='Pretend Racing', sport='Motorsport')
        self.drain(start=datetime.utcnow())
        text = self.league_html(league_id)
        self.assertIn('Pretend 500', text)
        self.assertIn('Time not listed', text)
        self.assertIn('Upcoming races', text)
        self.assertIn('Pretend Speedway, Pretendville', text)

    def test_a_league_gone_is_a_404(self):
        self.assertEqual(self.client.get('/sports/999').status_code, 404)

    def test_no_own_key_anywhere(self):
        league_id = self.seed()
        for text in (self.list_html(), self.league_html(league_id)):
            self.assertNotRegex(text.split('<main', 1)[1].split('</main>')[0], r'(?i)api key|own key|paid')


if __name__ == '__main__':
    unittest.main()
