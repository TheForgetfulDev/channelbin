"""Tier 2 - a conversion that yields to a recording is SUSPENDED, not killed
(dev/changelog/952).

Recording 17, 2026-09-13: an 8.1 GB / 5h03m capture re-encoding for timeline damage ran
4h26m, reached 66.4% and wrote 4.6 GB, then yielded local resources to a recording that had
not started yet. Yielding meant `terminate_or_kill(proc, hard=True)`, so the partial answered
ffprobe with "moov atom not found" and all 4h26m was gone; the retry needed ~6.6h from 0%.
Nothing about releasing the CPU required the child to die.

The invariants below, in the order the work happens:

  (a) A yielding run is SIGSTOPped and SIGCONTed - never terminated - and the same attempt
      carries on afterwards, so nothing about a yield reaches the restart loop or its budget.
  (b) Every clock the supervisor keeps stops with the child: the stall budget, the pre-output
      budget, and the elapsed wall the hooks are handed. A running stall clock would kill a
      suspended child at stall_seconds for not advancing, which is the exact loss suspension
      exists to remove.
  (c) terminate_or_kill() continues a child before terminating it. Measured on this box: a
      stopped ffmpeg 7.1 sat in state T five full seconds after SIGTERM and only acted once
      SIGCONT arrived, so without this every teardown burns its whole wait before SIGKILL.
  (d) Recording.postprocess_waiting_since records that a row is parked, and is cleared on
      every way out - resume, a run that ends while suspended, a cancelled pre-start wait,
      and a service restart that finds one left over.
  (e) tools/check_busy.py does not block a restart on a parked row, and still reports it
      with what a restart would cost.

The real-process tests here spawn `sleep`/`python3` locally, never a network input, so
tests/support/netguard.py is satisfied.
  python3 -m unittest tests.test_conversion_pause
"""
import os
import signal
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
import app.proc_utils as pumod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.test_restart_guard import run_check_busy  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, CONVERSION_YIELDED, CONVERSION_RESUMED,
    CONVERSION_RESTARTED, REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS,
    REC_STATUS_CONVERTING, REC_STATUS_ANALYZING,
)
from app.proc_utils import (  # noqa: E402
    supervise_ffmpeg, suspend_process, resume_process, terminate_or_kill,
)
from app.postprocessor import (  # noqa: E402
    do_postprocess, run_conversion_supervised, _active_conversions, _active_lock,
)


def _size(path):
    """Bytes at path, 0 while it does not exist yet."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _wait_until(pred, timeout, interval=0.02):
    """Poll pred() until it is true or timeout seconds pass. False on timeout.

    A generous ceiling costs nothing when the condition is met in milliseconds, and is what
    keeps a real-process test off the machine's clock: the assertion is that the thing
    happens at all, not that it happened inside a margin a loaded CI runner can eat.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


class _FakeChild:
    """Stands in for an ffmpeg under supervision, recording every signal it is sent.

    Signals are recorded rather than swallowed because the whole subject here is which one
    was sent and when: a fake that accepted SIGSTOP silently would let a suspension that
    never happened look exactly like one that did.
    """

    def __init__(self, alive_ticks=None, returncode=0):
        self._left = alive_ticks
        self._rc = returncode
        self.returncode = None
        self.terminated = False
        self.calls = []          # ordered log of 'SIGSTOP'/'SIGCONT'/'terminate'/'kill'

    def send_signal(self, sig):
        self.calls.append(signal.Signals(sig).name)

    def poll(self):
        if self._left is None:
            return None
        if self._left > 0:
            self._left -= 1
            return None
        self.returncode = self._rc
        return self._rc

    def terminate(self):
        self.calls.append('terminate')
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.calls.append('kill')
        self.terminated = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode

    @property
    def signals(self):
        return [c for c in self.calls if c.startswith('SIG')]


