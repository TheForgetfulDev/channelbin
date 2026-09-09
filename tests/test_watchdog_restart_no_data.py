"""Tier 2 - a restart that produced no data is closed on that verdict, not re-derived.

Guards dev/docs/BUGS.md 2026-08-05 "Watchdog spends a second stall timeout re-deriving a
restart it already proved dead". Design and reasoning: dev/changelog/470, measured in
dev/changelog/435.

The defect was a discarded measurement, not a wrong branch. proc_utils.wait_for_file_data
polls the relaunched segment for stall_timeout_seconds and the watchdog emits RESTART_FAILED
when it comes back empty - at which point the segment is established dead. Control then fell
through to the outer loop, which built a fresh GrowthMonitor whose first update() reads as
growth (last_size starts at -1), so the stall clock only began on the second poll and needed
another full stall_timeout to reach the identical conclusion. 17 of the 24 zero-byte segment
rows in the whole database sit at 21.1-21.2s because of it.

The production shape matters here and the pre-existing _RestartHarness deliberately does not
have it: its non-producing stub leaves the OLD, already-killed process in state.process, so
the watchdog's proc_exited fast path (dev/changelog/431) fires on the next poll and hides the
delay. In production the relaunched ffmpeg is alive and stuck on connect. _LiveRestartHarness
below supplies a live stand-in so the re-derivation actually happens.

No network and no provider host: every child here is `sys.executable -c ...`, a local argv
with no URL in it, which tests/support/netguard.py permits. Segment files are written under
make_test_app's temp dir, never /dvr.
"""
import json
import os
import sys
import time
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.recorder as recorder  # noqa: E402
import app.watchdog as wdmod  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    RESTART_FAILED, STALL_DETECTED, Recording, RecordingEvent, RecordingSegment,
)
from app.watchdog import WatchdogThread  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.test_downtime_accounting import _RestartHarness  # noqa: E402
from tests.test_watchdog_process_exit import _sleeper  # noqa: E402


class _LiveRestartHarness(_RestartHarness):
    """_RestartHarness with the one production detail it omits: a restart that produces no
    data leaves a LIVE process behind, the way a real ffmpeg stuck on connect does."""

    def setUp(self):
        super().setUp()
        self._spawned = []

    def tearDown(self):
        for proc in self._spawned:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        super().tearDown()

    def _live(self):
        proc = _sleeper()
        self._spawned.append(proc)
        return proc

    def _stub_launch_next(self, *, produces_data):
        inner = super()._stub_launch_next(produces_data=produces_data)

        def _stub(app, recording_id, seg_num):
            inner(app, recording_id, seg_num)
            # After the row exists, so the watchdog reads this process (not the old dead
            # one) when it passes proc= to wait_for_file_data.
            self.state.process = self._live()
        return _stub

    def _run_until_segment_closed(self, seg_num, *, stall_timeout, restart_delay=0,
                                  produces_data=False, timeout=60):
        """Run the real thread until segment seg_num has been closed out, then stop it."""
        with mock.patch.object(cfgmod, 'load_config',
                               return_value=self._cfg(stall_timeout, restart_delay)), \
             mock.patch.object(recorder, '_launch_segment',
                               self._stub_launch_next(produces_data=produces_data)):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + timeout
            closed = None
            while time.monotonic() < deadline:
                db.session.expire_all()
                row = RecordingSegment.query.filter_by(
                    recording_id=self.rid, segment_number=seg_num).first()
                if row is not None and row.ended_at is not None:
                    closed = row
                    break
                time.sleep(0.1)
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        self.assertIsNotNone(closed, f'segment {seg_num} was never closed within {timeout}s')
        db.session.expire_all()
        return RecordingSegment.query.filter_by(
            recording_id=self.rid, segment_number=seg_num).first()

    def _event(self, event_type, seg_num):
        db.session.expire_all()
        return (RecordingEvent.query
                .filter_by(recording_id=self.rid, event_type=event_type,
                           segment_number=seg_num)
                .order_by(RecordingEvent.id.asc()).first())


