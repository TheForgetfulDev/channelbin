"""Tier 2 - a relaunch whose spawn never happened is counted by exactly one owner.

Guards dev/docs/BUGS.md 2026-09-15 "A failed ffmpeg spawn during a watchdog restart is
counted twice, and two threads then own the retry". Design and reasoning:
dev/changelog/984.

The defect was two owners for one failure, not a wrong branch. When subprocess.Popen raises
inside _launch_segment during a watchdog-driven restart, _handle_launch_failure has already
done the whole accounting - the increment, the RESTART_FAILED, and either the give-up or the
relaunch thread. The watchdog could not tell: _launch_segment returned None whatever
happened, state.process still pointed at the OLD dead process, so wait_for_file_data came
back False on its first poll and the restart-failed path counted the identical failure a
second time. One transient Popen failure then cost three increments on a non-group
recording, burned a group member (and blended the recording fail floor into it for a LOCAL
failure, which health_score.py forbids by name), or wrote a second RECORDING_FAILED and a
second 'failed' health blend on a recording that was already FAILED.

No network and no real ffmpeg: recorder.subprocess.Popen is patched throughout, and the
stand-in children that do run are `sys.executable -c ...` - a local argv with no URL in it,
which tests/support/netguard.py permits. Segment files go under make_test_app's temp dir,
never /dvr.
  python3 -m unittest tests.test_watchdog_launch_failure_ownership
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.health_score as health_score  # noqa: E402
import app.recorder as recorder  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    ChannelEvent, Recording, RecordingEvent, RecordingSegment,
    CHANNEL_FAILOVER_HEALTH_OBSERVATION, GROUP_FAILOVER, RECORDING_FAILED, RESTART_FAILED,
    STALL_DETECTED,
)
from app.watchdog import WatchdogThread  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.test_downtime_accounting import _RestartHarness  # noqa: E402


class _FailedSpawnHarness(_RestartHarness):
    """The real WatchdogThread over the REAL _launch_segment, with Popen raising on the
    relaunch. Everything else in the rig is inherited: one IN_PROGRESS recording, one open
    segment, and an opener process that has already exited so the stall is noticed within
    a single poll rather than a stall timeout."""

    # Long enough that a relaunched segment killed on its first poll (~1s) is
    # unmistakably distinct from one left alone, so the assertion below is not deciding a
    # 10x margin on a stopwatch.
    stall_timeout = 10
    max_failures = 99
    restart_delay = 0

    def setUp(self):
        super().setUp()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self._spawned = []
        # The give-up path persists a final thumbnail from the joined file, which does not
        # exist here and is not what any of this asserts.
        self._thumb_patcher = mock.patch.object(recorder, 'persist_final_thumbnail')
        self._thumb_patcher.start()

    def tearDown(self):
        self._thumb_patcher.stop()
        # A relaunch's child is handed to production code that never owns it here, so the
        # harness reaps it rather than leaving a sleeper per test.
        for proc in self._spawned:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        super().tearDown()

    def _child(self):
        """A stand-in for a successfully spawned ffmpeg: a local sleeper, so poll()
        reports it alive the way a capture stuck on connect does, and it writes nothing to
        the segment path - which is what makes "was this segment killed on its first poll"
        a question with two different answers."""
        from tests.test_watchdog_process_exit import _sleeper
        proc = _sleeper()
        self._spawned.append(proc)
        return proc

    def _cfg(self, stall_timeout=None, restart_delay=None):
        cfg = super()._cfg(stall_timeout or self.stall_timeout,
                           self.restart_delay if restart_delay is None else restart_delay)
        cfg['watchdog']['max_consecutive_failures'] = self.max_failures
        cfg['recording']['dvr_output_dir'] = self.dvr
        cfg['recording']['live_thumbnail']['enabled'] = False
        return cfg

    def _run_with_failing_spawn(self, spawns, *, until, timeout=45):
        """Start the real thread with recorder.subprocess.Popen driven by `spawns` (a
        side_effect list: exceptions raise, objects are returned as the new child), and
        stop it once `until()` is true or `timeout` elapses.

        recorder.load_config is patched alongside cfgmod.load_config because recorder.py
        binds load_config at module import, so the watchdog's own patch does not reach the
        real _launch_segment - and without it the segment path would land in the real
        /dvr (CLAUDE.md, Testing).
        """
        cfg = self._cfg()
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(recorder, 'load_config', return_value=cfg), \
             mock.patch.object(recorder.subprocess, 'Popen', side_effect=spawns) as popen:
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                db.session.expire_all()
                if until():
                    break
                time.sleep(0.2)
            # A settling window: the defect's second increment, second RESTART_FAILED and
            # failover all land within one poll of the relaunch, so stopping the instant
            # the relaunch appears would let the old behavior pass by arriving late.
            time.sleep(2.5)
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        db.session.expire_all()
        return popen

    def _events(self, event_type):
        db.session.expire_all()
        return RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=event_type).order_by(
                RecordingEvent.id.asc()).all()

    def _segment(self, seg_num):
        db.session.expire_all()
        return RecordingSegment.query.filter_by(
            recording_id=self.rid, segment_number=seg_num).first()

    def _relaunched(self):
        return self._segment(1) is not None


class OneTransientSpawnFailureCostsOneFailureTests(_FailedSpawnHarness):
    """The non-group shape, branch (a) of the report: three increments and two
    RESTART_FAILED events for one Popen that raised, then the healthy relaunch killed on
    its first poll by a verdict recorded against a segment that was never spawned."""

    # A real restart delay, so the downtime assertion below has a floor that cannot be
    # supplied by anything else in the run.
    restart_delay = 1

    # The relaunch scenario, run ONCE for the whole class: it is a real WatchdogThread
    # measuring real elapsed time and costs ~5s of wall clock, and the four assertions
    # below are deliberately separate so a partial fix cannot pass one and be excused by
    # another. Same shape as test_downtime_accounting's _growth_stall_run.
    _run = None

    def _failed_then_healthy_run(self):
        if OneTransientSpawnFailureCostsOneFailureTests._run is None:
            self._exited_capture()
            self._run_with_failing_spawn(
                [OSError('ffmpeg not found'), self._child()],
                until=self._relaunched)
            rec = db.session.get(Recording, self.rid)
            stalls = [json.loads(e.extra_data or '{}').get('stall_reason')
                      for e in self._events(STALL_DETECTED)]
            OneTransientSpawnFailureCostsOneFailureTests._run = {
                'consecutive_failures': rec.consecutive_failures,
                'restart_failed': [e.detail for e in self._events(RESTART_FAILED)],
                'stall_reasons': stalls,
                'relaunch_open': (self._segment(1) is not None
                                  and self._segment(1).ended_at is None),
                'downtime': rec.total_downtime_seconds,
            }
        return OneTransientSpawnFailureCostsOneFailureTests._run

    def test_the_failure_is_counted_once(self):
        """The headline number. The opener's stall is one increment and the failed spawn
        is the second; the watchdog re-deriving the same failure made it three."""
        self.assertEqual(self._failed_then_healthy_run()['consecutive_failures'], 2,
                         'one transient Popen failure was charged more than once')

    def test_only_the_launch_failure_says_what_happened(self):
        """Asserted apart from the count so a fix that stops incrementing but still writes
        the event cannot pass: the second event claimed a segment that was never spawned
        "produced no data within Ts", which is simply not what occurred."""
        details = self._failed_then_healthy_run()['restart_failed']
        self.assertEqual(len(details), 1, f'expected one RESTART_FAILED, got {details}')
        self.assertIn('Launch failed', details[0])
        self.assertNotIn('produced no data', details[0])

    def test_the_healthy_relaunch_is_not_killed_on_its_first_poll(self):
        """restart_no_data_segment was set from a restart that never happened, so the next
        outer-loop pass killed the relaunch the retry thread had just made - the third
        increment, and the one that reaches max_consecutive_failures far below budget."""
        run = self._failed_then_healthy_run()
        self.assertNotIn('restart_no_data', run['stall_reasons'],
                         'the relaunched segment was killed on a verdict recorded against '
                         'a segment that was never spawned')
        self.assertTrue(run['relaunch_open'],
                        'the relaunched segment was closed instead of being watched')

    def test_the_gap_is_still_charged_as_downtime(self):
        """The clock was never the thing double-counted. Skipping the whole restart-failed
        path must not quietly stop measuring time in which nothing was captured.

        The floor is the 1s restart delay this class configures: an exited capture is
        noticed on the first poll with a 0.0s detection window, so everything banked here
        is the deliberate wait plus the failed spawn, and nothing else can supply it."""
        self.assertGreaterEqual(
            self._failed_then_healthy_run()['downtime'], 1,
            'the failed relaunch banked none of the gap it spent')


class AFailedSpawnDoesNotBurnAGroupMemberTests(_FailedSpawnHarness):
    """Branch (b): the failover took a working member out of the running and blended the
    recording fail floor into its score for a failure that was local to this machine."""

    def setUp(self):
        super().setUp()
        acct = seed.make_account(name='Failover Provider')
        self.current = seed.make_channel(acct, name='Current', health_score=90)
        self.spare = seed.make_channel(acct, name='Spare', health_score=80)
        grp = seed.make_group(members=[self.current, self.spare])
        rec = db.session.get(Recording, self.rid)
        rec.channel_id = self.current.id
        rec.group_id = grp.id
        db.session.commit()

    def test_a_failed_spawn_moves_nobody(self):
        self._exited_capture()
        # A third entry so a failover's own relaunch has something to take rather than
        # raising StopIteration and hiding the failover behind an exception.
        self._run_with_failing_spawn(
            [OSError('ffmpeg not found'), self._child(), self._child()],
            until=self._relaunched)

        self.assertEqual(self._events(GROUP_FAILOVER), [],
                         'a local spawn failure moved the recording off its member')

    def test_the_member_is_not_scored_for_a_local_failure(self):
        """health_score.py names this rule: the recording fail floor describes a feed that
        failed, and a Popen that raised says nothing whatever about the feed."""
        self._exited_capture()
        self._run_with_failing_spawn(
            [OSError('ffmpeg not found'), self._child(), self._child()],
            until=self._relaunched)

        db.session.expire_all()
        observations = ChannelEvent.query.filter_by(
            event_type=CHANNEL_FAILOVER_HEALTH_OBSERVATION).all()
        self.assertEqual(observations, [],
                         'a member was scored down for a failure on this machine')


class APersistentSpawnFailureFailsTheRecordingOnceTests(_FailedSpawnHarness):
    """Branch (c): the binary is gone, _handle_launch_failure gives up, and the watchdog
    then reached _give_up() as well - a second RECORDING_FAILED and a second 'failed'
    health blend, the duplicate-observation shape dev/changelog/951 removed elsewhere."""

    max_failures = 2

    def test_the_recording_is_failed_exactly_once(self):
        self._exited_capture()
        self._run_with_failing_spawn(
            OSError('ffmpeg not found'),
            until=lambda: db.session.get(Recording, self.rid).status == 'FAILED')

        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'FAILED')
        self.assertEqual(len(self._events(RECORDING_FAILED)), 1,
                         'the recording was failed twice for one give-up')


class AlreadyFailedIsNotFailedAgainTests(_FailedSpawnHarness):
    """_mark_recording_failed never read the status, so whichever thread arrived second
    wrote a second terminal event over a row another owner had already closed out. Driven
    directly rather than through a race, so nothing here depends on timing."""

    def _watchdog(self):
        """A WatchdogThread instance that is never started - only its give-up tail is
        under test, and running the loop would relaunch segments this test never asked
        for."""
        return WatchdogThread(self.rid, self.state, self.t.app)

    def test_a_failed_row_refuses_a_second_terminal_write(self):
        rec = db.session.get(Recording, self.rid)
        rec.status = 'FAILED'
        db.session.commit()

        with self.t.app.app_context():
            rec = db.session.get(Recording, self.rid)
            marked = self._watchdog()._fail_recording(rec, max_failures=5)

        self.assertFalse(marked, '_fail_recording claimed it failed an already-FAILED row')
        self.assertEqual(self._events(RECORDING_FAILED), [],
                         'a second terminal event was written over a closed-out row')

    def test_a_refused_failure_blends_no_second_health_observation(self):
        """Channel.health_score is a lossy exponential average, so a second 'failed' blend
        for one failure cannot be subtracted back out - refusing the write is only half
        the fix if _give_up scores it anyway."""
        rec = db.session.get(Recording, self.rid)
        rec.status = 'FAILED'
        db.session.commit()

        wd = self._watchdog()
        with mock.patch.object(health_score, 'apply_recording_health_observation') as blend:
            with self.t.app.app_context():
                r = db.session.get(Recording, self.rid)
                wd._give_up(lambda: wd._fail_recording(r, max_failures=5))

        blend.assert_not_called()

    def test_a_live_row_is_still_failed_normally(self):
        """The guard must not swallow the give-up it exists to de-duplicate."""
        with self.t.app.app_context():
            rec = db.session.get(Recording, self.rid)
            marked = self._watchdog()._fail_recording(rec, max_failures=5)

        self.assertTrue(marked)
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).status, 'FAILED')
        self.assertEqual(len(self._events(RECORDING_FAILED)), 1)


if __name__ == '__main__':
    unittest.main()
