"""Tier 2 - moving off a group member that keeps stalling while every restart succeeds.

The watchdog's three long-standing failover triggers are all "the feed is dead" shaped, so
a member that stalls constantly and always comes back reaches none of them: the successful
restart zeroes the very counter that would trip max_consecutive_failures. Recording 14 took
27 stalls, 27 restarts and 464s of downtime on one member with consecutive_failures_peak
stuck at 1 against a threshold of 10, while 29 same-format members sat idle in its group.

Design and measurements: dev/changelog/889, dev/docs/DESIGN-channel-groups-model.md 18.

Three properties are guarded here, and they fail in different ways:

  * the trigger is a RATE, and widening the window LOOSENS it (StallWindowTests, replayed
    against recording 14's real stall timestamps);
  * a stall-demoted member is demoted, never burned - it stays selectable and ranks below
    the members not yet moved off, so a small group cycles back around to it and a
    one-member group is a no-op rather than an abort (DemoteNotBurnTests);
  * the departed member is scored on its own measured share, never the recording fail
    floor, because it was still delivering content (DemotionScoringTests).

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_stall_rate_failover
"""
import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.recorder as recorder  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Channel, ChannelEvent, Recording, RecordingEvent, GROUP_FAILOVER, STALL_DETECTED,
    CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION, GROUP_FORMAT_MANUAL,
)
from app.health_score import RECORDING_FAIL_FLOOR_DEFAULT  # noqa: E402
from app.watchdog import WatchdogThread, stalls_within_window  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support.seed import (  # noqa: E402
    make_account, make_channel, make_channel_test, make_group, make_recording,
)
from tests.test_downtime_accounting import _RestartHarness, _sleeper  # noqa: E402
import app.config as cfgmod  # noqa: E402


# Recording 14's 27 stalls, in minutes from the start of the recording, read off its
# STALL_DETECTED events. This is the dataset the trigger's shape was chosen against, so it
# is the fixture that keeps a later "simplification" of the window from quietly changing
# which recordings move and when.
RECORDING_14_STALL_MINUTES = [
    10.3, 15.2, 24.2, 36.1, 64.2, 64.6, 65.0, 66.9, 77.7, 81.0, 85.9, 92.1, 94.5, 95.0,
    102.5, 103.1, 113.3, 117.8, 119.7, 120.1, 125.4, 127.7, 128.7, 131.5, 134.3, 134.6,
    142.0,
]


def _first_trip_minute(stall_minutes, count, window_minutes):
    """Replay `stall_minutes` through the watchdog's own window helper and return the
    minute at which `count` stalls first sit inside `window_minutes` - i.e. when the move
    would fire. None if it never does."""
    seen = []
    for minute in stall_minutes:
        now = minute * 60
        seen.append(now)
        seen = stalls_within_window(seen, now, window_minutes * 60)
        if len(seen) >= count:
            return minute
    return None


class StallWindowTests(unittest.TestCase):
    """The trigger is a rate over a rolling window, and the direction a wider window moves
    it is the counter-intuitive half worth pinning down."""

    def test_a_wider_window_fires_earlier_not_later(self):
        """Same count, more time to reach it. Read as "stricter" this would have been
        configured backwards."""
        narrow = _first_trip_minute(RECORDING_14_STALL_MINUTES, 3, 10)
        wide = _first_trip_minute(RECORDING_14_STALL_MINUTES, 3, 30)
        self.assertEqual(narrow, 65.0)
        self.assertEqual(wide, 24.2)
        self.assertLess(wide, narrow,
                        'widening the window must loosen the trigger, not tighten it')

    def test_a_slow_drip_never_trips_the_shipped_default(self):
        """Three stalls 20 minutes apart is a feed limping, not one collapsing, and the
        default 3-in-30 must leave it alone."""
        drip = [0.0, 20.0, 40.0, 60.0, 80.0, 100.0]
        self.assertIsNone(_first_trip_minute(drip, 3, 30))

    def test_stalls_older_than_the_window_are_dropped(self):
        now = 10_000.0
        times = [now - 3600, now - 60, now]
        self.assertEqual(stalls_within_window(times, now, 1800), [now - 60, now])

    def test_a_zero_length_window_keeps_only_the_current_stall(self):
        now = 500.0
        self.assertEqual(stalls_within_window([100.0, 400.0, now], now, 0), [now])


