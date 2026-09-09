"""Tier 2 - database-axis admission control (app/admission.py, dev/changelog/679).

The defect: every mutual-avoidance rule between background jobs was shaped
`if other_actor.is_running(): defer`, with nothing holding the check and the start
together, so two jobs that checked within the same millisecond both saw "clear" and both
proceeded. Observed in a real container boot log - `02:23:45,298 Fetching M3U for account 1`
followed 4ms later by `02:23:45,302 Starting channel test run: 17 channels`, with a
`database is locked` traceback in the same pileup.

Three things are asserted here, in this order:

  1. **The shape that was replaced really does lose the race.** `CheckThenActRaceTests` is a
     demonstration, not a regression guard - it exercises a local reproduction of the old
     shape rather than any production code, so that the registry is measured against a
     defect that was shown rather than assumed.
  2. **The registry itself is atomic and its yield order is the documented one.**
  3. **The four actor types are actually wired to it**, including that a ticket comes back
     on the failure paths - a leaked ticket blocks its dependents for the life of the
     process, which is the one way this module can make things worse rather than better.
"""
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

from app import admission, db  # noqa: E402
import app.channel_tester as channel_tester  # noqa: E402
import app.scheduler as sched  # noqa: E402
import app.search_index as search_index  # noqa: E402


class _Held:
    """Hold a ticket for the duration of a `with` block, releasing it however the block ends."""

    def __init__(self, kind, label='held'):
        self.kind = kind
        self.label = label
        self.ticket = None

    def __enter__(self):
        self.ticket = admission.try_start(self.kind, self.label, force=True)
        return self.ticket

    def __exit__(self, *exc):
        admission.release(self.ticket)
        return False


