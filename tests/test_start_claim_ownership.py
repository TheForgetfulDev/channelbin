"""Tier 2 - starting a recording has exactly one owner.

Guards dev/docs/BUGS.md 2026-09-16 "An abort landing between the IN_PROGRESS commit and
_active registration leaves a capture nothing can stop" and "A SCHEDULED recording whose
start time passed during a restart is started by two callers at once". Design and
reasoning: dev/changelog/987.

Two defects, one missing mechanism. start_recording used to check that the row said
SCHEDULED, do all its preparatory work, commit IN_PROGRESS, and only then register the live
RecordingState - so "I am the one starting this recording" was never a single atomic fact
and two different things exploited the gap:

  (a) A cancel landing in the commit-to-registration window found no live state to stop,
      marked the row ABORTED and removed the stop job, and then watched start_recording
      spawn ffmpeg anyway - a watchdog supervising an ABORTED recording, no stop job, no
      teardown that would ever run, and the account's connection slot held for the life of
      the process.

  (b) After a restart, the overdue persisted start_<id> job (misfire_grace_time is None, so
      APScheduler dispatches it on its first pass) and the startup sweep's case 2b both
      passed the SCHEDULED check and both launched segment 1 - two ffmpegs on one path, one
      of them reachable from nothing.

The fix is an ownership pairing, and the tests below exercise both halves of it: the live
state is registered BEFORE the status claim, the status flip is a conditional UPDATE rather
than a read-then-write, and abort_recording commits ABORTED BEFORE its own teardown.

No network and no real ffmpeg: recorder.subprocess.Popen is patched or _launch_segment is
stubbed throughout, and every path runs against make_test_app's temp DB and temp dirs.
  python3 -m unittest tests.test_start_claim_ownership
"""
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.recorder as recorder  # noqa: E402
import app.scheduler as scheduler  # noqa: E402
from app import db, connection_limits as connlim  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, RecordingSegment, SEGMENT_STARTED,
    REC_STATUS_ABORTED, REC_STATUS_IN_PROGRESS,
)
from tests.support import make_test_app, seed  # noqa: E402


def _fake_proc(pid=4242):
    proc = mock.MagicMock()
    proc.pid = pid
    proc.poll.return_value = None
    return proc


