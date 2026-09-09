"""Tier 2 - SQLite lock-contention (Class A) regression suite (dev/changelog/268, chunk 4).

Pins the retry_on_locked machinery and the specific write paths whose lock-retry timing
caused shipped/near-shipped defects:

  * retry_on_locked core semantics: retry-on-locked, exponential backoff, re-raise of
    non-locked errors, re-raise after exhaustion, rollback_session on/off, and that a
    unit which succeeds is never re-run (BUGS.md 2026-07-11 06:56 PM - the fix's contract).
  * rollback_session=False leaves db.session pending state untouched - the mechanism behind
    the "retried scheduler call discarded staged job fields" bug (BUGS.md 2026-07-16 06:49 AM,
    11:41 AM).
  * POST /recordings/new-json under a lock on the *second* commit produces exactly one
    Recording row and at most one CREATED_AFTER_EVENT_START event - the duplicate-row bug
    the two-closure split fixed (BUGS.md 2026-07-11 07:03 PM).
  * APScheduler jobstore add/remove retries instead of 500-ing, and remove-of-missing returns
    False rather than raising (BUGS.md 2026-07-16 11:41 AM).

All drives are deterministic via tests/support/contention.py - no real concurrency.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.exc import OperationalError  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support.contention import CommitLockInjector, _locked_error  # noqa: E402
from app import db  # noqa: E402
from app.db_utils import retry_on_locked  # noqa: E402
from app.database import Recording, RecordingEvent, RECORDING_CREATED_AFTER_EVENT_START  # noqa: E402


def _noop_job():
    """Module-level so APScheduler's SQLAlchemyJobStore can serialize it by reference."""
    pass


class RetryOnLockedSemanticsTests(unittest.TestCase):
    """The decorator's contract, exercised against a real (temp) db.session."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_retries_locked_then_returns_value(self):
        calls = {'n': 0}

        @retry_on_locked(base_delay=0.001)
        def unit():
            calls['n'] += 1
            if calls['n'] < 3:
                raise _locked_error()
            return 'done'

        self.assertEqual(unit(), 'done')
        self.assertEqual(calls['n'], 3)  # 2 locked + 1 success - succeeded unit not re-run

    def test_non_locked_operationalerror_is_not_retried(self):
        calls = {'n': 0}

        @retry_on_locked(base_delay=0.001)
        def unit():
            calls['n'] += 1
            raise OperationalError('SELECT', {}, Exception('no such table: nope'))

        with self.assertRaises(OperationalError):
            unit()
        self.assertEqual(calls['n'], 1)  # re-raised immediately, never retried

    def test_reraises_after_max_attempts(self):
        calls = {'n': 0}

        @retry_on_locked(max_attempts=3, base_delay=0.001)
        def unit():
            calls['n'] += 1
            raise _locked_error()

        with self.assertRaises(OperationalError):
            unit()
        self.assertEqual(calls['n'], 3)  # tried exactly max_attempts times, then gave up

    def test_backoff_delays_grow_exponentially(self):
        import app.db_utils as dbu
        recorded = []
        real_sleep = dbu.time.sleep
        dbu.time.sleep = lambda d: recorded.append(d)
        try:
            @retry_on_locked(max_attempts=4, base_delay=0.1)
            def unit():
                raise _locked_error()

            with self.assertRaises(OperationalError):
                unit()
        finally:
            dbu.time.sleep = real_sleep
        # 3 sleeps between 4 attempts, doubling each time.
        self.assertEqual(recorded, [0.1, 0.2, 0.4])

    def test_rollback_session_true_rolls_back_between_attempts(self):
        rolls = {'n': 0}
        real_rollback = db.session.rollback
        db.session.rollback = lambda: (rolls.__setitem__('n', rolls['n'] + 1),
                                       real_rollback())[1]
        try:
            calls = {'n': 0}

            @retry_on_locked(base_delay=0.001, rollback_session=True)
            def unit():
                calls['n'] += 1
                if calls['n'] < 2:
                    raise _locked_error()
                return 'ok'

            self.assertEqual(unit(), 'ok')
        finally:
            db.session.rollback = real_rollback
        self.assertEqual(rolls['n'], 1)  # one rollback before the single retry

    def test_rollback_session_false_never_touches_session(self):
        """The 2026-07-16 bug mechanism: a jobstore retry must NOT roll back db.session,
        or it discards the caller's staged (uncommitted) changes."""
        rolls = {'n': 0}
        real_rollback = db.session.rollback
        db.session.rollback = lambda: rolls.__setitem__('n', rolls['n'] + 1)
        try:
            calls = {'n': 0}

            @retry_on_locked(base_delay=0.001, rollback_session=False)
            def unit():
                calls['n'] += 1
                if calls['n'] < 3:
                    raise _locked_error()
                return 'ok'

            self.assertEqual(unit(), 'ok')
        finally:
            db.session.rollback = real_rollback
        self.assertEqual(rolls['n'], 0)  # session never rolled back despite two retries