class CheckThenActRaceTests(unittest.TestCase):
    """Characterization, NOT a regression guard: neither test here touches production code.
    They compare the two shapes head to head so the registry's reason for existing is
    demonstrated rather than asserted."""

    THREADS = 8

    def setUp(self):
        # Neither test here builds a TestApp, so reset_module_globals() never runs for
        # them and a ticket leaked by an earlier MODULE would otherwise refuse all eight
        # askers below - which reads as "the lock admitted nobody" rather than as the
        # isolation failure it is (dev/changelog/723).
        admission.reset_for_tests()

    def tearDown(self):
        admission.reset_for_tests()

    def test_the_check_then_act_shape_loses_the_race(self):
        """A separate check and register, exactly as the guards were written, admits more
        than one caller once they arrive together.

        The sleep is the point, not an artifact: in the real code the gap between the two
        steps held a DB commit, a tempfile create and a Popen, and the boot log measured it
        at 4ms. Without something standing in for that work the two statements are close
        enough to atomic under the GIL to hide the defect - which is exactly why this went
        unnoticed until a 1.5 GB database made everything in the window slower."""
        running = {'tester': False}
        admitted = []
        barrier = threading.Barrier(self.THREADS)

        def ask():
            barrier.wait()
            if running['tester']:           # ← the check
                return
            time.sleep(0.004)               # ← the window: commit, tempfile, Popen
            running['tester'] = True        # ← the register, a separate step
            admitted.append(1)

        threads = [threading.Thread(target=ask) for _ in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertGreater(len(admitted), 1,
                           'the shape being replaced is supposed to be racy - if this ever '
                           'admits exactly one, the reproduction stopped reproducing')

    def test_one_lock_spanning_both_steps_admits_exactly_one(self):
        admitted = []
        barrier = threading.Barrier(self.THREADS)

        def ask():
            barrier.wait()
            result = admission.try_start(admission.KIND_SYNC, 'racer')
            if result.granted:
                admitted.append(result)

        threads = [threading.Thread(target=ask) for _ in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(admitted), 1,
                         'check and register happen under one lock, so exactly one of N '
                         'simultaneous askers may proceed')


class YieldOrderTests(unittest.TestCase):
    """The documented doctrine, read straight off BLOCKED_BY: recordings are not clients at
    all (see the module docstring), the tester yields to nothing, sync yields to the tester
    and to another sync, rebuild yields to sync, maintenance yields to everything."""

    def tearDown(self):
        admission.reset_for_tests()

    def test_sync_is_refused_while_a_test_run_holds_the_axis(self):
        with _Held(admission.KIND_TESTER):
            result = admission.try_start(admission.KIND_SYNC, 'account 1')
        self.assertFalse(result.granted)
        self.assertEqual(result.blocked_by, admission.KIND_TESTER)

    def test_sync_is_refused_while_another_sync_holds_the_axis(self):
        with _Held(admission.KIND_SYNC, 'account 1'):
            result = admission.try_start(admission.KIND_SYNC, 'account 2')
        self.assertFalse(result.granted)

    def test_a_test_run_is_never_refused_by_a_sync(self):
        with _Held(admission.KIND_SYNC):
            result = admission.try_start(admission.KIND_TESTER, 'health check job 1')
        self.assertTrue(result.granted)

    def test_rebuild_yields_to_sync_but_not_to_maintenance(self):
        with _Held(admission.KIND_SYNC):
            self.assertFalse(admission.try_start(admission.KIND_REBUILD, 'janitor').granted)
        with _Held(admission.KIND_MAINTENANCE):
            self.assertTrue(admission.try_start(admission.KIND_REBUILD, 'janitor').granted)

    def test_rebuild_yields_to_another_rebuild(self):
        """Only the janitor asks rather than forces, and without this entry it would take a
        ticket and then park on _rebuild_lock for the length of a manual rebuild, to rebuild
        an index that had just been rebuilt (dev/changelog/680)."""
        with _Held(admission.KIND_REBUILD, 'manual rebuild from Maintenance'):
            result = admission.try_start(admission.KIND_REBUILD, 'index janitor')
            self.assertFalse(result.granted)
            self.assertEqual(result.blocked_by, admission.KIND_REBUILD)
            # The forcing callers - the sync close-out and the manual rebuild - are why this
            # edge is safe to add at all.
            self.assertTrue(
                admission.try_start(admission.KIND_REBUILD, 'account 3 sync', force=True).granted)

    def test_maintenance_yields_to_every_other_kind_including_itself(self):
        for blocker in (admission.KIND_TESTER, admission.KIND_SYNC,
                        admission.KIND_REBUILD, admission.KIND_MAINTENANCE):
            admission.reset_for_tests()
            with _Held(blocker):
                result = admission.try_start(admission.KIND_MAINTENANCE, 'retention')
            self.assertFalse(result.granted, f'maintenance must yield to {blocker}')

    def test_a_refusal_names_the_blocker_in_prose(self):
        """Product principle 1: a refused job has to be explainable, so the reason a caller
        hands to an alert must name what actually blocked it."""
        with _Held(admission.KIND_TESTER, 'health check job 4'):
            result = admission.try_start(admission.KIND_SYNC, 'account 1')
        self.assertIn('channel test run', result.reason)
        self.assertIn('health check job 4', result.reason)

    def test_force_registers_without_asking(self):
        with _Held(admission.KIND_TESTER):
            result = admission.try_start(admission.KIND_SYNC, 'manual', force=True)
            self.assertTrue(result.granted)
            self.assertIn(admission.KIND_SYNC, admission.active_kinds())

    def test_an_unknown_kind_is_a_programming_error_not_a_silent_pass(self):
        with self.assertRaises(ValueError):
            admission.try_start('vacuum', 'x')


class ReleaseTests(unittest.TestCase):
    def tearDown(self):
        admission.reset_for_tests()

    def test_releasing_frees_the_axis(self):
        ticket = admission.try_start(admission.KIND_TESTER, 'run')
        self.assertFalse(admission.try_start(admission.KIND_SYNC, 'a').granted)
        admission.release(ticket)
        self.assertTrue(admission.try_start(admission.KIND_SYNC, 'a').granted)

    def test_release_is_idempotent(self):
        ticket = admission.try_start(admission.KIND_SYNC, 'a')
        admission.release(ticket)
        admission.release(ticket)
        self.assertEqual(admission.active_kinds(), set())

    def test_releasing_a_refusal_or_none_is_a_no_op(self):
        """Callers release in a `finally` without re-deriving whether they were admitted,
        so a Refusal and a None both have to be safe to hand back."""
        with _Held(admission.KIND_TESTER):
            refusal = admission.try_start(admission.KIND_SYNC, 'a')
            admission.release(refusal)
            admission.release(None)
            self.assertEqual(admission.active_kinds(), {admission.KIND_TESTER})

    def test_describe_active_reports_holders_for_diagnostics(self):
        with _Held(admission.KIND_SYNC, 'account 7'):
            described = admission.describe_active()
        self.assertEqual([(k, lbl) for k, lbl, _age in described],
                         [(admission.KIND_SYNC, 'account 7')])


class TesterWiringTests(unittest.TestCase):
    """The tester registers in the same critical section that sets is_running, and gives the
    ticket back wherever that state is torn down."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        admission.reset_for_tests()
        self.t.cleanup()

    def test_a_run_holds_a_tester_ticket_for_its_duration(self):
        with channel_tester._lock:
            channel_tester._reset_run_state(job_id=3, label='health check job 3')
        try:
            self.assertEqual(admission.active_kinds(), {admission.KIND_TESTER})
            self.assertTrue(channel_tester.is_running())
        finally:
            channel_tester._end_run()
        self.assertEqual(admission.active_kinds(), set())
        self.assertFalse(channel_tester.is_running())

    def test_is_running_and_the_ticket_are_never_observed_apart(self):
        """The 4ms window in the boot log was exactly this: a sync that saw is_running False
        while the tester was already on its way in. They now move together."""
        with channel_tester._lock:
            channel_tester._reset_run_state(job_id=1, label='health check job 1')
            # Still inside the tester's own critical section - the registration has to have
            # landed already, not after the lock is dropped.
            self.assertIn(admission.KIND_TESTER, admission.active_kinds())
        channel_tester._end_run()

    def test_a_pre_check_that_raises_while_picking_a_channel_still_ends_its_run(self):
        """dev/docs/BUGS.md 2026-08-16 09:36. run_pre_check registers the run before it
        resolves which channel to test, and that resolution used to sit in an unguarded gap
        ahead of the try/finally - so a raise there left is_running True (and now the ticket
        held) for the life of the process, silently skipping every later health check and
        deferring every scheduled sync to a test run that was not running."""
        from app.database import Recording, RecordingEvent, PRE_CHECK_SKIPPED
        account = seed.make_account()
        channel = seed.make_channel(account)
        group = seed.make_group(members=[channel])
        rec = seed.make_recording(status='SCHEDULED', group_id=group.id,
                                  start_time=datetime.utcnow() + timedelta(hours=2))
        db.session.commit()

        cfg = {'channel_testing': {'pre_check': {'enabled': True}}}
        with mock.patch('app.config.load_config', return_value=cfg), \
             mock.patch('app.channel_groups.recording_members',
                        side_effect=RuntimeError('member lookup blew up')):
            channel_tester.run_pre_check(self.t.app, rec.id)

        self.assertFalse(channel_tester.is_running())
        self.assertEqual(admission.active_kinds(), set())
        db.session.expire_all()
        events = RecordingEvent.query.filter_by(recording_id=rec.id,
                                                event_type=PRE_CHECK_SKIPPED).all()
        self.assertEqual(len(events), 1,
                         'a pre-check that could not resolve a channel must say so, not '
                         'vanish (product principle 1)')
        self.assertIsNotNone(db.session.get(Recording, rec.id))

    def test_a_one_off_test_releases_its_ticket_when_the_test_raises(self):
        with mock.patch.object(channel_tester, 'run_channel_test',
                               side_effect=RuntimeError('ffmpeg exploded')):
            with self.assertRaises(RuntimeError):
                channel_tester.run_single_channel_test(self.t.app, 1)
        self.assertEqual(admission.active_kinds(), set(),
                         'a raising run must not carry its ticket out of the process')

    def _one_off_with_a_failing_channel_fetch(self):
        """Run the one-off path with the channel fetch raising the contended-SQLite error
        the setup phase is realistically exposed to, and hand back the raised exception.

        The fetch sits between the run's registration and the test itself, which is the
        window this class is about - patching run_channel_test would land past it."""
        from sqlalchemy.exc import OperationalError
        boom = OperationalError('SELECT * FROM channels', {},
                                Exception('database is locked'))
        with mock.patch.object(type(db.session), 'get', side_effect=boom):
            with self.assertRaises(OperationalError) as caught:
                channel_tester.run_single_channel_test(self.t.app, 1)
        return caught.exception

    def test_a_one_off_test_releases_its_ticket_when_the_setup_phase_raises(self):
        """The one-off path registered the run and then read the channel out of the database
        before entering the try/finally that gives the ticket back, so a raise on that read
        left is_running True and the KIND_TESTER ticket held for the life of the process -
        refusing every later account sync and maintenance job, since both yield to the tester
        in BLOCKED_BY. Same defect the pre-check path carried (dev/docs/BUGS.md 2026-08-16
        09:36), on the sibling that was missed."""
        self._one_off_with_a_failing_channel_fetch()

        self.assertEqual(admission.active_kinds(), set(),
                         'a raise before the test starts must not strand the tester ticket')
        self.assertFalse(channel_tester.is_running(),
                         'a raise before the test starts must not leave the tester busy')

    def test_a_one_off_test_that_dies_in_setup_says_so(self):
        """It runs on a bare daemon thread with no excepthook, so an unlogged raise goes to
        stderr and nowhere else while the page that started it waits forever for a refresh.
        The ERROR record is also what reaches the log-to-alert handler in production; the
        suite strips that handler (tests/support/app.py), so the record itself is the
        assertion here."""
        with self.assertLogs('app.channel_tester', level='ERROR') as logged:
            self._one_off_with_a_failing_channel_fetch()

        self.assertTrue(any('channel 1' in line for line in logged.output),
                        f'the failure must name the channel it happened on: {logged.output}')

        entries = channel_tester.get_status()['logs']
        self.assertTrue(any(e['level'] == 'ERROR' for e in entries),
                        'the tester run log is what the waiting page renders, so the '
                        'failure has to land there too (product principle 1)')


class SyncWiringTests(unittest.TestCase):
    """The scheduled sync asks admission inside sync_account, so the decision and the start
    cannot be pulled apart, and a refusal lands on the existing defer-and-retry surface."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.account = seed.make_account(name='Wired')
        db.session.commit()
        self.retry_id = sched.sync_retry_job_id(self.account.id)

    def tearDown(self):
        sched.remove_job_if_exists(self.retry_id)
        admission.reset_for_tests()
        self.t.cleanup()

    def test_a_scheduled_sync_is_refused_while_a_test_run_is_active(self):
        from app import accounts
        with _Held(admission.KIND_TESTER, 'health check job 2'):
            with mock.patch('app.accounts._do_sync') as body:
                outcome = accounts.sync_account(self.t.app, self.account.id)
        body.assert_not_called()
        self.assertIsInstance(outcome, admission.Refusal)

    def test_a_manual_sync_runs_anyway_and_still_registers(self):
        from app import accounts
        seen = {}

        def _body(*a, **kw):
            seen['kinds'] = admission.active_kinds()

        with _Held(admission.KIND_TESTER):
            with mock.patch('app.accounts._do_sync', _body):
                outcome = accounts.sync_account(self.t.app, self.account.id,
                                                force_admission=True)
        self.assertIsNone(outcome)
        self.assertIn(admission.KIND_SYNC, seen['kinds'],
                      'a forced sync must still register, or nothing can yield to it')

    def test_a_sync_releases_its_ticket_when_the_sync_body_raises(self):
        from app import accounts
        with mock.patch('app.accounts._do_sync', side_effect=RuntimeError('provider down')):
            with self.assertRaises(RuntimeError):
                accounts.sync_account(self.t.app, self.account.id)
        self.assertEqual(admission.active_kinds(), set())

    def test_a_refused_scheduled_sync_defers_and_queues_a_retry(self):
        cfg = {
            'sync': {'skip_sync_if_recording_active': True,
                     'skip_sync_if_recording_within_minutes': 5,
                     'tester_defer_retry_minutes': 20},
            'notifications': {'routing': {}, 'base_url': ''},
        }
        with _Held(admission.KIND_TESTER, 'health check job 9'):
            with mock.patch('app.accounts._do_sync') as body, \
                 mock.patch('app.config.load_config', return_value=cfg):
                sched._account_sync_job(self.account.id)
        body.assert_not_called()
        self.assertIsNotNone(sched._scheduler.get_job(self.retry_id))


class MaintenanceWiringTests(unittest.TestCase):
    """Maintenance is the most deferrable work in the app and had no guard at all before
    this: both jobs are daily crons that fire into the startup catch-up pileup."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        for job_id in ('db_maintenance_daily_retry', 'recording_retention_daily_retry'):
            sched.remove_job_if_exists(job_id)
        admission.reset_for_tests()
        self.t.cleanup()

    def test_db_maintenance_defers_to_a_running_sync_and_queues_a_retry(self):
        with _Held(admission.KIND_SYNC, 'account 1'):
            with mock.patch.object(sched, '_db_maintenance_sweep') as body:
                sched._db_maintenance_job()
        body.assert_not_called()
        self.assertIsNotNone(sched._scheduler.get_job('db_maintenance_daily_retry'),
                             'a deferred daily job must queue a retry, or a sync that '
                             'reliably overlaps 04:30 starves it forever')

    def test_retention_defers_to_a_running_sync(self):
        with _Held(admission.KIND_SYNC, 'account 1'):
            with mock.patch.object(sched, '_recording_retention_sweep') as body:
                sched._recording_retention_job()
        body.assert_not_called()

    def test_maintenance_runs_and_releases_when_the_axis_is_clear(self):
        with mock.patch.object(sched, '_db_maintenance_sweep') as body:
            sched._db_maintenance_job()
        body.assert_called_once()
        self.assertEqual(admission.active_kinds(), set())

    def test_maintenance_releases_its_ticket_when_the_sweep_raises(self):
        with mock.patch.object(sched, '_db_maintenance_sweep',
                               side_effect=RuntimeError('pruning blew up')):
            with self.assertRaises(RuntimeError):
                sched._db_maintenance_job()
        self.assertEqual(admission.active_kinds(), set())


class RebuildWiringTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        admission.reset_for_tests()
        self.t.cleanup()

    def test_a_refusable_rebuild_stands_down_while_a_sync_holds_the_axis(self):
        with _Held(admission.KIND_SYNC, 'account 1'):
            with mock.patch.object(search_index, '_rebuild_one') as one:
                results = search_index.rebuild_search_indexes('janitor', refusable=True)
        one.assert_not_called()
        self.assertEqual(results, {},
                         'a refused rebuild must report that it rebuilt nothing - an empty '
                         'result, never a success it did not earn')

    def test_the_sync_close_out_rebuild_is_not_refused_by_its_own_sync(self):
        """It is the tail of already-admitted work, so it registers rather than asking -
        otherwise every sync would end by declining to index what it just wrote."""
        with _Held(admission.KIND_SYNC, 'account 1'):
            with mock.patch.object(search_index, '_rebuild_one', return_value=True) as one:
                results = search_index.rebuild_search_indexes('account 1 sync',
                                                              names=('channels',))
        one.assert_called_once()
        self.assertEqual(results, {'channels': True})

    def test_a_rebuild_registers_so_maintenance_can_yield_to_it(self):
        seen = {}

        def _one(name, reason):
            seen['kinds'] = admission.active_kinds()
            return True

        with mock.patch.object(search_index, '_rebuild_one', _one):
            search_index.rebuild_search_indexes('manual rebuild', names=('channels',))
        self.assertIn(admission.KIND_REBUILD, seen['kinds'])
        self.assertEqual(admission.active_kinds(), set())


if __name__ == '__main__':
    unittest.main()
