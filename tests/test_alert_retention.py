"""Alert-table retention unit (app/alerts.py::cleanup_old_alerts).

Pins the safety invariant of the only alert-row retention in the app: dismissing an alert
just sets dismissed_at, so without this sweep the alerts table grows without bound. The
sweep must delete ONLY resolved (dismissed) alerts past the window and must NEVER touch an
active/unread alert regardless of age - an undismissed alert is an unresolved problem.

cleanup_old_alerts reads alerts.keep_days from load_config() at run time, and it re-imports
load_config locally, so the test patches app.config.load_config to drive the window (the
make_test_app extra_overrides are NOT visible to a runtime load_config() - CLAUDE.md Testing).
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app.database import Alert  # noqa: E402
from app.alerts import cleanup_old_alerts  # noqa: E402


class AlertRetentionTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _add(self, *, created_days_ago, dismissed_days_ago):
        now = datetime.utcnow()
        a = Alert(
            alert_type='LOG_ERROR', severity='ERROR', title='t',
            created_at=now - timedelta(days=created_days_ago),
            dismissed_at=None if dismissed_days_ago is None
            else now - timedelta(days=dismissed_days_ago),
        )
        db.session.add(a)
        db.session.commit()
        return a.id

    def _run(self, keep_days):
        with patch('app.config.load_config', return_value={'alerts': {'keep_days': keep_days}}):
            cleanup_old_alerts(self.t.app)

    def test_active_alert_never_deleted_even_if_ancient(self):
        aid = self._add(created_days_ago=3650, dismissed_days_ago=None)
        self._run(keep_days=90)
        self.assertIsNotNone(db.session.get(Alert, aid))

    def test_dismissed_but_recent_kept(self):
        aid = self._add(created_days_ago=200, dismissed_days_ago=10)
        self._run(keep_days=90)
        self.assertIsNotNone(db.session.get(Alert, aid))

    def test_dismissed_and_old_deleted(self):
        aid = self._add(created_days_ago=200, dismissed_days_ago=120)
        self._run(keep_days=90)
        self.assertIsNone(db.session.get(Alert, aid))

    def test_keep_days_zero_keeps_everything(self):
        aid = self._add(created_days_ago=3650, dismissed_days_ago=3600)
        self._run(keep_days=0)
        self.assertIsNotNone(db.session.get(Alert, aid))


if __name__ == '__main__':
    unittest.main(verbosity=2)