class _StallTriggerHarness(_RestartHarness):
    """The real WatchdogThread over a group-backed recording whose restarts all succeed -
    the exact shape none of the three dead-feed triggers catches."""

    _stall_move_cfg = {'stall_move_count': 2, 'stall_move_window_minutes': 30}

    def setUp(self):
        super().setUp()
        acct = make_account(name='Stall Provider')
        self.current = make_channel(acct, name='Stalls A Lot', health_score=90)
        self.spare = make_channel(acct, name='Spare', health_score=80)
        grp = make_group(members=[self.current, self.spare])
        rec = db.session.get(Recording, self.rid)
        rec.channel_id = self.current.id
        rec.group_id = grp.id
        db.session.commit()
        self.group_id = grp.id
        self._thumb_patcher = mock.patch.object(recorder, 'persist_final_thumbnail')
        self._thumb_patcher.start()

    def tearDown(self):
        self._thumb_patcher.stop()
        super().tearDown()

    def _cfg(self, stall_timeout, restart_delay=0):
        cfg = super()._cfg(stall_timeout, restart_delay)
        cfg['watchdog'].update(self._stall_move_cfg)
        return cfg

    def _run_until_stalls(self, n, *, stall_timeout=1, restart_delay=0, timeout=60):
        """Run the real thread until `n` STALL_DETECTED events exist, with every restart
        producing data (so consecutive_failures is reset each time, exactly as the feed
        under test did). Returns the recorded failover_group_member calls."""
        calls = []

        def _fake_failover(app, recording_id, reason, demote=False):
            db.session.expire_all()
            stalls = RecordingEvent.query.filter_by(
                recording_id=recording_id, event_type=STALL_DETECTED).count()
            calls.append({'reason': reason, 'demote': demote, 'stalls_so_far': stalls})
            return False

        self.state.process = _sleeper()
        with mock.patch.object(cfgmod, 'load_config',
                               return_value=self._cfg(stall_timeout, restart_delay)), \
             mock.patch.object(recorder, '_launch_segment',
                               self._stub_launch_next(produces_data=True)), \
             mock.patch.object(recorder, 'failover_group_member', _fake_failover):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                db.session.expire_all()
                if RecordingEvent.query.filter_by(
                        recording_id=self.rid, event_type=STALL_DETECTED).count() >= n:
                    break
                time.sleep(0.2)
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        db.session.expire_all()
        return calls


class StallTriggerTests(_StallTriggerHarness):

    def _demote_calls(self, n=2):
        """Only the stall-rate requests. Under load a restart can fail to produce data in
        time, and that is one of the three DEAD-feed triggers asking for its own
        (non-demoting) failover - a real event these tests are not about."""
        return [c for c in self._run_until_stalls(n) if c['demote']]

    def test_repeated_stalls_move_the_recording_even_though_every_restart_succeeds(self):
        """The whole defect in one assertion: consecutive_failures never reaches its
        threshold because each restart works, so before this trigger nothing ever asked
        for a failover."""
        calls = self._demote_calls()
        rec = db.session.get(Recording, self.rid)
        self.assertLess(rec.consecutive_failures, 10,
                        'the restarts were meant to succeed - the dead-feed triggers must '
                        'not be what fired here')
        self.assertTrue(calls, 'no stall-rate move was requested despite repeated stalls')

    def test_the_move_is_not_requested_before_the_count_is_reached(self):
        calls = self._demote_calls()
        self.assertTrue(calls)
        self.assertGreaterEqual(calls[0]['stalls_so_far'], 2,
                                'the trigger fired before its stall count was reached')

    def test_the_reason_names_the_rate_that_fired(self):
        calls = self._demote_calls()
        self.assertTrue(calls)
        self.assertIn('stalls in 30 minutes', calls[0]['reason'])

    def test_the_trigger_is_off_when_the_count_is_zero(self):
        self._stall_move_cfg = {'stall_move_count': 0, 'stall_move_window_minutes': 30}
        self.assertEqual(self._demote_calls(3), [],
                         'stall_move_count = 0 must disable the trigger entirely')


