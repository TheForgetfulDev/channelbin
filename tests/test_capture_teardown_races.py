"""Tier 2 - a capture's boundaries: what a teardown, a resume, a block move and a give-up
leave behind.

Four defects from the recording-pipeline correctness pass (dev/changelog/1180), fixed
together in dev/changelog/1242. Each guards its own dev/docs/BUGS.md entry, all dated
2026-10-20:

  * a teardown that lands while a segment is launching orphans the new ffmpeg - the
    launch only lost to a teardown that had finished, never to one that had begun;
  * resume_recording() on a recording that has already ended took a connection seat and
    registered live state that nothing ever released;
  * a block move whose target refused the seat released the old one first and could not
    get it back, so the recording ran on with no seat at all and was never moved again;
  * the max-failures give-up after a restart that produced no data left that segment's
    row open forever.

No network and no provider host: every child here is `sys.executable -c ...`, a local argv
with no URL in it, which tests/support/netguard.py permits. Segment files are written under
make_test_app's temp dir, never /dvr.
"""
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.recorder as recorder  # noqa: E402
from app import db  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app import scheduler  # noqa: E402
from app.database import (  # noqa: E402
    RESTART_FAILED, Recording, RecordingEvent, RecordingSegment,
    REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_IN_PROGRESS,
)
from app.watchdog import WatchdogThread  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.test_account_blocks import _LiveCase  # noqa: E402
from tests.support.watchdog import RestartHarness, WatchdogHarness, sleeper  # noqa: E402


# ── A launch loses to a teardown that has begun ───────────────────────────────
class LaunchLosesToATeardownInProgressTests(unittest.TestCase):
    """BUGS.md 2026-10-20 - a teardown that lands while the watchdog is launching the next
    segment orphans the new ffmpeg.

    _teardown_active_ffmpeg sets stop_event, kills state.process, closes the open row and
    only then pops _active. _launch_segment guarded the race with `get_state() is None`
    alone, so a launch that passed that check after the event was set but before the pop
    assigned a fresh child to state.process that the teardown had already looked past.
    """

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='Torn')
        db.session.commit()
        self.rid = rec.id
        self.seg_dir = os.path.join(self.t._tmpdir, 'segments')
        os.makedirs(self.seg_dir, exist_ok=True)
        self.state = recorder.RecordingState(current_segment_num=0)
        self.state.stop_event.set()   # the teardown has begun and not yet popped _active
        with recorder._lock:
            recorder._active[self.rid] = self.state
        self.spawned = []

    def tearDown(self):
        for proc in self.spawned:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        with recorder._lock:
            recorder._active.pop(self.rid, None)
        self.ctx.pop()
        self.t.cleanup()

    def _cfg(self):
        return cfgmod._deep_merge(cfgmod.load_config(),
                                  {'recording': {'dvr_output_dir': self.seg_dir,
                                                 'segment_duration_seconds': 0}})

    def _launch(self):
        real_popen = recorder.subprocess.Popen

        def _popen(cmd, *a, **kw):
            for arg in cmd:
                if str(arg).endswith('.ts'):
                    with open(arg, 'wb') as fh:
                        fh.write(b'\x47' * 188)
            # A live local child in place of ffmpeg, spawned with the real Popen (the
            # patch below would otherwise answer this spawn too).
            proc = real_popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                              stdout=recorder.subprocess.DEVNULL,
                              stderr=recorder.subprocess.DEVNULL)
            self.spawned.append(proc)
            return proc

        with mock.patch.object(recorder, 'load_config', self._cfg), \
             mock.patch.object(recorder.subprocess, 'Popen', _popen):
            outcome = recorder._launch_segment(self.t.app, self.rid, seg_num=1)
        db.session.expire_all()
        return outcome

    def test_the_launch_is_abandoned_and_its_child_is_dead(self):
        outcome = self._launch()
        self.assertIs(outcome, recorder.LAUNCH_ABANDONED)
        self.assertEqual(len(self.spawned), 1)
        self.assertIsNotNone(self.spawned[0].poll(),
                             'the child spawned under a teardown in progress must be killed '
                             'by the launch - nothing else can reach it')

    def test_the_child_is_not_handed_to_the_state_the_teardown_already_read(self):
        self._launch()
        self.assertIsNone(self.state.process)

    def test_the_raced_row_is_closed(self):
        self._launch()
        seg = RecordingSegment.query.filter_by(recording_id=self.rid, segment_number=1).one()
        self.assertIsNotNone(seg.ended_at)
        self.assertEqual(seg.exit_reason, 'TEARDOWN_RACE')


class WatchdogExitKillsALateChildTests(WatchdogHarness):
    """Same BUGS.md entry, the residual window: a child assigned to state.process after the
    teardown's kill read it is ended by the watchdog thread on its way out."""

    def test_a_live_child_on_the_state_is_dead_once_the_thread_has_left(self):
        proc = sleeper()
        self.state.process = proc
        self.state.stop_event.set()
        with mock.patch.object(cfgmod, 'load_config', return_value=self._cfg(30)):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            self.wd.join(timeout=15)
        self.assertFalse(self.wd.is_alive())
        self.assertIsNotNone(proc.poll(), 'the watchdog left a live child behind')