class _Supervised:
    """Drives supervise_ffmpeg() over a _FakeChild with no real ffmpeg and no progress file.

    `yield_for` is how many polls the suspend_check asks for suspension; after that it
    returns None and the run continues. `out_time_us` / file size are supplied directly so a
    test can say "this job is advancing" or "this job is frozen" without a child that writes.
    """

    def __init__(self, tmpdir, child, *, yield_for=0, mark_mode='advance', **kwargs):
        self.child = child
        self.out = os.path.join(tmpdir, 'supervised.out')
        self.suspend_calls = 0
        self.yield_for = yield_for
        # How the fake job's out_time behaves while it is RUNNING. It always freezes while
        # suspended, because that is what a stopped ffmpeg does and it is precisely what a
        # stall clock left running would punish it for.
        #   'advance'          - healthy, moves every running poll
        #   'none'             - never produces any output at all
        #   'none_then_advance'- produces nothing until the suspension is over
        self.mark_mode = mark_mode
        self.mark = 0
        self.progress_ticks = []      # (wall, out_time, size) per published tick
        self.suspended_reasons = []
        self.resumes = 0
        self.is_suspended = False
        self.kwargs = kwargs

    def _suspend_check(self, wall, out_time, size):
        self.suspend_calls += 1
        self.is_suspended = self.suspend_calls <= self.yield_for
        if self.is_suspended:
            return 'a recording needs the machine'
        return None

    def run(self):
        def _tail(_path):
            if not self.is_suspended:
                if self.mark_mode == 'advance':
                    self.mark += 1_000_000
                elif self.mark_mode == 'none_then_advance' and self.suspend_calls > self.yield_for:
                    self.mark += 1_000_000
            return self.mark, 0, False

        opts = dict(scratch_prefix='test', scratch_key=1, interval=0.02,
                    pre_output_timeout=30, stall_seconds=0,
                    noun='job', label='test job',
                    suspend_check=self._suspend_check,
                    on_suspend=self.suspended_reasons.append,
                    on_resume=lambda: setattr(self, 'resumes', self.resumes + 1),
                    on_progress=lambda w, o, s: self.progress_ticks.append((w, o, s)))
        opts.update(self.kwargs)
        with mock.patch.object(pumod.subprocess, 'Popen', return_value=self.child), \
             mock.patch.object(pumod, 'read_progress_tail', side_effect=_tail):
            return supervise_ffmpeg(['/usr/bin/ffmpeg', '-i', 'x.ts', self.out], self.out,
                                    **opts)


# ── (a) suspend instead of kill ───────────────────────────────────────────────
class SuspendNotKillTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_a_yielding_run_is_stopped_and_not_terminated(self):
        child = _FakeChild(alive_ticks=6)
        s = _Supervised(self.t._tmpdir, child, yield_for=3)
        run = s.run()
        self.assertIn('SIGSTOP', child.signals, 'the child was never suspended')
        self.assertFalse(child.terminated,
                         'the child was killed to yield - this is the whole defect')
        self.assertTrue(run.success, 'the same attempt must carry on to completion')

    def test_a_suspended_run_is_continued_when_the_conflict_clears(self):
        child = _FakeChild(alive_ticks=8)
        s = _Supervised(self.t._tmpdir, child, yield_for=2)
        s.run()
        self.assertEqual(['SIGSTOP', 'SIGCONT'], s_first_two(child.signals))
        self.assertEqual(1, s.resumes)
        self.assertEqual(['a recording needs the machine'], s.suspended_reasons)

    def test_the_transitions_fire_once_each_not_once_per_poll(self):
        # on_suspend writes an event and a row; firing it per poll would fill the timeline
        # with one CONVERSION_YIELDED every couple of seconds for the length of a recording.
        child = _FakeChild(alive_ticks=10)
        s = _Supervised(self.t._tmpdir, child, yield_for=5)
        s.run()
        self.assertEqual(1, len(s.suspended_reasons))
        self.assertEqual(1, s.resumes)

    def test_a_child_killed_while_suspended_still_ends_the_run(self):
        # A cancel or a shutdown SIGKILLs the child from outside while it is stopped
        # (measured on this box: SIGKILL reaches a stopped process immediately). The loop
        # must notice it exited rather than sitting in the suspended branch forever.
        child = _FakeChild(alive_ticks=2, returncode=-9)
        s = _Supervised(self.t._tmpdir, child, yield_for=99)
        run = s.run()
        self.assertEqual('died', run.reason)

    def test_no_progress_tick_is_published_while_suspended(self):
        # Nothing moves while the child is stopped, so a tick per poll would write the same
        # numbers to the row for hours.
        child = _FakeChild(alive_ticks=8)
        s = _Supervised(self.t._tmpdir, child, yield_for=4)
        s.run()
        self.assertLessEqual(len(s.progress_ticks), 5,
                             'published progress while the child was stopped')


