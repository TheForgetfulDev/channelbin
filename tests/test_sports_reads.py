"""Reads of TheSportsDB around a followed game (app/sports_reads.py, dev/changelog/1632): a game with an
automated recording or on a sports channel is read every 30 minutes from 2 hours before its earlier
start until its window ends, by its round when the round answer covers it and by itself when the round
was cut, only in a league that goes by TheSportsDB or covers both. Made-up league and teams; throwaway
temp SQLite DB - never the live dvr.db, and no call leaves the box.
  python3 -m unittest tests.test_sports_reads
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_recording  # noqa: E402

from app import db, sports_data, sports_feed, sports_reads, sports_times  # noqa: E402
from app.database import (  # noqa: E402
    SPORTS_FETCH_CAPPED_PAST, SPORTS_FETCH_OK, SPORTS_KIND_EVENT, SPORTS_KIND_ROUND, AutoRule, RecordingSports,
    SportsEvent, SportsFeedChannel, SportsFetch, SportsFollow, SportsLeague, SportsMatch, SportsState)

#: The guide the output sends with xmltv_days unset (output_feed.guide_horizon()).
HORIZON = timedelta(days=3)

H, M = timedelta(hours=1), timedelta(minutes=1)
ROUND = 'round:9001:2099:12'


class Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.t.sandbox_config({'integrations': {'output_feed': {'enabled': False}}})
        self.now = datetime.utcnow().replace(second=0, microsecond=0)
        self.league = SportsLeague(tsdb_id=9001, name='Pretend Football League', sport='Pretend Ball',
                                   current_season='2099', match_terms=json.dumps(['PFL']))
        db.session.add(self.league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=self.league.id, team_tsdb_id=0, rule_id=0))
        self.acc = make_account()
        self.ch = make_channel(self.acc, name='Local 1')
        self.round_row = SportsFetch(target=ROUND, kind=SPORTS_KIND_ROUND, league_id=self.league.id,
                                     outcome=SPORTS_FETCH_OK, last_at=self.now - 40 * M,
                                     next_due_at=self.now + 20 * H)
        db.session.add(self.round_row)
        self._tsdb = 7000
        db.session.commit()

    def game(self, start, **kw):
        self._tsdb += 1
        e = SportsEvent(tsdb_id=self._tsdb, league_id=self.league.id, name=f'Game {self._tsdb}', season='2099',
                        round=12, home_team_id=1, away_team_id=2, home_name='Ironvale Bears',
                        away_name='Harborview Gulls', start_at=start, guide_start=start, local_date=start.date(), **kw)
        db.session.add(e)
        db.session.flush()
        db.session.add(SportsMatch(event_id=e.id, channel_id=self.ch.id, kind='guide', program_start_time=start,
                                   program_stop_time=start + 3 * H, title_key='pfl football', episode_key='x',
                                   program_title='PFL Football', strength='strong', airing='first', reason='x'))
        db.session.commit()
        return e

    def recorded(self, event, kind='rule', status='SCHEDULED'):
        kw = {}
        if kind == 'rule':
            rule = AutoRule(name='PFL games', search_params='q=PFL')
            db.session.add(rule)
            db.session.flush()
            kw['rule_id'] = rule.id
        elif kind == 'record':
            kw['sports_record_tsdb_id'] = event.tsdb_id
        rec = make_recording(status=status, channel_id=self.ch.id, start_time=event.start_at,
                             stop_time=event.start_at + 3 * H, program_start_time=event.start_at,
                             program_stop_time=event.start_at + 3 * H, **kw)
        db.session.add(RecordingSports(recording_id=rec.id, tsdb_event_id=event.tsdb_id, league_name=self.league.name,
                                       event_name=event.name, strength='strong', airing='first', reason='x',
                                       linked_at=self.now, updated_at=self.now))
        db.session.commit()
        return rec

    def arm(self, at=None):
        n = sports_reads.arm(at or self.now)
        db.session.expire_all()
        return n

    def due(self, target=ROUND):
        row = SportsFetch.query.filter_by(target=target).first()
        return row.next_due_at if row else None


class ReachTests(Case):
    def test_a_rule_recorded_game_an_hour_off_is_read_now(self):
        self.recorded(self.game(self.now + H))
        self.assertEqual(self.arm(), 1)
        self.assertEqual(self.due(), self.now)

    def test_read_thirty_minutes_after_the_last_read_never_sooner(self):
        self.recorded(self.game(self.now + H))
        row = db.session.get(SportsFetch, self.round_row.id)
        row.last_at = self.now - 10 * M
        db.session.commit()
        self.arm()
        self.assertEqual(self.due(), self.now + 20 * M)

    def test_a_record_recording_and_one_under_way_follow_too(self):
        self.recorded(self.game(self.now + H), kind='record')
        self.assertEqual(self.arm(), 1)
        row = db.session.get(SportsFetch, self.round_row.id)
        row.next_due_at = self.now + 20 * H
        db.session.commit()
        self.recorded(self.game(self.now - H), status='IN_PROGRESS')
        self.assertEqual(self.arm(), 1)

    def test_further_off_than_two_hours_is_not_read(self):
        self.recorded(self.game(self.now + 2 * H + M))
        self.assertEqual(self.arm(), 0)
        self.assertEqual(self.due(), self.now + 20 * H)

    def test_the_earlier_of_the_two_starts_opens_the_reads(self):
        """TheSportsDB at 3 hours off, the guide still at 1 hour: the reads open on the guide's."""
        e = self.game(self.now + 3 * H, guide_behind_start=self.now + H, previous_start=self.now + H)
        self.recorded(e)
        self.assertEqual(self.arm(), 1)

    def test_a_game_whose_window_ended_is_not_read(self):
        self.recorded(self.game(self.now - 3 * H - M))
        self.assertEqual(self.arm(), 0)

    def test_a_game_running_late_is_read_until_its_later_end(self):
        """Moved 90 minutes later after it started (dev/changelog/1632): its window ends 90 minutes after
        its listing, and it is read until then."""
        start = self.now - 3 * H - 30 * M
        e = self.game(start)
        self.recorded(e, status='IN_PROGRESS')
        self.assertEqual(self.arm(), 0, 'its listing ended 30 minutes ago')
        e.previous_start, e.start_at, e.start_changed_at = start, start + 90 * M, start + 30 * M
        db.session.commit()
        self.assertEqual(self.arm(), 1)

    def test_go_by_the_guide_is_never_read_for_it(self):
        sports_times.set_league(9001, {'go_by': sports_times.GO_GUIDE})
        self.recorded(self.game(self.now + H))
        self.assertEqual(self.arm(), 0)

    def test_cover_both_is_read(self):
        sports_times.set_league(9001, {'go_by': sports_times.GO_BOTH})
        self.recorded(self.game(self.now + H))
        self.assertEqual(self.arm(), 1)

    def test_a_tv_guide_recording_does_not_make_a_game_followed(self):
        """Only automated surfaces follow the game (dev/changelog/1624)."""
        self.recorded(self.game(self.now + H), kind='guide')
        self.assertEqual(self.arm(), 0)

    def test_a_finished_recording_does_not_make_a_game_followed(self):
        self.recorded(self.game(self.now + H), status='COMPLETED')
        self.assertEqual(self.arm(), 0)


