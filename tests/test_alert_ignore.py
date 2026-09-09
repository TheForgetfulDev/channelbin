"""Ack & ignore future matching alerts (dev/changelog/539).

Covers three layers:
  * app/alerts.py::normalize_alert_title - the digit-collapse match key.
  * app/alerts.py::create_alert - an IgnoredAlertPattern match must suppress both the
    Alert row and the push, and must bump match_count/last_matched_at; a non-matching
    alert must be unaffected.
  * app/routes/alerts.py - POST .../ignore creates the pattern and dismisses the
    triggering alert; GET /alerts/ignored lists patterns; POST .../remove deletes one and
    alerts start surfacing again.

    python3 -m unittest tests.test_alert_ignore
"""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app import db
from app.alerts import create_alert, normalize_alert_title
from app.database import Alert, IgnoredAlertPattern
from tests.support.app import make_test_app


class NormalizeAlertTitleTests(unittest.TestCase):
    def test_digit_runs_collapse_to_a_single_placeholder(self):
        self.assertEqual(
            normalize_alert_title('Provider X: skipped 250 malformed channel URL(s)'),
            'Provider X: skipped # malformed channel URL(s)',
        )

    def test_two_titles_differing_only_by_count_normalize_equal(self):
        a = normalize_alert_title('Provider X: skipped 250 malformed channel URL(s)')
        b = normalize_alert_title('Provider X: skipped 251 malformed channel URL(s)')
        self.assertEqual(a, b)

    def test_titles_differing_in_non_digit_text_stay_distinct(self):
        a = normalize_alert_title('Provider X: skipped 250 malformed channel URL(s)')
        b = normalize_alert_title('Provider Y: skipped 250 malformed channel URL(s)')
        self.assertNotEqual(a, b)

    def test_multi_digit_number_is_one_placeholder_not_one_per_digit(self):
        self.assertEqual(normalize_alert_title('12345 things'), '# things')

    def test_none_title_does_not_raise(self):
        self.assertEqual(normalize_alert_title(None), '')


class CreateAlertSuppressionTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _add_pattern(self, alert_type, title_pattern, **kw):
        p = IgnoredAlertPattern(alert_type=alert_type, title_pattern=title_pattern,
                                example_title=kw.pop('example_title', title_pattern), **kw)
        db.session.add(p)
        db.session.commit()
        return p.id

    def test_matching_alert_writes_no_row_and_bumps_the_pattern(self):
        pid = self._add_pattern('MALFORMED_CHANNEL_URLS',
                                normalize_alert_title('Acct: skipped 100 malformed channel URL(s)'))
        create_alert('MALFORMED_CHANNEL_URLS', title='Acct: skipped 999 malformed channel URL(s)')
        self.assertEqual(Alert.query.count(), 0)
        p = db.session.get(IgnoredAlertPattern, pid)
        self.assertEqual(p.match_count, 1)
        self.assertIsNotNone(p.last_matched_at)

    def test_repeated_matches_keep_incrementing(self):
        pid = self._add_pattern('MALFORMED_CHANNEL_URLS',
                                normalize_alert_title('Acct: skipped 1 malformed channel URL(s)'))
        create_alert('MALFORMED_CHANNEL_URLS', title='Acct: skipped 2 malformed channel URL(s)')
        create_alert('MALFORMED_CHANNEL_URLS', title='Acct: skipped 3 malformed channel URL(s)')
        p = db.session.get(IgnoredAlertPattern, pid)
        self.assertEqual(p.match_count, 2)

    def test_non_matching_alert_type_is_unaffected(self):
        self._add_pattern('MALFORMED_CHANNEL_URLS',
                          normalize_alert_title('Acct: skipped 100 malformed channel URL(s)'))
        create_alert('JOB_SKIPPED', title='Acct: skipped 100 malformed channel URL(s)')
        self.assertEqual(Alert.query.count(), 1)

    def test_non_matching_title_shape_is_unaffected(self):
        self._add_pattern('MALFORMED_CHANNEL_URLS',
                          normalize_alert_title('Acct A: skipped 100 malformed channel URL(s)'))
        create_alert('MALFORMED_CHANNEL_URLS', title='Acct B: skipped 100 malformed channel URL(s)')
        self.assertEqual(Alert.query.count(), 1)

    def test_no_pattern_behaves_exactly_as_before(self):
        create_alert('MALFORMED_CHANNEL_URLS', title='Acct: skipped 5 malformed channel URL(s)')
        self.assertEqual(Alert.query.count(), 1)

    def test_matching_alert_also_suppresses_push(self):
        self._add_pattern('MALFORMED_CHANNEL_URLS',
                          normalize_alert_title('Acct: skipped 1 malformed channel URL(s)'))
        with patch('app.alerts._get_routing',
                   return_value={'in_app': True, 'push_services': ['pushover']}), \
             patch('app.notifications.enqueue_push') as mock_push:
            create_alert('MALFORMED_CHANNEL_URLS', title='Acct: skipped 2 malformed channel URL(s)')
        mock_push.assert_not_called()

    def test_a_fresh_type_still_pushes_when_routed(self):
        with patch('app.alerts._get_routing',
                   return_value={'in_app': True, 'push_services': ['pushover']}), \
             patch('app.notifications.enqueue_push') as mock_push:
            create_alert('MALFORMED_CHANNEL_URLS', title='Acct: skipped 2 malformed channel URL(s)')
        mock_push.assert_called_once()


