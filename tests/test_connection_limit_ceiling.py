"""An account's connection limit is a hard ceiling - a recording waits for a slot rather
than opening a connection over it (dev/changelog/854, DESIGN-concurrency.md §1 doctrine 1).

Until this shipped, `_acquire_slot_with_preemption` was documented to "never block or
refuse": at the limit it logged, alerted, and connected anyway. The damage of doing that
lands on the provider account rather than on one file - a provider that throttles or bans
over concurrent-connection abuse takes down every channel on the account, including the
recording that was already running legitimately - and with the shipped default of one
connection per account, two overlapping schedules reach it on day one.

Covered here: the scheduled start defers instead of starting, says so once, starts as soon
as a slot frees, and fails loudly if its own window ends first; two waiters on one slot are
ordered so the earlier cannot be starved; the same-channel handoff (explicitly exempt) still
starts first try; resume defers without flipping status; and failover never leaves a live
recording holding no slot.
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
    Alert, Recording, RecordingEvent, DIAGNOSTICS,
    RECORDING_FAILED, RECORDING_START_DEFERRED,
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_FAILED,
)


def _deferrals(recording_id):
    """RECORDING_START_DEFERRED events written for the connection-limit reason only -
    the same event type also carries the mp4-conversion collision defer."""
    return [e for e in RecordingEvent.query.filter_by(
        recording_id=recording_id, event_type=RECORDING_START_DEFERRED).all()
        if 'connection_limit' in (e.extra_data or '')]


class _SlotCase(unittest.TestCase):
    """One account limited to a single connection, one channel on it, and a sandboxed
    DVR dir so start_recording's directory probe does not send us down the FAILED path."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False},
        }})
        self.account = seed.make_account(name='One Slot', max_connections=1)
        self.channel = seed.make_channel(self.account, name='The Only Feed')
        self.other_channel = seed.make_channel(self.account, name='Sibling Feed')
        db.session.commit()
        self.account_id = self.account.id
        self.channel_id = self.channel.id
        self.other_channel_id = self.other_channel.id
        connlim._holders.clear()

    def tearDown(self):
        connlim._holders.clear()
        self.t.cleanup()

    def _scheduled(self, channel_id=None, start_offset=-60, hours=1, name='waiter'):
        now = datetime.utcnow()
        start = now + timedelta(seconds=start_offset)
        rec = seed.make_recording(
            status=REC_STATUS_SCHEDULED, name=name,
            channel_id=channel_id if channel_id is not None else self.channel_id,
            start_time=start, stop_time=start + timedelta(hours=hours))
        db.session.commit()
        return rec.id

    def _occupy_slot(self, holder_id=999999):
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', holder_id))
        return holder_id


class ScheduledStartWaitsForASlotTests(_SlotCase):

    def test_start_defers_instead_of_connecting_over_the_limit(self):
        self._occupy_slot()
        rid = self._scheduled()
        with mock.patch('app.scheduler.reschedule_recording_start') as resched, \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.status, REC_STATUS_SCHEDULED)
        self.assertFalse(launch.called, 'ffmpeg must not be launched without a slot')
        self.assertTrue(resched.called, 'the wait must re-arm the start job')
        self.assertNotIn(('recording', rid), connlim._holders[self.account_id])
        self.assertEqual(len(connlim._holders[self.account_id]), 1,
                         'the account must never be left over its limit')

    def test_the_wait_is_announced_once_on_the_recording_and_as_an_alert(self):
        self._occupy_slot()
        rid = self._scheduled()
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        events = _deferrals(rid)
        self.assertEqual(len(events), 1)
        self.assertIn('connection slot', events[0].detail)
        self.assertIn('One Slot', events[0].detail)
        alerts = Alert.query.filter_by(
            alert_type='RECORDING_WAITING_FOR_CONNECTION_SLOT').all()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].recording_id, rid)

    def test_repeated_waits_do_not_repeat_the_event(self):
        """The wait re-enters every SLOT_WAIT_POLL_SECONDS; one deferral is a fact, a
        hundred identical rows bury the recording's real history."""
        self._occupy_slot()
        rid = self._scheduled()
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment'):
            for _ in range(4):
                recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        self.assertEqual(len(_deferrals(rid)), 1)
        self.assertEqual(Alert.query.filter_by(
            alert_type='RECORDING_WAITING_FOR_CONNECTION_SLOT').count(), 1)

    def test_it_starts_as_soon_as_the_slot_frees(self):
        holder = self._occupy_slot()
        rid = self._scheduled()
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, rid)
            self.assertFalse(launch.called)
            connlim.release(self.account_id, 'recording', holder)
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.status, REC_STATUS_IN_PROGRESS)
        self.assertTrue(launch.called)
        self.assertIn(('recording', rid), connlim._holders[self.account_id])

    def test_a_window_that_expires_while_waiting_fails_loudly(self):
        self._occupy_slot()
        rid = self._scheduled(start_offset=-7200, hours=1)  # stop_time already passed
        with mock.patch('app.scheduler.reschedule_recording_start') as resched, \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.status, REC_STATUS_FAILED)
        self.assertIsNotNone(rec.completed_at)
        self.assertFalse(launch.called)
        self.assertFalse(resched.called, 'an expired window must stop re-arming')
        details = [e.detail for e in RecordingEvent.query.filter_by(
            recording_id=rid, event_type=RECORDING_FAILED).all()]
        self.assertEqual(len(details), 1)
        self.assertIn('connection limit', details[0])
        self.assertEqual(Alert.query.filter_by(
            alert_type='RECORDING_FAILED_CONNECTION_LIMIT').count(), 1)

    def test_an_unoccupied_account_starts_normally_with_no_deferral(self):
        rid = self._scheduled()
        with mock.patch('app.scheduler.reschedule_recording_start') as resched, \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.status, REC_STATUS_IN_PROGRESS)
        self.assertTrue(launch.called)
        self.assertFalse(resched.called)
        self.assertEqual(_deferrals(rid), [])