class TargetTests(Case):
    def test_two_followed_games_in_one_round_are_one_call(self):
        self.recorded(self.game(self.now + H))
        self.recorded(self.game(self.now + 90 * M))
        self.assertEqual(self.arm(), 1)
        self.assertEqual(SportsFetch.query.count(), 1)

    def test_a_cut_round_reads_the_game_by_itself(self):
        row = db.session.get(SportsFetch, self.round_row.id)
        row.outcome = SPORTS_FETCH_CAPPED_PAST
        db.session.commit()
        e = self.game(self.now + H)
        self.recorded(e)
        self.assertEqual(self.arm(), 1)
        self.assertEqual(self.due(f'event:{e.tsdb_id}'), self.now)
        self.assertEqual(self.due(), self.now + 20 * H)
        self.assertEqual(SportsFetch.query.filter_by(target=f'event:{e.tsdb_id}').one().kind, SPORTS_KIND_EVENT)

    def test_a_round_with_no_row_reads_the_game_by_itself_and_starts_no_walk(self):
        db.session.delete(db.session.get(SportsFetch, self.round_row.id))
        db.session.commit()
        e = self.game(self.now + H)
        self.recorded(e)
        self.arm()
        self.assertIsNone(SportsFetch.query.filter_by(target=ROUND).first())
        self.assertEqual(self.due(f'event:{e.tsdb_id}'), self.now)

    def test_a_second_pass_brings_nothing_forward(self):
        self.recorded(self.game(self.now + H))
        self.arm()
        self.assertEqual(self.arm(), 0)


class FeedTests(Case):
    def test_a_game_on_a_sports_channel_is_followed_while_the_feed_is_on(self):
        self.game(self.now + H)
        self.league.feed_league_channels = True
        db.session.add(SportsFeedChannel(league_id=self.league.id, number=1))
        db.session.commit()
        sports_feed.place_all(self.now, HORIZON)
        self.assertEqual(self.arm(), 0, 'the output feed is off')
        self.t.sandbox_config({'integrations': {'output_feed': {'enabled': True}}})
        self.assertEqual(self.arm(), 1)


class TickTests(Case):
    def test_the_worker_makes_the_read_on_the_tick_that_brings_it_forward(self):
        db.session.add(SportsState(id=1))
        db.session.commit()
        self.recorded(self.game(self.now + H))
        calls = []

        def answer(endpoint, params, list_key):
            calls.append((endpoint, params))
            return sports_data.Answer(outcome=SPORTS_FETCH_OK, rows=[])

        with mock.patch.object(sports_data, '_call', side_effect=answer), \
                mock.patch('app.scheduler.queue_sports_match'):
            sports_data.tick(now=self.now)
        self.assertEqual(calls, [('eventsround', {'id': 9001, 's': '2099', 'r': '12'})])

    def test_a_fault_bringing_reads_forward_never_stops_the_worker(self):
        db.session.add(SportsState(id=1))
        db.session.commit()
        with mock.patch.object(sports_reads, 'arm', side_effect=RuntimeError('boom')), \
                self.assertLogs('app.sports_reads', level='ERROR'):
            self.assertEqual(sports_data.tick(now=self.now), 'idle')


if __name__ == '__main__':
    unittest.main()
