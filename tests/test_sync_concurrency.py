"""Tier 2 - sync/tester/recording cross-guards (DESIGN-concurrency.md §5.2, §5.3).

Two gaps in the concurrency model, both about provider connections nobody was counting:

  * **G3** - a scheduled account sync fired while a channel test run was active. Sync had
    guards for recordings but none for the tester, so both opened provider connections.
    §5.2: sync defers and queues exactly one one-shot retry (never modify_job on the
    interval trigger, which would permanently drift the schedule).
  * **G11** - a recording started while a sync was in flight on the same account. Nothing
    detected it, and the provider may count the sync's HTTP fetch against the stream.
    §5.3: recording start cancels the in-flight same-account sync. Recordings always win
    and never wait.

The G11 check lives at the top of `_try_acquire_slot_with_preemption` - the single choke point
all three recording entry points (start, resume, failover cross-account swap) funnel
through - and is deliberately unconditional: sync holds no connection slot, so a
successful slot acquire proves nothing about a sync being in flight.
`test_preempts_sync_even_when_a_slot_is_free` is what pins that placement down.

No real syncs, no real ffmpeg: sync_account and cancel_sync are patched.
"""
import os
import re
import sys
import threading
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import admission, db, accounts, recorder  # noqa: E402
from app.database import Account, Alert, AccountSyncLog  # noqa: E402
import app.scheduler as sched  # noqa: E402


def _cfg(tester_defer_retry_minutes=20):
    """Minimal config for the paths under test: the sync guards read cfg['sync'], and
    create_alert reads cfg['notifications']."""
    return {
        'sync': {
            'skip_sync_if_recording_active': True,
            'skip_sync_if_recording_within_minutes': 5,
            'tester_defer_retry_minutes': tester_defer_retry_minutes,
        },
        'notifications': {'routing': {}, 'base_url': ''},
    }