class CreateRecordingContentionTests(unittest.TestCase):
    """POST /recordings/new-json: a lock on the second commit must not duplicate the
    Recording row (BUGS.md 2026-07-11 07:03 PM)."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        # This suite targets DB behavior, not CSRF; disable the token check so the POST
        # exercises the route body directly.
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        # The POST below deliberately uses a start_time in the past, so the live
        # scheduler fires the start job immediately and start_recording() spawns a real
        # ffmpeg against the fake URL - a network call the suite must never make, plus a
        # leaked WatchdogThread that outlives this test. Capture is not what is under
        # test here (the second commit's lock behavior is), so stub it out. The scheduler
        # job imports start_recording *inside* the function, so this binding is the one
        # it resolves.
        import app.recorder as recorder
        self._real_start_recording = recorder.start_recording
        recorder.start_recording = lambda *a, **kw: None

    def tearDown(self):
        import app.recorder as recorder
        # cleanup() FIRST, restore second: the scheduler fires the start job on a worker
        # thread at a time we don't control, and restoring before the scheduler is shut
        # down leaves a window where the job resolves the real start_recording and spawns
        # ffmpeg after all. Observed - it is what put a blocked network spawn on a later
        # test's cleanup. cleanup() shuts the scheduler down, so after it the job cannot
        # run at all.
        try:
            self.t.cleanup()
        finally:
            recorder.start_recording = self._real_start_recording

    def _post_past_start(self):
        # start_time a full day in the past (in display-local terms - the form parses
        # local and converts to UTC) → created_after_start → the SECOND commit (the
        # CREATED_AFTER_EVENT_START event) fires, which is the one we lock. A day's
        # margin keeps it past regardless of the display-tz UTC offset.
        start = datetime.now() - timedelta(days=1)
        stop = datetime.now() + timedelta(hours=1)
        return self.t.client.post('/recordings/new-json', data={
            'name': 'contention_rec',
            'url': 'http://example.test/live/9',
            'start_time': start.strftime('%Y-%m-%dT%H:%M'),
            'stop_time': stop.strftime('%Y-%m-%dT%H:%M'),
        })

    def test_lock_on_second_commit_yields_exactly_one_row(self):
        with CommitLockInjector([2]) as inj:
            resp = self._post_past_start()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertGreaterEqual(inj.raised, 1, 'injector never fired - no 2nd commit happened')

        recs = Recording.query.filter_by(name='contention_rec').all()
        self.assertEqual(len(recs), 1, f'expected exactly one Recording, got {len(recs)}')

        events = RecordingEvent.query.filter_by(
            recording_id=recs[0].id,
            event_type=RECORDING_CREATED_AFTER_EVENT_START).all()
        self.assertLessEqual(len(events), 1, 'duplicate created-after-start event rows')


class JobstoreContentionTests(unittest.TestCase):
    """APScheduler jobstore writes retry on lock instead of 500-ing (BUGS.md 2026-07-16
    11:41 AM). Needs the live scheduler against the temp DB."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        import app.scheduler as sched
        self.sched = sched

    def tearDown(self):
        self.t.cleanup()

    def _future(self):
        return datetime.utcnow() + timedelta(days=3650)

    def test_add_job_retries_on_lock(self):
        real_add = self.sched._scheduler.add_job
        calls = {'n': 0}

        def flaky(*a, **k):
            calls['n'] += 1
            if calls['n'] == 1:
                raise _locked_error()
            return real_add(*a, **k)

        self.sched._scheduler.add_job = flaky
        try:
            self.sched._add_job(func=_noop_job, trigger='date', run_date=self._future(),
                                id='ctest_add', replace_existing=True)
            self.assertEqual(calls['n'], 2)  # one lock, one success - no exception propagated
            self.assertIsNotNone(self.sched._scheduler.get_job('ctest_add'))
        finally:
            self.sched._scheduler.add_job = real_add
            # Leave no job row behind even if an assertion above fails - a persisted job
            # referencing this test module is what poisoned the jobstore historically.
            self.sched.remove_job_if_exists('ctest_add')

    def test_remove_job_if_exists_missing_returns_false(self):
        self.assertFalse(self.sched.remove_job_if_exists('nonexistent-job-id'))

    def test_remove_job_retries_on_lock(self):
        self.sched._add_job(func=_noop_job, trigger='date', run_date=self._future(),
                            id='ctest_rm', replace_existing=True)
        real_remove = self.sched._scheduler.remove_job
        calls = {'n': 0}

        def flaky(job_id, *a, **k):
            calls['n'] += 1
            if calls['n'] == 1:
                raise _locked_error()
            return real_remove(job_id, *a, **k)

        self.sched._scheduler.remove_job = flaky
        try:
            self.assertTrue(self.sched.remove_job_if_exists('ctest_rm'))
        finally:
            self.sched._scheduler.remove_job = real_remove
            self.sched.remove_job_if_exists('ctest_rm')  # no row left behind if the assert failed
        self.assertEqual(calls['n'], 2)
        self.assertIsNone(self.sched._scheduler.get_job('ctest_rm'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