class WaiterFairnessTests(_SlotCase):
    """Two recordings waiting on one slot need a defined order, or whichever happens to
    poll first when the slot frees takes it and the earlier one starves."""

    def test_the_later_waiter_yields_the_slot_it_just_took(self):
        early = self._scheduled(channel_id=self.channel_id, start_offset=-600,
                                name='been waiting')
        late = self._scheduled(channel_id=self.other_channel_id, start_offset=-60,
                               name='just arrived')
        with mock.patch('app.scheduler.reschedule_recording_start') as resched, \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, late)

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, late).status, REC_STATUS_SCHEDULED)
        self.assertFalse(launch.called)
        self.assertTrue(resched.called)
        self.assertEqual(connlim._holders.get(self.account_id, []), [],
                         'the yielded slot must be left free for the earlier waiter')
        events = _deferrals(late)
        self.assertEqual(len(events), 1)
        self.assertIn('been waiting', events[0].detail,
                      'the deferral must name the recording it is yielding to')
        self.assertIn(f'"waiting_on_recording_id": {early}', events[0].extra_data)

    def test_the_earlier_waiter_takes_the_slot(self):
        early = self._scheduled(channel_id=self.channel_id, start_offset=-600,
                                name='been waiting')
        self._scheduled(channel_id=self.other_channel_id, start_offset=-60,
                        name='just arrived')
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, early)

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, early).status, REC_STATUS_IN_PROGRESS)
        self.assertTrue(launch.called)
        self.assertEqual(_deferrals(early), [])

    def test_no_yield_when_the_account_still_has_a_free_slot(self):
        """The yield is about handing over the LAST slot. On a multi-connection account
        both waiters can run, and deferring the later one would be pure lost content."""
        acct = seed.make_account(name='Roomy', max_connections=2)
        a = seed.make_channel(acct, name='A')
        b = seed.make_channel(acct, name='B')
        db.session.commit()
        self._scheduled(channel_id=a.id, start_offset=-600, name='been waiting')
        late = self._scheduled(channel_id=b.id, start_offset=-60, name='just arrived')
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, late)

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, late).status, REC_STATUS_IN_PROGRESS)
        self.assertTrue(launch.called)

    def test_a_waiter_whose_window_has_ended_no_longer_holds_anyone_back(self):
        """The queue is bounded by construction: a stuck waiter can only block others
        until its own stop_time passes."""
        now = datetime.utcnow()
        seed.make_recording(
            status=REC_STATUS_SCHEDULED, name='expired waiter',
            channel_id=self.channel_id,
            start_time=now - timedelta(hours=3), stop_time=now - timedelta(hours=1))
        db.session.commit()
        late = self._scheduled(channel_id=self.other_channel_id, start_offset=-60)
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, late)

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, late).status, REC_STATUS_IN_PROGRESS)
        self.assertTrue(launch.called)