class _StartHarness(unittest.TestCase):
    """One SCHEDULED recording on a real channel/account, its window already open, with the
    DVR dir pointed at the temp tree so a launch that DOES happen writes nowhere real."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False},
        }})
        acct = seed.make_account()
        self.account_id = acct.id
        self.channel = seed.make_channel(acct, stream_id=77, name='Claim Channel')
        now = datetime.utcnow()
        self.rec = seed.make_recording(
            status='SCHEDULED', channel_id=self.channel.id,
            url='http://example.test/live/77',
            start_time=now - timedelta(seconds=5), stop_time=now + timedelta(hours=1))
        db.session.commit()
        self.rid = self.rec.id

    def tearDown(self):
        with recorder._lock:
            recorder._active.pop(self.rid, None)
        connlim.release(self.account_id, 'recording', self.rid)
        self.t.cleanup()

    def _segment_rows(self):
        db.session.expire_all()
        return RecordingSegment.query.filter_by(recording_id=self.rid).all()

    def _status(self):
        db.session.expire_all()
        return db.session.get(Recording, self.rid).status


class AbortDuringStartTests(_StartHarness):
    """(a) A cancel that lands inside the start must stop the start, not run beside it.

    end_slot_wait is the injection point because it is the last thing start_recording does
    before _launch_segment - the exact window the defect lived in. Before the fix it sat on
    the other side of the _active registration, so the abort found nothing to tear down.
    """

    def _start_with_abort_at(self, target):
        real = getattr(recorder, target)
        # One-shot: abort_recording calls end_slot_wait itself, so a wrapper that fired
        # every time would recurse instead of testing anything.
        fired = []

        def _abort_then_run(*args, **kwargs):
            if not fired:
                fired.append(True)
                recorder.abort_recording(self.t.app, self.rid)
            return real(*args, **kwargs)

        with mock.patch.object(recorder, target, _abort_then_run), \
             mock.patch.object(recorder.subprocess, 'Popen',
                               side_effect=lambda *a, **kw: _fake_proc()) as popen:
            recorder.start_recording(self.t.app, self.rid)
        return popen

    def test_no_ffmpeg_is_spawned_for_a_cancelled_recording(self):
        popen = self._start_with_abort_at('end_slot_wait')
        self.assertFalse(
            popen.called,
            'start_recording spawned ffmpeg for a recording that was cancelled mid-start')

    def test_no_segment_row_is_created_for_a_cancelled_recording(self):
        self._start_with_abort_at('end_slot_wait')
        self.assertEqual(
            self._segment_rows(), [],
            'a cancelled start left a segment row behind, so something was capturing')

    def test_the_cancelled_recording_keeps_its_aborted_status(self):
        self._start_with_abort_at('end_slot_wait')
        self.assertEqual(self._status(), REC_STATUS_ABORTED)

    def test_nothing_is_left_in_active(self):
        self._start_with_abort_at('end_slot_wait')
        self.assertIsNone(
            recorder.get_state(self.rid),
            '_active still holds live state for a cancelled recording - nothing would '
            'ever tear it down')

    def test_the_connection_slot_is_released(self):
        self._start_with_abort_at('end_slot_wait')
        self.assertTrue(
            connlim.try_acquire(self.account_id, 'test', 'probe'),
            'a cancelled start held its connection slot for the life of the process')
        connlim.release(self.account_id, 'test', 'probe')

    def test_an_abort_before_the_claim_refuses_the_claim(self):
        """The other side of the window: the cancel commits ABORTED while the start is
        still doing its preparatory work. The conditional UPDATE is what notices."""
        popen = self._start_with_abort_at('probe_dir')
        self.assertFalse(popen.called)
        self.assertEqual(self._status(), REC_STATUS_ABORTED)
        self.assertEqual(self._segment_rows(), [])


class AbortOrdersItsStatusWriteFirstTests(_StartHarness):
    """The pairing that makes AbortDuringStartTests work: abort_recording must commit
    ABORTED before it tears anything down. Tearing down first is what lost the interlock."""

    def test_the_teardown_sees_a_row_that_is_already_aborted(self):
        seen = []
        real_teardown = recorder._teardown_active_ffmpeg

        def _observe(app, recording_id, **kwargs):
            db.session.expire_all()
            row = db.session.get(Recording, recording_id)
            seen.append(row.status if row is not None else None)
            return real_teardown(app, recording_id, **kwargs)

        rec = seed.make_recording(status='IN_PROGRESS', channel_id=self.channel.id)
        db.session.commit()
        rid = rec.id
        with recorder._lock:
            recorder._active[rid] = recorder.RecordingState(current_segment_num=1)

        with mock.patch.object(recorder, '_teardown_active_ffmpeg', _observe):
            recorder.abort_recording(self.t.app, rid)

        self.assertEqual(
            seen, [REC_STATUS_ABORTED],
            'abort_recording tore down before committing ABORTED, so a start racing it '
            'can still win the claim and spawn ffmpeg onto an aborted row')


class TwoCallersOneStartTests(_StartHarness):
    """(b) Two threads inside start_recording for one row. Both pass the status check at the
    top - that is the defect - so the claim is what has to decide between them.

    probe_dir is the barrier point: it runs after the status check and before the claim, in
    the stretch of preparatory work that made the window wide enough to matter in the first
    place.
    """

    def _race(self):
        barrier = threading.Barrier(2, timeout=10)
        real_probe = recorder.probe_dir
        launches = []
        launch_lock = threading.Lock()

        def _synced_probe(path):
            barrier.wait()
            return real_probe(path)

        def _record_launch(app, recording_id, seg_num):
            with launch_lock:
                launches.append((recording_id, seg_num))
            return recorder.LAUNCH_SPAWNED

        with mock.patch.object(recorder, 'probe_dir', _synced_probe), \
             mock.patch.object(recorder, '_launch_segment', _record_launch):
            threads = [threading.Thread(target=recorder.start_recording,
                                        args=(self.t.app, self.rid))
                       for _ in range(2)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=20)
                self.assertFalse(th.is_alive(), 'a start_recording thread never finished')
        return launches

    def test_only_one_caller_reaches_a_launch(self):
        self.assertEqual(
            len(self._race()), 1,
            'both callers launched segment 1 - two ffmpegs on one segment path, one of '
            'them reachable from nothing')

    def test_the_loser_does_not_release_the_winners_slot(self):
        """connection_limits.try_acquire is idempotent on (kind, id), so both callers share
        ONE slot entry. A loser that released unconditionally would strip the winner's slot
        while the winner's ffmpeg is still connected."""
        self._race()
        self.assertFalse(
            connlim.try_acquire(self.account_id, 'test', 'probe'),
            "the losing start released the winner's connection slot, so the account can "
            'now oversubscribe its provider')

    def test_the_winner_keeps_its_live_state(self):
        self._race()
        self.assertIsNotNone(
            recorder.get_state(self.rid),
            "the loser's unwind deleted the winner's live state")


