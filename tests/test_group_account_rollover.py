"""A multi-account channel group rolls over to an account with a free connection slot
instead of queueing behind a busy one (dev/changelog/855).

Member selection used to avoid a busy *channel* and nothing else: rank_members() sorts on
health score, then bitrate, then id, with no account term at all. On the shipped default of
one connection per account that meant the highest-scoring member on an account already
streaming another recording won over an equally eligible member on a completely idle
account sitting one row below. Since dev/changelog/854 made the limit a hard ceiling, that
choice costs real capture time - the start waits for a slot rather than connecting over the
limit - and at schedule time it produced a 400 refusal over a collision the ranking had
just created.

Covered here: the live-slot preference at record start and at failover, both of which fall
back to a full account rather than skipping (zero survivors is an override, never a skip);
the batched occupancy helper's semantics, including that a preempted channel test never
makes an account look full and that a recording does not see its own slot as somebody
else's; and the schedule-time counterpart, which asks the same question of the recording's
own window rather than of the live registry.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, recorder  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.database import (  # noqa: E402
    ChannelGroupMember, Recording, RecordingEvent, GROUP_MEMBER_SELECTED, GROUP_FAILOVER,
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS,
)
from app.tz_utils import local_input_value  # noqa: E402


def _selection_event(recording_id):
    return RecordingEvent.query.filter_by(
        recording_id=recording_id, event_type=GROUP_MEMBER_SELECTED).first()


class OccupancyHelperTests(unittest.TestCase):
    """app/connection_limits.py::accounts_without_free_recording_slot - the one input the
    two runtime call sites share."""

    def setUp(self):
        self.t = make_test_app()
        self.busy = seed.make_account(name='Busy', max_connections=1)
        self.idle = seed.make_account(name='Idle', max_connections=1)
        self.roomy = seed.make_account(name='Roomy', max_connections=2)
        db.session.commit()
        self.busy_id, self.idle_id, self.roomy_id = self.busy.id, self.idle.id, self.roomy.id
        connlim._holders.clear()

    def tearDown(self):
        connlim._holders.clear()
        self.t.cleanup()

    def test_a_recording_holder_fills_the_account_and_an_idle_one_stays_free(self):
        connlim.try_acquire(self.busy_id, 'recording', 1)
        full = connlim.accounts_without_free_recording_slot(
            [self.busy_id, self.idle_id, self.roomy_id])
        self.assertEqual(full, {self.busy_id})

    def test_a_channel_test_never_makes_an_account_look_full(self):
        """A test holder is preempted by a recording rather than waited on
        (recorder._try_acquire_slot_with_preemption), so deprioritizing over one would
        pick a worse member to avoid a wait that never happens."""
        connlim.try_acquire(self.busy_id, 'test', 77)
        self.assertTrue(connlim.at_limit(self.busy_id),
                        'at_limit counts every holder - that is the difference being tested')
        self.assertEqual(
            connlim.accounts_without_free_recording_slot([self.busy_id]), set())

    def test_a_recording_does_not_see_its_own_slot_as_somebody_elses(self):
        connlim.try_acquire(self.busy_id, 'recording', 42)
        self.assertEqual(
            connlim.accounts_without_free_recording_slot(
                [self.busy_id], exclude_holder=('recording', 42)),
            set(), 'a same-account failover keeps the slot it already holds')
        self.assertEqual(
            connlim.accounts_without_free_recording_slot(
                [self.busy_id], exclude_holder=('recording', 43)),
            {self.busy_id})

    def test_an_account_row_that_is_gone_counts_as_unavailable(self):
        """try_acquire() refuses outright for a missing account, so preferring such a
        member would send the recording into a wait it can never finish."""
        self.assertEqual(
            connlim.accounts_without_free_recording_slot([999999]), {999999})

    def test_it_reads_the_config_once_for_many_accounts(self):
        """CLAUDE.md, no hidden I/O in per-row loops: at_limit() re-reads the config and
        one Account row per call, and selection asks this per group, not per member."""
        import app.config as appconfig
        with mock.patch.object(appconfig, '_parse_config_file',
                               wraps=appconfig._parse_config_file) as parse:
            connlim.accounts_without_free_recording_slot(
                [self.busy_id, self.idle_id, self.roomy_id])
        self.assertLessEqual(parse.call_count, 1)


class _GroupCase(unittest.TestCase):
    """Two single-connection accounts, one group holding a member on each. The busy
    account's member is deliberately the better one on the existing ranking (same score,
    higher bitrate), so any preference for the idle account has to be doing the work."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False},
        }})
        self.acct_busy = seed.make_account(name='Busy Account', max_connections=1)
        self.acct_idle = seed.make_account(name='Idle Account', max_connections=1)
        self.ch_strong = seed.make_channel(self.acct_busy, name='Strong Feed',
                                           health_score=100)
        self.ch_spare = seed.make_channel(self.acct_idle, name='Spare Feed',
                                          health_score=100)
        seed.make_channel_test(self.ch_strong, all_null=False, status='COMPLETED',
                               bitrate_kbps=6592)
        seed.make_channel_test(self.ch_spare, all_null=False, status='COMPLETED',
                               bitrate_kbps=4906)
        self.group = seed.make_group(name='Fox Sports 1',
                                     members=[self.ch_strong, self.ch_spare])
        db.session.commit()
        self.busy_id, self.idle_id = self.acct_busy.id, self.acct_idle.id
        self.strong_id, self.spare_id = self.ch_strong.id, self.ch_spare.id
        self.group_id = self.group.id
        connlim._holders.clear()

    def tearDown(self):
        connlim._holders.clear()
        self.t.cleanup()