class StopDuringTheRelaunchWaitIsNotAVerdictTests(RestartHarness):
    """Same BUGS.md entry, the restart branch's tail: a stop that lands while the relaunch
    is being waited for used to write RESTART_FAILED onto a recording stopped on purpose."""

    def _stub_launch_then_stop(self):
        inner = self._stub_launch_next(produces_data=False)

        def _stub(app, recording_id, seg_num):
            outcome = inner(app, recording_id, seg_num)
            self.state.stop_event.set()   # the stop lands as the relaunch is awaited
            return outcome
        return _stub

    def test_no_restart_failed_is_written_for_a_deliberate_stop(self):
        self._exited_capture()
        with mock.patch.object(cfgmod, 'load_config', return_value=self._cfg(5, 0)), \
             mock.patch.object(recorder, '_launch_segment', self._stub_launch_then_stop()):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            self.wd.join(timeout=30)
        self.assertFalse(self.wd.is_alive())
        self.assertEqual(self.launched, [1], 'the relaunch itself still happens')
        db.session.expire_all()
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=RESTART_FAILED).count(), 0)
        self.assertEqual(db.session.get(Recording, self.rid).consecutive_failures, 1,
                         'only the stall that preceded the relaunch is counted')


# ── A resume on a finished recording holds nothing ────────────────────────────
class ResumeOnAFinishedRecordingTests(unittest.TestCase):
    """BUGS.md 2026-10-20 - resume_recording() on a recording that has already ended takes
    a seat and registers live state that nothing ever releases.

    The function had no status guard: it acquired a seat, claimed _active, branched on
    PAUSED / RETRYING only, and let every other status fall through to _launch_segment,
    which refused - with the seat and the entry still held.
    """

    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account(max_connections=1)
        self.ch = seed.make_channel(self.acc, name='Ch')
        rec = seed.make_recording(status=REC_STATUS_COMPLETED, channel_id=self.ch.id,
                                  name='done')
        db.session.commit()
        self.rid, self.acc_id = rec.id, self.acc.id
        connlim._holders.clear()

    def tearDown(self):
        connlim._holders.clear()
        with recorder._lock:
            recorder._active.pop(self.rid, None)
        self.t.cleanup()

    def test_no_seat_and_no_live_state_are_left_behind(self):
        with mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.resume_recording(self.t.app, self.rid)
        self.assertFalse(launch.called)
        self.assertIsNone(recorder.get_state(self.rid),
                          'a resume on a finished recording registered live state')
        self.assertFalse(any(('recording', self.rid) in holders
                             for holders in connlim._holders.values()),
                         'a resume on a finished recording took a connection seat')

    def test_the_row_is_left_as_it_was(self):
        with mock.patch.object(recorder, '_launch_segment'):
            recorder.resume_recording(self.t.app, self.rid)
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).status, REC_STATUS_COMPLETED)


