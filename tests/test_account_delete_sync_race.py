"""Deleting an account stops its running sync first, or refuses.

Guards dev/docs/BUGS.md 2026-08-15 08:38 - the delete path tore an account's rows out from
under a live sync thread. SQLite foreign keys are off, so cascades are ORM-level only: the
thread went on inserting Channel rows for an account that no longer existed (orphans that
raise AttributeError on any page touching ch.account.name), and its updates to deleted rows
died as StaleDataError against a ghost account.

The sync thread here is a stand-in, registered directly in app.accounts' thread/stop-event
registries. That is the whole surface stop_sync_and_wait() reads, and it keeps the test off
the network and out of a real _do_sync. Both registries are deliberately NOT cleared by
tests/support/app.py::reset_module_globals (see test_global_state_isolation.py's allowlist -
a running sync deregisters itself), so every test here tears down its own entries.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 tests/test_account_delete_sync_race.py
"""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import accounts as accounts_mod  # noqa: E402
from app import db  # noqa: E402
from app.database import Account  # noqa: E402
from app.routes import accounts as routes_accounts  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support.seed import make_account  # noqa: E402


class DeleteAccountSyncRaceTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acct = make_account(name='Provider A')
        db.session.commit()
        self.account_id = self.acct.id
        self._release = threading.Event()
        self._threads = []
        # The shipped wait is 5s; every refusal case below would pay it in full. Nothing
        # here is timing-sensitive - the stand-in thread either exits on the signal at once
        # or never - so the wait is shortened to keep the suite's wall clock honest.
        patcher = mock.patch.object(routes_accounts, '_SYNC_STOP_TIMEOUT_SECONDS', 0.2)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        # Release before cleanup: a stand-in thread still parked on _release would outlive
        # the test and keep the registries populated for whatever runs next.
        self._release.set()
        for thread in self._threads:
            thread.join(5)
        with accounts_mod._sync_locks_mutex:
            accounts_mod._sync_threads.pop(self.account_id, None)
            accounts_mod._sync_stop_events.pop(self.account_id, None)
            accounts_mod._sync_cancel_reasons.pop(self.account_id, None)
            accounts_mod._sync_progress.pop(self.account_id, None)
        self.t.cleanup()

    def _register_sync(self, honors_stop):
        """Register a live stand-in sync thread. Returns its stop event.

        honors_stop=True exits as soon as the stop event is set (a sync sitting on a phase
        boundary); False parks until the test releases it (a sync mid channel-upsert, where
        the stop event is not polled at all).
        """
        stop_event = threading.Event()
        waits_on = stop_event if honors_stop else self._release

        def _run():
            waits_on.wait(10)

        thread = threading.Thread(target=_run, daemon=True, name='fake-account-sync')
        thread.start()
        self._threads.append(thread)
        with accounts_mod._sync_locks_mutex:
            accounts_mod._sync_threads[self.account_id] = thread
            accounts_mod._sync_stop_events[self.account_id] = stop_event
        return stop_event

    def _delete(self):
        return self.t.client.delete(f'/api/accounts/{self.account_id}')

    def test_delete_is_refused_while_a_sync_thread_is_still_running(self):
        stop_event = self._register_sync(honors_stop=False)

        resp = self._delete()

        self.assertEqual(resp.status_code, 409)
        self.assertIn('still syncing', resp.get_json()['error'])
        self.assertTrue(stop_event.is_set(),
                        'the refused delete must still have signalled the sync to stop')
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Account, self.account_id),
                             'the account must survive a refused delete')

    def test_refused_delete_names_the_phase_the_sync_is_in(self):
        self._register_sync(honors_stop=False)
        accounts_mod._set_sync_progress(self.account_id, 'channels', 400, 12000)

        error = self._delete().get_json()['error']

        self.assertIn('Provider A', error)
        self.assertIn('channels', error)

    def test_refused_delete_leaves_the_accounts_scheduler_jobs_alone(self):
        """The sync stop runs BEFORE the job removal, so a refusal has no side effects.
        Removing the jobs and then refusing would silently disable the account's
        scheduled sync while leaving the account in place."""
        self._register_sync(honors_stop=False)
        removed = []
        from app import scheduler as scheduler_mod

        # The scheduler is off in tests, so get_scheduler() is patched truthy too - without
        # it the removal branch is never entered and the assertion below proves nothing.
        with mock.patch.object(scheduler_mod, 'get_scheduler', return_value=object()), \
             mock.patch.object(scheduler_mod, 'remove_job_if_exists', side_effect=removed.append):
            resp = self._delete()

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(removed, [],
                         "a refused delete must not disable the account's scheduled sync")

    def test_delete_proceeds_once_the_sync_thread_stops_on_the_signal(self):
        self._register_sync(honors_stop=True)

        resp = self._delete()

        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Account, self.account_id))

    def test_delete_with_no_sync_running_is_unaffected(self):
        resp = self._delete()

        self.assertEqual(resp.status_code, 200)
        self.assertIn('Provider A', resp.get_json()['message'])
        db.session.expire_all()
        self.assertIsNone(db.session.get(Account, self.account_id))

    def test_orphaned_syncing_status_with_no_live_thread_does_not_block_delete(self):
        """A SYNCING row left behind by a restart has no thread to race - deleting it must
        not be refused forever on the strength of the status column alone."""
        self.acct.status = 'SYNCING'
        db.session.commit()

        resp = self._delete()

        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Account, self.account_id))


class StopSyncAndWaitTests(unittest.TestCase):
    """The helper itself, away from the route."""

    def setUp(self):
        self.account_id = 4242
        self.stop = threading.Event()
        self._release = threading.Event()
        self._thread = None

    def tearDown(self):
        self._release.set()
        if self._thread:
            self._thread.join(5)
        with accounts_mod._sync_locks_mutex:
            accounts_mod._sync_threads.pop(self.account_id, None)
            accounts_mod._sync_stop_events.pop(self.account_id, None)
            accounts_mod._sync_cancel_reasons.pop(self.account_id, None)

    def _register(self, waits_on):
        self._thread = threading.Thread(target=lambda: waits_on.wait(10), daemon=True)
        self._thread.start()
        with accounts_mod._sync_locks_mutex:
            accounts_mod._sync_threads[self.account_id] = self._thread
            accounts_mod._sync_stop_events[self.account_id] = self.stop

    def test_returns_true_when_no_sync_is_registered(self):
        self.assertTrue(accounts_mod.stop_sync_and_wait(self.account_id, 'why', 0.1))

    def test_returns_false_and_records_the_reason_when_the_thread_outlasts_the_timeout(self):
        self._register(self._release)

        self.assertFalse(accounts_mod.stop_sync_and_wait(self.account_id, 'Told to stop', 0.1))
        self.assertTrue(self.stop.is_set())
        with accounts_mod._sync_locks_mutex:
            self.assertEqual(accounts_mod._sync_cancel_reasons.get(self.account_id),
                             'Told to stop')

    def test_returns_true_when_the_thread_exits_on_the_signal(self):
        self._register(self.stop)

        self.assertTrue(accounts_mod.stop_sync_and_wait(self.account_id, 'why', 5))


if __name__ == '__main__':
    unittest.main()