class RecordStartPrefersAFreeAccountTests(_GroupCase):

    def _scheduled(self):
        now = datetime.utcnow()
        rec = seed.make_recording(
            status=REC_STATUS_SCHEDULED, name='rollover', group_id=self.group_id,
            start_time=now - timedelta(seconds=30), stop_time=now + timedelta(hours=1))
        db.session.commit()
        return rec.id

    def test_it_takes_the_idle_accounts_member_over_the_better_ranked_busy_one(self):
        connlim.try_acquire(self.busy_id, 'recording', 999999)
        rid = self._scheduled()
        # reschedule_recording_start is stubbed only so a regression that sends this into
        # the wait loop reports a clean assertion instead of an unrelated scheduler error.
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.channel_id, self.spare_id,
                         'the higher-bitrate member is on the account already streaming')
        self.assertEqual(rec.status, REC_STATUS_IN_PROGRESS,
                         'rolling over means it starts now, not after a wait')

    def test_the_rollover_is_named_on_the_recording(self):
        connlim.try_acquire(self.busy_id, 'recording', 999999)
        rid = self._scheduled()
        # reschedule_recording_start is stubbed only so a regression that sends this into
        # the wait loop reports a clean assertion instead of an unrelated scheduler error.
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        ev = _selection_event(rid)
        self.assertIsNotNone(ev)
        self.assertIn('rolled over to a free account', ev.detail)
        self.assertIn('Strong Feed', ev.detail)
        self.assertIn(str(self.strong_id), ev.extra_data)
        self.assertIn('"took_busy_account": false', ev.extra_data)

    def test_the_better_member_still_wins_when_nothing_is_streaming(self):
        rid = self._scheduled()
        with mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).channel_id, self.strong_id)
        self.assertNotIn('rolled over', _selection_event(rid).detail)

    def test_every_account_full_is_an_override_not_a_skip(self):
        """Principle 2: the recording is still started from the best member and waits for
        a slot - the same answer the format lock's zero-survivors case gives."""
        connlim.try_acquire(self.busy_id, 'recording', 999998)
        connlim.try_acquire(self.idle_id, 'recording', 999999)
        rid = self._scheduled()
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.channel_id, self.strong_id,
                         'a member is still chosen; the wait happens after selection')
        ev = _selection_event(rid)
        self.assertIn('"took_busy_account": true', ev.extra_data)
        self.assertIn('free connection slot', ev.detail)


class FailoverPrefersAFreeAccountTests(_GroupCase):
    """The failover swap refuses rather than exceeding the target's limit
    (dev/changelog/854), so a ranking blind to accounts walks straight into that refusal."""

    def setUp(self):
        super().setUp()
        # A third member on a second idle account, so the recording can fail over off its
        # own account to either a busy or an idle one.
        self.acct_third = seed.make_account(name='Third Account', max_connections=1)
        self.ch_third = seed.make_channel(self.acct_third, name='Third Feed',
                                          health_score=100)
        seed.make_channel_test(self.ch_third, all_null=False, status='COMPLETED',
                               bitrate_kbps=1000)
        db.session.add(ChannelGroupMember(
            group_id=self.group_id, channel_id=self.ch_third.id, position=2,
            recording_enabled=True, test_enabled=True))
        db.session.commit()
        self.third_id, self.third_acct_id = self.ch_third.id, self.acct_third.id

        now = datetime.utcnow()
        rec = seed.make_recording(
            status=REC_STATUS_IN_PROGRESS, name='live', channel_id=self.third_id,
            group_id=self.group_id,
            start_time=now - timedelta(minutes=10), stop_time=now + timedelta(hours=1))
        db.session.commit()
        self.rid = rec.id
        connlim._holders.clear()
        self.assertTrue(connlim.try_acquire(self.third_acct_id, 'recording', self.rid))
        recorder._active[self.rid] = recorder.RecordingState(current_segment_num=1)

    def tearDown(self):
        recorder._active.pop(self.rid, None)
        super().tearDown()

    def test_it_fails_over_onto_the_idle_account_not_the_busy_one(self):
        connlim.try_acquire(self.busy_id, 'recording', 999999)
        switched = recorder.failover_group_member(self.t.app, self.rid, 'stream died')
        self.assertTrue(switched, 'an idle account was available; the swap must happen')

        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.channel_id, self.spare_id)
        ev = RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=GROUP_FAILOVER).first()
        self.assertIn('rolled over to a free account', ev.detail)

    def test_a_full_target_is_still_taken_when_it_is_all_that_is_left(self):
        """Zero survivors is an override: the swap is attempted and the existing
        cross-account guard decides, rather than the preference silently aborting."""
        connlim.try_acquire(self.busy_id, 'recording', 999998)
        connlim.try_acquire(self.idle_id, 'recording', 999999)
        recorder.failover_group_member(self.t.app, self.rid, 'stream died')

        db.session.expire_all()
        self.assertIn(('recording', self.rid), connlim._holders[self.third_acct_id],
                      'a live recording must never be left holding no slot')


