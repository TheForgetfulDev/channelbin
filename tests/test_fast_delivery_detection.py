"""Tier 2 - a feed that keeps delivering content faster than real time is caught DURING
the capture and the recording stops riding it.

Guards dev/docs/BUGS.md 2026-09-14 "A frozen provider feed is recorded for hours and
reported as a successful capture". Design, evidence and the measured ratios:
dev/changelog/964.

The defect: recording 19's last segment ran 2h18m, wrote 31.9 GB, kept ffmpeg's frame
counter moving and never stalled, so every liveness check the app had passed it - while
all 8h38m of the content it produced was one repeated stretch of a race, re-served at
3.76x. The watchdog measured whether the FILE was growing; nothing measured whether the
CONTENT was advancing.

No network anywhere: the stand-in capture is a local python child writing ffmpeg-shaped
progress lines into a real stderr spool, and every file is written under make_test_app's
temp dir (tests/support/netguard.py would refuse otherwise).
"""
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.recorder as recorder  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    FAST_DELIVERY_DETECTED, CHANNEL_FAST_DELIVERY_HEALTH_OBSERVATION,
    STALL_DETECTED, Channel, ChannelEvent, Recording, RecordingEvent, RecordingSegment,
)
from app.proc_utils import (  # noqa: E402
    DeliveryRateMonitor, delivery_ratio, read_capture_content_position,
)
from app.recorder import RecordingState  # noqa: E402
from app.watchdog import WatchdogThread  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402


class ContentPositionTests(unittest.TestCase):
    """Reading ffmpeg's content position out of a spool it is still writing."""

    def setUp(self):
        self.t = make_test_app()
        self.path = os.path.join(self.t._tmpdir, 'spool.log')

    def tearDown(self):
        self.t.cleanup()

    def _write(self, text):
        with open(self.path, 'w') as fh:
            fh.write(text)

    def test_reads_the_newest_position(self):
        self._write('frame=1 time=00:00:10.00 speed=1x\rframe=2 time=00:01:05.50 speed=1x\r')
        self.assertAlmostEqual(read_capture_content_position(self.path), 65.5, places=2)

    def test_hours_are_counted(self):
        self._write('time=02:18:41.00 speed=3.76x\r')
        self.assertAlmostEqual(read_capture_content_position(self.path), 8321.0, places=2)

    def test_a_partial_trailing_line_falls_back_to_the_last_complete_one(self):
        """A live spool's last line is often a partial write. A truncated position must
        fail to match rather than be read as a much smaller one - taking `time=00:00:4`
        for 4 seconds would invent a content position that went backwards."""
        self._write('time=00:05:00.00 speed=1x\rframe=9 time=00:0')
        self.assertAlmostEqual(read_capture_content_position(self.path), 300.0, places=2)

    def test_a_banner_only_spool_has_no_opinion(self):
        """Every spool opens with ffmpeg's banner, so an early read legitimately finds no
        position at all. That is None, never 0 - a zero would read as "no content yet"
        and anchor the window at a value that never existed."""
        self._write('ffmpeg version n7.1.5 Copyright (c) 2000-2026\n  configuration: --prefix\n')
        self.assertIsNone(read_capture_content_position(self.path))

    def test_a_missing_spool_has_no_opinion(self):
        self.assertIsNone(read_capture_content_position(
            os.path.join(self.t._tmpdir, 'nope.log')))
        self.assertIsNone(read_capture_content_position(None))

    def test_reads_the_end_of_a_long_spool(self):
        """A multi-hour capture's spool is unbounded on disk, so this seeks rather than
        reading it whole - and must still see the newest line."""
        self._write(('time=00:00:01.00 speed=1x\r' * 5000) + 'time=01:00:00.00 speed=1x\r')
        self.assertAlmostEqual(read_capture_content_position(self.path), 3600.0, places=2)


class DeliveryRatioTests(unittest.TestCase):
    """The one measurement, shared by the live detector and the post-capture disclosure."""

    def test_ratio(self):
        self.assertAlmostEqual(delivery_ratio(8321.0, 8275.0), 1.0056, places=3)
        self.assertAlmostEqual(delivery_ratio(31122.0, 8275.0), 3.7610, places=3)

    def test_unmeasurable_pairs_are_none_not_zero(self):
        """"Not measurable" is a different answer from "not fast" - a caller that treated
        them alike would fire on a zero-length window."""
        self.assertIsNone(delivery_ratio(100.0, 0))
        self.assertIsNone(delivery_ratio(100.0, -1))
        self.assertIsNone(delivery_ratio(None, 10.0))
        self.assertIsNone(delivery_ratio(100.0, None))