def s_first_two(sigs):
    return sigs[:2]


# ── (b) every clock stops with the child ──────────────────────────────────────
class ClocksStopWhileSuspendedTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_the_stall_clock_does_not_run_while_suspended(self):
        # THE test of this change. A stopped child cannot advance by definition, so a stall
        # clock left running kills it at stall_seconds and destroys exactly the work
        # suspension exists to keep. The job is advancing (marks rise) up to the pause, then
        # frozen for many multiples of stall_seconds while stopped.
        child = _FakeChild(alive_ticks=40)
        s = _Supervised(self.t._tmpdir, child, yield_for=25,
                        stall_seconds=0.1, interval=0.02)
        run = s.run()
        self.assertNotEqual('stalled', run.reason,
                            'a suspended child was killed as stalled for not advancing')
        self.assertFalse(child.terminated)
        self.assertTrue(run.success)

    def test_the_pre_output_budget_does_not_run_while_suspended(self):
        # Same shape one phase earlier: a conversion suspended before it muxes its first
        # frame must not be killed as "produced no output in N seconds" for a wait it was
        # told to take.
        child = _FakeChild(alive_ticks=40)
        s = _Supervised(self.t._tmpdir, child, yield_for=25,
                        mark_mode='none_then_advance',
                        pre_output_timeout=0.2, stall_seconds=5, interval=0.02)
        run = s.run()
        self.assertNotEqual('no_output', run.reason,
                            'a suspended child was killed on the pre-output budget')
        self.assertTrue(run.success)

    def test_the_pre_output_budget_still_fires_once_running_again(self):
        # The budget is frozen, not cancelled - a job that genuinely never produces anything
        # is still killed, so suspension does not become a way to run forever.
        child = _FakeChild(alive_ticks=None)
        s = _Supervised(self.t._tmpdir, child, yield_for=3,
                        mark_mode='none', pre_output_timeout=0.2, interval=0.02)
        run = s.run()
        self.assertEqual('no_output', run.reason)

    def test_the_elapsed_wall_excludes_suspended_time(self):
        # The wall handed to on_progress drives the conversion's ETA. If it counted the
        # pause, a recording-length suspension would report an encode running many times
        # slower than it is for the whole rest of the job. Compared against the real elapsed
        # time rather than an absolute bound, because an absolute one passes either way.
        child = _FakeChild(alive_ticks=20)
        s = _Supervised(self.t._tmpdir, child, yield_for=8, interval=0.05,
                        stall_seconds=5)
        started = time.monotonic()
        s.run()
        real_elapsed = time.monotonic() - started
        self.assertTrue(s.progress_ticks, 'no progress was published at all')
        last_wall = s.progress_ticks[-1][0]
        paused_for = 8 * 0.05
        self.assertGreater(real_elapsed - last_wall, paused_for * 0.7,
                           'the elapsed clock ran through the suspension')


