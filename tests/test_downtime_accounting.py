"""Tier 2 - what "Downtime" counts, and the post-capture content shortfall beside it.

Guards dev/docs/BUGS.md 2026-08-01 "Downtime under-reports content loss by ~5x". Design and
reasoning: dev/changelog/432.

The defect was arithmetic, not a wrong branch: the watchdog added `restart_delay` per stall
and nothing else, so the counter measured the app's own deliberate pause and ignored both
the window spent noticing the stall and the wait for the replacement segment to start
writing. Recording 71 (dev/changelog/429) reported 375s against a real 1814s shortfall.

Three quantities ship, and these tests hold the line between them: `total_downtime_seconds`
is the capture-time gap counter, which by construction cannot see a stream that stays
connected and delivers almost nothing; `capture_gap_seconds` is its post-capture subset,
read off the segment clocks, covering only the time no capture process was running at all;
and `content_vs_capture_seconds` compares the finished file against that capture time,
which is the one that can see a connected feed delivering short.

No network and no provider host: every child here is `sys.executable -c ...`, a local argv
with no URL in it, which tests/support/netguard.py permits. Segment files are written under
make_test_app's temp dir, never /dvr.
"""
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.recorder as recorder  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    RESTART_FAILED, RESTART_SUCCEEDED, Recording, RecordingSegment,
)
from app.watchdog import WatchdogThread  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.watchdog import RestartHarness, sleeper  # noqa: E402


class DowntimeCountsTheWholeGapTests(RestartHarness):

    # The canonical growth-stall run, kept for the whole class. See _growth_stall_run.
    _growth_run = None

    def _growth_stall_run(self):
        """The growth-stall scenario - 3s stall timeout, 2s restart delay, a restart that
        produces data - run ONCE and read by the two tests below.

        They assert different things about the same run deliberately, so that a partial
        fix cannot pass one and be excused by the other. But the run itself is a real
        WatchdogThread measuring real elapsed time, so it costs ~6.3s of wall clock;
        performing it twice proved nothing the two assertions do not already prove
        separately and cost the suite 6.3s. Same shape as
        tests/test_channel_search_page_js.py::_observations - one expensive fixture,
        many assertions on it. Do not inline it back into the tests.
        """
        if DowntimeCountsTheWholeGapTests._growth_run is None:
            self.state.process = sleeper()
            rec = self._run_until_event(RESTART_SUCCEEDED, stall_timeout=3,
                                        restart_delay=2, produces_data=True)
            DowntimeCountsTheWholeGapTests._growth_run = {
                'downtime': rec.total_downtime_seconds,
                'detection': self._stall_seconds(),
            }
        return DowntimeCountsTheWholeGapTests._growth_run

    def test_a_growth_stall_banks_the_detection_window_as_well_as_the_delay(self):
        """The headline defect. With a 3s stall timeout and a 2s restart delay the old
        code recorded exactly 2.0 - the deliberate pause - and threw away the 3s during
        which, by definition, nothing was being written."""
        run = self._growth_stall_run()

        detection = run['detection']
        self.assertGreaterEqual(detection, 2.5, 'the growth timer did not run its course')
        self.assertGreaterEqual(
            run['downtime'], detection + 2,
            f"downtime {run['downtime']:.1f}s is short of the "
            f'{detection:.1f}s detection window plus the 2s restart delay - the '
            f'detection window is being thrown away again')

    def test_downtime_is_not_merely_the_restart_delay(self):
        """The precise shape of the old bug, asserted on its own so a partial fix cannot
        pass: the counter must not land on restart_delay x stalls."""
        run = self._growth_stall_run()

        self.assertGreater(run['downtime'], 2.5,
                           'downtime is still just the restart delay')

    def test_a_restart_that_produced_no_data_still_counts_its_gap(self):
        """Dropping it would make the worst captures - the ones whose restarts never come
        back - look like the cleanest ones."""
        self.state.process = sleeper()
        rec = self._run_until_event(RESTART_FAILED, stall_timeout=3, restart_delay=2,
                                    produces_data=False)

        self.assertGreaterEqual(
            rec.total_downtime_seconds, self._stall_seconds() + 2,
            'a failed restart banked no downtime for its own dead time')

    def test_an_exited_capture_is_not_charged_a_detection_window_it_did_not_spend(self):
        """Characterization, not a regression guard - it passes with the fix reverted too,
        because the old code banked a flat restart_delay of 0 here. It is kept because it
        pins the other side of the rule: dev/changelog/431 made a dead process cost about
        one poll to notice, and a later change that assumed the detection window equals
        stall_timeout would invent 30s of downtime that never happened."""
        self._exited_capture()
        rec = self._run_until_event(RESTART_SUCCEEDED, stall_timeout=30, restart_delay=0,
                                    produces_data=True)

        self.assertLess(rec.total_downtime_seconds, 10,
                        f'downtime {rec.total_downtime_seconds:.1f}s on a capture that was '
                        f'noticed within a poll - the stall timeout is being charged '
                        f'instead of the time actually lost')


