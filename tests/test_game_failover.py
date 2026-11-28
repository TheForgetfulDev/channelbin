"""A recording of a game whose channel dies moves to another channel carrying the game, the way a
channel group moves between its members (dev/docs/BUGS.md 2026-11-28, dev/changelog/1793).

Prod recording 88 (Supercars, 2026-11-27) was made from the one channel of its listing, an event
channel that never sent a byte, and was in no group, so it had nowhere to go while 95 channels
carried the race on a wider listing. These drive recorder.failover_group_member() - the call every
watchdog give-up point makes - against a game laid out the same way.

Made-up league and teams; a throwaway temp SQLite database.

  python3 -m unittest tests.test_game_failover
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (make_account, make_channel, make_group,  # noqa: E402
                                make_guide_match, make_recording)

from app import db, recorder  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.database import (CHANNEL_FAILOVER_HEALTH_OBSERVATION, GROUP_FAILOVER,  # noqa: E402
                          AccountBlock,
                          REC_STATUS_IN_PROGRESS, ChannelEvent, Recording, RecordingEvent,
                          SportsEvent, SportsFollow, SportsLeague)
from app.health_score import _final_member_success_quality  # noqa: E402

TITLE = 'PFL Racing'


class _Game(unittest.TestCase):
    """One game: a lone event channel listing it near its start, and a wider listing on other
    channels that started earlier, the shape of prod recording 88."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.t.sandbox_config({'display': {'timezone': 'America/New_York', 'time_format': '12h'},
                               'recording': {'live_thumbnail': {'enabled': False}}})
        self.now = datetime.utcnow().replace(second=0, microsecond=0)
        self.race = self.now + timedelta(minutes=20)
        league = SportsLeague(tsdb_id=9101, name='Pretend Racing', sport='Motorsport', current_season='2099',
                              match_terms=json.dumps(['PFL']))
        db.session.add(league)
        db.session.flush()
        self.league = league
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0, rule_id=0))
        self.event = SportsEvent(tsdb_id=8101, league_id=league.id, name='Pretend Grand Final Race 35',
                                 start_at=self.race, guide_start=self.race, local_date=self.race.date())
        self.other = SportsEvent(tsdb_id=8102, league_id=league.id, name='Pretend Sprint Race 34',
                                 start_at=self.race, guide_start=self.race, local_date=self.race.date())
        db.session.add_all([self.event, self.other])
        db.session.commit()
        self._n = 0
        connlim._holders.clear()
        self.addCleanup(connlim._holders.clear)
        self.lead_start = self.now + timedelta(minutes=15)
        self.wide_start = self.now - timedelta(minutes=40)

    def channel(self, name, score=None):
        self._n += 1
        ch = make_channel(make_account(f'Account {self._n}', max_connections=1), stream_id=700 + self._n,
                          name=name)
        ch.health_score = score
        db.session.flush()
        return ch

    def lists(self, channel, start, hours=1.0, event=None, **fields):
        make_guide_match(event or self.event, channel, start, hours=hours, title=TITLE, **fields)
        db.session.commit()

    def recording(self, channel, group=None):
        rec = make_recording(status=REC_STATUS_IN_PROGRESS, name='race', channel_id=channel.id,
                             group_id=group.id if group else None,
                             start_time=self.now - timedelta(minutes=5),
                             stop_time=self.now + timedelta(hours=2, minutes=40),
                             program_start_time=self.lead_start,
                             program_stop_time=self.lead_start + timedelta(hours=1),
                             program_title=TITLE, source_channel_id=channel.id)
        db.session.commit()
        recorder._active[rec.id] = recorder.RecordingState(current_segment_num=1)
        self.addCleanup(recorder._active.pop, rec.id, None)
        self.assertTrue(connlim.try_acquire(channel.account_id, 'recording', rec.id))
        return rec.id

    def fail_over(self, rid, **kw):
        moved = recorder.failover_group_member(self.t.app, rid, 'dead-stream fast-fail', **kw)
        db.session.expire_all()
        return moved, db.session.get(Recording, rid)

    def moves(self, rid):
        return [json.loads(e.extra_data) for e in RecordingEvent.query.filter_by(
            recording_id=rid, event_type=GROUP_FAILOVER).order_by(RecordingEvent.id)]

    def supercars(self):
        """The event channel on its own listing, and three channels on the wider one."""
        lone = self.channel('Event 12')
        self.lists(lone, self.lead_start)
        wide = [self.channel('Sport 2', 80), self.channel('Sport 506', 60), self.channel('Fox 506', 40)]
        for ch in wide:
            self.lists(ch, self.wide_start, hours=3.5)
        return lone, wide


