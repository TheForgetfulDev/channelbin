"""Guards dev/changelog/672 "mp4 conversion collision avoidance"
(recording.post_process.collision_policy).

Conversion is local CPU/disk work with no coordination against the recorder before this -
DESIGN-concurrency.md's precedence doctrine ("recordings always win") covers the
tester/sync/recorder triangle but predates conversion as an actor. Two policies close the
gap: 'cancel' (the conversion yields to a colliding recording - self-preempts and resumes
once clear, so the recording is never delayed) and 'wait' (the opposite - a recording defers
its own start to a running conversion, failing loudly if its own window would pass first).

No real ffmpeg and no network throughout: subprocess.Popen is faked where a conversion
"runs", and every recorder/scheduler side effect that would otherwise touch a live process or
job store is mocked.
  python3 -m unittest tests.test_conversion_collision
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
import app.postprocessor as ppmod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, CONVERSION_YIELDED, RECORDING_START_DEFERRED,
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_FAILED,
)
from app.postprocessor import (  # noqa: E402
    _collision_window_seconds, _conversion_collision_conflict, _wait_for_conversion_clear,
    ConversionResult, do_postprocess, run_conversion_supervised,
    _active_conversions, _active_lock,
)


# ── Pure helper ─────────────────────────────────────────────────────────────
class CollisionWindowSecondsTests(unittest.TestCase):
    def test_default_multiplier_equals_duration(self):
        self.assertEqual(600.0, _collision_window_seconds(600, 1.0))

    def test_multiplier_2_halves_the_window(self):
        self.assertEqual(300.0, _collision_window_seconds(600, 2.0))

    def test_multiplier_below_floor_is_clamped_to_0_1(self):
        self.assertEqual(6000.0, _collision_window_seconds(600, 0.05))
        self.assertEqual(6000.0, _collision_window_seconds(600, 0))

    def test_unknown_duration_is_zero(self):
        self.assertEqual(0.0, _collision_window_seconds(None, 1.0))
        self.assertEqual(0.0, _collision_window_seconds(0, 1.0))


# ── The conflict query ────────────────────────────────────────────────────────
class ConversionCollisionConflictTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_in_progress_recording_is_a_conflict_regardless_of_window(self):
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='live')
        db.session.commit()
        conflict = _conversion_collision_conflict(0)
        self.assertIsNotNone(conflict)
        self.assertEqual('live', conflict.name)

    def test_scheduled_recording_outside_window_is_not_a_conflict(self):
        far = datetime.utcnow() + timedelta(hours=2)
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='later',
                            start_time=far, stop_time=far + timedelta(hours=1))
        db.session.commit()
        self.assertIsNone(_conversion_collision_conflict(60))

    def test_scheduled_recording_inside_window_is_a_conflict(self):
        soon = datetime.utcnow() + timedelta(seconds=30)
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='soon',
                            start_time=soon, stop_time=soon + timedelta(hours=1))
        db.session.commit()
        conflict = _conversion_collision_conflict(60)
        self.assertIsNotNone(conflict)
        self.assertEqual('soon', conflict.name)

    def test_terminal_recording_is_never_a_conflict(self):
        seed.make_recording(status='COMPLETED', name='done')
        db.session.commit()
        self.assertIsNone(_conversion_collision_conflict(999999))


# ── The blocking wait ──────────────────────────────────────────────────────────
class WaitForConversionClearTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_returns_immediately_when_clear(self):
        """"Immediately" means it never polled, which is what the code actually promises.

        This used to assert a wall clock under 1.0s, which on a loaded box running three
        shards is not a claim about the code: one contended query is enough to spend it,
        and the test went red at 1.28s with nothing wrong (dev/changelog/724). Counting
        the sleeps is exact and costs nothing.
        """
        rec = seed.make_recording(status='CONVERTING', name='conv')
        db.session.commit()
        with mock.patch.object(ppmod.time, 'sleep') as slept:
            _wait_for_conversion_clear(rec.id, 0, poll_seconds=0.01)
        slept.assert_not_called()

    def test_blocks_until_the_conflict_clears(self):
        conv = seed.make_recording(status='CONVERTING', name='conv')
        blocker = seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='blocker')
        db.session.commit()
        cleared = threading.Event()

        def _clear_soon():
            time.sleep(0.15)
            with self.t.app.app_context():
                r = db.session.get(Recording, blocker.id)
                r.status = 'COMPLETED'
                db.session.commit()
            cleared.set()

        threading.Thread(target=_clear_soon, daemon=True).start()
        started = time.monotonic()
        _wait_for_conversion_clear(conv.id, 0, poll_seconds=0.02)
        self.assertTrue(cleared.is_set())
        self.assertGreaterEqual(time.monotonic() - started, 0.1,
                                'returned before the conflict actually cleared')

    def test_stops_waiting_once_this_recording_is_cancelled(self):
        conv = seed.make_recording(status='CONVERTING', name='conv')
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='never clears')
        db.session.commit()

        def _cancel_soon():
            time.sleep(0.1)
            with self.t.app.app_context():
                r = db.session.get(Recording, conv.id)
                r.status = 'ABORTED'
                db.session.commit()

        threading.Thread(target=_cancel_soon, daemon=True).start()
        started = time.monotonic()
        _wait_for_conversion_clear(conv.id, 0, poll_seconds=0.02)
        self.assertLess(time.monotonic() - started, 2.0,
                        'kept waiting on an unrelated recording after being cancelled')


# ── run_conversion_supervised: in-loop preemption (fake Popen, no real ffmpeg) ────────────
class _FakePopen:
    """Stands in for a conversion ffmpeg. alive_ticks=None -> never exits on its own;
    otherwise poll() returns None for that many calls, then the returncode."""
    def __init__(self, *args, alive_ticks=None, returncode=0, **kwargs):
        self._polls = 0
        self._alive_ticks = alive_ticks
        self._rc = returncode
        self.returncode = None
        self.terminated = False

    def poll(self):
        self._polls += 1
        if self._alive_ticks is not None and self._polls > self._alive_ticks:
            self.returncode = self._rc
            return self._rc
        return None

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = self._rc

    def kill(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = self._rc

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode


class RunnerCollisionTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status='CONVERTING', name='conv')
        db.session.commit()
        self.rid = self.rec.id
        self.out = os.path.join(self.t._tmpdir, f'out_{self.rid}.mkv')
        self.cmd = ['/usr/bin/ffmpeg', '-i', 'src.ts', '-c', 'copy', '-y', self.out]

    def tearDown(self):
        with _active_lock:
            _active_conversions.clear()
        self.t.cleanup()

    def _run(self, fake, collision_policy='cancel', collision_window_seconds=999999,
             interval=0.05, pre_output_timeout=30):
        with mock.patch.object(ppmod.subprocess, 'Popen', return_value=fake), \
             mock.patch.object(ppmod, '_read_progress_tail',
                               side_effect=lambda p: (5_000_000, 1000, False)):
            return run_conversion_supervised(
                self.t.app, self.rid, self.cmd, self.out,
                expected_duration=100, pre_output_timeout=pre_output_timeout,
                interval=interval, stall_seconds=0,
                collision_policy=collision_policy,
                collision_window_seconds=collision_window_seconds)

    def test_yields_to_an_in_progress_recording(self):
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='blocker')
        db.session.commit()
        fake = _FakePopen(alive_ticks=None)  # never exits on its own
        result = self._run(fake, collision_policy='cancel')
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'preempted')
        self.assertTrue(fake.terminated, 'the ffmpeg was not killed on preemption')

    def test_does_not_yield_when_policy_is_off(self):
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='blocker')
        db.session.commit()
        fake = _FakePopen(alive_ticks=2, returncode=0)
        result = self._run(fake, collision_policy='off')
        self.assertTrue(result.success, 'policy off must never preempt')

    def test_does_not_yield_when_policy_is_wait(self):
        # 'wait' means the RECORDING defers, not the conversion - the conversion must run
        # through a colliding recording untouched under this policy.
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='blocker')
        db.session.commit()
        fake = _FakePopen(alive_ticks=2, returncode=0)
        result = self._run(fake, collision_policy='wait')
        self.assertTrue(result.success, "'wait' policy must not preempt the conversion")

    def test_does_not_yield_when_nothing_conflicts(self):
        fake = _FakePopen(alive_ticks=2, returncode=0)
        result = self._run(fake, collision_policy='cancel')
        self.assertTrue(result.success)


# ── do_postprocess: pre-start wait + preempted-restart accounting ─────────────
class PostprocessCollisionTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status='CONCATENATING', name='conv',
                                       recorded_duration_seconds=100.0)
        self.ts = os.path.join(self.t._tmpdir, f'rec_{self.rec.id}.ts')
        with open(self.ts, 'wb') as fh:
            fh.write(b'x' * 2048)
        self.rec.output_path = self.ts
        db.session.commit()
        self.rid = self.rec.id

    def tearDown(self):
        with _active_lock:
            _active_conversions.clear()
            ppmod._cancel_requested.clear()
        self.t.cleanup()

    def _config(self, **pp_overrides):
        pp = dict({'enabled': True, 'format': 'mkv', 'delete_source': False,
                   'reencode_mode': 'never', 'pre_output_timeout_seconds': 60,
                   'auto_restart': True, 'max_restart_attempts': 3,
                   'stall_seconds': 0, 'progress_interval_seconds': 5,
                   'collision_policy': 'cancel', 'collision_lookahead_multiplier': 1.0},
                  **pp_overrides)
        return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {
            'gather_health_data': False,
            'move_on_complete': {'enabled': False},
            'post_script': {'enabled': False},
            'post_process': pp,
        }})

    def _run(self, cfg, supervised_result):
        stub = mock.Mock(side_effect=supervised_result)
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', stub), \
             mock.patch.object(ppmod, '_wait_for_conversion_clear') as waited:
            do_postprocess(self.t.app, self.rid, self.ts)
        db.session.expire_all()
        return stub, waited

    def test_prestart_wait_when_a_recording_is_imminent(self):
        soon = datetime.utcnow() + timedelta(seconds=10)
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='soon',
                            start_time=soon, stop_time=soon + timedelta(hours=1))
        db.session.commit()
        cfg = self._config()
        stub, waited = self._run(cfg, lambda *a, **k: ConversionResult(True, 'success'))
        self.assertTrue(waited.called, 'did not wait despite an imminent recording')
        self.assertTrue(stub.called, 'never proceeded to convert after the wait')
        events = [e.event_type for e in RecordingEvent.query.filter_by(recording_id=self.rid).all()]
        self.assertIn(CONVERSION_YIELDED, events)

    def test_no_wait_when_nothing_conflicts(self):
        cfg = self._config()
        stub, waited = self._run(cfg, lambda *a, **k: ConversionResult(True, 'success'))
        self.assertFalse(waited.called, 'waited despite nothing colliding')

    def test_no_wait_when_policy_off(self):
        soon = datetime.utcnow() + timedelta(seconds=10)
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='soon',
                            start_time=soon, stop_time=soon + timedelta(hours=1))
        db.session.commit()
        cfg = self._config(collision_policy='off')
        stub, waited = self._run(cfg, lambda *a, **k: ConversionResult(True, 'success'))
        self.assertFalse(waited.called, "collision_policy 'off' must skip the pre-start check")

    def test_preempted_result_does_not_count_against_the_restart_budget(self):
        cfg = self._config(max_restart_attempts=1)
        outcomes = iter([
            ConversionResult(False, 'preempted', 'yielding to recording "x" (starts soon)'),
            ConversionResult(False, 'preempted', 'yielding to recording "x" (starts soon)'),
            ConversionResult(True, 'success'),
        ])
        stub, _ = self._run(cfg, lambda *a, **k: next(outcomes))
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'COMPLETED', 'two preemptions must not exhaust a budget of 1')
        self.assertEqual(rec.conversion_attempts, 0, 'a preemption is not a restart')
        self.assertEqual(stub.call_count, 3)
        events = [e.event_type for e in RecordingEvent.query.filter_by(recording_id=self.rid).all()]
        self.assertEqual(2, events.count(CONVERSION_YIELDED))

    def test_a_cancel_during_the_collision_wait_sticks(self):
        cfg = self._config()

        def _cancel_during_wait(*a, **k):
            r = db.session.get(Recording, self.rid)
            r.status = 'ABORTED'
            db.session.commit()

        stub = mock.Mock(return_value=ConversionResult(False, 'preempted', 'yielding'))
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', stub), \
             mock.patch.object(ppmod, '_wait_for_conversion_clear', side_effect=_cancel_during_wait):
            do_postprocess(self.t.app, self.rid, self.ts)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'ABORTED', 'a cancel during the collision wait must stick')
        self.assertEqual(stub.call_count, 1, 'must not resume converting after a cancel')


# ── start_recording: the 'wait' policy's defer/fail path ─────────────────────────
class StartRecordingCollisionTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)

    def tearDown(self):
        with _active_lock:
            _active_conversions.clear()
        self.t.cleanup()

    def _sandbox(self, collision_policy):
        # sandbox_config() writes a full replacement config.yaml, not a merge onto
        # TestApp's own dvr_output_dir override - so a runtime load_config() call (like
        # start_recording's) needs the dvr dir restated here too, or it 404s on the
        # unrelated "DVR output directory does not exist" FAILED path.
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False},
            'post_process': {'collision_policy': collision_policy},
        }})

    def test_defers_when_a_conversion_is_live_and_the_window_is_open(self):
        soon = datetime.utcnow() + timedelta(seconds=5)
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='waits',
                                  start_time=soon, stop_time=soon + timedelta(hours=1))
        db.session.commit()
        rid = rec.id
        self._sandbox('wait')
        with _active_lock:
            _active_conversions[999999] = object()  # some OTHER recording's live conversion

        from app.recorder import start_recording
        with mock.patch('app.scheduler.reschedule_recording_start') as resched:
            start_recording(self.t.app, rid)

        db.session.expire_all()
        r = db.session.get(Recording, rid)
        self.assertEqual(r.status, REC_STATUS_SCHEDULED, 'must not start while a conversion is live')
        self.assertTrue(resched.called, 'did not schedule a retry')
        events = [e.event_type for e in RecordingEvent.query.filter_by(recording_id=rid).all()]
        self.assertIn(RECORDING_START_DEFERRED, events)

    def test_defer_event_is_recorded_only_once_across_retries(self):
        soon = datetime.utcnow() + timedelta(seconds=5)
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='waits',
                                  start_time=soon, stop_time=soon + timedelta(hours=1))
        db.session.commit()
        rid = rec.id
        self._sandbox('wait')
        with _active_lock:
            _active_conversions[999999] = object()

        from app.recorder import start_recording
        with mock.patch('app.scheduler.reschedule_recording_start'):
            start_recording(self.t.app, rid)
            start_recording(self.t.app, rid)

        db.session.expire_all()
        events = [e.event_type for e in RecordingEvent.query.filter_by(recording_id=rid).all()]
        self.assertEqual(1, events.count(RECORDING_START_DEFERRED))

    def test_fails_loudly_once_the_recordings_own_window_has_passed(self):
        past_stop = datetime.utcnow() - timedelta(seconds=1)
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='too late',
                                  start_time=past_stop - timedelta(hours=1), stop_time=past_stop)
        db.session.commit()
        rid = rec.id
        self._sandbox('wait')
        with _active_lock:
            _active_conversions[999999] = object()

        from app.recorder import start_recording
        with mock.patch('app.scheduler.reschedule_recording_start') as resched, \
             mock.patch('app.alerts.create_alert') as alert:
            start_recording(self.t.app, rid)

        db.session.expire_all()
        r = db.session.get(Recording, rid)
        self.assertEqual(r.status, REC_STATUS_FAILED)
        self.assertFalse(resched.called, 'must not schedule another retry once the window has passed')
        self.assertTrue(alert.called)
        self.assertEqual(alert.call_args[0][0], 'RECORDING_FAILED_CONVERSION_COLLISION')
        events = [e.event_type for e in RecordingEvent.query.filter_by(recording_id=rid).all()]
        self.assertIn('RECORDING_FAILED', events)

    def test_cancel_policy_never_delays_the_start_even_with_a_live_conversion(self):
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='starts now',
                                  start_time=datetime.utcnow() - timedelta(seconds=1),
                                  stop_time=datetime.utcnow() + timedelta(hours=1))
        db.session.commit()
        rid = rec.id
        self._sandbox('cancel')
        with _active_lock:
            _active_conversions[999999] = object()

        from app import recorder
        with mock.patch('app.scheduler.reschedule_recording_start') as resched, \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        r = db.session.get(Recording, rid)
        self.assertEqual(r.status, REC_STATUS_IN_PROGRESS,
                         "'cancel' policy must never delay a recording's start")
        self.assertFalse(resched.called)
        self.assertTrue(launch.called)


# ── scheduler.reschedule_recording_start actually re-registers the job ────────────
class RescheduleRecordingStartJobTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        self.t.cleanup()

    def test_reregisters_start_job_under_the_same_id(self):
        from app.scheduler import reschedule_recording_start, get_scheduler
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='deferred')
        db.session.commit()
        run_date = datetime.utcnow() + timedelta(seconds=30)

        reschedule_recording_start(rec.id, run_date)

        job = get_scheduler().get_job(f'start_{rec.id}')
        self.assertIsNotNone(job, 'reschedule_recording_start did not register start_<id>')
        self.assertAlmostEqual(job.next_run_time.replace(tzinfo=None), run_date,
                               delta=timedelta(seconds=2))


if __name__ == '__main__':
    unittest.main(verbosity=2)
