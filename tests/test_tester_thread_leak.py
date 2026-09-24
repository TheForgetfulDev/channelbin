"""Background work must not outlive the test that started it.

Guards dev/docs/BUGS.md 2026-09-17 @ 05:17:19 PM ET: a channel-test run started during a
test kept going after that test finished, spawning ffmpeg at the seeded stream URL once
per channel with a wait in between, so netguard blocked the spawn and charged the
violation to whichever unrelated test happened to be tearing down at that moment. The
suite went red on test_finalize_on_demand_job_status three separate times with nothing
wrong in that module at all.

Two holes let the run out, and both are covered here:
  * TestApp.cleanup() never asked a live tester run to stop, and shut the scheduler down
    with wait=False - so a job already handed to a worker kept running past teardown,
    past the netguard drain, and into the next test.
  * reset_module_globals() swapped in a fresh RunState on the way into the next test.
    _run_channel_loop re-reads that module global every iteration, so the swap handed a
    still-running loop stop_requested=False and made it unstoppable for the rest of the
    process, while hiding it from is_running().

Runs against a throwaway temp SQLite DB - never the live dvr.db.
    python3 -m unittest tests.test_tester_thread_leak
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import app as appsupport  # noqa: E402
from tests.support.app import make_test_app, reset_module_globals  # noqa: E402

# Coordination for the scheduler-drain case. Module level because APScheduler pickles a
# textual reference to the job's callable into the jobstore and cannot serialize a closure.
_job_started = threading.Event()
_cleanup_returned = threading.Event()
_job_saw_cleanup_already_returned = []


def _slow_scheduler_job():
    _job_started.set()
    time.sleep(0.4)
    _job_saw_cleanup_already_returned.append(_cleanup_returned.is_set())


class _FakeRun:
    """Stands in for a channel-test run without touching ffmpeg, the network or the DB.

    Mirrors the two things about _run_channel_loop that make a leaked run dangerous: it
    holds channel_tester's real is_running() flag up, and it notices stop_requested only
    between iterations (_interruptible_sleep's 1-second granularity, compressed here).

    honor_stop=False is the stuck run - work that teardown asks to stop and that does not.
    """

    def __init__(self, honor_stop=True):
        import app.channel_tester as channel_tester
        self.channel_tester = channel_tester
        self.honor_stop = honor_stop
        self.iterations = 0
        self.release = threading.Event()
        self.finished = threading.Event()

    def start(self):
        from datetime import datetime
        ct = self.channel_tester
        with ct._lock:
            ct._state.is_running = True
            # Stamped because that is what _reset_run_state() does, and what tells a run in
            # flight apart from the bare is_running flag several tests set as a fixture.
            ct._state.run_started_at = datetime.utcnow()
            ct._state.run_kind = 'job'
            ct._state.total_channels = 50
            ct._state.current_phase = 'testing'
            ct._state.current_channel_name = 'Leaky Channel'
        threading.Thread(target=self._loop, name='od-test-job-fake', daemon=True).start()
        return self

    def _loop(self):
        ct = self.channel_tester
        try:
            while not self.release.is_set():
                with ct._lock:
                    # Deliberately the module global, not a captured reference - that is
                    # what makes a RunState swap able to orphan a real run.
                    stop = ct._state.stop_requested
                if stop and self.honor_stop:
                    return
                self.iterations += 1
                time.sleep(0.005)
        finally:
            with ct._lock:
                ct._state.clear()
            self.finished.set()

    def abandon(self):
        """Let the loop go no matter what, so a failing assertion cannot leak it either."""
        self.release.set()
        self.finished.wait(timeout=5)


class TesterRunIsStoppedByTeardownTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.run = _FakeRun().start()

    def tearDown(self):
        self.run.abandon()
        if self.t is not None:
            self.t.cleanup()

    def test_cleanup_stops_a_run_that_is_still_going(self):
        import app.channel_tester as channel_tester
        self.assertTrue(channel_tester.is_running(),
                        'precondition: the fake run must be live before cleanup()')

        t, self.t = self.t, None
        with self.assertRaises(AssertionError):
            t.cleanup()

        self.assertFalse(channel_tester.is_running(),
                         'cleanup() must stop a channel-test run that outlived its test')
        self.assertTrue(self.run.finished.is_set(),
                        'cleanup() must wait for the run to unwind, not just ask it to stop')

    def test_no_work_lands_after_cleanup_returns(self):
        """The observable consequence. That window is where a real run spawns its next
        ffmpeg, and where netguard then charges the violation to an innocent test."""
        t, self.t = self.t, None
        with self.assertRaises(AssertionError):
            t.cleanup()

        settled = self.run.iterations
        time.sleep(0.2)
        self.assertEqual(self.run.iterations, settled,
                         'the run was still doing work after its test finished')

    def test_a_run_that_stops_cleanly_is_still_reported(self):
        """Repairing the state is not the same as saying what happened. A leak that
        teardown silently absorbs leaves the test that caused it looking innocent, which
        is how the original went three reports deep without ever being located."""
        t, self.t = self.t, None
        with self.assertRaises(AssertionError) as caught:
            t.cleanup()

        msg = str(caught.exception)
        self.assertIn('still going when its test ended', msg)
        self.assertIn('Leaky Channel', msg,
                      'the message must name the run, or it cannot be acted on')


class StuckRunIsNamedNotSwallowedTests(unittest.TestCase):
    """Work that will not stop is reported, not cleaned up quietly.

    Teardown could stop this work and say nothing, and the suite would stay green while a
    test kept handing its work to the next one. The failure has to land on the test that
    started it.
    """

    def setUp(self):
        self.t = make_test_app()
        self.run = _FakeRun(honor_stop=False).start()
        self._real_ceiling = appsupport._TEARDOWN_DRAIN_SECONDS
        appsupport._TEARDOWN_DRAIN_SECONDS = 0.2

    def tearDown(self):
        appsupport._TEARDOWN_DRAIN_SECONDS = self._real_ceiling
        self.run.abandon()
        if self.t is not None:
            self.t.cleanup()

    def test_cleanup_fails_the_test_and_names_the_run(self):
        t, self.t = self.t, None
        with self.assertRaises(AssertionError) as caught:
            t.cleanup()

        msg = str(caught.exception)
        self.assertIn('outlived this test', msg)
        self.assertIn('did not stop within', msg,
                      'work that refused to stop must read differently from work teardown '
                      'successfully stopped - the two need different follow-up')
        self.assertIn('Leaky Channel', msg,
                      'the message must name the run, or it cannot be acted on')


class BareIsRunningFlagIsNotALeakTests(unittest.TestCase):
    """A hand-set is_running flag is a display fixture, not work in flight.

    Several tests set it directly to render the "health check running" state
    (test_dashboard_nav_count, test_tester_preemption). Nothing is looping behind it and
    nothing will ever clear it, so treating it as a leak means waiting out the full drain
    ceiling and then failing a test that leaked nothing - which is how this guard failed
    its own first full-suite run.
    """

    def setUp(self):
        self.t = make_test_app()
        self._real_ceiling = appsupport._TEARDOWN_DRAIN_SECONDS
        appsupport._TEARDOWN_DRAIN_SECONDS = 0.2

    def tearDown(self):
        appsupport._TEARDOWN_DRAIN_SECONDS = self._real_ceiling
        if self.t is not None:
            self.t.cleanup()

    def test_cleanup_passes_and_does_not_wait(self):
        """Decided on the exit path, not the clock: request_stop() is the one way into the
        drain wait, so it must never be called for a bare flag. The old elapsed < 0.2s
        check used the drain ceiling itself as its margin, so a slow runner failed a test
        about a flag (dev/docs/BUGS.md 2026-09-11 @ 05:41:46 AM, 2026-09-23 @ 09:08:47 PM).
        """
        import app.channel_tester as channel_tester
        channel_tester._state.is_running = True

        t, self.t = self.t, None
        with mock.patch.object(channel_tester, 'request_stop',
                               wraps=channel_tester.request_stop) as stop:
            t.cleanup()

        stop.assert_not_called()


class ResetModuleGlobalsStopsBeforeSwappingTests(unittest.TestCase):
    """reset_module_globals() must stop a live run before replacing _state.

    Swapping first is worse than doing nothing: the loop keeps reading the module global,
    so it sees a pristine stop_requested=False and can never be stopped again, while
    is_running() reports idle to everything that might have noticed.
    """

    def setUp(self):
        self.run = _FakeRun().start()

    def tearDown(self):
        self.run.abandon()

    def test_reset_stops_the_run_it_is_about_to_hide(self):
        self.assertFalse(self.run.finished.is_set())

        reset_module_globals()

        self.assertTrue(self.run.finished.is_set(),
                        'reset_module_globals() swapped RunState out from under a live '
                        'run instead of stopping it first - the run can no longer be '
                        'stopped by anything')


class SchedulerJobsAreDrainedBeforeTheNetguardCheckTests(unittest.TestCase):
    """A job already handed to an APScheduler worker must finish inside its own test.

    shutdown(wait=False) stops the dispatch loop but leaves a running worker alone, so
    before this the job's side effects - a blocked ffmpeg spawn among them - landed in
    whichever test came next.
    """

    def setUp(self):
        _job_started.clear()
        _cleanup_returned.clear()
        _job_saw_cleanup_already_returned.clear()
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        if self.t is not None:
            self.t.cleanup()

    def test_cleanup_waits_for_a_job_already_running_on_a_worker(self):
        from datetime import datetime
        from apscheduler.events import EVENT_JOB_REMOVED
        from app.scheduler import _add_job, get_scheduler

        # The loop thread removes a one-shot job right after handing it to a worker, and
        # shutdown() flips the state before taking the jobstore lock - so tearing down
        # the moment the worker starts had the loop's remove_job() raise JobLookupError
        # into a green run (dev/docs/BUGS.md 2026-09-23 09:51 PM). Waiting for the removal event
        # lets the bookkeeping finish before the store goes away.
        removed = threading.Event()

        def _on_removed(event):
            if event.job_id == 'drain_probe':
                removed.set()

        get_scheduler().add_listener(_on_removed, EVENT_JOB_REMOVED)
        _add_job(func=_slow_scheduler_job, trigger='date', run_date=datetime.utcnow(),
                 id='drain_probe', replace_existing=True)
        self.assertTrue(_job_started.wait(timeout=5),
                        'precondition: the job must have started on a worker')
        self.assertTrue(removed.wait(timeout=5),
                        'precondition: the loop must have finished its post-dispatch '
                        'bookkeeping before the store is torn down')

        t, self.t = self.t, None
        t.cleanup()
        _cleanup_returned.set()

        self.assertEqual(_job_saw_cleanup_already_returned, [False],
                         'the job finished after cleanup() returned - anything it did, '
                         'including a blocked network call, would be charged to the '
                         'next test')


if __name__ == '__main__':
    unittest.main()