class SyncDefersToTesterTests(unittest.TestCase):
    """§5.2 / G3. Needs the live scheduler (against the temp jobstore) because the
    assertion is about a job actually landing in the store.

    The tester is simulated by a real admission ticket rather than by patching
    `channel_tester.is_running`: since dev/changelog/679 the deferral decision is made
    inside `sync_account` under the admission lock, and `is_running` is no longer what
    the sync job consults."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.account = seed.make_account(name='Deferrable')
        db.session.commit()
        self.retry_id = sched.sync_retry_job_id(self.account.id)
        self.tester_ticket = None

    def tearDown(self):
        admission.release(self.tester_ticket)
        sched.remove_job_if_exists(self.retry_id)
        self.t.cleanup()

    def _retry_jobs(self):
        return [j for j in sched._scheduler.get_jobs()
                if re.match(r'^account_sync_retry_\d+$', j.id)]

    def _run_sync_job(self, *, tester_running, retry_minutes=20):
        """Returns a spy standing in for the sync body. sync_account itself runs for real,
        so the admission decision under test is the real one."""
        if tester_running and self.tester_ticket is None:
            self.tester_ticket = admission.try_start(admission.KIND_TESTER, 'health check job 1')
        spy = mock.Mock()
        with mock.patch('app.accounts._do_sync', spy), \
             mock.patch('app.config.load_config', return_value=_cfg(retry_minutes)):
            sched._account_sync_job(self.account.id)
        return spy

    def test_sync_skipped_while_a_test_run_is_active(self):
        spy = self._run_sync_job(tester_running=True)
        spy.assert_not_called()

    def test_sync_runs_when_no_test_run_is_active(self):
        spy = self._run_sync_job(tester_running=False)
        spy.assert_called_once()
        self.assertEqual(self._retry_jobs(), [])

    def test_deferral_schedules_a_retry_job(self):
        self._run_sync_job(tester_running=True)
        job = sched._scheduler.get_job(self.retry_id)
        self.assertIsNotNone(job, 'deferring the sync must queue a retry')

    def test_repeated_deferral_collapses_into_one_retry(self):
        for _ in range(3):
            self._run_sync_job(tester_running=True)
        self.assertEqual(len(self._retry_jobs()), 1,
                         'replace_existing must collapse repeated deferrals into one pending retry')

    def test_retry_job_reenters_the_sync_job_so_guards_rerun(self):
        """The retry must call back into _account_sync_job, not straight into sync_account -
        otherwise a still-running test (or a recording) would be ignored on the retry."""
        self._run_sync_job(tester_running=True)
        job = sched._scheduler.get_job(self.retry_id)
        self.assertIs(job.func, sched._account_sync_job)
        self.assertEqual(job.kwargs, {'account_id': self.account.id, 'retry': True})

    def test_retry_is_a_one_shot_date_trigger_not_an_interval_nudge(self):
        """APScheduler 3.x recomputes subsequent interval fires from a modified
        next_run_time, so the deferral must never touch the interval job."""
        from apscheduler.triggers.date import DateTrigger
        self._run_sync_job(tester_running=True)
        self.assertIsInstance(sched._scheduler.get_job(self.retry_id).trigger, DateTrigger)

    def test_zero_retry_minutes_skips_with_no_retry(self):
        spy = self._run_sync_job(tester_running=True, retry_minutes=0)
        spy.assert_not_called()
        self.assertEqual(self._retry_jobs(), [],
                         'tester_defer_retry_minutes=0 means plain skip, no retry job')

    def test_deferral_is_observable_on_the_accounts_own_sync_history(self):
        """dev/changelog/928: a deferred sync is recorded where that account's sync history
        already lives - the account page renders these rows - rather than as an alert."""
        self._run_sync_job(tester_running=True)
        rows = AccountSyncLog.query.filter_by(account_id=self.account.id,
                                              status='SKIPPED').all()
        self.assertEqual(len(rows), 1)
        self.assertIn('test run', (rows[0].error_message or '').lower(),
                      'the row must name the tester as the reason for the skip')

    def test_retry_job_is_removed_when_sync_is_disabled(self):
        """Teardown releases what the create path acquired: a pending retry must not
        outlive the sync job it belongs to."""
        self._run_sync_job(tester_running=True)
        self.account.sync_enabled = False
        db.session.commit()
        sched.schedule_account_sync(self.t.app, self.account.id)
        self.assertIsNone(sched._scheduler.get_job(self.retry_id))


class RecordingPreemptsSyncTests(unittest.TestCase):
    """§5.3 / G11."""

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Syncing Account')
        self.channel = seed.make_channel(self.account)
        self.rec = seed.make_recording(status='SCHEDULED', channel_id=self.channel.id)
        db.session.commit()

    def tearDown(self):
        from app import connection_limits as connlim
        connlim.release(self.account.id, 'recording', self.rec.id)
        self.t.cleanup()

    def _set_status(self, status):
        self.account.status = status
        db.session.commit()

    def test_recording_start_cancels_an_in_flight_sync(self):
        self._set_status('SYNCING')
        with mock.patch('app.accounts.cancel_sync', return_value='cancelled') as spy:
            recorder._preempt_sync_for_recording(self.rec.id, self.account.id)
        spy.assert_called_once()
        self.assertEqual(spy.call_args.args[0], self.account.id)

    def test_cancel_reason_names_the_recording_not_the_user(self):
        self._set_status('SYNCING')
        with mock.patch('app.accounts.cancel_sync', return_value='cancelled') as spy:
            recorder._preempt_sync_for_recording(self.rec.id, self.account.id)
        reason = spy.call_args.kwargs.get('reason', '')
        self.assertIn('recording', reason.lower())

    def test_non_syncing_account_is_left_alone(self):
        self._set_status('OK')
        with mock.patch('app.accounts.cancel_sync') as spy:
            recorder._preempt_sync_for_recording(self.rec.id, self.account.id)
        spy.assert_not_called()

    def test_orphaned_syncing_state_does_not_raise(self):
        """cancel_sync returns 'reset' when the SYNCING status is stale (no live thread).
        Nothing to preempt - and a recording must never be blocked by an exception here."""
        self._set_status('SYNCING')
        with mock.patch('app.accounts.cancel_sync', return_value='reset'):
            recorder._preempt_sync_for_recording(self.rec.id, self.account.id)

    def test_preempts_sync_even_when_a_slot_is_free(self):
        """The choke-point invariant. Sync holds no connection slot, so the check must run
        before (and independently of) the slot acquire succeeding - otherwise every
        recording that starts on an idle account silently skips the sync preempt. Placing
        it here is also what covers resume_recording and the failover cross-account swap,
        which are the other two callers of _try_acquire_slot_with_preemption."""
        self._set_status('SYNCING')
        with mock.patch('app.accounts.cancel_sync', return_value='cancelled') as spy:
            recorder._try_acquire_slot_with_preemption(self.t.app, self.rec.id, self.account.id)
        spy.assert_called_once()


class CancelReasonPlumbingTests(unittest.TestCase):
    """The reason travels from the cancelling thread to the sync thread that writes the
    cancelled state, so the stored message can't claim a user did it."""

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Cancelme')
        db.session.commit()
        self.sync_log = AccountSyncLog(account_id=self.account.id, status='IN_PROGRESS')
        db.session.add(self.sync_log)
        db.session.commit()
        # Stand in for a live sync thread so cancel_sync takes the 'cancelled' branch.
        self._alive = threading.Event()
        self._thread = threading.Thread(target=self._alive.wait, daemon=True)
        self._thread.start()
        with accounts._sync_locks_mutex:
            accounts._sync_threads[self.account.id] = self._thread
            accounts._sync_stop_events[self.account.id] = threading.Event()

    def tearDown(self):
        self._alive.set()
        self._thread.join(timeout=5)
        with accounts._sync_locks_mutex:
            accounts._sync_threads.pop(self.account.id, None)
            accounts._sync_stop_events.pop(self.account.id, None)
            accounts._sync_cancel_reasons.pop(self.account.id, None)
        self.t.cleanup()

    def _cancel_and_mark(self, **kw):
        result = accounts.cancel_sync(self.account.id, **kw)
        self.assertEqual(result, 'cancelled')
        accounts._mark_sync_cancelled(self.account.id, self.sync_log)
        db.session.expire_all()

    def test_custom_reason_is_stored_on_the_log_and_the_account(self):
        self._cancel_and_mark(reason='Cancelled - a recording needed this account')
        log_row = db.session.get(AccountSyncLog, self.sync_log.id)
        acct = db.session.get(Account, self.account.id)
        self.assertEqual(log_row.error_message, 'Cancelled - a recording needed this account')
        self.assertIn('recording', acct.last_error.lower())
        self.assertNotIn('by user', acct.last_error.lower())

    def test_default_reason_is_unchanged(self):
        self._cancel_and_mark()
        log_row = db.session.get(AccountSyncLog, self.sync_log.id)
        acct = db.session.get(Account, self.account.id)
        self.assertEqual(log_row.error_message, 'Cancelled by user')
        self.assertEqual(acct.last_error, 'Sync cancelled by user')

    def test_reason_is_consumed_not_left_behind(self):
        """A stale reason must not be able to mislabel a later cancellation."""
        self._cancel_and_mark(reason='Cancelled - a recording needed this account')
        with accounts._sync_locks_mutex:
            self.assertNotIn(self.account.id, accounts._sync_cancel_reasons)