class CaptureGapAndContentTests(unittest.TestCase):
    """The post-capture half, rebuilt by dev/changelog/942. Pure model arithmetic over
    columns already on the row, so it is correct retroactively for every recording already
    in the database.

    What it replaced: one property, `content_shortfall_seconds`, computing
    max(0, window - content). That is a NET figure, and a provider that replays its buffer
    when a dropped connection is re-established delivers more content than the wall clock
    it ran on - so the surplus cancelled real gap time and the clamp hid the sign. On
    recording 14 (28 segments, 27 stalls) it reported "missing 0s (0%)" against 135.7s of
    measured gaps. Two facts are reported now and never netted: gap time from the segment
    clocks, and the delivered length against the time the capture actually ran.
    """

    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow()

    def tearDown(self):
        self.t.cleanup()

    def _rec(self, **kw):
        rec = seed.make_recording(start_time=self.now - timedelta(hours=1),
                                  stop_time=self.now, **kw)
        db.session.commit()
        return rec

    def _seg(self, rec, start_min_ago, end_min_ago, **kw):
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=kw.pop('segment_number', 0),
            file_path='/nonexistent.ts',
            started_at=self.now - timedelta(minutes=start_min_ago),
            ended_at=self.now - timedelta(minutes=end_min_ago),
            bytes_recorded=kw.pop('bytes_recorded', 4096), **kw))

    def _reload(self, rec):
        db.session.commit()
        db.session.expire_all()
        return db.session.get(Recording, rec.id)

    def test_a_surplus_can_no_longer_cancel_gap_time(self):
        """The defect, at recording 14's shape: two segments covering 50 of the window's
        60 minutes, and a file LONGER than the window because the feed replayed its buffer
        at the join. The old net figure read zero. The gap is 10 minutes and has to say so
        whatever the content length does."""
        rec = self._rec(status='COMPLETED', recorded_duration_seconds=3900.0)
        self._seg(rec, 60, 35, segment_number=0)
        self._seg(rec, 25, 0, segment_number=1)
        rec = self._reload(rec)

        self.assertAlmostEqual(rec.covered_capture_seconds, 3000.0, delta=1)
        self.assertAlmostEqual(rec.capture_gap_seconds, 600.0, delta=1)
        self.assertAlmostEqual(rec.content_vs_capture_seconds, 900.0, delta=1)

    def test_the_two_figures_reconcile_to_the_window_minus_the_content(self):
        """gap - content_vs_capture == window - content, exactly. The old single number is
        still derivable from the pair, which is what lets the detail page explain why a
        file is longer than the window it was recording."""
        rec = self._rec(status='COMPLETED', recorded_duration_seconds=3900.0)
        self._seg(rec, 60, 35, segment_number=0)
        self._seg(rec, 25, 0, segment_number=1)
        rec = self._reload(rec)

        self.assertAlmostEqual(
            rec.capture_gap_seconds - rec.content_vs_capture_seconds,
            rec.duration_seconds - rec.actual_duration_seconds, delta=0.01)

    def test_a_feed_delivering_less_than_real_time_reads_negative(self):
        """The case content_shortfall existed to catch, and the one downtime structurally
        cannot see: the stream stayed connected for the whole window and delivered short.
        A signed figure keeps it visible without a second stat."""
        rec = self._rec(status='COMPLETED', recorded_duration_seconds=3000.0)
        self._seg(rec, 60, 0)
        rec = self._reload(rec)

        self.assertAlmostEqual(rec.capture_gap_seconds, 0.0, delta=1)
        self.assertAlmostEqual(rec.content_vs_capture_seconds, -600.0, delta=1)

    def test_overlapping_segment_spans_are_a_union_not_a_sum(self):
        """Summing spans would count the shared time twice and push covered time past the
        window itself, which would invent a negative gap on a recording that had none."""
        rec = self._rec(status='COMPLETED', recorded_duration_seconds=3600.0)
        self._seg(rec, 60, 20, segment_number=0)
        self._seg(rec, 30, 0, segment_number=1)
        rec = self._reload(rec)

        self.assertAlmostEqual(rec.covered_capture_seconds, 3600.0, delta=1)
        self.assertAlmostEqual(rec.capture_gap_seconds, 0.0, delta=1)

    def test_a_segment_running_past_the_stop_is_clipped_to_the_window(self):
        """stop_time is stamped when the app decides to stop; ffmpeg finishes writing just
        after. Counting that tail as covered window would report a negative gap."""
        rec = self._rec(status='COMPLETED', recorded_duration_seconds=3600.0)
        self._seg(rec, 60, -2)
        rec = self._reload(rec)

        self.assertAlmostEqual(rec.covered_capture_seconds, 3600.0, delta=1)
        self.assertEqual(rec.capture_gap_seconds, 0.0)

    def test_both_are_unknown_while_the_recording_is_still_running(self):
        """A number that cannot be known yet must read as unknown, not as zero - zero would
        say 'nothing missing' about a recording halfway through a stall."""
        rec = self._rec(status='IN_PROGRESS')

        self.assertIsNone(rec.capture_gap_seconds)
        self.assertIsNone(rec.content_vs_capture_seconds)

    def test_a_failed_recording_falls_back_to_the_segment_span_and_says_so(self):
        """FAILED never concatenates, so there is no file to probe and the content length
        falls back to the segment span - which is the capture time, so the content figure
        is structurally zero there. actual_duration_source is what stops the UI presenting
        that zero as a measurement of a finished file."""
        rec = self._rec(status='FAILED')
        self._seg(rec, 60, 30)
        rec = self._reload(rec)

        self.assertAlmostEqual(rec.capture_gap_seconds, 1800.0, delta=1)
        self.assertAlmostEqual(rec.content_vs_capture_seconds, 0.0, delta=1)
        self.assertEqual(rec.actual_duration_source, 'segments')

    def test_the_gap_is_measured_against_the_adjusted_window(self):
        """CLAUDE.md's time-and-counter-accounting rule: scheduled, requested, actual and
        content duration are four different values. An abort rewrites stop_time to the real
        stop, so the window we were trying to fill is start_time..stop_time - measuring
        against the untouched scheduled window would report the whole cancelled remainder
        as time the app failed to capture."""
        rec = self._rec(status='ABORTED', recorded_duration_seconds=1800.0)
        rec.stop_time = self.now - timedelta(minutes=30)
        self._seg(rec, 60, 30)
        rec = self._reload(rec)

        self.assertEqual(rec.scheduled_duration_seconds, 3600.0)
        self.assertEqual(rec.duration_seconds, 1800.0)
        self.assertAlmostEqual(rec.capture_gap_seconds, 0.0, delta=1)


