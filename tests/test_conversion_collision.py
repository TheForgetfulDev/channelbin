"""Guards dev/changelog/672 "mp4 conversion collision avoidance"
(recording.post_process.collision_policy).

Conversion is local CPU/disk work with no coordination against the recorder before this -
DESIGN-concurrency.md's precedence doctrine ("recordings always win") covers the
tester/sync/recorder triangle but predates conversion as an actor. Two policies close the
gap: 'cancel' (the conversion yields to a colliding recording - suspends and continues once
clear, so the recording is never delayed) and 'wait' (the opposite - a recording defers its
own start to a running conversion, failing loudly if its own window would pass first).

What a yield does to the ffmpeg is tests/test_conversion_pause.py's subject
(dev/changelog/952); what is here is when a yield fires and who yields to whom.

No real ffmpeg and no network throughout: subprocess.Popen is faked where a conversion
"runs", and every recorder/scheduler side effect that would otherwise touch a live process or
job store is mocked.
  python3 -m unittest tests.test_conversion_collision
"""
import contextlib
import os
import signal
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event  # noqa: E402

import app.config as cfgmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
import app.proc_utils as pumod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, CONVERSION_YIELDED, RECORDING_START_DEFERRED,
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_FAILED,
    REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING,
)
from app.postprocessor import (  # noqa: E402
    _collision_window_seconds, _conversion_collision_conflict, _wait_for_conversion_clear,
    ConversionResult, do_postprocess, run_conversion_supervised,
    _active_conversions, _active_lock,
)


# ── Pure helper ─────────────────────────────────────────────────────────────
class CollisionWindowSecondsTests(unittest.TestCase):
    """The argument is the source LEFT to encode, not the recording's whole duration
    (dev/changelog/953). The function itself only divides; which number it is handed is the
    caller's decision, and the two callers deliberately hand it different ones."""

    def test_default_multiplier_equals_remaining(self):
        self.assertEqual(600.0, _collision_window_seconds(600, 1.0))

    def test_multiplier_2_halves_the_window(self):
        self.assertEqual(300.0, _collision_window_seconds(600, 2.0))

    def test_multiplier_below_floor_is_clamped_to_0_1(self):
        self.assertEqual(6000.0, _collision_window_seconds(600, 0.05))
        self.assertEqual(6000.0, _collision_window_seconds(600, 0))

    def test_unknown_duration_is_zero(self):
        self.assertEqual(0.0, _collision_window_seconds(None, 1.0))
        self.assertEqual(0.0, _collision_window_seconds(0, 1.0))

    def test_exhausted_remainder_is_zero_not_negative(self):
        """A conversion that has encoded past its probed duration must degrade to the
        no-lookahead rule, never to a negative window that a max(0.0, ...) somewhere else
        has to rescue."""
        self.assertEqual(0.0, _collision_window_seconds(-5, 1.0))

    def test_the_window_shrinks_with_the_work_left(self):
        # Recording 17's numbers: an 18,212s source with ~1.7h left held a 5.06h window.
        self.assertEqual(18212.0, _collision_window_seconds(18212, 1.0))
        self.assertEqual(6120.0, _collision_window_seconds(6120, 1.0))


