"""Tier 2 - the concat's ffmpeg is reachable from tracked state and dies at shutdown
(dev/changelog/986, dev/docs/BUGS.md 2026-09-16).

The defect: `_run_concatenation()` ran its join through `supervise_ffmpeg()` with no
`on_spawn` hook, so the child lived only in that stack frame. `run.py`'s signal handler
called `kill_all_active()` and `kill_active_conversions()`, and neither knew the join
existed. A SIGTERM mid-join therefore orphaned an ffmpeg that kept writing `output_path`
for the length of the join, while the next process's startup CONCATENATING sweep started a
*second* join beside it - `reserve_concat_output_path()` saw the orphan's growing file and
landed on a `_2` name, and the segments were deleted on the new join's success while the
orphan was still reading them. The leftover was a multi-gigabyte file that
`recording_disk_paths()` never lists, so nothing would ever have removed it.
`restart.sh`'s busy guard was the only thing in the way, and a crash, an OOM kill or a host
shutdown does not consult it.

Covers, in order:
  - JoinChildIsTrackedTests: the registry holds the live child and its output path for the
    length of the join, and is empty again on every exit - success, ffmpeg failure, and an
    exception out of the supervisor.
  - ShutdownKillsTheJoinTests: kill_active_joins() over real child processes - every one of
    them, a SIGTERM-ignoring one included, plus the partial output each was writing.
  - ShutdownHandlerWiringTests: run.py's signal handler actually calls it. Registering a
    child that no terminal path reaches is the defect restated, not a fix for it.
  - SupervisedJoinShutdownTests: the item's own reproduction end to end - a real
    _run_concatenation against a real slow local child, killed through the shutdown hook.

No network anywhere, per CLAUDE.md §Testing: the "ffmpeg" is a local python child writing
to a local file. Run standalone:
  python3 -m unittest tests.test_concat_shutdown_teardown
"""
import ast
import contextlib
import os
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.concatenator as catmod  # noqa: E402
import app.proc_utils as pumod  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import RecordingSegment  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sleeper():
    """A child that outlives the test unless something kills it."""
    return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _sigterm_ignoring_sleeper():
    """A join that refuses SIGTERM - what forces the escalation to SIGKILL. ffmpeg can sit
    in a flush and ignore a terminate, and an ignored one at shutdown is precisely the
    orphan this teardown exists to prevent."""
    return subprocess.Popen(
        [sys.executable, '-c',
         'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
         'time.sleep(120)'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _slow_writer(path, popen=None):
    """A stand-in for the join ffmpeg: writes into the output file for minutes, the way a
    `-c copy` over a multi-gigabyte capture does. Local file only - no input URL, nothing
    the netguard would have to stop.

    `popen` is the real constructor, saved before the supervisor's own Popen is patched:
    the patch replaces the attribute on the shared subprocess module, so spawning this
    child through the module would re-enter the very side effect that spawns it.
    """
    return (popen or subprocess.Popen)(
        [sys.executable, '-c',
         'import sys, time\n'
         'p = sys.argv[1]\n'
         'for _ in range(2400):\n'
         '    fh = open(p, "ab")\n'
         '    fh.write(b"\\x00" * 4096)\n'
         '    fh.close()\n'
         '    time.sleep(0.05)\n',
         path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _assert_dead(case, proc, msg, timeout=10):
    """Bounded wait, not a bare poll(): a child that was never signalled sleeps for two
    minutes, so an exhausted deadline is a real failure rather than a slow machine."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.05)
    case.assertIsNotNone(proc.poll(), msg)


class _ConcatCase(unittest.TestCase):
    """One recording with two joinable segments on disk, ready for _run_concatenation."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        rec = seed.make_recording(status='IN_PROGRESS', name='Long Race')
        db.session.commit()
        self.rid = rec.id
        self.segs = []
        for n in (1, 2):
            path = os.path.join(self.dvr, f'seg_{self.rid}_{n}.ts')
            with open(path, 'wb') as fh:
                fh.write(b'\x00' * 4096)
            self.segs.append(path)
            db.session.add(RecordingSegment(
                recording_id=self.rid, segment_number=n, file_path=path,
                started_at=datetime.utcnow(), exit_reason='STOP_TIME_REACHED',
                bytes_recorded=4096))
        db.session.commit()

        self.cfg = load_config()
        self.cfg['recording']['dvr_output_dir'] = self.dvr
        self.cfg['recording']['post_process']['enabled'] = False
        self.cfg['recording']['move_on_complete']['enabled'] = False
        self.addCleanup(catmod._untrack_join_child, self.rid)

    def tearDown(self):
        self.t.cleanup()

    def _patches(self):
        return (
            mock.patch('app.config.load_config', return_value=self.cfg),
            mock.patch('app.recorder.persist_final_thumbnail'),
            # Both of the join's image grabs, not just the thumbnail: each one spawns a
            # real ffmpeg against a fake segment file, and the second one left the join
            # thread alive past the end of the test (dev/changelog/1060).
            mock.patch('app.recorder.persist_poster_frame'),
            mock.patch('app.health_score.apply_capture_phase_health_observation'),
            mock.patch.object(catmod, '_measure_segment_content_durations'),
        )


class JoinChildIsTrackedTests(_ConcatCase):
    """The registry is what every terminal path outside the join's own thread reaches
    through, so what matters is that it holds the child while ffmpeg is alive and holds
    nothing once it is not."""

    def _run_with(self, supervise):
        """Drive _run_concatenation with `supervise` standing in for the supervisor, and
        report what the registry held at the moment on_spawn fired."""
        seen = {}

        def _fake_supervise(cmd, output_path, **kwargs):
            seen['path'] = output_path
            on_spawn = kwargs.get('on_spawn')
            if on_spawn is not None:
                proc = _sleeper()
                self.addCleanup(proc.kill)
                seen['proc'] = proc
                on_spawn(proc)
            with catmod._active_join_procs_lock:
                seen['during'] = dict(catmod._active_join_procs)
            return supervise(output_path)

        ctxs = self._patches() + (
            mock.patch.object(catmod, 'supervise_ffmpeg', side_effect=_fake_supervise),)
        # Entered through an ExitStack rather than by index: a `with ctxs[0] .. ctxs[n]`
        # line silently drops the tail when _patches() grows, and the patch it dropped was
        # the supervisor stub, so the real one ran and the test failed somewhere else
        # entirely (dev/changelog/1060).
        with contextlib.ExitStack() as stack:
            for ctx in ctxs:
                stack.enter_context(ctx)
            catmod._run_concatenation(self.t.app, self.rid, reason='test')
        return seen

    def test_the_live_join_is_reachable_from_the_registry(self):
        """The whole defect: without this the child exists only in _run_concatenation's
        stack frame and no shutdown path can find it."""
        seen = self._run_with(lambda path: pumod.SupervisedRun('success', returncode=0))

        self.assertIn(self.rid, seen['during'],
                      'the join ffmpeg was never registered - nothing outside its own '
                      'thread can reach it')
        proc, out = seen['during'][self.rid]
        self.assertIs(proc, seen['proc'], 'the registry holds a different process')
        self.assertEqual(out, seen['path'],
                         'the registry does not name the file the join is writing, so a '
                         'shutdown cannot remove the partial')

    def test_a_successful_join_leaves_nothing_registered(self):
        """A stale entry would let a later shutdown unlink an output the success path has
        already committed to the row."""
        self._run_with(lambda path: pumod.SupervisedRun('success', returncode=0))

        self.assertNotIn(self.rid, catmod._active_join_procs,
                         'a finished join stayed in the registry')

    def test_a_failed_join_leaves_nothing_registered(self):
        self._run_with(lambda path: pumod.SupervisedRun(
            'stalled', error_msg='No concat progress for 300s'))

        self.assertNotIn(self.rid, catmod._active_join_procs,
                         'a failed join stayed in the registry')

    def test_an_exception_out_of_the_supervisor_leaves_nothing_registered(self):
        """CLAUDE.md teardown-releases-everything: every path out, not just the two that
        return a SupervisedRun. _run_concatenation catches this one itself."""
        def _boom(path):
            raise RuntimeError('ffmpeg blew up')

        self._run_with(_boom)

        self.assertNotIn(self.rid, catmod._active_join_procs,
                         'a join that raised stayed in the registry')


class ShutdownKillsTheJoinTests(unittest.TestCase):
    """kill_active_joins() - the shutdown/restart terminal path, run from run.py's signal
    handler. It deliberately does no DB work (a signal handler cannot); what it owes is
    entirely about what outlives the process."""

    def setUp(self):
        self.t = make_test_app()
        self.tracked = []
        self.addCleanup(self._kill_leftovers)
        self.addCleanup(catmod._active_join_procs.clear)

    def tearDown(self):
        self.t.cleanup()

    def _kill_leftovers(self):
        for proc in self.tracked:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def _live_join(self, rid, proc, *, partial_bytes=8192):
        """What _run_concatenation's on_spawn hook leaves behind: a tracked child and the
        partial output it is writing."""
        out = os.path.join(self.t._tmpdir, f'join_{rid}.ts')
        with open(out, 'wb') as fh:
            fh.write(b'\x00' * partial_bytes)
        catmod._track_join_child(rid, proc, out)
        self.tracked.append(proc)
        return out

    def test_every_tracked_join_is_killed_not_just_the_first(self):
        """Two recordings can be joining at once (serialize_concat is off by default), and
        a loop collapsed to the first entry leaves the other's ffmpeg writing after the
        process that owned it is gone."""
        self._live_join(1, _sleeper())
        self._live_join(2, _sleeper())

        catmod.kill_active_joins()

        for proc in self.tracked:
            _assert_dead(self, proc, 'a tracked join kept its ffmpeg child after shutdown')

    def test_a_join_that_ignores_sigterm_is_killed_anyway(self):
        """An ignored SIGTERM is exactly the orphan this exists to prevent, so the
        teardown must escalate rather than trust the terminate."""
        self._live_join(1, _sigterm_ignoring_sleeper())

        catmod.kill_active_joins()

        _assert_dead(self, self.tracked[0],
                     'a join that ignored SIGTERM survived the shutdown')

    def test_the_partial_output_is_removed(self):
        """Unlike a conversion, a join keeps no checkpoint and always re-runs from the top,
        so its half-written output is referenced by nothing - and left behind it would push
        every future attempt at this recording onto a `_2` name, permanently."""
        out = self._live_join(1, _sleeper())

        catmod.kill_active_joins()

        self.assertFalse(os.path.exists(out),
                         'the killed join left its partial output behind')

    def test_the_registry_is_empty_afterwards(self):
        self._live_join(1, _sleeper())
        self._live_join(2, _sleeper())

        catmod.kill_active_joins()

        self.assertEqual(catmod._active_join_procs, {},
                         'the shutdown left entries pointing at dead processes')

    def test_a_missing_partial_is_not_an_error(self):
        """The success path unregisters before it commits, but a half-open race or a file
        already removed elsewhere must not stop the remaining joins being killed."""
        out = self._live_join(1, _sleeper())
        os.unlink(out)
        self._live_join(2, _sleeper())

        catmod.kill_active_joins()

        for proc in self.tracked:
            _assert_dead(self, proc, 'a missing partial stopped the teardown')

    def test_nothing_registered_is_a_no_op(self):
        catmod.kill_active_joins()

        self.assertEqual(catmod._active_join_procs, {})


class ShutdownHandlerWiringTests(unittest.TestCase):
    """Registering the child in a registry nothing reaches is the defect restated. Read
    statically because run.py builds the real app at import time."""

    def _handler_calls(self):
        with open(os.path.join(_REPO_ROOT, 'run.py')) as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_handle_shutdown':
                return {n.func.id for n in ast.walk(node)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.fail('run.py has no _handle_shutdown')

    def test_the_signal_handler_kills_live_joins(self):
        self.assertIn('kill_active_joins', self._handler_calls(),
                      "run.py's shutdown handler does not kill live concat joins")

    def test_the_other_two_teardowns_are_still_wired(self):
        """The join is an addition, not a replacement - a capture and a conversion are
        different children on different registries."""
        calls = self._handler_calls()
        self.assertIn('kill_all_active', calls)
        self.assertIn('kill_active_conversions', calls)


class SupervisedJoinShutdownTests(_ConcatCase):
    """The item's own reproduction: a real supervised concat against a slow local child,
    with the shutdown hook called mid-join.

    The join runs on its own thread, and that thread MUST NOT outlive the test. It holds a
    `mock.patch` on app.config.load_config, and mock.patch is process-global rather than
    thread-local, so a worker still inside that patch when the NEXT test calls
    make_test_app() hands create_app() this test's cfg - whose database.path is the real
    one - and the next test app comes up bound to the production dvr.db and writes rows
    into it. Measured while this file was being written, not theorized: three junk
    recordings reached the live database exactly that way, every one of them from a run in
    which one of these two tests failed early and left its worker running.
    `_join_worker()` below is what closes it, on every path.
    """

    def setUp(self):
        self._worker = None
        self._children = []
        super().setUp()

    def tearDown(self):
        # Before _ConcatCase's tearDown, not after and not as an addCleanup: unittest runs
        # tearDown ahead of every cleanup handler, so the thread has to be gone before the
        # test app it is working against is taken apart. The finally is what keeps a hung
        # worker from also leaking the app.
        try:
            self._join_worker()
        finally:
            super().tearDown()

    def _join_worker(self):
        """Kill the join's child so the supervisor returns, then wait for the thread."""
        for proc in self._children:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        if self._worker is not None:
            self._worker.join(timeout=60)
            self.assertFalse(
                self._worker.is_alive(),
                'the join thread outlived the test - it still holds the load_config patch, '
                'so the next test app would bind to the production database')

    def test_a_shutdown_mid_join_leaves_no_orphan_and_no_partial(self):
        started = threading.Event()
        children = self._children
        real_popen = pumod.subprocess.Popen

        def _fake_popen(cmd, **kwargs):
            # supervise_ffmpeg's own Popen, replaced by a local writer. The output path is
            # the last argument of the command it built.
            proc = _slow_writer(cmd[-1], popen=real_popen)
            children.append(proc)
            started.set()
            return proc

        def _join():
            with mock.patch('app.config.load_config', return_value=self.cfg), \
                 mock.patch('app.recorder.persist_final_thumbnail'), \
                 mock.patch('app.recorder.persist_poster_frame'), \
                 mock.patch('app.health_score.apply_capture_phase_health_observation'), \
                 mock.patch.object(catmod, '_measure_segment_content_durations'), \
                 mock.patch.object(pumod.subprocess, 'Popen', side_effect=_fake_popen):
                catmod._run_concatenation(self.t.app, self.rid, reason='test')

        self._worker = threading.Thread(target=_join, daemon=True)
        self._worker.start()
        self.assertTrue(started.wait(timeout=20), 'the join never spawned a child')

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.rid not in catmod._active_join_procs:
            time.sleep(0.05)
        with catmod._active_join_procs_lock:
            entry = catmod._active_join_procs.get(self.rid)
        self.assertIsNotNone(entry, 'the live join was not reachable from tracked state')
        out = entry[1]

        catmod.kill_active_joins()

        _assert_dead(self, children[0],
                     'the shutdown left an orphaned ffmpeg writing the join output')
        self.assertFalse(os.path.exists(out),
                         'the shutdown left the partial join output on disk, where it '
                         'pushes the next attempt onto a `_2` name')
        self._join_worker()

    def test_the_segments_survive_a_shutdown_mid_join(self):
        """The premise the partial's removal rests on: they are deleted on the join's
        success only, so a killed join can never be the sole copy of the capture."""
        self.test_a_shutdown_mid_join_leaves_no_orphan_and_no_partial()

        for path in self.segs:
            self.assertTrue(os.path.exists(path),
                            f'a shutdown mid-join destroyed segment {path}')


if __name__ == '__main__':
    unittest.main()