class LoneChannelTests(_Game):

    def test_a_dead_event_channel_moves_to_a_channel_on_the_game_s_wider_listing(self):
        lone, wide = self.supercars()
        rid = self.recording(lone)
        moved, rec = self.fail_over(rid)
        self.assertTrue(moved)
        self.assertIn(rec.channel_id, {ch.id for ch in wide})
        self.assertEqual(rec.consecutive_failures, 0)
        [move] = self.moves(rid)
        self.assertIsNone(move['group_id'])
        self.assertEqual((move['game_event_id'], move['from_channel_id']), (self.event.id, lone.id))
        self.assertIn('counters_at_failover', move)
        detail = RecordingEvent.query.filter_by(recording_id=rid, event_type=GROUP_FAILOVER).one().detail
        self.assertIn('Feed "Event 12" died (dead-stream fast-fail)', detail)
        self.assertIn('another channel carrying', detail)
        # The seat went with it: the old account is free, the new one held.
        self.assertFalse(connlim.at_limit(lone.account_id))
        self.assertTrue(connlim.at_limit(rec.channel.account_id))

    def test_the_channel_moved_off_takes_a_health_hit(self):
        """The channel a recording has to move off is scored, the same as a group member that
        dies (dev/changelog/1793)."""
        lone, _wide = self.supercars()
        rid = self.recording(lone)
        self.assertTrue(self.fail_over(rid)[0])
        ev = ChannelEvent.query.filter_by(channel_id=lone.id,
                                          event_type=CHANNEL_FAILOVER_HEALTH_OBSERVATION).one()
        self.assertIn('the recording moved to another channel', ev.detail)
        self.assertIsNotNone(db.session.get(type(lone), lone.id).health_score)

    def test_it_walks_every_channel_carrying_the_game_and_then_stops(self):
        lone, wide = self.supercars()
        rid = self.recording(lone)
        seen = []
        for _ in wide:
            moved, rec = self.fail_over(rid)
            self.assertTrue(moved)
            seen.append(rec.channel_id)
        self.assertEqual(sorted(seen), sorted(ch.id for ch in wide))
        self.assertFalse(self.fail_over(rid)[0], 'it came back to a channel that already died')

    def test_a_channel_showing_another_game_is_never_taken(self):
        lone = self.channel('Event 12')
        self.lists(lone, self.lead_start)
        elsewhere = self.channel('Other race', 99)
        self.lists(elsewhere, self.wide_start, hours=3.5, event=self.other)
        rid = self.recording(lone)
        self.assertFalse(self.fail_over(rid)[0])

    def test_a_listing_starting_well_after_the_recording_s_program_is_not_taken(self):
        lone = self.channel('Event 12')
        self.lists(lone, self.lead_start)
        later = self.channel('Delayed')
        self.lists(later, self.lead_start + timedelta(minutes=45))
        rid = self.recording(lone)
        self.assertFalse(self.fail_over(rid)[0])

    def test_a_replay_recording_has_no_copy_to_move_to(self):
        lone = self.channel('Event 12')
        self.lists(lone, self.lead_start, airing='replay')
        wide = self.channel('Sport 2', 80)
        self.lists(wide, self.wide_start, hours=3.5)
        rid = self.recording(lone)
        self.assertFalse(self.fail_over(rid)[0])

    def test_a_recording_of_no_game_has_nowhere_to_go(self):
        plain = self.channel('News')
        rid = self.recording(plain)
        self.assertFalse(self.fail_over(rid)[0])

    def test_only_a_dead_feed_moves_it(self):
        """A stall demotion, a block move and a person's pick stay a group's."""
        lone, _wide = self.supercars()
        rid = self.recording(lone)
        self.assertFalse(self.fail_over(rid, demote=True)[0])
        self.assertFalse(self.fail_over(rid, block_move=True)[0])
        self.assertFalse(self.fail_over(rid, target_channel_id=_wide[0].id)[0])
        self.assertEqual(self.moves(rid), [])

    def test_a_blocked_account_and_a_full_one_are_passed_over(self):
        lone, (blocked, full, free) = self.supercars()
        db.session.add(AccountBlock(account_id=blocked.account_id, start_time=self.now - timedelta(hours=1),
                                    stop_time=self.now + timedelta(hours=4)))
        db.session.commit()
        self.assertTrue(connlim.try_acquire(full.account_id, 'recording', 99999))
        rid = self.recording(lone)
        moved, rec = self.fail_over(rid)
        self.assertTrue(moved)
        self.assertEqual(rec.channel_id, free.id)
        [move] = self.moves(rid)
        self.assertEqual(move['skipped_no_seat_channel_ids'], [full.id])
        self.assertEqual(move['skipped_blocked_channel_ids'], [blocked.id])

    def test_a_free_connection_goes_ahead_of_cutting_off_an_iptv_player(self):
        """The game's first channel is on an account an IPTV player is watching through; the
        next one's account is free, so the recording takes that one and nobody is cut off."""
        lone, (watched, free, _last) = self.supercars()
        self.assertTrue(connlim.try_acquire(watched.account_id, 'viewer', 'player-1'))
        self.assertEqual(connlim.seat_standing({watched.account_id}, 'recording')[watched.account_id],
                         connlim.SEAT_BY_CUTTING_VIEWER)
        rid = self.recording(lone)
        moved, rec = self.fail_over(rid)
        self.assertTrue(moved)
        self.assertEqual(rec.channel_id, free.id)

    def test_the_recording_page_names_the_move(self):
        """The move shows on the recording's page with links to both channels, though no group
        is behind it."""
        lone, wide = self.supercars()
        rid = self.recording(lone)
        self.assertTrue(self.fail_over(rid)[0])
        resp = self.t.client.get(f'/recordings/{rid}')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('another channel carrying', html)
        self.assertIn(f'/channels/{lone.id}', html)

    def test_the_last_channel_is_scored_on_its_own_share(self):
        """A recording outside a group that moved is scored like a group's: the channel it ends
        on answers only for its own time (health_score._final_member_success_quality)."""
        lone, _wide = self.supercars()
        rid = self.recording(lone)
        rec = db.session.get(Recording, rid)
        rec.total_downtime_seconds = 600
        db.session.commit()
        self.assertTrue(self.fail_over(rid)[0])
        rec = db.session.get(Recording, rid)
        _quality, breakdown = _final_member_success_quality(rec, {})
        self.assertEqual(breakdown['member_share']['downtime_seconds'], 0)