# ── (c) teardown continues before it terminates ───────────────────────────────
class TerminateContinuesFirstTests(unittest.TestCase):
    def test_terminate_or_kill_continues_the_child_before_terminating(self):
        child = _FakeChild(alive_ticks=None)
        terminate_or_kill(child)
        self.assertEqual('SIGCONT', child.calls[0],
                         'terminated without continuing a possibly-suspended child first')
        self.assertIn('terminate', child.calls)

    def test_a_real_stopped_child_is_torn_down_without_burning_the_whole_wait(self):
        # The measured behavior this design rests on: a stopped process that HANDLES SIGTERM
        # does not act on it until something continues it, so terminate_or_kill() would spend
        # its entire timeout and then SIGKILL. With the SIGCONT it exits promptly.
        #
        # Decided on the exit code, never on a clock: the child's handler exits 0, and the
        # handler can only run once something has continued the process, so returncode 0 IS
        # the SIGCONT. Without it the pending SIGTERM is never acted on, the wait times out
        # and SIGKILL lands, giving -9. A stopwatch bound instead of this measured a shared
        # CI runner and turned a green commit red (dev/changelog/983).
        #
        # The one stdout line is a handshake, not a stream: the child writes it and nothing
        # else, and it is read immediately, so no pipe buffer can fill behind it. It has to
        # be a handshake rather than a sleep because the outcome above is only decidable
        # once the handler is installed - SIGSTOP landing first leaves SIGTERM at its default
        # action, which the kernel applies to a stopped process without any SIGCONT, and the
        # test would then pass against a teardown that never continues anything.
        #
        # The child sleeps in short slices, never one long sleep. CPython runs a Python-level
        # handler only at a bytecode boundary or when a blocking call returns EINTR, so a
        # SIGTERM landing after the last check but before the child enters nanosleep() is
        # noted and then ignored until the sleep ends: with time.sleep(60) that was about 1
        # run in 100, stuck in state S with SIGTERM already consumed, and teardown SIGKILLed a
        # child it had in fact continued (dev/docs/BUGS.md 2026-09-24).
        proc = subprocess.Popen(
            [sys.executable, '-c',
             'import signal, sys, time\n'
             'signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(SystemExit(0)))\n'
             'sys.stdout.write("ready\\n"); sys.stdout.flush()\n'
             'while True: time.sleep(0.05)\n'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True)
        try:
            self.assertEqual('ready', proc.stdout.readline().strip(),
                             'the child never reported its SIGTERM handler installed')
            self.assertTrue(suspend_process(proc))
            # Deliberately generous, and free: nothing here measures the timeout, and on the
            # passing path terminate_or_kill returns the moment the child exits. The headroom
            # only widens the window a continued child has to be scheduled and run its
            # handler before a loaded machine could make a SIGKILL look like a missing SIGCONT.
            terminate_or_kill(proc, timeout=10.0)
            self.assertIsNotNone(proc.poll(), 'the stopped child outlived its teardown')
            self.assertEqual(0, proc.returncode,
                             'the stopped child did not exit through its SIGTERM handler, so '
                             'teardown never continued it')
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdout.close()

    def test_suspend_and_resume_actually_stop_and_start_a_real_child(self):
        # Proves the two helpers do what they claim on this OS rather than only recording
        # calls on a fake: a stopped child writes nothing, and picks up again on resume.
        path = os.path.join(os.environ.get('TMPDIR', '/tmp'), f'pause_probe_{os.getpid()}.txt')
        proc = subprocess.Popen(
            [sys.executable, '-c',
             'import sys, time\n'
             'fh = open(sys.argv[1], "w")\n'
             'while True:\n'
             '    fh.write("x"); fh.flush(); time.sleep(0.01)\n', path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # Bounded polls rather than fixed sleeps on both sides: a child slow to open its
            # file made the first read raise FileNotFoundError instead of failing with a
            # message, and half a second was not enough to prove a resumed child had picked
            # back up on a loaded machine (dev/changelog/983). The unchanged-size check
            # between them stays a fixed wait - absence cannot be polled for.
            self.assertTrue(_wait_until(lambda: _size(path) > 0, 10),
                            'the child never started writing')
            self.assertTrue(suspend_process(proc))
            time.sleep(0.2)  # let any write already in flight land
            frozen = os.path.getsize(path)
            time.sleep(0.5)
            self.assertEqual(frozen, os.path.getsize(path),
                             'a suspended child kept working')
            self.assertTrue(resume_process(proc))
            self.assertTrue(_wait_until(lambda: _size(path) > frozen, 10),
                            'a resumed child did not pick back up')
        finally:
            proc.kill()
            proc.wait(timeout=5)
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_the_helpers_are_a_no_op_on_a_process_that_is_already_gone(self):
        proc = subprocess.Popen([sys.executable, '-c', 'pass'])
        proc.wait(timeout=10)
        self.assertFalse(suspend_process(proc))
        self.assertFalse(resume_process(proc))
        self.assertFalse(suspend_process(None))
        self.assertFalse(resume_process(None))


# ── (d) the row records that it is parked ─────────────────────────────────────
class WaitingStampTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status=REC_STATUS_CONVERTING, name='conv')
        db.session.commit()
        self.rid = self.rec.id
        self.out = os.path.join(self.t._tmpdir, f'out_{self.rid}.mkv')

    def tearDown(self):
        with _active_lock:
            _active_conversions.clear()
        self.t.cleanup()

    def _run_conversion(self, child, blocker_clears_after=None):
        """run_conversion_supervised over a fake child, with the collision cleared partway
        through when blocker_clears_after is set."""
        calls = {'n': 0}
        blocker = seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='blocker')
        db.session.commit()
        real_conflict = ppmod._conversion_collision_conflict

        def _conflict(within, exclude_recording_id):
            calls['n'] += 1
            if blocker_clears_after is not None and calls['n'] > blocker_clears_after:
                return None
            return real_conflict(within, exclude_recording_id)

        with mock.patch.object(pumod.subprocess, 'Popen', return_value=child), \
             mock.patch.object(pumod, 'read_progress_tail',
                               side_effect=lambda p: (5_000_000, 1000, False)), \
             mock.patch.object(ppmod, '_conversion_collision_conflict', _conflict):
            result = run_conversion_supervised(
                self.t.app, self.rid, ['/usr/bin/ffmpeg', '-i', 'a.ts', self.out], self.out,
                expected_duration=100, pre_output_timeout=30, interval=0.02,
                stall_seconds=0, collision_policy='cancel', collision_multiplier=1.0)
        db.session.expire_all()
        self.assertIsNotNone(blocker.id)
        return result

    def _events(self, kind):
        return [e for e in RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=kind).all()]

    def test_a_suspended_conversion_stamps_the_row_and_says_so(self):
        child = _FakeChild(alive_ticks=8)
        self._run_conversion(child, blocker_clears_after=3)
        self.assertEqual(1, len(self._events(CONVERSION_YIELDED)))
        self.assertEqual(1, len(self._events(CONVERSION_RESUMED)))

    def test_the_stamp_is_cleared_when_the_conversion_resumes(self):
        child = _FakeChild(alive_ticks=10)
        self._run_conversion(child, blocker_clears_after=3)
        rec = db.session.get(Recording, self.rid)
        self.assertIsNone(rec.postprocess_waiting_since,
                          'a resumed conversion still claims to be waiting')

    def test_a_run_that_ends_while_suspended_clears_the_stamp(self):
        # The cancel/shutdown path: the child is killed while stopped, so on_resume never
        # fires. A stamp left here outlives everything that could clear it, and the restart
        # guard would read a working recording as idle for the rest of its life.
        child = _FakeChild(alive_ticks=3, returncode=-9)
        self._run_conversion(child)  # blocker never clears
        rec = db.session.get(Recording, self.rid)
        self.assertIsNone(rec.postprocess_waiting_since)

    def test_a_yield_never_spends_the_restart_budget(self):
        child = _FakeChild(alive_ticks=8)
        self._run_conversion(child, blocker_clears_after=3)
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(0, rec.conversion_attempts or 0, 'a yield was counted as a restart')
        self.assertEqual([], self._events(CONVERSION_RESTARTED))


