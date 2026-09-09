"""Tier 2 - account.next_sync_at must mirror the actually-scheduled sync job.

Guards BUGS.md 2026-07-25: after an account sync was interrupted by a service restart,
_reset_stuck_accounts() reset the account to UNSYNCED but never touched next_sync_at, so
the stale (already-past) timestamp stuck around. schedule_account_sync() computed a real,
correct next-run candidate (~1 minute out, since status == UNSYNCED) and registered the
APScheduler job at that time - the actual scheduling was never broken - but never wrote
that candidate back to account.next_sync_at, so the dashboard/accounts page (which render
next_sync_at | time_until) kept showing "NEXT SYNC: now" even though the job was correctly
queued to run shortly.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Account  # noqa: E402
import app.scheduler as sched  # noqa: E402


class NextSyncAtMirrorsScheduledJobTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        self.t.cleanup()

    def test_interrupted_sync_reschedule_updates_next_sync_at(self):
        """The exact restart scenario: status UNSYNCED, next_sync_at stale/in the past."""
        stale = datetime.utcnow() - timedelta(hours=2)
        account = seed.make_account(name='Interrupted', next_sync_at=stale)
        account.status = 'UNSYNCED'
        db.session.commit()

        sched.schedule_account_sync(self.t.app, account.id)

        job = sched._scheduler.get_job(f'account_sync_{account.id}')
        self.assertIsNotNone(job)

        db.session.expire_all()
        refreshed = db.session.get(Account, account.id)
        self.assertIsNotNone(refreshed.next_sync_at,
                              'next_sync_at must be populated once a job is scheduled')
        self.assertGreater(refreshed.next_sync_at, datetime.utcnow(),
                            'next_sync_at must not still be the stale past timestamp')
        self.assertEqual(refreshed.next_sync_at, job.next_run_time.replace(tzinfo=None),
                          'account.next_sync_at must mirror the actual job next_run_time')

    def test_next_sync_at_unchanged_when_job_already_correctly_scheduled(self):
        """No spurious write (and no commit noise) when nothing needs to change."""
        account = seed.make_account(name='AlreadyScheduled')
        db.session.commit()

        sched.schedule_account_sync(self.t.app, account.id)
        db.session.expire_all()
        first_value = db.session.get(Account, account.id).next_sync_at

        # Re-running with no interval/status change should hit the early-return path
        # and leave next_sync_at exactly as it was.
        sched.schedule_account_sync(self.t.app, account.id)
        db.session.expire_all()
        second_value = db.session.get(Account, account.id).next_sync_at

        self.assertEqual(first_value, second_value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