class UpcomingRecordingSkipIsObservableTests(unittest.TestCase):
    """A scheduled sync yielding to a recording that starts soon used to only write a log
    line; the recording-IN_PROGRESS branch was already recording the skip on the account, and
    dev/changelog/603 gave the within-minutes branch the same treatment. Both branches now
    defer rather than drop the occurrence (dev/changelog/941) - what is asserted here is the
    unchanged half: the sync does not run, and the account's own history says why."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.account = seed.make_account(name='Upcoming Account')
        self.channel = seed.make_channel(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _run_sync_job(self, within_minutes=5):
        cfg = {
            'sync': {
                'skip_sync_if_recording_active': True,
                'skip_sync_if_recording_within_minutes': within_minutes,
            },
            'notifications': {'routing': {}, 'base_url': ''},
        }
        spy = mock.Mock()
        with mock.patch('app.channel_tester.is_running', return_value=False), \
             mock.patch('app.accounts.sync_account', spy), \
             mock.patch('app.config.load_config', return_value=cfg):
            sched._account_sync_job(self.account.id)
        return spy

    def test_sync_is_skipped_when_a_recording_starts_soon(self):
        seed.make_recording(status='SCHEDULED', channel_id=self.channel.id, name='Soon Rec',
                            start_time=sched.datetime.utcnow() + timedelta(minutes=2))
        db.session.commit()
        spy = self._run_sync_job()
        spy.assert_not_called()

    def test_skip_is_observable_on_the_accounts_own_sync_history(self):
        """dev/changelog/928: recorded as a SKIPPED row on the account rather than alerted.
        The reason names the recording, which is the actionable half."""
        seed.make_recording(status='SCHEDULED', channel_id=self.channel.id, name='Soon Rec',
                            start_time=sched.datetime.utcnow() + timedelta(minutes=2))
        db.session.commit()
        self._run_sync_job(within_minutes=5)
        rows = AccountSyncLog.query.filter_by(account_id=self.account.id,
                                              status='SKIPPED').all()
        self.assertEqual(len(rows), 1)
        self.assertIn('Soon Rec', rows[0].error_message)
        self.assertIn('5 minutes', rows[0].error_message)
        self.assertIsNotNone(rows[0].completed_at,
                             'a skipped occurrence is closed, never left looking in-progress')

    def test_sync_runs_when_nothing_is_imminent(self):
        seed.make_recording(status='SCHEDULED', channel_id=self.channel.id, name='Far Off',
                            start_time=sched.datetime.utcnow() + timedelta(hours=3))
        db.session.commit()
        spy = self._run_sync_job(within_minutes=5)
        spy.assert_called_once()
        self.assertEqual(Alert.query.filter_by(alert_type='JOB_SKIPPED').count(), 0)


if __name__ == '__main__':
    unittest.main()
