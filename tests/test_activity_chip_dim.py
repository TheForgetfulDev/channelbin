"""Tier 2 - the nav bar's Recording/Background chips' 'dim' state (scheduled-but-not-
running), revived by dev/changelog (see the entry added alongside this file).

`_activity_status_dict()` (app/routes/dashboard.py) already computed a `dim` state for
both chips, but the frontend could never reach it (base.html applyChip/applyIndicators
hid any chip whose count was zero, and dim carries no count) - purely a frontend defect,
not covered here (see tests/test_nav_shell.py for the source-shape guard on that fix).

This file guards the backend correctness this revival depends on:
  * the Background chip's dim window is bounded to "starting soon" (~1 hour), not "any
    future job" - unbounded, a daily job like config_backup_daily would leave it dim
    almost permanently, which is exactly what was not wanted when reviving
    this (2026-08-05).
  * the per-account sync job id (`account_sync_<id>`) is matched correctly - the prior
    dead code matched a literal `xtream_sync` id that no real job has used since sync
    went per-account, so "Account / EPG Sync" could never actually appear as the next
    background job even once reachable.
  * a scheduled recording never flips the Background chip's state - the two chips are
    separate indicators and must stay that way.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Recording  # noqa: E402
from app.tz_utils import UTC  # noqa: E402
import app.scheduler as sched  # noqa: E402

ACTIVITY_URL = '/api/activity/status'


def _set_next_run(job_id, when_utc_aware):
    sched._scheduler.modify_job(job_id, next_run_time=when_utc_aware)


class BackgroundDimWindowTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        self.t.cleanup()

    def test_config_backup_within_the_hour_is_dim(self):
        _set_next_run('config_backup_daily', datetime.now(UTC) + timedelta(minutes=30))
        body = self.t.client.get(ACTIVITY_URL).get_json()
        self.assertEqual(body['background']['state'], 'dim')
        self.assertEqual(body['background']['next_scheduled']['label'], 'Config Backup')

    def test_config_backup_hours_away_stays_hidden(self):
        """The bounded-window fix: an unbounded 'any future job' check would make this
        dim almost permanently, since a daily job nearly always has *some* future run."""
        _set_next_run('config_backup_daily', datetime.now(UTC) + timedelta(hours=5))
        body = self.t.client.get(ACTIVITY_URL).get_json()
        self.assertEqual(body['background']['state'], 'hidden')
        self.assertIsNone(body['background']['next_scheduled'])

    def test_account_sync_within_the_hour_is_dim_and_labeled(self):
        """Guards the id-matching fix: the real per-account job id is `account_sync_<id>`,
        not the dead code's literal `xtream_sync`."""
        account = seed.make_account(name='Dim Test Account')
        db.session.commit()
        sched.schedule_account_sync(self.t.app, account.id)
        _set_next_run(f'account_sync_{account.id}', datetime.now(UTC) + timedelta(minutes=10))
        # Push config_backup_daily out of the window so only the sync job is in play.
        _set_next_run('config_backup_daily', datetime.now(UTC) + timedelta(hours=5))
        body = self.t.client.get(ACTIVITY_URL).get_json()
        self.assertEqual(body['background']['state'], 'dim')
        self.assertEqual(body['background']['next_scheduled']['label'], 'Account / EPG Sync')

    def test_a_scheduled_recording_does_not_dim_the_background_chip(self):
        """The two indicators must stay separate - a scheduled recording is the Recording
        chip's story, never the Background chip's."""
        _set_next_run('config_backup_daily', datetime.now(UTC) + timedelta(hours=5))
        seed.make_recording(
            name='Future Rec', status='SCHEDULED',
            start_time=datetime.utcnow() + timedelta(minutes=20),
            stop_time=datetime.utcnow() + timedelta(minutes=80),
        )
        db.session.commit()
        body = self.t.client.get(ACTIVITY_URL).get_json()
        self.assertEqual(body['recording']['state'], 'dim')
        self.assertEqual(body['background']['state'], 'hidden')


class RecordingDimStateTests(unittest.TestCase):
    """Locks in the backend contract the frontend fix now finally surfaces."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_scheduled_future_recording_is_dim_not_hidden(self):
        seed.make_recording(
            name='Later', status='SCHEDULED',
            start_time=datetime.utcnow() + timedelta(hours=1),
            stop_time=datetime.utcnow() + timedelta(hours=2),
        )
        db.session.commit()
        body = self.t.client.get(ACTIVITY_URL).get_json()
        self.assertEqual(body['recording']['state'], 'dim')
        self.assertEqual(body['recording']['active'], [])
        self.assertEqual(body['recording']['next_scheduled']['name'], 'Later')

    def test_no_recordings_at_all_is_hidden(self):
        self.assertEqual(Recording.query.count(), 0)
        body = self.t.client.get(ACTIVITY_URL).get_json()
        self.assertEqual(body['recording']['state'], 'hidden')


if __name__ == '__main__':
    unittest.main(verbosity=2)