class PrestartWaitStampTests(unittest.TestCase):
    """The other yield site: parked before any ffmpeg exists, so there is nothing to
    suspend - but the row must still say it is waiting, and stop saying so afterwards."""

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
        self.t.cleanup()

    def _config(self):
        return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {
            'gather_health_data': False,
            'move_on_complete': {'enabled': False},
            'post_script': {'enabled': False},
            'post_process': {'enabled': True, 'format': 'mkv', 'delete_source': False,
                             'reencode_mode': 'never', 'pre_output_timeout_seconds': 60,
                             'auto_restart': True, 'max_restart_attempts': 3,
                             'stall_seconds': 0, 'progress_interval_seconds': 5,
                             'collision_policy': 'cancel',
                             'collision_lookahead_multiplier': 1.0},
        }})

    def test_the_row_is_stamped_while_it_waits_and_cleared_after(self):
        soon = datetime.utcnow() + timedelta(seconds=10)
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='soon',
                            start_time=soon, stop_time=soon + timedelta(hours=1))
        db.session.commit()
        seen = {}

        def _observe_wait(*a, **k):
            db.session.expire_all()
            seen['during'] = db.session.get(Recording, self.rid).postprocess_waiting_since

        from app.postprocessor import ConversionResult
        with mock.patch.object(cfgmod, 'load_config', return_value=self._config()), \
             mock.patch.object(ppmod, 'run_conversion_supervised',
                               return_value=ConversionResult(True, 'success')), \
             mock.patch.object(ppmod, '_wait_for_conversion_clear', side_effect=_observe_wait):
            do_postprocess(self.t.app, self.rid, self.ts)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertIsNotNone(seen.get('during'),
                             'the row never said it was waiting while it waited')
        self.assertIsNone(rec.postprocess_waiting_since,
                          'the row still claims to be waiting after the wait ended')
        kinds = [e.event_type for e in
                 RecordingEvent.query.filter_by(recording_id=self.rid).all()]
        self.assertIn(CONVERSION_YIELDED, kinds)
        self.assertIn(CONVERSION_RESUMED, kinds)