class DeliveryRateMonitorTests(unittest.TestCase):
    """Window mechanics, on an injected clock so no test waits on one."""

    def test_no_verdict_until_the_window_is_full(self):
        m = DeliveryRateMonitor(120)
        self.assertIsNone(m.update(0.0, now=1000.0))
        self.assertIsNone(m.update(400.0, now=1060.0))
        self.assertIsNone(m.update(790.0, now=1119.0))

    def test_a_full_window_yields_the_rolling_ratio(self):
        m = DeliveryRateMonitor(120)
        m.update(0.0, now=1000.0)
        m.update(200.0, now=1060.0)
        self.assertAlmostEqual(m.update(451.2, now=1120.0), 3.76, places=2)

    def test_the_measured_back_buffer_stays_under_the_default_trigger(self):
        """The worst per-connect back-buffer measured is 29s of content arriving at
        connect (dev/changelog/942). Over the 120s window it reads 1.24x, which is what
        makes a 1.5x trigger safe - and over 60s it would read 1.48x, which is why the
        window is not shorter."""
        m = DeliveryRateMonitor(120)
        m.update(0.0, now=0.0)
        self.assertAlmostEqual(m.update(149.0, now=120.0), 1.2417, places=3)

        short = DeliveryRateMonitor(60)
        short.update(0.0, now=0.0)
        self.assertAlmostEqual(short.update(89.0, now=60.0), 1.4833, places=3)

    def test_the_window_rolls_rather_than_averaging_from_the_start(self):
        """A feed that runs clean for an hour and then freezes must be caught on what it
        is doing now. ffmpeg's own cumulative `speed=` would still read ~1.0 here."""
        m = DeliveryRateMonitor(120)
        now = 0.0
        content = 0.0
        while now < 3600:                      # an hour at 1.0x
            now += 30.0
            content += 30.0
            m.update(content, now=now)
        for _ in range(4):                     # then 4x
            now += 30.0
            content += 120.0
            observed = m.update(content, now=now)
        self.assertAlmostEqual(observed, 4.0, places=2)

    def test_the_anchor_is_the_newest_sample_at_or_before_the_window_start(self):
        """Two ways to get this wrong, and the window has to be shorter than the sample
        history to tell them apart. Keeping every sample anchors on the FIRST one ever and
        measures a window that keeps growing (here: 300s over 100s, 3.0x). Pruning to
        "inside the window" leaves the oldest sample YOUNGER than the window and measures a
        shorter one than asked for. The rule is the newest sample at or before the window
        start - here t=50 - so the answer is 250s of content across 50s."""
        m = DeliveryRateMonitor(50)
        for i in range(0, 10):
            m.update(float(i) * 10.0, now=float(i) * 10.0)   # 1.0x for 90s
        observed = m.update(300.0, now=100.0)                # +210s of content in 10s
        self.assertAlmostEqual(observed, 5.0, places=2)

    def test_an_unreadable_sample_is_a_gap_not_a_datapoint(self):
        """Interpolating across a tick whose spool could not be read would attribute the
        missed interval's content to whichever sample landed next."""
        m = DeliveryRateMonitor(120)
        m.update(0.0, now=0.0)
        self.assertIsNone(m.update(None, now=60.0))
        self.assertAlmostEqual(m.update(240.0, now=120.0), 2.0, places=2)

    def test_reset_makes_the_window_refill_from_scratch(self):
        """What gives a restarted segment its warm-up: the reconnect's back-buffer lands
        in a window that is not yet full, so it can never be measured on its own."""
        m = DeliveryRateMonitor(120)
        m.update(0.0, now=0.0)
        self.assertAlmostEqual(m.update(600.0, now=120.0), 5.0, places=2)
        m.reset()
        self.assertIsNone(m.update(600.0, now=130.0))
        self.assertIsNone(m.update(700.0, now=200.0))