class HandoffIsExemptTests(_SlotCase):
    """Specified as: "Two recordings on the exact same channel or group that overlap
    can and should do the graceful hand off ... That part is fine." The handoff runs before
    the acquire and releases its predecessor's slot synchronously, so the new recording must
    still start on the first attempt - and must never yield that slot to a third waiter,
    which would leave exactly the gap the handoff exists to avoid."""

    def test_a_same_channel_handoff_still_starts_first_try(self):
        now = datetime.utcnow()
        old = seed.make_recording(
            status=REC_STATUS_IN_PROGRESS, name='outgoing', channel_id=self.channel_id,
            start_time=now - timedelta(hours=1), stop_time=now + timedelta(minutes=5))
        db.session.commit()
        old_id = old.id
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', old_id))
        recorder._active[old_id] = recorder.RecordingState()
        new_id = self._scheduled(channel_id=self.channel_id, name='incoming')

        with mock.patch('app.scheduler.reschedule_recording_start') as resched, \
             mock.patch('app.concatenator.do_concatenation'), \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, new_id)

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, new_id).status, REC_STATUS_IN_PROGRESS)
        self.assertTrue(launch.called, 'the handoff must not be made to wait')
        self.assertFalse(resched.called)
        self.assertIn(('recording', new_id), connlim._holders[self.account_id])

    def test_a_handoff_does_not_yield_its_slot_to_an_earlier_waiter(self):
        now = datetime.utcnow()
        old = seed.make_recording(
            status=REC_STATUS_IN_PROGRESS, name='outgoing', channel_id=self.channel_id,
            start_time=now - timedelta(hours=1), stop_time=now + timedelta(minutes=5))
        db.session.commit()
        old_id = old.id
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', old_id))
        recorder._active[old_id] = recorder.RecordingState()
        # A third recording on a sibling channel that has been waiting longer.
        self._scheduled(channel_id=self.other_channel_id, start_offset=-900,
                        name='been waiting')
        new_id = self._scheduled(channel_id=self.channel_id, start_offset=-60,
                                 name='incoming')

        with mock.patch('app.scheduler.reschedule_recording_start') as resched, \
             mock.patch('app.concatenator.do_concatenation'), \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, new_id)

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, new_id).status, REC_STATUS_IN_PROGRESS)
        self.assertTrue(launch.called)
        self.assertFalse(resched.called)


class ResumeWaitsForASlotTests(_SlotCase):
    """resume_recording takes its slot BEFORE any status write, so a refusal has nothing
    to unwind - the recording is left exactly as it was and the resume job is re-armed."""

    def _paused(self):
        now = datetime.utcnow()
        rec = seed.make_recording(
            status=REC_STATUS_PAUSED, name='paused', channel_id=self.channel_id,
            start_time=now - timedelta(minutes=30), stop_time=now + timedelta(hours=1))
        db.session.commit()
        return rec.id

    def test_resume_defers_without_flipping_status(self):
        self._occupy_slot()
        rid = self._paused()
        with mock.patch('app.scheduler.reschedule_recording_resume', create=True) as resched, \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.resume_recording(self.t.app, rid)

        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.status, REC_STATUS_PAUSED,
                         'a refused resume must leave the recording where it was')
        self.assertFalse(launch.called)
        self.assertTrue(resched.called)
        self.assertEqual(len(_deferrals(rid)), 1)
        self.assertEqual(len(connlim._holders[self.account_id]), 1)

    def test_resume_proceeds_once_the_slot_frees(self):
        holder = self._occupy_slot()
        rid = self._paused()
        with mock.patch('app.scheduler.reschedule_recording_resume', create=True), \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.resume_recording(self.t.app, rid)
            self.assertFalse(launch.called)
            connlim.release(self.account_id, 'recording', holder)
            recorder.resume_recording(self.t.app, rid)

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, REC_STATUS_IN_PROGRESS)
        self.assertTrue(launch.called)

    def test_resume_stops_re_arming_once_the_window_has_ended(self):
        """No terminal write of its own: the recording's stop job fires independently and
        concatenates whatever is already on disk."""
        self._occupy_slot()
        now = datetime.utcnow()
        rec = seed.make_recording(
            status=REC_STATUS_PAUSED, name='too late', channel_id=self.channel_id,
            start_time=now - timedelta(hours=3), stop_time=now - timedelta(minutes=1))
        db.session.commit()
        rid = rec.id
        with mock.patch('app.scheduler.reschedule_recording_resume', create=True) as resched, \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.resume_recording(self.t.app, rid)

        db.session.expire_all()
        self.assertFalse(resched.called)
        self.assertFalse(launch.called)
        self.assertEqual(db.session.get(Recording, rid).status, REC_STATUS_PAUSED)


