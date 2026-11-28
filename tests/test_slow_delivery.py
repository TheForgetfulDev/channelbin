"""A feed that slows to a trickle is called stalled, on a recording and on an IPTV output play
alike, by one check: proc_utils.SlowDeliveryCheck.

Guards dev/docs/BUGS.md 2026-11-28 @ 12:21:46 PM. A provider that keeps sending a little never
tripped either path's "nothing arrived for stall_timeout" check: a play reads its pipe, which got
a few KB every second, and a recording's file grew one 256 KiB block at a time, which at anything
over ~9 kB/s lands inside the stall timeout. Both now measure seconds of program per second of
wall clock over a rolling window (dev/changelog/1799). The play's half is
tests/test_output_feed.py::SlowDeliveryReaperTests; this module holds the check itself and the
recording's half, through the real WatchdogThread against a live spool.

No network: the stand-in capture is a local python child writing ffmpeg-shaped progress lines
(tests/support/watchdog.py::FAKE_PROGRESS_CAPTURE), under make_test_app's temp dir.
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    SEGMENT_EXIT_SLOW_DELIVERY, STALL_DETECTED, Recording, RecordingEvent,
)
from app.proc_utils import (  # noqa: E402
    SlowDeliveryCheck, read_capture_content_position, slow_delivery_detail,
    slow_delivery_settings,
)
import app.recorder as recorder  # noqa: E402
from tests.support.watchdog import FAKE_PROGRESS_CAPTURE, WatchdogHarness  # noqa: E402


class SlowDeliveryCheckTests(unittest.TestCase):
    """The judge both readers hold, on an injected clock."""

    def _feed(self, check, samples):
        verdict = None
        for at, content in samples:
            verdict = check.update(content, now=at)
        return verdict

    def test_a_trickle_is_named_once_the_window_is_full(self):
        check = SlowDeliveryCheck(0.4, 60)
        self.assertIsNone(self._feed(check, [(t, t * 0.02) for t in range(0, 59, 2)]))
        ratio = check.update(61 * 0.02, now=61)
        self.assertAlmostEqual(ratio, 0.02, places=3)

    def test_real_time_is_left_alone(self):
        check = SlowDeliveryCheck(0.4, 60)
        self.assertIsNone(self._feed(check, [(t, float(t)) for t in range(0, 300, 2)]))

    def test_one_in_process_reconnect_is_left_alone(self):
        """ffmpeg waits read_timeout_seconds (20) with nothing, up to reconnect_delay_max (2)
        and a connect - 28 s at worst - and the replay may never land. 32/60 = 0.53x."""
        check = SlowDeliveryCheck(0.4, 60)
        samples = [(t, float(t)) for t in range(0, 100, 2)]
        samples += [(t, 98.0) for t in range(100, 128, 2)]          # 28 s with nothing new
        samples += [(t, 98.0 + (t - 128)) for t in range(128, 300, 2)]   # 1x, no replay
        verdicts = [check.update(c, now=at) for at, c in samples]
        self.assertEqual([v for v in verdicts if v is not None], [])

    def test_a_single_gap_trips_it_only_past_the_stall_timeout(self):
        """Under 0.4 across 60 s from one gap takes over 36 s of nothing - longer than the
        30 s stall timeout, so it is never the slow check that ends an honest reconnect."""
        for gap, tripped in ((34, False), (40, True)):
            check = SlowDeliveryCheck(0.4, 60)
            samples = [(t, float(t)) for t in range(0, 100, 2)]
            samples += [(t, 98.0) for t in range(100, 100 + gap, 2)]
            samples += [(t, 98.0 + (t - 100 - gap)) for t in range(100 + gap, 300, 2)]
            verdicts = [check.update(c, now=at) for at, c in samples]
            self.assertEqual(any(v is not None for v in verdicts), tripped, gap)

    def test_two_reconnects_in_one_window_are_a_trickle(self):
        check = SlowDeliveryCheck(0.4, 60)
        samples = [(t, float(t)) for t in range(0, 62, 2)]
        samples += [(t, 60.0) for t in range(62, 88, 2)]
        samples += [(t, 60.0 + (t - 88)) for t in range(88, 92, 2)]
        samples += [(t, 62.0) for t in range(92, 122, 2)]
        verdicts = [check.update(c, now=at) for at, c in samples]
        self.assertTrue(any(v is not None and v < 0.4 for v in verdicts), verdicts[-10:])

    def test_an_unreadable_sample_is_a_gap_not_a_stall(self):
        check = SlowDeliveryCheck(0.4, 60)
        self.assertIsNone(self._feed(check, [(t, None) for t in range(0, 200, 2)]))

    def test_zero_disables_it(self):
        check = SlowDeliveryCheck(0, 60)
        self.assertFalse(check.enabled)
        self.assertIsNone(self._feed(check, [(t, 0.0) for t in range(0, 200, 2)]))

    def test_reset_makes_the_window_refill(self):
        check = SlowDeliveryCheck(0.4, 60)
        self._feed(check, [(t, 0.0) for t in range(0, 50, 2)])
        check.reset()
        self.assertIsNone(check.update(0.0, now=70))

    def test_one_pair_of_settings_for_both_readers(self):
        self.assertEqual(slow_delivery_settings(cfgmod.load_config()), (0.4, 60.0))
        self.assertEqual(slow_delivery_settings({'watchdog': {'slow_delivery_ratio': 0}}),
                         (0.0, 60.0))
        self.assertEqual(slow_delivery_detail(0.0234, 60.0),
                         'the feed delivered 0.02 seconds of video per second for 60s straight')


class _SlowRig(WatchdogHarness):
    """WatchdogHarness with only the slow-delivery check on, and a stand-in capture whose
    content advances `rate` times as fast as the clock."""

    RATIO = 0.4

    def _cfg(self, stall_timeout, restart_delay=0):
        return cfgmod._deep_merge(super()._cfg(stall_timeout, restart_delay), {'watchdog': {
            'poll_interval_seconds': 0.2,
            'fast_delivery_ratio': 0,
            'placeholder_content_ratio': 0,
            'stall_move_count': 0,
            'slow_delivery_ratio': self.RATIO,
            'slow_delivery_window_seconds': 1.0,
        }})

    def _capture(self, rate):
        path, fh = recorder._open_segment_stderr_spool(self.t.app, self.rid, 0)
        self.state.stderr_path, self.state.stderr_fh = path, fh
        self.state.process = subprocess.Popen(
            [sys.executable, '-c', FAKE_PROGRESS_CAPTURE, str(rate)],
            stdout=subprocess.DEVNULL, stderr=(fh or subprocess.DEVNULL))

    def _left_alone(self, rate):
        samples = []

        def _counting(path):
            pos = read_capture_content_position(path)
            samples.append(pos)
            return pos
        self._capture(rate)
        with mock.patch('app.watchdog.read_capture_content_position', _counting):
            seg, _ = self._run_until_segment_closed(stall_timeout=600, timeout=4)
        return seg, [p for p in samples if p is not None]


class WatchdogSlowDeliveryTests(_SlowRig):
    """The recording's half: a capture whose content trickles is closed as a stall named for
    what it was, and restarted, while its file never stopped growing long enough to trip the
    growth check."""

    def test_a_trickling_capture_is_closed_as_a_slow_delivery_stall(self):
        self._capture(0.05)
        # The growth check is held off (600 s): this is about the other signal.
        seg, _ = self._run_until_segment_closed(stall_timeout=600, timeout=30)
        self.assertIsNotNone(seg.ended_at, 'the trickle was never called')
        self.assertEqual(seg.exit_reason, SEGMENT_EXIT_SLOW_DELIVERY)
        self.assertEqual(self.launched, [1], 'it restarts, the way any stall does')
        evt = self._stall_event()
        self.assertIsNotNone(evt)
        self.assertIn('seconds of video per second', evt.detail)
        self.assertIn('"stall_reason": "slow_delivery"', evt.extra_data)
        self.assertIn('"window_seconds": 1.0', evt.extra_data)
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.total_stall_count, 1)

    def test_a_real_time_capture_is_left_alone(self):
        seg, seen = self._left_alone(1.0)
        self.assertIsNone(seg.ended_at)
        self.assertGreaterEqual(len(seen), 5, 'not a window of positions read - no verdict reached')
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=STALL_DETECTED).count(), 0)


class WatchdogSlowDeliveryPrecedenceTests(_SlowRig):
    """A trickle verdict on the same tick as a plainer fact yields to it: the segment is told
    by how it really ended, so a row never calls a dead or silent feed a trickle."""

    def _verdict_from_call(self, n):
        """SlowDeliveryCheck.update() answering a trickle from its `n`-th call on."""
        calls = []

        def _update(check, content_seconds, now=None):
            calls.append(content_seconds)
            return 0.05 if len(calls) >= n else None
        return mock.patch('app.watchdog.SlowDeliveryCheck.update', _update)

    def test_a_capture_that_exited_is_told_as_an_exit(self):
        self._exited_capture()
        with self._verdict_from_call(1):
            seg, _ = self._run_until_segment_closed(stall_timeout=600, timeout=30)
        self.assertEqual(seg.exit_reason, 'PROCESS_EXITED')
        self.assertNotIn('"ratio"', self._stall_event().extra_data,
                         'an exit is not reported with a trickle\'s measurement')

    def test_a_capture_that_also_went_silent_is_told_as_a_stall(self):
        from tests.support.watchdog import sleeper
        self.state.process = sleeper()
        growth_calls = []

        def _growth(monitor, size):
            growth_calls.append(size)
            return 999.0 if len(growth_calls) >= 3 else 0.0
        # The growth clock passes the stall timeout on the same poll the trickle verdict lands.
        with self._verdict_from_call(3), \
                mock.patch('app.watchdog.GrowthMonitor.update', _growth):
            seg, _ = self._run_until_segment_closed(stall_timeout=600, timeout=30)
        self.assertEqual(seg.exit_reason, 'STALL_KILLED')


class RecordingPageSaysTrickleTests(unittest.TestCase):
    """The segment row and the timeline name a trickle as one, never as a plain stall."""

    def test_the_page_names_it(self):
        from datetime import datetime, timedelta
        from app.database import RecordingSegment, add_recording_event
        from tests.support import seed
        from tests.support.app import make_test_app
        t = make_test_app()
        self.addCleanup(t.cleanup)
        now = datetime.utcnow()
        rec = seed.make_recording(status='COMPLETED', name='trickle', started_at=now - timedelta(minutes=5),
                                  start_time=now - timedelta(minutes=5), stop_time=now)
        db.session.add(RecordingSegment(recording_id=rec.id, segment_number=0, file_path='/nonexistent/a.ts',
                                        started_at=now - timedelta(minutes=5),
                                        ended_at=now - timedelta(minutes=3),
                                        exit_reason=SEGMENT_EXIT_SLOW_DELIVERY, bytes_recorded=1000))
        add_recording_event(rec.id, STALL_DETECTED, detail='Slowed to a trickle', segment_number=0,
                            extra={'stall_reason': 'slow_delivery', 'ratio': 0.05, 'window_seconds': 60.0})
        db.session.commit()
        html = t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        self.assertIn('SLOW_DELIVERY_KILLED', html)
        self.assertIn('The feed slowed to a trickle', html)
        self.assertIn('Slowed to a trickle (seg 1 restarted)', html)


class WatchdogSlowDeliveryOffTests(_SlowRig):
    RATIO = 0

    def test_a_disabled_check_keeps_a_trickling_capture(self):
        seg, seen = self._left_alone(0.05)
        self.assertIsNone(seg.ended_at, 'a disabled check closed the capture')
        self.assertEqual(seen, [], 'a disabled check (and fast delivery off) reads no positions')


if __name__ == '__main__':
    unittest.main()