class FastDeliveryHealthObservationTests(unittest.TestCase):
    """The member takes the score hit, and the hit replays."""

    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow()
        self.account = seed.make_account()
        self.ch = seed.make_channel(self.account, stream_id='1', name='Member A')
        db.session.commit()
        self.rec = seed.make_recording(status='IN_PROGRESS', name='fd',
                                       channel_id=self.ch.id, started_at=self.now,
                                       start_time=self.now,
                                       stop_time=self.now + timedelta(hours=1))
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_observation_lands_on_the_channel_and_is_replayable(self):
        from app.health_score import apply_fast_delivery_health_observation
        from app.health_recompute import SOURCE_FAST_DELIVERY, observation_ledger

        ch = db.session.get(Channel, self.ch.id)
        ch.health_score = 100.0
        ch.health_score_sample_count = 5
        ch.health_score_updated_at = self.now
        db.session.commit()

        apply_fast_delivery_health_observation(self.t.app, self.ch.id, self.rec.id, 3,
                                               3.76, 120.0)

        db.session.expire_all()
        ch = db.session.get(Channel, self.ch.id)
        self.assertLess(ch.health_score, 100.0)

        evt = ChannelEvent.query.filter_by(
            channel_id=self.ch.id,
            event_type=CHANNEL_FAST_DELIVERY_HEALTH_OBSERVATION).one()
        self.assertIn('3.76x', evt.detail)

        ledger = observation_ledger(self.ch.id, cfgmod.load_config())
        self.assertIn(SOURCE_FAST_DELIVERY, [o.kind for o in ledger])

    def test_a_recording_with_no_channel_is_a_no_op(self):
        from app.health_score import apply_fast_delivery_health_observation
        apply_fast_delivery_health_observation(self.t.app, None, self.rec.id, 1, 3.0, 120.0)
        self.assertEqual(ChannelEvent.query.filter_by(
            event_type=CHANNEL_FAST_DELIVERY_HEALTH_OBSERVATION).count(), 0)


# A stand-in capture: writes ffmpeg-shaped progress lines into the spool it inherits as
# stderr, advancing the content position `rate` times faster than the wall clock, and stays
# alive so the watchdog sees a running process rather than one that exited.
_FAKE_CAPTURE = (
    'import sys, time\n'
    'rate = float(sys.argv[1])\n'
    'step = 0.1\n'
    'content = 0.0\n'
    'while True:\n'
    '    content += step * rate\n'
    '    h, rem = divmod(content, 3600)\n'
    '    m, s = divmod(rem, 60)\n'
    '    sys.stderr.write("frame=1 time=%02d:%02d:%05.2f speed=%.2fx    \\r" % (h, m, s, rate))\n'
    '    sys.stderr.flush()\n'
    '    time.sleep(step)\n'
)