class StaleStampAtStartupTests(unittest.TestCase):
    """No parked chain survives a restart - the thread is gone and any suspended ffmpeg died
    with it - so every stamp present at startup is orphaned."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        self.t.cleanup()

    def test_startup_clears_a_stale_wait_stamp(self):
        from app.scheduler import resume_in_progress_recordings
        rec = seed.make_recording(status=REC_STATUS_ANALYZING, name='parked')
        rec.postprocess_waiting_since = datetime.utcnow() - timedelta(hours=3)
        rec.output_path = os.path.join(self.t._tmpdir, 'gone.ts')
        db.session.commit()
        rid = rec.id
        with mock.patch('app.concatenator.do_concatenation'):
            resume_in_progress_recordings(self.t.app)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, rid).postprocess_waiting_since)


# ── (e) the restart guard ─────────────────────────────────────────────────────
class RestartGuardParkedTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _check_busy(self):
        # In-process, like every other check_busy case; the one real spawn lives in
        # tests/test_restart_guard.py (dev/changelog/979).
        return run_check_busy(self.t.db_path)

    def test_a_parked_recording_does_not_block_a_restart(self):
        rec = seed.make_recording(status=REC_STATUS_CONVERTING, name='parked conv')
        rec.postprocess_waiting_since = datetime.utcnow()
        rec.conversion_progress_pct = 66.4
        db.session.commit()
        out = self._check_busy()
        self.assertEqual(0, out.returncode,
                         f'a parked recording refused a restart:\n{out.stdout}')
        self.assertNotIn('blocking-kinds:', out.stdout)

    def test_a_parked_recording_is_still_reported_with_what_a_restart_costs(self):
        rec = seed.make_recording(status=REC_STATUS_CONVERTING, name='parked conv')
        rec.postprocess_waiting_since = datetime.utcnow()
        rec.conversion_progress_pct = 66.4
        db.session.commit()
        out = self._check_busy()
        self.assertIn('parked waiting on another recording', out.stdout)
        self.assertIn('66%', out.stdout,
                      'said nothing about the encode a restart would discard')

    def test_a_parked_recording_with_no_ffmpeg_yet_says_so(self):
        rec = seed.make_recording(status=REC_STATUS_ANALYZING, name='parked pre-start')
        rec.postprocess_waiting_since = datetime.utcnow()
        db.session.commit()
        out = self._check_busy()
        self.assertEqual(0, out.returncode)
        self.assertIn('nothing is running for it yet', out.stdout)

    def test_a_working_conversion_still_blocks(self):
        rec = seed.make_recording(status=REC_STATUS_CONVERTING, name='working conv')
        rec.conversion_progress_pct = 40.0
        db.session.commit()
        out = self._check_busy()
        self.assertEqual(1, out.returncode, 'a live conversion no longer blocks a restart')
        self.assertIn('blocking-kinds: recordings', out.stdout)

    def test_the_parked_line_names_the_recording_it_waits_on(self):
        """Guards dev/docs/BUGS.md 2026-09-18 "The restart modal blocked on a parked
        recording" - the CLI half: the row carries the name since dev/changelog/954."""
        rec = seed.make_recording(status=REC_STATUS_ANALYZING, name='parked pre-start')
        rec.postprocess_waiting_since = datetime.utcnow()
        rec.postprocess_waiting_on_name = 'Live Match'
        db.session.commit()
        out = self._check_busy()
        self.assertIn('parked waiting on "Live Match"', out.stdout)
        self.assertNotIn('another recording', out.stdout)

    def test_a_parked_row_does_not_mask_a_working_one(self):
        parked = seed.make_recording(status=REC_STATUS_CONVERTING, name='parked conv')
        parked.postprocess_waiting_since = datetime.utcnow()
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='live capture')
        db.session.commit()
        out = self._check_busy()
        self.assertEqual(1, out.returncode)
        self.assertIn('live capture', out.stdout)
        self.assertIn('parked waiting on another recording', out.stdout)


if __name__ == '__main__':
    unittest.main()