class RestartWithNoDataIsActedOnTests(_LiveRestartHarness):
    """Segment 0 is the opener; segment 1 is the restart that gets nothing, and is the one
    under test."""

    # 5s stall timeout: long enough that a re-derived window cannot be mistaken for noise,
    # short enough to keep the run near 6s. The opener is an already-exited capture so it
    # is noticed within a poll (dev/changelog/431) instead of costing a stall timeout of
    # its own - this file is about what happens to the RESTART, not to the opener.
    STALL_TIMEOUT = 5

    # The dead-restart run, performed ONCE for the whole class. Same one-expensive-fixture
    # shape as test_downtime_accounting.py::_growth_stall_run, and for the same reason: this
    # is a real WatchdogThread measuring real elapsed time, so each repeat costs ~6s of
    # wall clock and proves nothing the separate assertions do not already prove. The
    # assertions stay separate so a partial fix cannot pass one and be excused by another.
    # Do not inline it back into the tests.
    _run = None

    def _dead_restart_run(self):
        if RestartWithNoDataIsActedOnTests._run is None:
            self._exited_capture()
            seg = self._run_until_segment_closed(1, stall_timeout=self.STALL_TIMEOUT)
            failed = self._event(RESTART_FAILED, 1)
            stalled = self._event(STALL_DETECTED, 1)
            self.assertIsNotNone(failed, 'no RESTART_FAILED for the dead restart')
            self.assertIsNotNone(stalled, 'the dead restart segment was never closed out')
            rec = db.session.get(Recording, self.rid)
            RestartWithNoDataIsActedOnTests._run = {
                'gap': (stalled.timestamp - failed.timestamp).total_seconds(),
                'exit_reason': seg.exit_reason,
                'bytes_recorded': seg.bytes_recorded,
                'detail': stalled.detail,
                'extra': json.loads(stalled.extra_data),
                'downtime': rec.total_downtime_seconds,
                'stall_count': rec.total_stall_count,
            }
        return RestartWithNoDataIsActedOnTests._run

    def test_the_dead_restart_is_closed_without_a_second_stall_timeout(self):
        """The headline defect. Old code: RESTART_FAILED, then one poll of "growth" from
        -1 to 0, then a full stall timeout, then STALL_DETECTED - about 6s here and ~11s
        on the shipped config."""
        gap = self._dead_restart_run()['gap']

        self.assertLess(
            gap, self.STALL_TIMEOUT,
            f'{gap:.1f}s passed between establishing the restart produced no data and '
            f'acting on it, with a {self.STALL_TIMEOUT}s stall timeout - the verdict is '
            f'being re-derived from scratch')

    def test_the_row_says_the_restart_got_nothing_rather_than_claiming_a_stall(self):
        """Asserted separately from the timing so falsification does not rest on a clock.
        STALL_KILLED here would describe a capture that was running and stopped; this one
        never delivered a byte, and the recording detail page renders the two differently."""
        run = self._dead_restart_run()

        self.assertEqual(run['exit_reason'], 'RESTART_NO_DATA')
        self.assertEqual(run['bytes_recorded'], 0)

    def test_the_event_names_the_reason_and_the_zero_detection_window(self):
        """Product Principle 1: the event log has to say which shape this was. The 0.0s
        stall_duration is the fix's own evidence - the detection window is zero because the
        answer was already in hand, and it is what a later analysis pass reads."""
        run = self._dead_restart_run()

        self.assertEqual(run['extra']['stall_reason'], 'restart_no_data')
        self.assertEqual(run['extra']['stall_duration'], 0.0)
        self.assertIn('produced no data', run['detail'])

    def test_the_already_banked_reconnect_wait_is_not_charged_twice(self):
        """The reconnect wait is banked as the restart gap on the RESTART_FAILED row. The
        old code then spent a real second window and banked that too, which was honest
        because the time was genuinely lost; now that it is not spent, adding it again
        would invent downtime that never happened."""
        run = self._dead_restart_run()

        # The opener is noticed within a poll, so essentially all of this is the reconnect
        # wait (~STALL_TIMEOUT). A second window on top means the dead restart was charged
        # for a detection window it did not spend.
        self.assertLess(
            run['downtime'], self.STALL_TIMEOUT * 2,
            f"downtime {run['downtime']:.1f}s covers more than the one reconnect wait that "
            f'was actually spent - the same window is being counted twice')

    def test_the_capture_stalls_counter_still_moves(self):
        """A restart that got nothing is still a capture interruption - it must not fall
        out of total_stall_count and quietly make the recording look healthier than it was.
        Characterization: this passed before the fix too (the re-derived stall counted),
        and it is here to pin that the faster path did not drop the accounting."""
        self.assertGreaterEqual(self._dead_restart_run()['stall_count'], 2)