# ── The conflict query ────────────────────────────────────────────────────────
class ConversionCollisionConflictTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        # The row doing the asking. Every call excludes it, which is what stops the
        # pre-start check - which runs while the asker is still ANALYZING - from waiting
        # forever on itself.
        self.asker = seed.make_recording(status='CONVERTING', name='asker')
        db.session.commit()
        self.rid = self.asker.id

    def tearDown(self):
        self.t.cleanup()

    def test_in_progress_recording_is_a_conflict_regardless_of_window(self):
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='live')
        db.session.commit()
        conflict = _conversion_collision_conflict(0, self.rid)
        self.assertIsNotNone(conflict)
        self.assertEqual('live', conflict.name)

    def test_scheduled_recording_outside_window_is_not_a_conflict(self):
        far = datetime.utcnow() + timedelta(hours=2)
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='later',
                            start_time=far, stop_time=far + timedelta(hours=1))
        db.session.commit()
        self.assertIsNone(_conversion_collision_conflict(60, self.rid))

    def test_scheduled_recording_inside_window_is_a_conflict(self):
        soon = datetime.utcnow() + timedelta(seconds=30)
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='soon',
                            start_time=soon, stop_time=soon + timedelta(hours=1))
        db.session.commit()
        conflict = _conversion_collision_conflict(60, self.rid)
        self.assertIsNotNone(conflict)
        self.assertEqual('soon', conflict.name)

    def test_terminal_recording_is_never_a_conflict(self):
        seed.make_recording(status='COMPLETED', name='done')
        db.session.commit()
        self.assertIsNone(_conversion_collision_conflict(999999, self.rid))

    # ── Post-capture work counts (dev/changelog/953) ──────────────────────────
    def test_a_concatenating_recording_is_a_conflict(self):
        """Recording 19's 42.6 GB join started 13 seconds after recording 17's conversion
        did, because CONCATENATING was invisible to this query."""
        seed.make_recording(status=REC_STATUS_CONCATENATING, name='joining')
        db.session.commit()
        conflict = _conversion_collision_conflict(0, self.rid)
        self.assertIsNotNone(conflict, 'a concat is heavy disk work and must count')
        self.assertEqual('joining', conflict.name)

    def test_an_analyzing_recording_is_a_conflict(self):
        seed.make_recording(status=REC_STATUS_ANALYZING, name='probing')
        db.session.commit()
        conflict = _conversion_collision_conflict(0, self.rid)
        self.assertIsNotNone(conflict, 'a full-file ffprobe is heavy work and must count')
        self.assertEqual('probing', conflict.name)

    def test_a_parked_post_capture_recording_is_not_a_conflict(self):
        """postprocess_waiting_since is the recorded fact that a row has stopped and is
        waiting on someone else, so it is consuming nothing. Counting it is what would let
        two parked rows wait on each other forever - the exact state recordings 17 and 19
        were in on 2026-09-13, both ANALYZING and both yielding."""
        for status in (REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING):
            with self.subTest(status=status):
                parked = seed.make_recording(
                    status=status, name=f'parked_{status}',
                    postprocess_waiting_since=datetime.utcnow())
                db.session.commit()
                self.assertIsNone(_conversion_collision_conflict(0, self.rid))
                db.session.delete(parked)
                db.session.commit()

    def test_a_recording_never_conflicts_with_itself(self):
        """do_postprocess runs its pre-start check while its OWN row is still ANALYZING.
        Without the exclusion every conversion waits forever on itself."""
        self.asker.status = REC_STATUS_ANALYZING
        db.session.commit()
        self.assertIsNone(_conversion_collision_conflict(999999, self.rid),
                          'a recording blocked its own conversion on its own analysis')

    def test_two_parked_recordings_do_not_block_each_other(self):
        other = seed.make_recording(status=REC_STATUS_ANALYZING, name='other',
                                    postprocess_waiting_since=datetime.utcnow())
        self.asker.status = REC_STATUS_ANALYZING
        self.asker.postprocess_waiting_since = datetime.utcnow()
        db.session.commit()
        self.assertIsNone(_conversion_collision_conflict(999999, self.rid))
        self.assertIsNone(_conversion_collision_conflict(999999, other.id))


