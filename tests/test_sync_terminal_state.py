"""Regression coverage for the two "mark sync terminal" call sites that had no test at
all before the changelog/307 DRY-up: scheduler startup's stuck-account reset and the
`/accounts/<id>/sync/cancel` route's orphaned-state reset. Both now go through the
shared `app.accounts.finalize_sync_state` helper alongside `_mark_sync_cancelled`
(covered by tests.test_sync_concurrency.CancelReasonPlumbingTests) and `_do_sync`'s
error path (covered by tests.test_account_url_masking).

The account message and the log message are asserted separately in both tests below
because they are genuinely different strings, not a shared `message` - a refactor that
collapsed them to one parameter would pass every other test in the suite but fail here.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Account, AccountSyncLog  # noqa: E402


class ResetStuckAccountsOnStartupTests(unittest.TestCase):
    """app.scheduler.resume_in_progress_recordings's _reset_stuck_accounts closure."""

    def setUp(self):
        # Real jobstore needed - resume_in_progress_recordings also schedules on-demand
        # test jobs later in the same function, which needs a live scheduler even though
        # this test has none seeded.
        self.t = make_test_app(start_scheduler=True)
        self.account = seed.make_account(name='Stuck')
        self.account.status = 'SYNCING'
        db.session.commit()
        self.open_log = AccountSyncLog(account_id=self.account.id, status='IN_PROGRESS')
        db.session.add(self.open_log)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_stuck_account_and_its_open_log_are_reset(self):
        from app.scheduler import resume_in_progress_recordings
        resume_in_progress_recordings(self.t.app)

        db.session.expire_all()
        acct = db.session.get(Account, self.account.id)
        log_row = db.session.get(AccountSyncLog, self.open_log.id)

        self.assertEqual(acct.status, 'UNSYNCED')
        self.assertEqual(acct.last_error, 'Sync interrupted by service restart')
        self.assertEqual(log_row.status, 'CANCELLED')
        self.assertEqual(log_row.error_message, 'Interrupted by service restart')
        self.assertIsNotNone(log_row.completed_at)

    def test_previously_synced_account_resets_to_error_not_unsynced(self):
        # dev/docs/BUGS.md 2026-08-22: a restart mid-sync used to reset EVERY orphaned
        # SYNCING account to UNSYNCED, so an account that had synced successfully many
        # times before reported "Never synced" with no way to tell otherwise.
        # last_sync_at is set only on a genuine successful sync, so its presence is what
        # distinguishes this account from self.account (never synced) above.
        previous_sync_at = datetime.utcnow() - timedelta(hours=6)
        synced_account = seed.make_account(name='PreviouslySynced', last_sync_at=previous_sync_at)
        synced_account.status = 'SYNCING'
        db.session.commit()
        open_log = AccountSyncLog(account_id=synced_account.id, status='IN_PROGRESS')
        db.session.add(open_log)
        db.session.commit()

        from app.scheduler import resume_in_progress_recordings
        resume_in_progress_recordings(self.t.app)

        db.session.expire_all()
        acct = db.session.get(Account, synced_account.id)
        log_row = db.session.get(AccountSyncLog, open_log.id)

        self.assertEqual(acct.status, 'ERROR')
        self.assertEqual(acct.last_error, 'Sync interrupted by service restart')
        self.assertEqual(acct.last_sync_at, previous_sync_at)
        self.assertEqual(log_row.status, 'CANCELLED')
        self.assertEqual(log_row.error_message, 'Interrupted by service restart')
        self.assertIsNotNone(log_row.completed_at)


class CancelSyncRouteResetTests(unittest.TestCase):
    """The 'reset' branch of app.routes.accounts.cancel_sync_api - no live sync thread
    registered for the account, so app.accounts.cancel_sync reports 'reset'.

    The form-POST route this used to drive was deleted in dev/changelog/456; both Accounts
    surfaces cancel through the JSON API now, and the shared _cancel_sync helper underneath
    it is unchanged."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.account = seed.make_account(name='Orphaned')
        self.account.status = 'SYNCING'
        db.session.commit()
        self.open_log = AccountSyncLog(account_id=self.account.id, status='IN_PROGRESS')
        db.session.add(self.open_log)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_orphaned_syncing_state_is_reset_via_the_route(self):
        resp = self.t.client.post(f'/api/accounts/{self.account.id}/sync/cancel')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])

        db.session.expire_all()
        acct = db.session.get(Account, self.account.id)
        log_row = db.session.get(AccountSyncLog, self.open_log.id)

        self.assertEqual(acct.status, 'UNSYNCED')
        self.assertEqual(acct.last_error, 'Sync reset (was stuck after service restart)')
        self.assertEqual(log_row.status, 'CANCELLED')
        self.assertEqual(log_row.error_message, 'Reset after service restart')
        self.assertIsNotNone(log_row.completed_at)


if __name__ == '__main__':
    unittest.main()
