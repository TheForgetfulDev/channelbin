"""The alert signature the Alerts page re-renders on (dev/changelog/1131).

routes/alerts.py::alert_signature() rides on /api/nav-status, and /alerts renders the one
its cards were read at; the page swaps itself when the two differ. So the signature has to
move for every way a row the page shows can change - a part that misses one is a change the
open page never shows - and stay still otherwise, or every open tab re-renders every poll.

Each case drives the real writer of that change rather than poking columns, because the
case that decided the signature's shape is one no column diff predicts: a standing alert
re-raised IN PLACE keeps its id and the table's size, and only its created_at says so.
Each part of the signature has a case here that fails without it.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_alert_signature
"""
import os
import re
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app.accounts import _raise_or_resolve_standing_alert  # noqa: E402
from app.alerts import cleanup_old_alerts  # noqa: E402
from app.database import Alert  # noqa: E402
from app.routes.alerts import alert_signature  # noqa: E402

SOURCE = 'account:1:feed-shrunk'


class AlertSignatureTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.past = Alert(alert_type='CONCATENATION_FAILED', severity='ERROR',
                          title='Concatenation failed', created_at=datetime.utcnow())
        db.session.add(self.past)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        self.t.cleanup()

    def _sig(self):
        db.session.expire_all()
        return alert_signature()

    def _moves(self, change):
        before = self._sig()
        change()
        after = self._sig()
        self.assertNotEqual(before, after)

    def _standing(self, active, title='Feed shrank by 40%'):
        _raise_or_resolve_standing_alert('SYNC_FEED_SHRUNK', SOURCE, active,
                                         title=title, body='details')

    def test_a_new_alert_moves_it(self):
        self._moves(lambda: self._standing(True))

    def test_a_standing_alert_re_raised_in_place_moves_it(self):
        """The case "newest id plus newest read/dismiss time" would miss: the open row is
        rewritten and marked unread again, so no id and no row count moves."""
        self._standing(True)
        row = Alert.query.filter_by(source=SOURCE).one()
        self.client.post(f'/api/alerts/{row.id}/read')
        # Another alert read later holds max(read_at), so clearing this row's read_at moves
        # nothing but its created_at.
        self.client.post(f'/api/alerts/{self.past.id}/read')
        count = Alert.query.count()
        self._moves(lambda: self._standing(True, title='Feed shrank by 55%'))
        self.assertEqual(Alert.query.count(), count, 'the refresh must be in place for this case')
        self.assertIsNone(db.session.get(Alert, row.id).read_at)

    def test_mark_read_moves_it(self):
        self._moves(lambda: self.client.post(f'/api/alerts/{self.past.id}/read'))

    def test_mark_all_read_moves_it(self):
        self._moves(lambda: self.client.post('/api/alerts/read_all'))

    def test_a_dismiss_moves_it(self):
        self.client.post(f'/api/alerts/{self.past.id}/read')
        self._moves(lambda: self.client.post(f'/api/alerts/{self.past.id}/dismiss'))

    def test_a_problem_clearing_itself_moves_it(self):
        self._standing(True)
        self._moves(lambda: self._standing(False))
        self.assertIsNotNone(Alert.query.filter_by(source=SOURCE).one().dismissed_at)

    def test_the_retention_prune_moves_it(self):
        """The prune deletes old dismissed rows, which only ?include_dismissed=1 shows. It
        never holds any of the maxima - a newer dismissed row sits above it here - so only
        the count can say anything went."""
        old = datetime.utcnow() - timedelta(days=200)
        gone = Alert(alert_type='CONCATENATION_FAILED', severity='ERROR', title='Old',
                     created_at=old, read_at=old, dismissed_at=old)
        db.session.add(gone)
        db.session.commit()
        gone_id = gone.id
        self.client.post(f'/api/alerts/{self.past.id}/dismiss')
        with mock.patch('app.config.load_config', return_value={'alerts': {'keep_days': 90}}):
            self._moves(lambda: cleanup_old_alerts(self.t.app))
        self.assertIsNone(db.session.get(Alert, gone_id))

    def test_reading_the_page_and_polling_do_not_move_it(self):
        before = self._sig()
        self.client.get('/alerts')
        self.client.get('/alerts?include_dismissed=1')
        self.client.get('/api/nav-status')
        self.client.get('/api/alerts')
        self.assertEqual(self._sig(), before)

    def test_the_page_renders_the_signature_the_nav_poll_reports(self):
        html = self.client.get('/alerts').get_data(as_text=True)
        nav = self.client.get('/api/nav-status').get_json()
        m = re.search(r'<div id="al-live" data-alert-sig="([^"]*)">', html)
        self.assertIsNotNone(m, 'the live region must carry its signature')
        self.assertEqual(m.group(1), nav['alert_signature'])
        self.assertEqual(nav['alert_signature'], self._sig())

    def test_an_empty_table_still_has_a_signature(self):
        Alert.query.delete()
        db.session.commit()
        self.assertTrue(self._sig())
        html = self.client.get('/alerts').get_data(as_text=True)
        self.assertIn('<div id="al-live" data-alert-sig="0|||">', html)


class AlertAgeTests(unittest.TestCase):
    """DESIGN.md 5: a time is shown with its relative age beside it. The page re-renders
    once a minute, which is what keeps the age true."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_each_row_shows_its_age(self):
        with self.t.app.app_context():
            db.session.add(Alert(alert_type='CONCATENATION_FAILED', severity='ERROR',
                                 title='Concatenation failed',
                                 created_at=datetime.utcnow() - timedelta(minutes=12, seconds=5)))
            db.session.commit()
        html = self.client.get('/alerts').get_data(as_text=True)
        meta = re.search(r'<div class="al-meta">(.*?)</div>', html, re.S).group(1)
        self.assertRegex(meta, r'&middot;&nbsp;12m \d+s ago')


if __name__ == '__main__':
    unittest.main()