class _FailoverCase(unittest.TestCase):
    """Direct calls into failover_group_member, the one selection path all four callers
    share - no watchdog thread, no ffmpeg."""

    def setUp(self):
        self.t = make_test_app()
        self.acct = make_account()

    def tearDown(self):
        with recorder._lock:
            recorder._active.clear()
        self.t.cleanup()

    def _state(self, rec_id):
        with recorder._lock:
            state = recorder.RecordingState()
            recorder._active[rec_id] = state
        return state

    def _demote(self, rec_id, reason='2 stalls in 30 minutes'):
        import app.health_score as health_score
        with mock.patch.object(health_score, 'apply_stall_demotion_health_observation'):
            return recorder.failover_group_member(self.t.app, rec_id, reason, demote=True)


class DemoteNotBurnTests(_FailoverCase):

    def test_the_departed_member_is_demoted_not_burned(self):
        """failed_member_ids is permanent for the run; nothing died here, so the member
        must stay selectable."""
        current = make_channel(self.acct, name='Stally', health_score=95)
        spare = make_channel(self.acct, name='Spare', health_score=60)
        grp = make_group(members=[current, spare])
        rec = make_recording(status='IN_PROGRESS', channel_id=current.id, group_id=grp.id)
        db.session.commit()
        state = self._state(rec.id)

        self.assertTrue(self._demote(rec.id))
        self.assertEqual(state.demoted_member_ids, {current.id})
        self.assertEqual(state.failed_member_ids, set(),
                         'a stall-demoted member must never enter the permanent burn set')
        self.assertEqual(state.stall_moves, 0,
                         'stall_moves is the watchdog\'s to count, not this function\'s')

    def test_a_one_member_group_stays_put_instead_of_aborting(self):
        """A voluntary move to yourself is not a move. The caller reads False here as
        "stay put and take the normal restart", never as a give-up."""
        only = make_channel(self.acct, name='The Only One', health_score=95)
        grp = make_group(members=[only])
        rec = make_recording(status='IN_PROGRESS', channel_id=only.id, group_id=grp.id)
        db.session.commit()
        self._state(rec.id)

        self.assertFalse(self._demote(rec.id))
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rec.id).channel_id, only.id)
        self.assertIsNone(RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type=GROUP_FAILOVER).first(),
            'a refused voluntary move must not log a failover that did not happen')

    def test_an_undemoted_member_wins_over_a_higher_scoring_demoted_one(self):
        current = make_channel(self.acct, name='Stally', health_score=99)
        already = make_channel(self.acct, name='Demoted Earlier', health_score=95)
        fresh = make_channel(self.acct, name='Never Tried', health_score=40)
        grp = make_group(members=[current, already, fresh])
        rec = make_recording(status='IN_PROGRESS', channel_id=current.id, group_id=grp.id)
        db.session.commit()
        state = self._state(rec.id)
        state.demoted_member_ids.add(already.id)

        self.assertTrue(self._demote(rec.id))
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rec.id).channel_id, fresh.id,
                         'a member not yet moved off must outrank a demoted one whatever '
                         'their scores')

    def test_a_small_group_cycles_back_to_its_demoted_members(self):
        """Three members, two already demoted and the third being demoted now: the group
        must come back to the best of them rather than run out of candidates."""
        current = make_channel(self.acct, name='On Now', health_score=50)
        best_demoted = make_channel(self.acct, name='Least Bad', health_score=88)
        worse_demoted = make_channel(self.acct, name='Worse', health_score=30)
        grp = make_group(members=[current, best_demoted, worse_demoted])
        rec = make_recording(status='IN_PROGRESS', channel_id=current.id, group_id=grp.id)
        db.session.commit()
        state = self._state(rec.id)
        state.demoted_member_ids.update({best_demoted.id, worse_demoted.id})

        self.assertTrue(self._demote(rec.id))
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rec.id).channel_id, best_demoted.id)
        extra = json.loads(RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type=GROUP_FAILOVER).first().extra_data)
        self.assertTrue(extra['took_demoted'],
                        'the event must say the recording came back to a demoted member')

    def test_a_member_whose_feed_actually_died_stays_excluded(self):
        """Demotion widens the candidate set; it must not resurrect a burned member."""
        current = make_channel(self.acct, name='Stally', health_score=50)
        dead = make_channel(self.acct, name='Died Earlier', health_score=99)
        spare = make_channel(self.acct, name='Spare', health_score=10)
        grp = make_group(members=[current, dead, spare])
        rec = make_recording(status='IN_PROGRESS', channel_id=current.id, group_id=grp.id)
        db.session.commit()
        state = self._state(rec.id)
        state.failed_member_ids.add(dead.id)

        self.assertTrue(self._demote(rec.id))
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rec.id).channel_id, spare.id)

    def test_a_locked_group_stays_put_rather_than_overriding_its_own_lock(self):
        """The stalling member is excluded AFTER the format filters, not before, which is
        the deliberate difference from the burn path. Leaving it in keeps it as a survivor
        of the lock, so the zero-survivors override does not fire: that override exists to
        keep a dying recording alive, and a recording that is still delivering is not
        dying. The burn path, where the member really did die, still overrides."""
        current = make_channel(self.acct, name='Only Match', health_score=50)
        wrong = make_channel(self.acct, name='Wrong Format', health_score=99)
        grp = make_group(members=[current, wrong],
                         format_strategy=GROUP_FORMAT_MANUAL)
        grp.set_locked_format('1920x1080', 60)
        make_channel_test(current, all_null=False, status='COMPLETED',
                          resolution='1920x1080', fps=59.94)
        make_channel_test(wrong, all_null=False, status='COMPLETED',
                          resolution='1280x720', fps=30)
        rec = make_recording(status='IN_PROGRESS', channel_id=current.id, group_id=grp.id)
        db.session.commit()
        self._state(rec.id)

        self.assertFalse(self._demote(rec.id),
                         'a voluntary move must not override the group format lock')
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rec.id).channel_id, current.id)

        import app.health_score as health_score
        with mock.patch.object(health_score, 'apply_failover_health_observation'):
            self.assertTrue(
                recorder.failover_group_member(self.t.app, rec.id, 'restart produced no data'),
                'a feed that actually died must still override the lock to stay alive')
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rec.id).channel_id, wrong.id)

    def test_the_event_says_the_feed_kept_stalling_rather_than_died(self):
        """UI text describing backend behavior is part of the change surface: "died" is
        false for a feed that is still delivering."""
        current = make_channel(self.acct, name='Stally', health_score=95)
        spare = make_channel(self.acct, name='Spare', health_score=60)
        grp = make_group(members=[current, spare])
        rec = make_recording(status='IN_PROGRESS', channel_id=current.id, group_id=grp.id)
        db.session.commit()
        self._state(rec.id)

        self.assertTrue(self._demote(rec.id))
        ev = RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type=GROUP_FAILOVER).first()
        self.assertIn('kept stalling', ev.detail)
        self.assertNotIn('died', ev.detail)
        self.assertIn('demoted', ev.detail)
        self.assertTrue(json.loads(ev.extra_data)['demoted'])

    def test_a_dead_feed_failover_still_burns_its_member(self):
        """The demote flag must not have changed the three existing callers."""
        current = make_channel(self.acct, name='Dead', health_score=95)
        spare = make_channel(self.acct, name='Spare', health_score=60)
        grp = make_group(members=[current, spare])
        rec = make_recording(status='IN_PROGRESS', channel_id=current.id, group_id=grp.id)
        db.session.commit()
        state = self._state(rec.id)

        import app.health_score as health_score
        with mock.patch.object(health_score, 'apply_failover_health_observation'):
            self.assertTrue(recorder.failover_group_member(
                self.t.app, rec.id, 'max consecutive failures'))
        self.assertEqual(state.failed_member_ids, {current.id})
        self.assertEqual(state.demoted_member_ids, set())
        ev = RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type=GROUP_FAILOVER).first()
        self.assertIn('died', ev.detail)
        self.assertFalse(json.loads(ev.extra_data)['demoted'])