class WatchdogFastDeliveryTests(unittest.TestCase):
    """The whole path, driven by the real WatchdogThread against a live spool: a capture
    delivering faster than real time is stopped, said out loud, and charged to nothing that
    means something else."""

    def setUp(self):
        self.t = make_test_app()
        self.wd = None
        self.launched = []
        self.failovers = []
        self.failover_result = False
        self.state = RecordingState(current_segment_num=0)
        now = datetime.utcnow()
        self.account = seed.make_account()
        self.ch = seed.make_channel(self.account, stream_id='1', name='Member A')
        db.session.commit()
        rec = seed.make_recording(status='IN_PROGRESS', name='wdfd', channel_id=self.ch.id,
                                  started_at=now, start_time=now,
                                  stop_time=now + timedelta(hours=1))
        self.rid = rec.id
        self.seg_path = os.path.join(self.t._tmpdir, f'rec_{self.rid}_seg_000.ts')
        with open(self.seg_path, 'wb') as fh:
            fh.write(b'\x47' * 4096)
        db.session.add(RecordingSegment(recording_id=self.rid, segment_number=0,
                                        channel_id=self.ch.id, file_path=self.seg_path,
                                        started_at=now))
        db.session.commit()
        with recorder._lock:
            recorder._active[self.rid] = self.state

    def tearDown(self):
        self.state.stop_event.set()
        if self.wd is not None:
            self.wd.join(timeout=15)
        proc = self.state.process
        if isinstance(proc, subprocess.Popen) and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        with recorder._lock:
            recorder._active.pop(self.rid, None)
        self.t.cleanup()

    def _start_fake_capture(self, rate):
        path, fh = recorder._open_segment_stderr_spool(self.t.app, self.rid, 0)
        self.state.stderr_path, self.state.stderr_fh = path, fh
        proc = subprocess.Popen([sys.executable, '-c', _FAKE_CAPTURE, str(rate)],
                                stdout=subprocess.DEVNULL,
                                stderr=(fh or subprocess.DEVNULL))
        self.state.process = proc
        return proc

    def _cfg(self, ratio=1.5, window=1.0, strikes=3, early_fail=99):
        return cfgmod._deep_merge(cfgmod.load_config(), {'watchdog': {
            'poll_interval_seconds': 0.2,
            # High enough that the non-growing stand-in file cannot trip the growth
            # detector inside the test's window - this test is about the other signal.
            'stall_timeout_seconds': 600,
            'restart_delay_seconds': 0,
            'max_consecutive_failures': 99,
            'early_fail_abort_count': early_fail,
            'stall_move_count': 0,
            'placeholder_content_ratio': 0,
            'fast_delivery_ratio': ratio,
            'fast_delivery_window_seconds': window,
            'fast_delivery_strike_count': strikes,
        }})

    def _run(self, rate=5.0, ratio=1.5, window=1.0, strikes=3, early_fail=99, timeout=30):
        def _stub_launch(app, recording_id, seg_num):
            self.launched.append(seg_num)

        def _stub_failover(app, recording_id, reason, demote=False, score_departure=True):
            self.failovers.append((reason, demote, score_departure))
            return self.failover_result

        self._start_fake_capture(rate)
        # The replacement segment is never really launched here, so the reconnect wait is
        # answered rather than left to time out against a row that will never exist. Without
        # it the stub turns every retry into a failed restart, which is the harness's
        # behavior and not the watchdog's.
        with mock.patch.object(cfgmod, 'load_config',
                               return_value=self._cfg(ratio, window, strikes, early_fail)), \
             mock.patch('app.watchdog.wait_for_file_data', return_value=True), \
             mock.patch.object(recorder, '_launch_segment', _stub_launch), \
             mock.patch.object(recorder, 'failover_group_member', _stub_failover):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                db.session.expire_all()
                seg = RecordingSegment.query.filter_by(
                    recording_id=self.rid, segment_number=0).first()
                rec = db.session.get(Recording, self.rid)
                # Closing the segment is only the first half. Waiting for the branch that
                # follows it to reach a decision as well is what keeps stop_event from
                # landing mid-restart and being read as "the recording was stopped".
                resolved = bool(self.launched or self.failovers) or rec.status == 'FAILED'
                if seg.ended_at is not None and resolved:
                    break
                time.sleep(0.1)
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        db.session.expire_all()
        return RecordingSegment.query.filter_by(
            recording_id=self.rid, segment_number=0).first()

    # The default detection - a 5x feed, three strikes left, nowhere asked to fail over -
    # performed ONCE for the whole class and read by five tests. Same one-expensive-fixture
    # shape as tests/test_downtime_accounting.py::_growth_stall_run: the real WatchdogThread
    # has to watch a real window fill, so each run costs ~2s and five identical runs proved
    # nothing the five separate assertions do not already prove (dev/changelog/979). The
    # assertions stay separate so a partial fix cannot pass one and be excused by another.
    # Do not inline it back into the tests; a test that needs a different rate, ratio or
    # strike count still calls _run() itself.
    _default_run = None

    def _default_detection(self):
        if WatchdogFastDeliveryTests._default_run is None:
            seg = self._run(rate=5.0)
            evt = RecordingEvent.query.filter_by(
                recording_id=self.rid, event_type=FAST_DELIVERY_DETECTED).first()
            rec = db.session.get(Recording, self.rid)
            WatchdogFastDeliveryTests._default_run = {
                'ended': seg.ended_at is not None,
                'exit_reason': seg.exit_reason,
                'event_detail': evt.detail if evt is not None else None,
                'event_segment': evt.segment_number if evt is not None else None,
                'observations': ChannelEvent.query.filter_by(
                    channel_id=self.ch.id,
                    event_type=CHANNEL_FAST_DELIVERY_HEALTH_OBSERVATION).count(),
                'failovers': list(self.failovers),
                'launched': list(self.launched),
                'status': rec.status,
            }
        return WatchdogFastDeliveryTests._default_run

    def test_a_fast_feed_has_its_capture_stopped(self):
        run = self._default_detection()
        self.assertTrue(run['ended'])
        self.assertEqual(run['exit_reason'], 'FAST_DELIVERY_KILLED')

    def test_the_detection_is_announced_on_the_recording(self):
        """Failure paths must be observable - stopping a capture that was still receiving
        data is the loudest thing this watchdog does."""
        run = self._default_detection()
        self.assertIsNotNone(run['event_detail'])
        self.assertIn('real time', run['event_detail'])
        self.assertEqual(run['event_segment'], 0)

    def test_the_event_claims_only_what_the_ratio_measured(self):
        """The ratio proves the delivery rate. It does not prove the picture is frozen,
        and the app never looks at the picture - an event asserting a diagnosis this app
        cannot make is the unexplainable number Product Principle 1 forbids, pointed the
        other way (dev/changelog/964, and the correction it carries)."""
        lowered = self._default_detection()['event_detail'].lower()
        for claim in ('frozen', 'buffer', 'repeat', 'same few seconds', 'looping'):
            self.assertNotIn(claim, lowered, f'event should not claim {claim!r}')

    def test_it_is_not_counted_as_a_stall(self):
        """One flag, one meaning. Charging this to the stall counters would file the most
        expensive failure this app has under the name of a different one, and would arm
        the stall-rate demotion on evidence that is not stalls.

        Read on the give-up path deliberately: a successful restart assigns
        consecutive_failures = 0 on its way through, which would make this assertion pass
        whatever the detection did to it."""
        self.failover_result = False
        seg = self._run(rate=5.0, strikes=1)
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.total_stall_count, 0)
        self.assertEqual(rec.consecutive_failures, 0)
        self.assertEqual(seg.stall_count or 0, 0)
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=STALL_DETECTED).count(), 0)

    def test_the_member_takes_the_score_hit(self):
        self.assertEqual(self._default_detection()['observations'], 1)

    def test_a_real_time_feed_is_left_alone(self):
        """The detector has to sit still through an ordinary capture. 1.0x for the whole
        run is what all 26 measured long segments look like."""
        seg = self._run(rate=1.0, timeout=6)
        self.assertIsNone(seg.ended_at)
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=FAST_DELIVERY_DETECTED).count(), 0)

    def test_a_disabled_detector_keeps_the_capture(self):
        seg = self._run(rate=5.0, ratio=0, timeout=6)
        self.assertIsNone(seg.ended_at)
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=FAST_DELIVERY_DETECTED).count(), 0)

    def test_strikes_left_means_the_same_member_is_retried(self):
        """Nothing died here, and a provider stuck re-serving its buffer has a real chance
        of coming back on a fresh connection - deliberate, see dev/changelog/964."""
        run = self._default_detection()
        self.assertEqual(run['failovers'], [])
        self.assertEqual(run['launched'], [1])
        self.assertEqual(run['status'], 'IN_PROGRESS')

    def test_the_last_strike_moves_off_the_member(self):
        self.failover_result = True
        self._run(rate=5.0, strikes=1)
        self.assertEqual(len(self.failovers), 1)
        reason, demote, score_departure = self.failovers[0]
        self.assertIn('fast-delivery', reason)
        self.assertTrue(demote)
        # The detection already wrote the member's observation, so the failover must not
        # add a second one for the same departure.
        self.assertFalse(score_departure)

    def test_nowhere_to_go_stops_the_recording_and_names_why(self):
        """The one place this app stops a capture that is still receiving data. Its own
        failure_reason: nothing failed in the sense that word carries elsewhere - the
        connection held, the bytes flowed, every restart succeeded."""
        self.failover_result = False
        self._run(rate=5.0, strikes=1)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'FAILED')
        self.assertEqual(rec.failure_reason, 'FAST_DELIVERY_DETECTED')

    def test_a_dead_stream_abort_is_not_what_this_produces(self):
        """A fast-delivery kill is never an early failure. "The feed is dead" is the one
        thing this signature proves false, and the streak it would arm ends in a
        dead-stream abort naming the wrong cause - reached first, since that trip-wire is
        checked ahead of the strike ladder.

        A real fast-delivery segment is long and fat, so no default reaches the streak.
        This one is short and small by construction, which is what makes it able to prove
        the guard rather than the numbers - and it is run with strikes REMAINING, because
        that is the only path that reaches the dead-stream check at all. Out of strikes,
        the ladder gives up and returns first, and an unguarded streak would sit there
        unnoticed until the day someone raised the strike count."""
        self.failover_result = False
        self._run(rate=5.0, strikes=3, early_fail=1)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        # Not diverted: the ordinary restart ran, on the same member, with the recording
        # still going.
        self.assertEqual(self.launched, [1])
        self.assertEqual(rec.status, 'IN_PROGRESS')
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type='RECORDING_FAILED_DEAD_STREAM').count(), 0)


if __name__ == '__main__':
    unittest.main()