class FailoverBanksTheReconnectWaitTests(RestartHarness):
    """BUGS.md 2026-10-20 - total_downtime_seconds skips the reconnect wait on every
    failover branch.

    The same-member restart measures the gap from the stall to the replacement's first
    byte inline. The five branches that fail over instead launched the next member's
    segment and handed off to the outer loop at once, banking nothing for the wait until
    that segment wrote its first byte - so a group recording under-reported downtime by
    one reconnect per move (dev/changelog/1242).
    """

    FIRST_BYTE_AFTER = 3.0

    def setUp(self):
        super().setUp()
        self._spawned = []
        self._timers = []

    def tearDown(self):
        for t in self._timers:
            t.cancel()
        for proc in self._spawned:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        super().tearDown()

    def _cfg(self, stall_timeout, restart_delay=0):
        # The first stall hits the ceiling, so the watchdog takes the max-consecutive-
        # failures branch - a failover, with the move itself stubbed to succeed.
        return cfgmod._deep_merge(super()._cfg(stall_timeout, restart_delay),
                                  {'watchdog': {'max_consecutive_failures': 1}})

    def _stub_launch_slow_first_byte(self):
        """The next member's segment: live child, empty file, first byte after a delay."""
        def _stub(app, recording_id, seg_num):
            self.launched.append(seg_num)
            path = os.path.join(self.t._tmpdir, f'rec_{self.rid}_seg_{seg_num:03d}.ts')
            open(path, 'wb').close()
            db.session.add(RecordingSegment(recording_id=recording_id,
                                            segment_number=seg_num, file_path=path,
                                            started_at=datetime.utcnow()))
            db.session.commit()
            proc = sleeper()
            self._spawned.append(proc)
            self.state.process = proc
            self.state.current_segment_num = seg_num

            def _first_byte():
                with open(path, 'ab') as fh:
                    fh.write(b'\x47' * 188)
            timer = threading.Timer(self.FIRST_BYTE_AFTER, _first_byte)
            self._timers.append(timer)
            timer.start()
            return recorder.LAUNCH_SPAWNED
        return _stub

    def test_the_wait_for_the_new_member_s_first_byte_is_downtime(self):
        self._exited_capture()
        with mock.patch.object(cfgmod, 'load_config', return_value=self._cfg(30)), \
             mock.patch.object(recorder, 'failover_group_member', return_value=True), \
             mock.patch.object(recorder, '_launch_segment',
                               self._stub_launch_slow_first_byte()):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + 25
            downtime = 0.0
            while time.monotonic() < deadline:
                db.session.expire_all()
                downtime = db.session.get(Recording, self.rid).total_downtime_seconds or 0.0
                if downtime >= self.FIRST_BYTE_AFTER - 0.5:
                    break
                time.sleep(0.2)
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        self.assertEqual(self.launched, [1], 'the failover did not launch the next member')
        self.assertGreaterEqual(
            downtime, self.FIRST_BYTE_AFTER - 0.5,
            f'a failover whose new member took {self.FIRST_BYTE_AFTER}s to write its first '
            f'byte banked {downtime:.1f}s of downtime')


if __name__ == '__main__':
    unittest.main()