class DemotionScoringTests(_FailoverCase):
    """The member that motivated this feature delivered 104% of its expected content
    across 27 stalls. Handing it the recording fail floor would have been a lie about a
    working feed, so the demotion path measures instead."""

    def _run(self, *, downtime, restarts, elapsed_minutes):
        current = make_channel(self.acct, name='Stally', health_score=None)
        spare = make_channel(self.acct, name='Spare', health_score=60)
        grp = make_group(members=[current, spare])
        rec = make_recording(
            status='IN_PROGRESS', channel_id=current.id, group_id=grp.id,
            start_time=datetime.utcnow() - timedelta(minutes=elapsed_minutes),
            stop_time=datetime.utcnow() + timedelta(hours=1))
        rec.total_downtime_seconds = downtime
        rec.total_restart_count = restarts
        db.session.commit()
        self._state(rec.id)
        self.assertTrue(recorder.failover_group_member(
            self.t.app, rec.id, f'{restarts} stalls in 30 minutes', demote=True))
        db.session.expire_all()
        return db.session.get(Channel, current.id), rec.id

    def test_the_demoted_member_is_scored_on_its_share_not_the_fail_floor(self):
        channel, _ = self._run(downtime=120, restarts=7, elapsed_minutes=65)
        self.assertIsNotNone(channel.health_score)
        self.assertGreater(
            channel.health_score, RECORDING_FAIL_FLOOR_DEFAULT + 40,
            'a feed that was still delivering must not be scored as a total failure')

    def test_more_stalls_score_worse(self):
        """Proportional, not categorical - the point of measuring rather than flooring."""
        light, _ = self._run(downtime=30, restarts=2, elapsed_minutes=65)
        self.tearDown()
        self.setUp()
        heavy, _ = self._run(downtime=400, restarts=25, elapsed_minutes=65)
        self.assertLess(heavy.health_score, light.health_score)

    def test_the_score_move_is_visible_on_the_channels_own_timeline(self):
        """A silent Channel.health_score mutation is the defect dev/docs/BUGS.md 2026-08-10
        already caught once on the sibling path."""
        channel, rec_id = self._run(downtime=120, restarts=7, elapsed_minutes=65)
        ev = ChannelEvent.query.filter_by(
            channel_id=channel.id,
            event_type=CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION).first()
        self.assertIsNotNone(ev, 'the demotion wrote no event on the demoted channel')
        self.assertIn('Kept stalling', ev.detail)
        extra = json.loads(ev.extra_data)
        self.assertEqual(extra['recording_id'], rec_id)
        self.assertFalse(extra['quality_breakdown']['fail_floor_applied'])
        self.assertEqual(extra['quality_breakdown']['member_share']['restarts'], 7)

    def test_only_the_members_own_share_is_charged_against_it(self):
        """A second demotion must be scored on what happened since the first one, not on
        the recording's cumulative counters - otherwise every later member inherits the
        downtime of the feeds it escaped."""
        first = make_channel(self.acct, name='First', health_score=None)
        second = make_channel(self.acct, name='Second', health_score=None)
        third = make_channel(self.acct, name='Third', health_score=40)
        grp = make_group(members=[first, second, third])
        rec = make_recording(
            status='IN_PROGRESS', channel_id=first.id, group_id=grp.id,
            start_time=datetime.utcnow() - timedelta(minutes=60),
            stop_time=datetime.utcnow() + timedelta(hours=1))
        rec.total_downtime_seconds = 600
        rec.total_restart_count = 30
        db.session.commit()
        self._state(rec.id)

        self.assertTrue(recorder.failover_group_member(
            self.t.app, rec.id, 'first move', demote=True))
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rec.id).channel_id, second.id)
        # Backdate the first move so the second member has a real 30-minute window to be
        # judged over. Both failovers happen in the same millisecond otherwise, and a
        # zero-length window is not a scenario this scoring is meant to describe.
        first_move = RecordingEvent.query.filter_by(
            recording_id=rec.id, event_type=GROUP_FAILOVER).first()
        first_move.timestamp = datetime.utcnow() - timedelta(minutes=30)
        db.session.commit()
        # The second member then behaves almost perfectly: 5 more seconds lost, 1 restart.
        rec = db.session.get(Recording, rec.id)
        rec.total_downtime_seconds = 605
        rec.total_restart_count = 31
        db.session.commit()
        self.assertTrue(recorder.failover_group_member(
            self.t.app, rec.id, 'second move', demote=True))
        db.session.expire_all()

        share = json.loads(ChannelEvent.query.filter_by(
            channel_id=second.id,
            event_type=CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION
        ).first().extra_data)['quality_breakdown']['member_share']
        self.assertEqual(share['downtime_seconds'], 5)
        self.assertEqual(share['restarts'], 1)
        self.assertGreater(
            db.session.get(Channel, second.id).health_score,
            db.session.get(Channel, first.id).health_score,
            'the second member behaved better and must score better')


if __name__ == '__main__':
    unittest.main()