# ── The phrase each conflicting status renders as ─────────────────────────────
class ConflictPhraseTests(unittest.TestCase):
    """Every status the query can return has its own words. A status rendering through a
    fallback is the 'states are enumerated' defect - the next status added would land there
    silently (CLAUDE.md)."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_every_matchable_status_has_its_own_phrase(self):
        seen = set()
        for status in (REC_STATUS_IN_PROGRESS, REC_STATUS_SCHEDULED,
                       REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING):
            rec = seed.make_recording(status=status, name=f'r_{status}')
            db.session.commit()
            phrase = ppmod._conflict_phrase(rec)
            self.assertNotIn(status, phrase, f'{status} fell through to the raw-status fallback')
            self.assertNotIn(phrase, seen, f'{status} shares its wording with another status')
            seen.add(phrase)


# ── The blocking wait ──────────────────────────────────────────────────────────
@contextlib.contextmanager
def _joined_thread(target):
    """Run target on a thread and join it before the caller moves on.

    A helper that changes a row mid-wait is the only way to exercise a blocking poll loop,
    but an unjoined one keeps touching the database while tearDown removes the session and
    deletes the temp file underneath it - and being a daemon thread, nothing else ever reaps
    it. An exception inside the helper is re-raised here rather than printed to stderr and
    forgotten (dev/changelog/1014).
    """
    box = {}

    def _run():
        try:
            target()
        except Exception as exc:  # surfaced on the caller's thread below, never swallowed
            box['exc'] = exc

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    try:
        yield th
    finally:
        th.join(timeout=10)
    if th.is_alive():
        raise AssertionError('helper thread outlived the test that started it')
    if 'exc' in box:
        raise AssertionError(f'helper thread raised {box["exc"]!r}') from box['exc']


class WaitForConversionClearTests(unittest.TestCase):
    """Every statement this class's own session runs must come from the thread that owns it.

    An ORM object stays attached to the session that loaded it, and a commit expires its
    attributes - so reading `blocker.id` from inside a helper thread issues the refresh on
    the MAIN thread's session and connection, concurrently with the poll loop already
    querying there. Two threads on one sqlite3 connection is what produced
    `sqlite3.InterfaceError: bad parameter or other API misuse` in the main thread and
    `ObjectDeletedError` in the helper (BUGS.md 2026-09-17, dev/changelog/1014). Helpers
    therefore close over plain ids, and this listener is what keeps that true.
    """

    def setUp(self):
        self.t = make_test_app()
        self._stmt_threads = set()
        self._session = db.session()

        @event.listens_for(self._session, 'do_orm_execute')
        def _record_thread(orm_execute_state):
            self._stmt_threads.add(threading.current_thread().name)

        self._record_thread = _record_thread

    def tearDown(self):
        try:
            event.remove(self._session, 'do_orm_execute', self._record_thread)
        finally:
            self.t.cleanup()
        foreign = self._stmt_threads - {threading.main_thread().name}
        self.assertFalse(
            foreign,
            f'a helper thread ran a statement on the test session: {sorted(foreign)}')

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
        # Read both ids HERE, on the owning thread. See the class docstring.
        conv_id, blocker_id = conv.id, blocker.id
        cleared = threading.Event()

        def _clear_soon():
            time.sleep(0.15)
            with self.t.app.app_context():
                r = db.session.get(Recording, blocker_id)
                r.status = 'COMPLETED'
                db.session.commit()
            cleared.set()

        with _joined_thread(_clear_soon):
            started = time.monotonic()
            _wait_for_conversion_clear(conv_id, 0, poll_seconds=0.02)
        self.assertTrue(cleared.is_set())
        self.assertGreaterEqual(time.monotonic() - started, 0.1,
                                'returned before the conflict actually cleared')

    def test_stops_waiting_once_this_recording_is_cancelled(self):
        conv = seed.make_recording(status='CONVERTING', name='conv')
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='never clears')
        db.session.commit()
        conv_id = conv.id

        def _cancel_soon():
            time.sleep(0.1)
            with self.t.app.app_context():
                r = db.session.get(Recording, conv_id)
                r.status = 'ABORTED'
                db.session.commit()

        with _joined_thread(_cancel_soon):
            started = time.monotonic()
            _wait_for_conversion_clear(conv_id, 0, poll_seconds=0.02)
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
        self.signals = []

    def send_signal(self, sig):
        # SIGSTOP/SIGCONT are how a conversion yields without dying, so a fake that swallows
        # them silently would let a suspension that never happened look like one that did.
        self.signals.append(sig)

    @property
    def suspended(self):
        stops = [s for s in self.signals if s in (signal.SIGSTOP, signal.SIGCONT)]
        return bool(stops) and stops[-1] == signal.SIGSTOP

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

    def _run(self, fake, collision_policy='cancel', collision_multiplier=1.0,
             interval=0.05, pre_output_timeout=30):
        with mock.patch.object(pumod.subprocess, 'Popen', return_value=fake), \
             mock.patch.object(pumod, 'read_progress_tail',
                               side_effect=lambda p: (5_000_000, 1000, False)):
            return run_conversion_supervised(
                self.t.app, self.rid, self.cmd, self.out,
                expected_duration=100, pre_output_timeout=pre_output_timeout,
                interval=interval, stall_seconds=0,
                collision_policy=collision_policy,
                collision_multiplier=collision_multiplier)

    def test_yields_to_an_in_progress_recording(self):
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='blocker')
        db.session.commit()
        fake = _FakePopen(alive_ticks=3)
        self._run(fake, collision_policy='cancel')
        self.assertIn(signal.SIGSTOP, fake.signals, 'did not yield to an IN_PROGRESS recording')

    def test_does_not_yield_when_policy_is_off(self):
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='blocker')
        db.session.commit()
        fake = _FakePopen(alive_ticks=2, returncode=0)
        result = self._run(fake, collision_policy='off')
        self.assertTrue(result.success, 'policy off must never yield')
        self.assertNotIn(signal.SIGSTOP, fake.signals)

    def test_does_not_yield_when_policy_is_wait(self):
        # 'wait' means the RECORDING defers, not the conversion - the conversion must run
        # through a colliding recording untouched under this policy.
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='blocker')
        db.session.commit()
        fake = _FakePopen(alive_ticks=2, returncode=0)
        result = self._run(fake, collision_policy='wait')
        self.assertTrue(result.success, "'wait' policy must not suspend the conversion")
        self.assertNotIn(signal.SIGSTOP, fake.signals)

    def test_does_not_yield_when_nothing_conflicts(self):
        fake = _FakePopen(alive_ticks=2, returncode=0)
        result = self._run(fake, collision_policy='cancel')
        self.assertTrue(result.success)

    def test_yields_to_a_recording_doing_its_own_post_capture_work(self):
        """The contention that killed recording 19: a conversion that waited politely for a
        capture and then started on top of that capture's 42.6 GB concat."""
        seed.make_recording(status=REC_STATUS_CONCATENATING, name='joining')
        db.session.commit()
        fake = _FakePopen(alive_ticks=3)
        self._run(fake, collision_policy='cancel')
        self.assertIn(signal.SIGSTOP, fake.signals,
                      'did not yield to a recording that was joining its segments')

    def test_does_not_yield_to_a_recording_that_is_itself_parked(self):
        seed.make_recording(status=REC_STATUS_ANALYZING, name='parked',
                            postprocess_waiting_since=datetime.utcnow())
        db.session.commit()
        fake = _FakePopen(alive_ticks=2, returncode=0)
        result = self._run(fake, collision_policy='cancel')
        self.assertTrue(result.success)
        self.assertNotIn(signal.SIGSTOP, fake.signals,
                         'yielded to a recording that was parked and consuming nothing')


