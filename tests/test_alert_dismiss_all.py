"""POST /api/alerts/dismiss_all must dismiss only already-read alerts (dev/docs/BUGS.md
2026-08-15).

    python3 -m unittest tests.test_alert_dismiss_all
"""
import unittest
from datetime import datetime

from app import db
from app.database import Alert
from tests.support.app import make_test_app


class DismissAllTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _alert(self, **kw):
        kw.setdefault('alert_type', 'MALFORMED_CHANNEL_URLS')
        kw.setdefault('severity', 'INFO')
        kw.setdefault('title', 'Acct: skipped 100 malformed channel URL(s)')
        a = Alert(**kw)
        db.session.add(a)
        db.session.commit()
        return a.id

    def test_unread_alert_is_left_alone(self):
        with self.t.app.app_context():
            aid = self._alert()
        resp = self.client.post('/api/alerts/dismiss_all')
        self.assertEqual(resp.status_code, 200)
        with self.t.app.app_context():
            a = db.session.get(Alert, aid)
            self.assertIsNone(a.read_at)
            self.assertIsNone(a.dismissed_at)

    def test_already_read_alert_is_dismissed(self):
        with self.t.app.app_context():
            aid = self._alert(read_at=datetime.utcnow())
        self.client.post('/api/alerts/dismiss_all')
        with self.t.app.app_context():
            a = db.session.get(Alert, aid)
            self.assertIsNotNone(a.dismissed_at)

    def test_mixed_batch_dismisses_only_the_read_one(self):
        with self.t.app.app_context():
            unread_id = self._alert(title='Acct: skipped 1 malformed channel URL(s)')
            read_id = self._alert(title='Acct: skipped 2 malformed channel URL(s)',
                                   read_at=datetime.utcnow())
        self.client.post('/api/alerts/dismiss_all')
        with self.t.app.app_context():
            unread = db.session.get(Alert, unread_id)
            read = db.session.get(Alert, read_id)
            self.assertIsNone(unread.read_at)
            self.assertIsNone(unread.dismissed_at)
            self.assertIsNotNone(read.dismissed_at)

    def test_already_dismissed_alert_is_untouched(self):
        with self.t.app.app_context():
            now = datetime.utcnow()
            aid = self._alert(read_at=now, dismissed_at=now)
        self.client.post('/api/alerts/dismiss_all')
        with self.t.app.app_context():
            a = db.session.get(Alert, aid)
            self.assertEqual(a.dismissed_at, now)