class GroupOfTheGameTests(_Game):

    def test_a_group_recording_whose_members_are_used_up_goes_on_to_the_game_s_channels(self):
        lone, wide = self.supercars()
        member = self.channel('Event 12 copy')
        self.lists(member, self.lead_start)
        group = make_group('Event 12', members=[lone, member])
        db.session.commit()
        rid = self.recording(lone, group=group)
        moved, rec = self.fail_over(rid)
        self.assertTrue(moved)
        self.assertEqual(rec.channel_id, member.id, 'its own members come first')
        moved, rec = self.fail_over(rid)
        self.assertTrue(moved)
        self.assertIn(rec.channel_id, {ch.id for ch in wide})
        last = RecordingEvent.query.filter_by(recording_id=rid, event_type=GROUP_FAILOVER).order_by(
            RecordingEvent.id.desc()).first()
        self.assertIn('Group "Event 12" has no member left to move to.', last.detail)
        self.assertEqual(json.loads(last.extra_data)['group_id'], group.id)


class PlayTests(_Game):
    """The IPTV output side already walks the game's channels when one dies
    (output_feed._failover over auto_rules.game_sources()); this pins it for recording 88's
    shape, a lone channel on one listing and the rest on a wider one. A characterization test:
    it passed before dev/changelog/1793."""

    def test_a_play_walks_from_the_lone_listing_to_the_wider_one(self):
        import app.output_feed as feed
        from app.config import load_config
        self.t.sandbox_config({'integrations': {'output_feed': {'enabled': True, 'username': 'u',
                                                                'password': 'p', 'mode': 'redirect'}}})
        lone, wide = self.supercars()
        cfg, tried = load_config(), set()
        for _ in range(4):
            play = feed.resolve_game_play(self.event, self.league, 1, 'race', cfg,
                                          exclude_ids=frozenset(tried), now=self.now)
            self.assertIsNotNone(play.member, play.why_none)
            tried.add(play.member.id)
        self.assertEqual(tried, {lone.id} | {ch.id for ch in wide})
        play = feed.resolve_game_play(self.event, self.league, 1, 'race', cfg,
                                      exclude_ids=frozenset(tried), now=self.now)
        self.assertIsNone(play.member)


if __name__ == '__main__':
    unittest.main()