class InRunWindowNarrowingTests(unittest.TestCase):
    """The in-run window is sized on the source LEFT to encode, recomputed every poll
    (dev/changelog/953). Recording 17 yielded five hours before the recording it yielded to
    began, because its window was its whole 18,212s duration with ~1.7h of source left.

    Both cases below use the SAME scheduled recording and the same multiplier - only how
    far the encode has got differs, which is the whole point.
    """

    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status='CONVERTING', name='conv')
        db.session.commit()
        self.rid = self.rec.id
        self.out = os.path.join(self.t._tmpdir, f'out_{self.rid}.mkv')
        # Starts inside the full-duration window (100s) and outside a nearly-finished
        # conversion's remaining-work window (10s).
        soon = datetime.utcnow() + timedelta(seconds=60)
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='in 60s',
                            start_time=soon, stop_time=soon + timedelta(hours=1))
        db.session.commit()

    def tearDown(self):
        with _active_lock:
            _active_conversions.clear()
        self.t.cleanup()

    def _run_at(self, out_time_us, fake):
        with mock.patch.object(pumod.subprocess, 'Popen', return_value=fake), \
             mock.patch.object(pumod, 'read_progress_tail',
                               side_effect=lambda p: (out_time_us, 1000, False)):
            return run_conversion_supervised(
                self.t.app, self.rid, ['/usr/bin/ffmpeg', '-i', 'a.ts', self.out], self.out,
                expected_duration=100, pre_output_timeout=30, interval=0.02,
                stall_seconds=0, collision_policy='cancel', collision_multiplier=1.0)

    def test_yields_early_in_the_encode(self):
        """The control half of the pair: it passes with or without the narrowing, and is
        here so the other half cannot be satisfied by a window that simply stopped working.
        A yield that never fires at all is the failure mode this catches."""
        fake = _FakePopen(alive_ticks=3)
        self._run_at(5_000_000, fake)   # 5s of 100s done -> 95s window, 60s away: inside
        self.assertIn(signal.SIGSTOP, fake.signals,
                      'a conversion with 95s of source left must still step aside')

    def test_does_not_yield_when_barely_any_source_is_left(self):
        fake = _FakePopen(alive_ticks=2, returncode=0)
        result = self._run_at(90_000_000, fake)  # 90s done -> 10s window, 60s away: outside
        self.assertTrue(result.success)
        self.assertNotIn(signal.SIGSTOP, fake.signals,
                         'yielded on a window sized by work already done')


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

    def test_a_yield_never_reaches_the_restart_loop_at_all(self):
        # A mid-run yield used to end the attempt with reason='preempted', and the restart
        # loop had to recognize that token and refuse to count it. Suspension removed the
        # whole branch: the attempt is continued in place, so every reason the loop now sees
        # is a real outcome. Guarded here because the token quietly coming back - a caller
        # returning 'preempted' again - would be spent against the budget as a failure.
        self.assertNotIn('preempted', ppmod._RESTART_REASON_PHRASE)
        import inspect
        self.assertNotIn("'preempted'", inspect.getsource(ppmod.do_postprocess),
                         'do_postprocess still branches on a preempted result')

    def test_a_cancel_during_the_collision_wait_sticks(self):
        soon = datetime.utcnow() + timedelta(seconds=10)
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='soon',
                            start_time=soon, stop_time=soon + timedelta(hours=1))
        db.session.commit()
        cfg = self._config()

        def _cancel_during_wait(*a, **k):
            r = db.session.get(Recording, self.rid)
            r.status = 'ABORTED'
            db.session.commit()

        stub = mock.Mock(return_value=ConversionResult(True, 'success'))
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', stub), \
             mock.patch.object(ppmod, '_wait_for_conversion_clear', side_effect=_cancel_during_wait):
            do_postprocess(self.t.app, self.rid, self.ts)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'ABORTED', 'a cancel during the collision wait must stick')
        self.assertEqual(stub.call_count, 0, 'must not start converting after a cancel')
        self.assertIsNone(rec.postprocess_waiting_since,
                          'a cancelled wait must not leave the row claiming to be waiting')


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