class LiveSchedulerDoubleStartTests(unittest.TestCase):
    """(b) as it actually arrives: an overdue persisted start_<id> job and the startup
    sweep's case 2b, against a real running APScheduler.

    This is the reproduction the defect was filed with, but it is a real race and so a
    probabilistic guard: with the fix reverted it catches two starters on most runs, not all
    of them, because the job can commit IN_PROGRESS before the sweep's thread reads the row.
    TwoCallersOneStartTests is the deterministic guard; this one proves the fix against the
    real scheduler, and it can only miss a duplicate, never fail a correct run.
    """

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        # The DVR dir is read by a runtime load_config() inside start_recording, so without
        # the sandbox it resolves to the real default. Where that path does not exist (a CI
        # runner) the start fails the recording before either caller launches, and the test
        # reports 0 starters instead of testing ownership at all.
        dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(dvr, exist_ok=True)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False},
        }})

    def tearDown(self):
        self.t.cleanup()

    def test_one_starter_after_a_restart_that_missed_the_start_time(self):
        acct = seed.make_account()
        ch = seed.make_channel(acct, stream_id=5, name='Restart Channel')
        now = datetime.utcnow()
        rec = seed.make_recording(status='SCHEDULED', channel_id=ch.id,
                                  url='http://example.test/live/5',
                                  start_time=now - timedelta(minutes=2),
                                  stop_time=now + timedelta(minutes=30))
        db.session.commit()
        rid = rec.id

        launches = []
        launch_lock = threading.Lock()

        def _record_launch(app, recording_id, seg_num):
            with launch_lock:
                launches.append((recording_id, seg_num))
            return recorder.LAUNCH_SPAWNED

        try:
            with mock.patch.object(recorder, '_launch_segment', _record_launch):
                # What survives a restart: the start job, already overdue. The scheduler is
                # running, so this is dispatched on its next pass exactly as it would be at
                # startup - and then init_scheduler's very next line runs the sweep.
                scheduler.reschedule_recording_start(rid, now - timedelta(minutes=2))
                scheduler.resume_in_progress_recordings(self.t.app)
                # Wait for the first launch however slow the machine is, then give a
                # duplicate starter time to show itself. A slow machine can only make the
                # settle miss a second launch, never fail a correct run.
                deadline = time.monotonic() + 30
                while not launches and time.monotonic() < deadline:
                    time.sleep(0.05)
                time.sleep(3)

            self.assertEqual(
                len(launches), 1,
                f'{len(launches)} callers started one recording after a restart')
        finally:
            with recorder._lock:
                recorder._active.pop(rid, None)
            connlim.release(acct.id, 'recording', rid)


class LaunchSegmentStatusGateTests(_StartHarness):
    """The belt-and-braces gate: IN_PROGRESS is the only status under which a capture may
    exist, so _launch_segment re-reads it rather than trusting whatever its caller saw."""

    def _launch_with_status(self, status):
        rec = seed.make_recording(status=status, channel_id=self.channel.id,
                                  url='http://example.test/live/77')
        db.session.commit()
        rid = rec.id
        state = recorder.RecordingState()
        with recorder._lock:
            recorder._active[rid] = state
        try:
            with mock.patch.object(recorder.subprocess, 'Popen',
                                   side_effect=lambda *a, **kw: _fake_proc()) as popen:
                result = recorder._launch_segment(self.t.app, rid, seg_num=1)
            return result, popen, rid
        finally:
            # A launch that succeeds starts a REAL watchdog thread, which would outlive this
            # test and relaunch against an unpatched Popen - netguard then fails whichever
            # unrelated test happens to be running when it does.
            state.stop_event.set()
            if state.watchdog is not None:
                state.watchdog.join(timeout=10)
                self.assertFalse(state.watchdog.is_alive(), 'watchdog thread outlived the test')
            with recorder._lock:
                recorder._active.pop(rid, None)

    def test_an_aborted_recording_is_refused(self):
        result, popen, rid = self._launch_with_status('ABORTED')
        self.assertIs(result, recorder.LAUNCH_REFUSED)
        self.assertFalse(popen.called, 'ffmpeg was spawned for an ABORTED recording')
        self.assertEqual(RecordingSegment.query.filter_by(recording_id=rid).count(), 0)

    def test_a_paused_recording_is_refused(self):
        result, popen, _ = self._launch_with_status('PAUSED')
        self.assertIs(result, recorder.LAUNCH_REFUSED)
        self.assertFalse(popen.called)

    def test_an_in_progress_recording_still_launches(self):
        """The gate must not cost the ordinary path anything."""
        result, popen, rid = self._launch_with_status(REC_STATUS_IN_PROGRESS)
        self.assertIs(result, recorder.LAUNCH_SPAWNED)
        self.assertTrue(popen.called)
        self.assertEqual(
            RecordingEvent.query.filter_by(
                recording_id=rid, event_type=SEGMENT_STARTED).count(), 1)

    def test_a_deleted_recording_is_refused_rather_than_raising(self):
        rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS)
        db.session.commit()
        rid = rec.id
        db.session.delete(rec)
        db.session.commit()
        with mock.patch.object(recorder.subprocess, 'Popen') as popen:
            self.assertIs(recorder._launch_segment(self.t.app, rid, seg_num=1),
                          recorder.LAUNCH_REFUSED)
        self.assertFalse(popen.called)