class StopRemovesTheArmedResumeTests(unittest.TestCase):
    """Same BUGS.md entry, the other half: the resume_<id> and retry_<id> jobs are removed
    by the stop path, so neither fires on the finished row at all."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        now = datetime.utcnow()
        rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='stopping',
                                  start_time=now - timedelta(hours=1),
                                  stop_time=now - timedelta(seconds=1))
        db.session.commit()
        self.rid = rec.id

    def tearDown(self):
        self.t.cleanup()

    def test_resume_and_retry_jobs_are_gone_after_the_stop(self):
        later = datetime.utcnow() + timedelta(hours=1)
        scheduler.reschedule_recording_resume(self.rid, later)
        scheduler.schedule_dead_stream_retry(self.rid, later)
        sched = scheduler.get_scheduler()
        self.assertIsNotNone(sched.get_job(f'resume_{self.rid}'))
        self.assertIsNotNone(sched.get_job(f'retry_{self.rid}'))
        with mock.patch('app.concatenator.do_concatenation'):
            recorder.stop_recording(self.t.app, self.rid)
        self.assertIsNone(sched.get_job(f'resume_{self.rid}'),
                          'the deferred resume outlived the capture it was armed for')
        self.assertIsNone(sched.get_job(f'retry_{self.rid}'),
                          'the dead-stream retry outlived the capture it was armed for')


# ── A refused block move keeps its seat ───────────────────────────────────────
class RefusedBlockMoveKeepsItsSeatTests(_LiveCase):
    """BUGS.md 2026-10-20 - a block move whose target refuses the seat leaves the recording
    running with no seat at all, and the move is never tried again.

    The swap released the old account's seat, tried the target, and on a refusal
    re-acquired on the old account under a comment saying that cannot fail. It fails
    whenever the old account is blocked - its usable limit is 0 - which is exactly when a
    block move asks. The seat is now moved in one step, or not at all.
    """

    def setUp(self):
        super().setUp()
        # The spare account's one seat is held by another recording.
        self.assertTrue(connlim.try_acquire(self.spare_id, 'recording', self.rid + 1000))
        self._block(self.tv_id)

    def _move(self):
        return recorder.failover_group_member(
            self.t.app, self.rid, 'Account "TV Account" is blocked', block_move=True)

    def test_the_refused_move_leaves_the_seat_where_it_was(self):
        self.assertFalse(self._move())
        self.assertIn(('recording', self.rid), connlim._holders.get(self.tv_id, []),
                      'the recording lost its seat on the account it stayed on')
        self.assertNotIn(('recording', self.rid), connlim._holders.get(self.spare_id, []))

    def test_the_block_still_asks_for_the_move(self):
        self.assertFalse(self._move())
        self.assertTrue(connlim.must_yield_to_block(self.tv_id, self.rid),
                        'with its seat gone the block check read "nothing here to move"')

    def test_the_recording_stays_on_its_member(self):
        self.assertFalse(self._move())
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).channel_id, self.ch_tv_id)


class MoveSeatTests(unittest.TestCase):
    """connection_limits.move_seat: the one-step swap the fix above is built on."""

    def setUp(self):
        self.t = make_test_app()
        self.a = seed.make_account(name='A', max_connections=1)
        self.b = seed.make_account(name='B', max_connections=1)
        db.session.commit()
        self.a_id, self.b_id = self.a.id, self.b.id
        connlim._holders.clear()

    def tearDown(self):
        connlim._holders.clear()
        self.t.cleanup()

    def test_a_move_lands_on_the_target_and_leaves_the_source(self):
        self.assertTrue(connlim.try_acquire(self.a_id, 'recording', 7))
        self.assertTrue(connlim.move_seat('recording', 7, self.a_id, self.b_id))
        self.assertNotIn(('recording', 7), connlim._holders.get(self.a_id, []))
        self.assertIn(('recording', 7), connlim._holders.get(self.b_id, []))
        self.assertTrue(connlim.try_acquire(self.a_id, 'recording', 8),
                        'the source seat is free again')

    def test_a_refused_move_changes_nothing(self):
        self.assertTrue(connlim.try_acquire(self.a_id, 'recording', 7))
        self.assertTrue(connlim.try_acquire(self.b_id, 'recording', 9))
        self.assertFalse(connlim.move_seat('recording', 7, self.a_id, self.b_id))
        self.assertIn(('recording', 7), connlim._holders.get(self.a_id, []))
        self.assertEqual(connlim._holders.get(self.b_id, []), [('recording', 9)])


# ── The no-data give-up closes its row ────────────────────────────────────────
class NoDataGiveUpClosesTheRowTests(RestartHarness):
    """BUGS.md 2026-10-20 - the max-failures give-up after a restart that produced no data
    leaves that segment's row open forever.

    Every other give-up reaches _give_up after the stall handling closed the row; this one
    reached it straight from _record_restart_failed_and_commit, and _mark_recording_failed
    deliberately left ended_at NULL for a backlog item that did not exist.
    """

    def setUp(self):
        super().setUp()
        self._stubs = [
            mock.patch('app.health_score.apply_recording_health_observation'),
            mock.patch.object(recorder, 'persist_final_thumbnail'),
        ]
        for s in self._stubs:
            s.start()

    def tearDown(self):
        for s in self._stubs:
            s.stop()
        super().tearDown()

    def _cfg(self, stall_timeout, restart_delay=0):
        # One stall, one failed restart, then the ceiling.
        return cfgmod._deep_merge(super()._cfg(stall_timeout, restart_delay),
                                  {'watchdog': {'max_consecutive_failures': 2}})

    def _run_until_failed(self, timeout=30):
        self._exited_capture()
        with mock.patch.object(cfgmod, 'load_config', return_value=self._cfg(5)), \
             mock.patch.object(recorder, '_launch_segment',
                               self._stub_launch_next(produces_data=False)):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                db.session.expire_all()
                if db.session.get(Recording, self.rid).status == REC_STATUS_FAILED:
                    break
                time.sleep(0.1)
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, REC_STATUS_FAILED)
        return rec

    def test_the_restarted_segment_is_closed_when_the_recording_fails(self):
        self._run_until_failed()
        seg = RecordingSegment.query.filter_by(recording_id=self.rid, segment_number=1).one()
        self.assertIsNotNone(seg.ended_at, 'the give-up left its last segment open')
        self.assertEqual(seg.exit_reason, 'ERROR')

    def test_no_segment_of_a_failed_recording_is_left_open(self):
        self._run_until_failed()
        self.assertEqual(RecordingSegment.query.filter_by(
            recording_id=self.rid, ended_at=None).count(), 0)


if __name__ == '__main__':
    unittest.main()