class FailoverKeepsItsSlotTests(unittest.TestCase):
    """A cross-account failover releases the old slot then acquires the new one. When the
    target account is full the swap is undone rather than pushed through, so a live
    recording is never left holding no slot at all - and never blocks the watchdog thread
    it runs on."""

    def setUp(self):
        self.t = make_test_app()
        self.acct_a = seed.make_account(name='Account A', max_connections=1)
        self.acct_b = seed.make_account(name='Account B', max_connections=1)
        self.ch_a = seed.make_channel(self.acct_a, name='Feed A')
        self.ch_b = seed.make_channel(self.acct_b, name='Feed B')
        self.group = seed.make_group(name='Failover Group',
                                     members=[self.ch_a, self.ch_b])
        db.session.commit()
        now = datetime.utcnow()
        rec = seed.make_recording(
            status=REC_STATUS_IN_PROGRESS, name='live', channel_id=self.ch_a.id,
            group_id=self.group.id,
            start_time=now - timedelta(minutes=10), stop_time=now + timedelta(hours=1))
        db.session.commit()
        self.rid = rec.id
        self.a_id, self.b_id = self.acct_a.id, self.acct_b.id
        self.ch_b_id = self.ch_b.id
        connlim._holders.clear()
        self.assertTrue(connlim.try_acquire(self.a_id, 'recording', self.rid))
        recorder._active[self.rid] = recorder.RecordingState(current_segment_num=1)

    def tearDown(self):
        recorder._active.pop(self.rid, None)
        connlim._holders.clear()
        self.t.cleanup()

    def test_failover_is_refused_rather_than_exceeding_the_target_limit(self):
        connlim.try_acquire(self.b_id, 'recording', 424242)  # B is full
        switched = recorder.failover_group_member(self.t.app, self.rid, 'test reason')
        self.assertFalse(switched, 'must not move onto an account at its limit')

        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertNotEqual(rec.channel_id, self.ch_b_id, 'the member must not have moved')
        self.assertIn(('recording', self.rid), connlim._holders[self.a_id],
                      'a live recording must never be left holding no slot')
        self.assertEqual(len(connlim._holders[self.b_id]), 1,
                         'the target account must not be pushed over its limit')

    def test_the_held_back_failover_is_recorded_on_the_recording(self):
        connlim.try_acquire(self.b_id, 'recording', 424242)
        recorder.failover_group_member(self.t.app, self.rid, 'test reason')

        db.session.expire_all()
        events = [e for e in RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=DIAGNOSTICS).all()
            if 'failover_blocked_by_connection_limit' in (e.extra_data or '')]
        self.assertEqual(len(events), 1)
        self.assertIn('connection limit', events[0].detail)
        self.assertIn('Account B', events[0].detail)

    def test_failover_still_swaps_when_the_target_account_is_free(self):
        switched = recorder.failover_group_member(self.t.app, self.rid, 'test reason')
        self.assertTrue(switched)

        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.channel_id, self.ch_b_id)
        self.assertIn(('recording', self.rid), connlim._holders[self.b_id])
        self.assertEqual(connlim._holders.get(self.a_id, []), [],
                         'the old account\'s slot must be released on a real swap')


class SlotIsReleasedForTheNextWaiterTests(_SlotCase):
    """The wait is only as good as the release that ends it - a recording that finishes
    must hand its slot back, or every waiter behind it waits out its own window."""

    def test_teardown_releases_the_slot_a_waiter_is_waiting_for(self):
        now = datetime.utcnow()
        live = seed.make_recording(
            status=REC_STATUS_IN_PROGRESS, name='live', channel_id=self.channel_id,
            start_time=now - timedelta(minutes=5), stop_time=now + timedelta(hours=1))
        db.session.commit()
        live_id = live.id
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', live_id))
        recorder._active[live_id] = recorder.RecordingState()
        try:
            recorder._teardown_active_ffmpeg(self.t.app, live_id, exit_reason='MANUAL_STOP')
        finally:
            recorder._active.pop(live_id, None)
        self.assertEqual(connlim._holders.get(self.account_id, []), [])

        waiter = self._scheduled(channel_id=self.other_channel_id)
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, waiter)
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, waiter).status, REC_STATUS_IN_PROGRESS)
        self.assertTrue(launch.called)


if __name__ == '__main__':
    unittest.main()