class IgnoreRoutesTests(unittest.TestCase):
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

    def test_ignore_creates_a_pattern_and_dismisses_the_alert(self):
        with self.t.app.app_context():
            aid = self._alert()
        resp = self.client.post(f'/api/alerts/{aid}/ignore')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        with self.t.app.app_context():
            a = db.session.get(Alert, aid)
            self.assertIsNotNone(a.dismissed_at)
            self.assertEqual(IgnoredAlertPattern.query.count(), 1)
            p = IgnoredAlertPattern.query.first()
            self.assertEqual(p.alert_type, 'MALFORMED_CHANNEL_URLS')
            self.assertEqual(p.example_title, 'Acct: skipped 100 malformed channel URL(s)')

    def test_ignoring_two_alerts_of_the_same_shape_reuses_one_pattern(self):
        with self.t.app.app_context():
            a1 = self._alert(title='Acct: skipped 100 malformed channel URL(s)')
            a2 = self._alert(title='Acct: skipped 200 malformed channel URL(s)')
        self.client.post(f'/api/alerts/{a1}/ignore')
        self.client.post(f'/api/alerts/{a2}/ignore')
        with self.t.app.app_context():
            self.assertEqual(IgnoredAlertPattern.query.count(), 1)
            self.assertEqual(IgnoredAlertPattern.query.first().match_count, 0)

    def test_ignore_unknown_alert_404s(self):
        resp = self.client.post('/api/alerts/999999/ignore')
        self.assertEqual(resp.status_code, 404)

    def test_future_matching_sync_no_longer_surfaces_after_ignore(self):
        with self.t.app.app_context():
            aid = self._alert(title='Acct: skipped 100 malformed channel URL(s)')
        self.client.post(f'/api/alerts/{aid}/ignore')
        with self.t.app.app_context():
            create_alert('MALFORMED_CHANNEL_URLS',
                         title='Acct: skipped 250 malformed channel URL(s)')
            # Only the original (now-dismissed) alert exists - the second sync's
            # recurrence never became a row.
            self.assertEqual(Alert.query.count(), 1)

    def test_ignored_alerts_page_lists_patterns(self):
        with self.t.app.app_context():
            db.session.add(IgnoredAlertPattern(
                alert_type='MALFORMED_CHANNEL_URLS',
                title_pattern=normalize_alert_title('Acct: skipped 5 malformed channel URL(s)'),
                example_title='Acct: skipped 5 malformed channel URL(s)',
                created_at=datetime.utcnow(), match_count=3,
                last_matched_at=datetime.utcnow() - timedelta(minutes=2),
            ))
            db.session.commit()
        html = self.client.get('/alerts/ignored').get_data(as_text=True)
        self.assertEqual(self.client.get('/alerts/ignored').status_code, 200)
        self.assertIn('Acct: skipped 5 malformed channel URL(s)', html)
        self.assertIn('Malformed Channel URLs Skipped', html)

    def test_remove_deletes_the_pattern_and_alerts_surface_again(self):
        with self.t.app.app_context():
            aid = self._alert(title='Acct: skipped 100 malformed channel URL(s)')
        self.client.post(f'/api/alerts/{aid}/ignore')
        with self.t.app.app_context():
            pid = IgnoredAlertPattern.query.first().id
        resp = self.client.post(f'/api/alerts/ignored/{pid}/remove')
        self.assertEqual(resp.status_code, 200)
        with self.t.app.app_context():
            self.assertEqual(IgnoredAlertPattern.query.count(), 0)
            create_alert('MALFORMED_CHANNEL_URLS',
                         title='Acct: skipped 300 malformed channel URL(s)')
            self.assertEqual(Alert.query.filter_by(dismissed_at=None).count(), 1)

    def test_remove_unknown_pattern_404s(self):
        resp = self.client.post('/api/alerts/ignored/999999/remove')
        self.assertEqual(resp.status_code, 404)


if __name__ == '__main__':
    unittest.main(verbosity=2)