class RestartThatRecoveredIsNotKilledTests(_LiveRestartHarness):
    """Characterization, not a regression guard - it passes with the fix reverted, because
    the old code had no verdict to carry forward at all. It pins the guard that makes
    carrying one safe: wait_for_file_data polls every 2s, so a reconnect whose first byte
    lands just after it gives up is a LIVE segment and must not be killed on a stale
    verdict."""

    def test_a_segment_with_bytes_is_left_alone_even_after_a_failed_restart_verdict(self):
        # An opener that has already exited stalls within one poll, so the restart happens
        # immediately and the 30s stall timeout applies only to the segment under test.
        self._exited_capture()
        # produces_data=True writes to the new segment, while wait_for_file_data is forced
        # to report failure - exactly the race, with the timing taken out of it.
        with mock.patch.object(wdmod, 'wait_for_file_data', return_value=False), \
             mock.patch.object(cfgmod, 'load_config', return_value=self._cfg(30, 0)), \
             mock.patch.object(recorder, '_launch_segment',
                               self._stub_launch_next(produces_data=True)):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                db.session.expire_all()
                if RecordingEvent.query.filter_by(
                        recording_id=self.rid, event_type=RESTART_FAILED).first():
                    break
                time.sleep(0.1)
            # Three polls past the verdict: long enough for a stale verdict to have been
            # acted on, far short of the 30s stall timeout.
            time.sleep(3)
            self.state.stop_event.set()
            self.wd.join(timeout=15)

        db.session.expire_all()
        seg = RecordingSegment.query.filter_by(
            recording_id=self.rid, segment_number=1).first()
        self.assertIsNotNone(seg, 'the restart segment was never created')
        self.assertIsNone(
            seg.ended_at,
            'a segment that was writing bytes was killed on a stale no-data verdict')


class RestartNoDataRenderTests(unittest.TestCase):
    """The recording detail page surface. An exit_reason with no entry in the template's
    exit_pill map renders as a bare grey chip with no explanation, and the timeline's
    STALL_DETECTED label would otherwise call this a stall kill."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.rec = seed.make_recording(status='COMPLETED')
        start = self.rec.start_time
        for num, reason in ((1, 'RESTART_NO_DATA'), (2, 'STOP_TIME_REACHED')):
            db.session.add(RecordingSegment(
                recording_id=self.rec.id, segment_number=num,
                file_path=f'/dvr/x_seg_{num:03d}.ts',
                started_at=start + timedelta(minutes=num),
                ended_at=start + timedelta(minutes=num + 1),
                exit_reason=reason, bytes_recorded=0, ffmpeg_exit_code=-15))
        db.session.add(RecordingEvent(
            recording_id=self.rec.id, event_type=STALL_DETECTED, segment_number=1,
            timestamp=start + timedelta(minutes=2),
            detail='Restarted segment produced no data within 10s - closed on that verdict '
                   'rather than waiting to re-derive it',
            extra_data=json.dumps({'bytes': 0, 'stall_duration': 0.0,
                                   'stall_reason': 'restart_no_data'})))
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _page(self):
        resp = self.t.client.get(f'/recordings/{self.rec.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_the_pill_is_explained_rather_than_rendered_bare(self):
        html = self._page()
        self.assertIn('RESTART_NO_DATA', html)
        self.assertIn('never produced a byte', html)

    def test_the_timeline_does_not_call_it_a_stall_kill(self):
        html = self._page()
        self.assertIn('Restart got no data (seg 1)', html)
        self.assertNotIn('Stall (seg 1 killed)', html)

    def test_no_duplicate_segment_boundary_milestone(self):
        """The STALL_DETECTED event already marks this boundary; the gate at the segment
        loop suppresses a second marker, and it only knew STALL_KILLED and PROCESS_EXITED."""
        self.assertNotIn('Segment boundary', self._page())


if __name__ == '__main__':
    unittest.main()