class ScheduleTimeRollsOverToAFreeAccountTests(_GroupCase):
    """The schedule-time half. Live occupancy says nothing about a window at 8pm tomorrow,
    so this asks the overlap question _check_account_connection_limit() already asks - the
    one that produced the refusal, measured live on 2026-08-28."""

    def setUp(self):
        super().setUp()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.start = datetime.utcnow() + timedelta(hours=2)
        self.stop = self.start + timedelta(hours=1)

    def _post(self, channel_id, group_id, **extra):
        # make_test_app runs with the scheduler off, so the APScheduler registration the
        # route does after a successful create has nothing to register against.
        data = {
            'name': 'Big Game',
            'url': 'http://example.test/live/1',
            'start_time': local_input_value(self.start),
            'stop_time': local_input_value(self.stop),
            'channel_id': str(channel_id),
            'group_id': str(group_id),
        }
        data.update(extra)
        with mock.patch('app.routes.recordings.schedule_recording'):
            return self.t.client.post('/recordings/new-json', data=data)

    def _occupy_window(self, channel):
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='earlier',
                            channel_id=channel.id,
                            start_time=self.start, stop_time=self.stop)
        db.session.commit()

    def test_the_guide_rows_member_is_replaced_when_its_account_has_no_room(self):
        other = seed.make_channel(self.acct_busy, name='Unrelated Feed')
        db.session.commit()
        self._occupy_window(other)

        # force=1: 'other' still overlaps in time (a different, unrelated channel/account
        # from the one the group rolls onto), which is exactly the general overlap warning
        # dev/changelog/858 added - this test is about the rollover outcome, not that
        # warning, so bypass it and assert on the resolved channel_id directly.
        resp = self._post(self.strong_id, self.group_id, force='1')
        self.assertEqual(resp.status_code, 200, resp.get_json())

        rec = Recording.query.filter_by(name='Big Game').first()
        self.assertEqual(rec.channel_id, self.spare_id,
                         'the group had an account with room; use it instead of refusing')
        self.assertEqual(rec.group_id, self.group_id)

    def test_a_member_whose_account_has_room_is_left_exactly_as_supplied(self):
        resp = self._post(self.strong_id, self.group_id)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        rec = Recording.query.filter_by(name='Big Game').first()
        self.assertEqual(rec.channel_id, self.strong_id,
                         'a schedule that would have gone through is never restamped')

    def test_it_still_warns_when_no_member_has_room(self):
        """dev/changelog/858 turned the schedule-time refusal into a proceedable warning
        - runtime enforcement (dev/changelog/854) is what protects the account now, so
        the schedule-time check has nothing left to protect by refusing outright."""
        busy_other = seed.make_channel(self.acct_busy, name='Unrelated Busy')
        idle_other = seed.make_channel(self.acct_idle, name='Unrelated Idle')
        db.session.commit()
        self._occupy_window(busy_other)
        self._occupy_window(idle_other)

        resp = self._post(self.strong_id, self.group_id)
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('connection_limit_warning', payload)
        self.assertIn('connection limit', payload['connection_limit_warning']['message'])
        self.assertEqual(Recording.query.filter_by(name='Big Game').count(), 0)

    def test_same_channel_overlap_is_a_handoff_and_does_not_move_the_member(self):
        """_check_account_connection_limit() excludes same-channel overlap deliberately,
        so the window helper must too, or a handoff reads as a conflict."""
        self._occupy_window(self.ch_strong)

        resp = self._post(self.strong_id, self.group_id)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        recs = Recording.query.filter_by(name='Big Game').all()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].channel_id, self.strong_id)


if __name__ == '__main__':
    unittest.main()
