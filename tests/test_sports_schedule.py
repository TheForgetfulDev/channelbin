"""The local schedule from TheSportsDB (app/sports_data.py, app/sports_view.py, the Sports Schedules
pages), per dev/docs/DESIGN-sports-data.md and mockup 50, dev/changelog/1444.

The client runs over real HTTP against tests/support/fake_sportsdb.py: TheSportsDB's field names and
odd shapes, with made-up data. Throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_schedule
"""
import html as html_lib
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.fake_sportsdb import (  # noqa: E402
    FIELD_LEAGUE_ID, LEAGUE_ID, FakeSportsDB, game, league_row, race, team_row,
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
        team = kw.get('team')
        return sports_data.follow_league(payload, [team['team_id']] if team else None, [team] if team else None)

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
        self.assertIn('where each event is in your guide', text)
        self.assertEqual(self.fake.calls, [])

    def test_the_about_text_says_the_schedule_is_matched_to_the_guide(self):
        """The About panel said the schedule was "not matched to your guide listings yet" long
        after matching shipped (dev/changelog/1500)."""
        text = self.client.get('/sports').get_data(as_text=True)
        about = text[text.index('class="page-about"'):text.index('id="sports-body"')]
        self.assertNotIn('not matched', about)
        self.assertIn('checks each event against your', about)
        self.assertIn('{sports:league}', about)


class AnswerShapeTests(SportsCase):
    def test_a_429_is_not_read_as_no_games(self):
        self.fake.fault = 'rate_limited'
        answer = sports_data._call('eventsround', {'id': 1}, 'events')
        self.assertEqual(answer.outcome, SPORTS_FETCH_RATE_LIMITED)

    def test_a_429_carries_the_pause_it_asked_for(self):
        self.fake.fault, self.fake.retry_after = 'rate_limited', '180'
        answer = sports_data._call('eventsround', {'id': 1}, 'events')
        self.assertEqual(answer.retry_after, 180)
        self.assertIn('180 seconds', answer.reason)

    def test_a_retry_after_is_read_as_seconds_between_a_minute_and_an_hour(self):
        """The docs say a minute; the measured header said 120 (dev/changelog/1551)."""
        self.assertEqual(sports_data.retry_after_seconds('120'), 120)
        self.assertEqual(sports_data.retry_after_seconds(' 90 '), 90)
        self.assertEqual(sports_data.retry_after_seconds('5'), 60)
        self.assertEqual(sports_data.retry_after_seconds('99999'), sports_data.MAX_BACKOFF_SECONDS)
        self.assertEqual(sports_data.retry_after_seconds(None), 120)
        self.assertEqual(sports_data.retry_after_seconds('Wed, 21 Oct 2099 07:28:00 GMT'), 120)

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
        self.assertGreaterEqual(wait, 8)
        self.assertLessEqual(wait, 12)

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

    def test_a_429_doubles_the_spacing_changes_nothing_stored_and_asks_again(self):
        self.seeded()
        row = self.fetch(f'round:{LEAGUE_ID}:2099:9')
        later = self.now + timedelta(hours=6)
        self.fake.fault = 'rate_limited'
        self.assertEqual(self.ask_now(row.target, later), SPORTS_FETCH_RATE_LIMITED)
        db.session.expire_all()
        state = db.session.get(SportsState, 1)
        self.assertEqual(state.backoff_seconds, 20)
        self.assertEqual(SportsEvent.query.count(), 2)
        self.assertLessEqual(self.fetch(row.target).next_due_at, later)

    def limited_wait(self):
        self.seeded()
        row = self.fetch(f'round:{LEAGUE_ID}:2099:9')
        later = self.now + timedelta(hours=6)
        self.fake.fault = 'rate_limited'
        self.ask_now(row.target, later)
        db.session.expire_all()
        return (db.session.get(SportsState, 1).next_call_at - later).total_seconds()

    def test_the_first_call_after_a_429_waits_out_the_pause_it_asked_for(self):
        """dev/docs/BUGS.md 2026-11-14: the next call came after the doubled spacing, inside the pause,
        where every call is refused again."""
        self.fake.retry_after = '300'
        self.assertGreaterEqual(self.limited_wait(), 300)

    def test_a_429_with_no_retry_after_waits_the_measured_two_minutes(self):
        """dev/docs/BUGS.md 2026-11-14."""
        self.fake.retry_after = None
        self.assertGreaterEqual(self.limited_wait(), 120)

    def test_after_the_pause_calls_come_at_the_doubled_spacing_not_the_pause(self):
        self.seeded()
        settings = sports_data._settings()
        state = db.session.get(SportsState, 1)
        sports_data._note_call(state, settings, sports_data.Answer(SPORTS_FETCH_RATE_LIMITED, retry_after=120), self.now)
        self.assertGreaterEqual((state.next_call_at - self.now).total_seconds(), 120)
        after = state.next_call_at
        sports_data._note_call(state, settings, sports_data.Answer(SPORTS_FETCH_OK), after)
        self.assertLessEqual((state.next_call_at - after).total_seconds(), 20 + settings['call_jitter_seconds'])

    def test_clean_calls_bring_a_backoff_back_down(self):
        self.seeded()
        with db.session.begin_nested():
            db.session.get(SportsState, 1).backoff_seconds = 240
        db.session.commit()
        settings = sports_data._settings()
        state = db.session.get(SportsState, 1)
        seen = []
        while state.backoff_seconds and len(seen) < 10:
            for _ in range(sports_data.CLEAN_CALLS_TO_EASE):
                sports_data._note_call(state, settings, sports_data.Answer(SPORTS_FETCH_OK), self.now)
            seen.append(state.backoff_seconds)
        # Halved each time until half would be no slower than the spacing (10), then gone.
        self.assertEqual(seen, [120, 60, 30, 15, 0])

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
        self.assertEqual(sports_data.tick(now=at + timedelta(seconds=5)), 'waiting')
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
        # Round 0 is a real round (the NBA's regular season, dev/changelog/1550); no round is an empty one.
        self.fake.next_event[LEAGUE_ID] = [game(1, 'Reds', 'Blues', iso(self.now + timedelta(days=1)), intRound='')]
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



class SeveralTeamsTests(SportsCase):
    """dev/changelog/1641: Add league or team follows several teams of one league, replacing what the
    person followed in it, and a league already here is answered from what is stored with no call."""

    def lookup(self, **body):
        return self.client.post('/api/sports/lookup', json=body)

    def clear_pace(self):
        state = db.session.get(SportsState, 1)
        if state is not None:
            state.last_call_at = state.next_call_at = None
            state.backoff_seconds = 0
            db.session.commit()

    def search(self, team_id, name, league=LEAGUE_ID, league_name='Pretend League'):
        self.fake.team_search[name.lower()] = [team_row(team_id, name, league=league, league_name=league_name)]
        self.clear_pace()
        return self.lookup(text=name)

    def follow(self, **body):
        return self.client.post('/api/sports/leagues', json={'tsdb_id': LEAGUE_ID, **body})

    def follows(self, rule=0):
        db.session.expire_all()
        league = SportsLeague.query.filter_by(tsdb_id=LEAGUE_ID).one()
        return sorted(f.team_tsdb_id for f in SportsFollow.query.filter_by(league_id=league.id, rule_id=rule))

    def seed_game(self, league_id, home=11, away=12):
        start = datetime.utcnow() + timedelta(days=1)
        db.session.add(SportsEvent(tsdb_id=500 + home, league_id=league_id, name='Reds vs Blues', start_at=start,
                                   local_date=start.date(), home_team_id=home, away_team_id=away,
                                   home_name='Pretendville Reds', away_name='Pretendville Blues'))
        db.session.commit()

    def test_several_checked_teams_are_followed_in_one_request(self):
        self.search(11, 'Pretendville Reds')
        self.search(12, 'Pretendville Blues')
        r = self.follow(team_ids=[12, 11])
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.follows(), [11, 12])
        self.assertEqual(sorted(t.full_name for t in SportsTeam.query.filter(SportsTeam.tsdb_id.in_([11, 12]))),
                         ['Pretendville Blues', 'Pretendville Reds'])

    def test_following_replaces_the_persons_follow_and_keeps_a_rules(self):
        league_id = self.add_league()
        db.session.add(SportsFollow(league_id=league_id, team_tsdb_id=13, rule_id=99))
        db.session.commit()
        self.search(11, 'Pretendville Reds')
        self.follow(team_ids=[11])
        self.assertEqual(self.follows(), [11], 'the whole-league follow was replaced, not added to')
        self.assertEqual(self.follows(rule=99), [13])
        self.follow(team_ids=[])
        self.assertEqual(self.follows(), [0])

    def test_a_team_nobody_checked_or_stored_is_refused_and_the_follow_stands(self):
        self.add_league()
        r = self.follow(team_ids=[77])
        self.assertEqual(r.status_code, 409)
        self.assertIn('has not checked team 77 in the Pretend League', r.get_json()['error'])
        self.assertEqual(self.follows(), [0])

    def test_a_team_checked_in_another_league_is_refused(self):
        self.add_league()
        self.search(21, 'Elsewhere Owls', league=FIELD_LEAGUE_ID, league_name='Other League')
        r = self.follow(team_ids=[21])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.follows(), [0])

    def test_a_league_already_here_takes_its_stored_teams_with_no_fresh_check(self):
        league_id = self.add_league()
        self.seed_game(league_id)
        calls = len(self.fake.calls)
        r = self.follow(team_ids=[11, 12])
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.follows(), [11, 12])
        self.assertEqual(len(self.fake.calls), calls)

    def test_bad_team_ids_are_refused(self):
        self.add_league()
        for bad in ([0, 11], [-4], ['11'], [True], 11):
            r = self.follow(team_ids=bad)
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(self.follows(), [0])

    def test_a_league_here_is_answered_from_what_is_stored_with_no_call(self):
        league_id = self.add_league()
        self.seed_game(league_id)
        self.lookup(text=str(FIELD_LEAGUE_ID))  # a call just went out, so the next one would be refused
        calls = len(self.fake.calls)
        for body in ({'tsdb_id': LEAGUE_ID}, {'text': str(LEAGUE_ID)}):
            r = self.lookup(**body)
            self.assertEqual(r.status_code, 200, body)
            data = r.get_json()
            self.assertEqual(data['league']['name'], 'Pretend League')
            self.assertEqual(data['here']['id'], league_id)
            self.assertTrue(data['here']['whole'])
            self.assertEqual([t['id'] for t in data['here']['teams']], [12, 11])
        self.assertEqual(len(self.fake.calls), calls)

    def test_a_team_search_says_what_is_followed_in_its_league(self):
        self.assertIsNone(self.search(11, 'Pretendville Reds').get_json()['here'], 'a league not here')
        self.follow(team_ids=[11])
        here = self.search(12, 'Pretendville Blues').get_json()['here']
        self.assertEqual(here['chosen'], [11])
        self.assertFalse(here['whole'])
        self.assertEqual(here['chosen_names'], {'11': 'Pretendville Reds'})

    def test_change_teams_refuses_a_team_nobody_checked_or_stored(self):
        league_id = self.add_league()
        r = self.client.put(f'/api/sports/leagues/{league_id}/following', json={'team_ids': [999999]})
        self.assertEqual(r.status_code, 409)
        self.assertIn('has not checked team 999999 in the Pretend League', r.get_json()['error'])
        self.search(21, 'Elsewhere Owls', league=FIELD_LEAGUE_ID, league_name='Other League')
        r = self.client.put(f'/api/sports/leagues/{league_id}/following', json={'team_ids': [21]})
        self.assertEqual(r.status_code, 409)
        self.assertIn('Elsewhere Owls plays in the Other League, not the Pretend League', r.get_json()['error'])
        self.assertEqual(self.follows(), [0])

    def test_a_choice_from_several_says_what_is_followed_in_its_league(self):
        league_id = self.add_league()
        self.fake.team_search['reds'] = [team_row(11, 'Pretendville Reds'), team_row(12, 'Reds Reserves')]
        self.clear_pace()
        choices = self.lookup(text='Reds').get_json()['choices']
        self.assertEqual([c['here']['id'] for c in choices], [league_id, league_id])
        self.assertTrue(choices[0]['here']['whole'])

    def test_change_teams_stores_the_name_of_a_team_just_found(self):
        league_id = self.add_league()
        self.search(14, 'Pretendville Greens')
        r = self.client.put(f'/api/sports/leagues/{league_id}/following', json={'team_ids': [14]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.follows(), [14])
        self.assertEqual(SportsTeam.query.filter_by(tsdb_id=14).one().full_name, 'Pretendville Greens')

    def test_the_add_list_says_what_is_followed_in_each_league_here(self):
        nfl = 4391
        league_id = self.add_league(tsdb_id=nfl, name='NFL', sport='American Football')
        shipped = lambda: {s['tsdb_id']: s['following'] for s in sports_view.add_dialog_config()['shipped']}  # noqa: E731
        self.assertEqual(shipped()[nfl], 'Following all games')
        self.assertIsNone(shipped()[4387], 'a league never here shows its sport')
        sports_data.set_following(league_id, [11, 12])
        self.assertEqual(shipped()[nfl], 'Following 2 teams')
        sports_data.stop_following(league_id)
        self.assertEqual(shipped()[nfl], 'Not followed')
        db.session.add(SportsFollow(league_id=league_id, team_tsdb_id=0, rule_id=99))
        db.session.commit()
        self.assertEqual(shipped()[nfl], 'Followed by a rule')

    def test_change_teams_shows_for_a_league_followed_by_team_before_its_games(self):
        league_id = self.add_league(team={'team_id': 77, 'team': 'Early Team'})
        rows = {r['id']: r for r in sports_view.list_context()['rows']}
        self.assertTrue(rows[league_id]['has_teams'])
        self.assertIn('data-act="teams"', self.client.get(f'/sports/{league_id}').get_data(as_text=True))
        self.assertEqual(rows[league_id]['dialog']['chosen_names'], {77: 'Early Team'})
        sports_data.set_following(league_id, [])
        rows = {r['id']: r for r in sports_view.list_context()['rows']}
        self.assertFalse(rows[league_id]['has_teams'], 'a league followed whole with no games has no teams to pick')

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


class LeaguePageMidnightTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-11-14 12:14 AM: a league page kept a game only from the start of
    today, so a game that started at 11:42 PM and was still on, or recording, left the page at
    midnight."""
    TZ = ZoneInfo('America/New_York')
    NOW = datetime(2099, 11, 14, 5, 12)          # 12:12 AM Eastern

    def game(self, start):
        return SimpleNamespace(start_at=start, home_team_id=1, away_team_id=2, local_date=None)

    def test_a_game_that_started_before_midnight_stays_after_it(self):
        since = sports_view._league_page_since(self.NOW, self.TZ)
        late = self.game(self.NOW - timedelta(minutes=30))       # 11:42 PM the day before
        self.assertTrue(sports_view._on_league_page(late, since, self.NOW, self.TZ))

    def test_a_game_from_the_day_before_that_is_long_over_goes(self):
        since = sports_view._league_page_since(self.NOW, self.TZ)
        early = self.game(self.NOW - timedelta(hours=7))
        self.assertFalse(sports_view._on_league_page(early, since, self.NOW, self.TZ))

    def test_in_the_evening_a_morning_game_still_stays_for_the_day(self):
        evening = datetime(2099, 11, 14, 23, 0)                  # 6 PM Eastern
        since = sports_view._league_page_since(evening, self.TZ)
        morning = self.game(datetime(2099, 11, 14, 14, 0))       # 9 AM Eastern
        self.assertTrue(sports_view._on_league_page(morning, since, evening, self.TZ))


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
        self.assertEqual(sports_data.parse_entry('https://www.thesportsdb.com/team/134936-philadelphia-eagles'),
                         ('teamlink', 134936))
        self.assertEqual(sports_data.parse_entry('https://www.thesportsdb.com/player/1-X')[0], 'badlink')
        self.assertEqual(sports_data.parse_entry('https://example.test/x')[0], 'badlink')

    def test_a_link_to_something_else_names_both_shapes_with_no_call(self):
        r = self.post(text='https://www.thesportsdb.com/player/1-X')
        self.assertEqual(r.status_code, 400)
        error = r.get_json()['error']
        self.assertIn('not a league or a team', error)
        self.assertIn('thesportsdb.com/league/', error)
        self.assertIn('thesportsdb.com/team/', error)
        self.assertEqual(self.fake.calls, [])

    def test_a_team_link_finds_the_team_and_its_league_in_one_call(self):
        """dev/changelog/1497: a pasted team link does what typing the team's name and picking it does."""
        self.fake.teams[11] = team_row(11, 'Pretendville Reds', 'Reds')
        body = self.post(text='https://www.thesportsdb.com/team/11-pretendville-reds').get_json()
        self.assertEqual(body['team']['team_id'], 11)
        self.assertEqual(body['league']['tsdb_id'], LEAGUE_ID)
        self.assertFalse(body.get('best_match'))
        self.assertEqual([c[0] for c in self.fake.calls], ['lookupteam'])
        # Following it needs no second call: the team row was the check of its league.
        r = self.client.post('/api/sports/leagues', json={'tsdb_id': LEAGUE_ID, 'team_ids': [11]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([c[0] for c in self.fake.calls], ['lookupteam'])
        self.assertEqual(SportsLeague.query.count(), 1)

    def test_a_team_link_to_no_such_team_says_so(self):
        r = self.post(text='https://www.thesportsdb.com/team/424242-nobody')
        self.assertEqual(r.status_code, 404)
        self.assertIn('no team with the number 424242', r.get_json()['error'])

    def test_a_team_link_answered_with_some_other_team_is_not_that_team(self):
        self.fake.answer_overrides = {'lookupteam': {'teams': [team_row(12, 'Reds Reserves')]}}
        r = self.post(text='https://www.thesportsdb.com/team/11-pretendville-reds')
        self.assertEqual(r.status_code, 404)
        self.assertIn('no team with the number 11', r.get_json()['error'])

    def test_a_team_link_to_a_defunct_team_says_so(self):
        self.fake.teams[13] = team_row(13, 'Pretendville Soul', league=4809, league_name='_Defunct Pretend Ball Teams')
        r = self.post(text='https://www.thesportsdb.com/team/13-pretendville-soul')
        self.assertEqual(r.status_code, 404)
        self.assertIn('Pretendville Soul, a defunct team', r.get_json()['error'])

    def test_a_name_whose_best_match_is_defunct_says_what_was_found_and_why(self):
        """dev/changelog/1497: `philadelphia` gave Philadelphia Soul, in `_Defunct American Football Teams`."""
        self.fake.team_search['pretendville'] = [
            team_row(13, 'Pretendville Soul', league=4809, league_name='_Defunct Pretend Ball Teams')]
        r = self.post(text='Pretendville')
        self.assertEqual(r.status_code, 404)
        error = r.get_json()['error']
        self.assertIn('best match for "Pretendville" is Pretendville Soul, a defunct team', error)
        self.assertIn('Type more of the name', error)

    def test_a_defunct_team_is_never_offered_beside_live_ones_and_is_named(self):
        self.fake.team_search['reds'] = [team_row(11, 'Pretendville Reds'),
                                         team_row(13, 'Old Reds', league=4809, league_name='_Defunct Pretend Ball Teams')]
        body = self.post(text='Reds').get_json()
        self.assertEqual(body['team']['team_id'], 11)
        self.assertEqual(body['skipped'], ['Old Reds'])

    def test_a_defunct_teams_holding_league_cannot_be_followed_by_number(self):
        self.fake.leagues[4809] = league_row(4809, '_Defunct Pretend Ball Teams')
        r = self.post(text='4809')
        self.assertEqual(r.status_code, 404)
        self.assertIn('_Defunct Pretend Ball Teams, where TheSportsDB files teams that no longer play', r.get_json()['error'])

    def test_several_defunct_matches_are_all_named(self):
        self.fake.team_search['reds'] = [
            team_row(13, 'Old Reds', league=4809, league_name='_Defunct Pretend Ball Teams'),
            team_row(14, 'Older Reds', league=4809, league_name='_Defunct Pretend Ball Teams')]
        error = self.post(text='Reds').get_json()['error']
        self.assertIn('found only defunct teams for "Reds" (Old Reds and Older Reds), so they are left out', error)

    def test_only_a_name_search_is_marked_a_best_match(self):
        self.fake.team_search['pretendville reds'] = [team_row(11, 'Pretendville Reds', 'Reds')]
        self.assertTrue(self.post(text='Pretendville Reds').get_json()['best_match'])
        self.clear_pace()
        self.assertNotIn('best_match', self.post(tsdb_id=LEAGUE_ID).get_json())

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
        self.assertGreater(state.next_call_at, datetime.utcnow() + timedelta(seconds=sports_data.MIN_WAIT_SECONDS - 1))

    def test_a_lookup_inside_a_429s_pause_says_when_and_makes_no_call(self):
        """dev/docs/BUGS.md 2026-11-14: a person's lookup checked only the floor, so one made inside
        the pause drew another 429 and doubled the backoff again."""
        now = datetime.utcnow()
        db.session.add(SportsState(id=1, backoff_seconds=20, clean_calls=0, daily_offset_seconds=0,
                                   last_call_at=now - timedelta(seconds=30),
                                   next_call_at=now + timedelta(seconds=90)))
        db.session.commit()
        r = self.post(text=str(LEAGUE_ID))
        self.assertEqual(r.status_code, 409)
        self.assertGreaterEqual(r.get_json()['retry_after'], 85)
        self.assertEqual(self.fake.calls, [])

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
        self.client.post('/api/sports/leagues', json={'tsdb_id': LEAGUE_ID, 'team_ids': [11]})
        league = SportsLeague.query.filter_by(tsdb_id=LEAGUE_ID).one()
        self.assertEqual([f.team_tsdb_id for f in SportsFollow.query.filter_by(league_id=league.id)], [11])
        self.assertEqual(SportsTeam.query.filter_by(tsdb_id=11).one().full_name, 'Pretendville Reds')

    def seed_teams(self, league_id, *pairs):
        """Stored games between these teams, so a follow may name them (sports_data.known_team_ids())."""
        start = datetime.utcnow() + timedelta(days=1)
        for n, (home, away) in enumerate(pairs):
            db.session.add(SportsEvent(tsdb_id=700 + n, league_id=league_id, name='x', start_at=start,
                                       local_date=start.date(), home_team_id=home, away_team_id=away))
        db.session.commit()

    def test_changing_teams_makes_no_calls(self):
        league_id = self.add_league()
        self.seed_teams(league_id, (11, 12))
        calls = len(self.fake.calls)
        r = self.client.put(f'/api/sports/leagues/{league_id}/following', json={'team_ids': [11, 12]})
        self.assertTrue(r.get_json()['success'])
        self.assertEqual(sorted(f.team_tsdb_id for f in SportsFollow.query.all()), [11, 12])
        self.assertEqual(len(self.fake.calls), calls)
        self.client.put(f'/api/sports/leagues/{league_id}/following', json={'team_ids': []})
        db.session.expire_all()
        self.assertEqual([f.team_tsdb_id for f in SportsFollow.query.all()], [0])

    def test_a_repeated_team_is_stored_once(self):
        """dev/docs/BUGS.md 2026-11-15 @ 12:58:58 PM: a repeated id broke the follow's unique key
        and answered 500 with nothing saved."""
        league_id = self.add_league()
        self.seed_teams(league_id, (11, 12))
        r = self.client.put(f'/api/sports/leagues/{league_id}/following', json={'team_ids': [11, 11, 12]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sorted(f.team_tsdb_id for f in SportsFollow.query.all()), [11, 12])

    def test_anything_but_a_team_number_is_refused_and_the_follow_stands(self):
        """dev/docs/BUGS.md 2026-11-15 @ 12:58:58 PM: a 0 beside a team stored the whole league and
        that team together, and a negative id, a fraction or a number too big to hold was stored
        or answered 500."""
        league_id = self.add_league()
        for bad in ([0, 11], [-4], [11.7], ['11'], [True], [1e400], [None]):
            r = self.client.put(f'/api/sports/leagues/{league_id}/following', json={'team_ids': bad})
            self.assertEqual(r.status_code, 400, bad)
            db.session.expire_all()
            self.assertEqual([f.team_tsdb_id for f in SportsFollow.query.all()], [0], bad)

    def test_the_dialog_offers_the_six_default_leagues_alphabetically_and_follows_none(self):
        """The shipped picks (dev/changelog/1446): offered, never followed until a click."""
        shipped = sports_view.add_dialog_config()['shipped']
        names = [s['name'] for s in shipped]
        self.assertEqual(names, ['English Premier League', 'Formula 1', 'NASCAR Cup Series', 'NBA', 'NFL', 'NHL'])
        self.assertEqual(names, sorted(names, key=str.lower))
        self.assertTrue(all(s['following'] is None for s in shipped))
        self.assertEqual(SportsFollow.query.count(), 0)

    def test_stop_and_delete_routes_answer_404_for_a_league_gone(self):
        self.assertEqual(self.client.delete('/api/sports/leagues/999/following').status_code, 404)
        self.assertEqual(self.client.delete('/api/sports/leagues/999').status_code, 404)


class WorkerWakeTests(unittest.TestCase):
    """The worker's interval is how often an idle worker looks; a pace under it holds because the job
    wakes itself at the stored next call (dev/changelog/1551)."""

    def wake(self, due, next_run, job=True):
        from app import scheduler
        stub = SimpleNamespace(get_job=lambda job_id: SimpleNamespace(next_run_time=next_run) if job else None)
        with patch.object(scheduler, '_scheduler', stub), patch.object(scheduler, '_modify_job') as modify:
            scheduler._wake_sports_worker_at(due)
        return modify

    def test_a_next_call_sooner_than_the_interval_brings_the_tick_forward(self):
        due = datetime.utcnow() + timedelta(seconds=9)
        modify = self.wake(due, datetime.now(ZoneInfo('UTC')) + timedelta(seconds=15))
        modify.assert_called_once()
        self.assertEqual(modify.call_args.kwargs['next_run_time'], due)

    def test_a_next_call_after_the_next_tick_leaves_it(self):
        self.wake(datetime.utcnow() + timedelta(seconds=40),
                  datetime.now(ZoneInfo('UTC')) + timedelta(seconds=15)).assert_not_called()

    def test_a_next_call_already_past_leaves_it_so_an_idle_worker_never_spins(self):
        self.wake(datetime.utcnow() - timedelta(seconds=5),
                  datetime.now(ZoneInfo('UTC')) + timedelta(seconds=15)).assert_not_called()

    def test_no_job_no_change(self):
        self.wake(datetime.utcnow() + timedelta(seconds=9), None, job=False).assert_not_called()


class PaceSettingTests(SportsCase):
    def save(self, path, value):
        return self.client.post('/api/settings/field', json={'path': path, 'value': value})

    def test_pace_error_is_about_the_shortest_wait(self):
        self.assertEqual(sports_data.pace_error(60, 20), '')
        self.assertEqual(sports_data.pace_error(10, 2), '')
        self.assertIn('8', sports_data.pace_error(9, 2))

    def test_a_spacing_too_fast_for_the_jitter_is_refused(self):
        r = self.save('sports_data.call_spacing_seconds', 9)
        self.assertEqual(r.status_code, 400)
        self.assertIn('at least 8', r.get_json()['error'])

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
        self.assertIn('deletes each one 9 days after', self.league_html(league_id))

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

    def test_a_league_not_followed_says_it_is_not_checking(self):
        league_id = self.seed()
        SportsFollow.query.delete()
        db.session.commit()
        text = self.league_html(league_id)
        self.assertIn('Not followed.', text)
        self.assertIn('Follow again', text)
        self.assertIn('It still has 2 games', text)
        self.assertIn('Not checking for games', self.list_html())

    def test_a_league_not_followed_with_nothing_stored_mentions_no_games(self):
        """dev/docs/BUGS.md 2026-11-07 10:31: a league with nothing stored read "Its 0 games stay until..."."""
        league_id = self.seed()
        SportsFollow.query.delete()
        SportsEvent.query.delete()
        db.session.commit()
        text = self.league_html(league_id)
        self.assertIn('Not followed.', text)
        self.assertNotIn('0 games', text)
        self.assertNotIn('after each one', text)
        self.assertNotIn("after it&#39;s over", text)

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

    def test_a_racing_league_is_never_called_games_in_its_dialogs(self):
        """Stop following and Delete league read the league's own noun from the row
        (dev/changelog/1568): they said "games" of a race."""
        when = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=3)
        self.fake.next_event[FIELD_LEAGUE_ID] = [race(7, 'Pretend 500', iso(when))]
        self.fake.rounds[(FIELD_LEAGUE_ID, '2099', 30)] = [race(7, 'Pretend 500', iso(when))]
        league_id = self.add_league(FIELD_LEAGUE_ID, name='Pretend Racing', sport='Motorsport')
        self.drain(start=datetime.utcnow())
        for text in (self.league_html(league_id), self.list_html()):
            dialog = json.loads(html_lib.unescape(re.search(r'data-dialog="([^"]*)"', text).group(1)))
            self.assertEqual((dialog['one'], dialog['many']), ('race', 'races'))

    def test_a_league_gone_is_a_404(self):
        self.assertEqual(self.client.get('/sports/999').status_code, 404)

    def test_no_own_key_anywhere(self):
        league_id = self.seed()
        for text in (self.list_html(), self.league_html(league_id)):
            self.assertNotRegex(text.split('<main', 1)[1].split('</main>')[0], r'(?i)api key|own key|paid')


if __name__ == '__main__':
    unittest.main()