class LiveStateClaimTests(_StartHarness):
    """The in-memory half, on its own. A plain assignment into _active is a silent
    overwrite, which is how the second caller used to orphan the first one's ffmpeg."""

    def test_a_second_claim_is_refused(self):
        first = recorder.RecordingState()
        second = recorder.RecordingState()
        self.assertTrue(recorder._claim_live_state(self.rid, first))
        self.assertFalse(recorder._claim_live_state(self.rid, second))
        self.assertIs(recorder.get_state(self.rid), first)

    def test_release_only_drops_the_state_it_was_given(self):
        winner = recorder.RecordingState()
        loser = recorder.RecordingState()
        self.assertTrue(recorder._claim_live_state(self.rid, winner))
        recorder._release_live_state(self.rid, loser)
        self.assertIs(recorder.get_state(self.rid), winner,
                      "a loser's unwind deleted the winner's live state")
        recorder._release_live_state(self.rid, winner)
        self.assertIsNone(recorder.get_state(self.rid))


class ResumeClaimTests(_StartHarness):
    """resume_recording is the same shape and gets the same treatment - a cancel landing
    inside it must not leave a capture running on an ABORTED row either."""

    def _paused_recording(self):
        rec = seed.make_recording(status='PAUSED', channel_id=self.channel.id,
                                  url='http://example.test/live/77')
        db.session.commit()
        return rec.id

    def test_a_cancel_during_resume_spawns_nothing(self):
        rid = self._paused_recording()
        real = recorder.end_slot_wait
        fired = []

        def _abort_then_run(*args, **kwargs):
            if not fired:
                fired.append(True)
                recorder.abort_recording(self.t.app, rid)
            return real(*args, **kwargs)

        try:
            with mock.patch.object(recorder, 'end_slot_wait', _abort_then_run), \
                 mock.patch.object(recorder.subprocess, 'Popen',
                                   side_effect=lambda *a, **kw: _fake_proc()) as popen:
                recorder.resume_recording(self.t.app, rid)
            self.assertFalse(popen.called)
            db.session.expire_all()
            self.assertEqual(db.session.get(Recording, rid).status, REC_STATUS_ABORTED)
            self.assertEqual(RecordingSegment.query.filter_by(recording_id=rid).count(), 0)
        finally:
            with recorder._lock:
                recorder._active.pop(rid, None)
            connlim.release(self.account_id, 'recording', rid)

    def test_a_resume_refuses_when_something_is_already_live(self):
        rid = self._paused_recording()
        with recorder._lock:
            recorder._active[rid] = recorder.RecordingState(current_segment_num=3)
        try:
            with mock.patch.object(recorder, '_launch_segment') as launch:
                recorder.resume_recording(self.t.app, rid)
            self.assertFalse(launch.called)
            db.session.expire_all()
            self.assertEqual(db.session.get(Recording, rid).status, 'PAUSED',
                             'a refused resume still flipped the status')
        finally:
            with recorder._lock:
                recorder._active.pop(rid, None)
            connlim.release(self.account_id, 'recording', rid)


if __name__ == '__main__':
    unittest.main()
