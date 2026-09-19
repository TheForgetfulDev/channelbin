"""Malformed request input is a 4xx with the error envelope, never a 500.

Guards dev/docs/BUGS.md 2026-09-18 @ 07:12:59 PM: a bare `int()` on a query parameter, an
unhashable JSON value handed to a dict lookup, and an unvalidated account field each raised
inside the route, so Flask answered 500 where CLAUDE.md's envelope rule wants a 4xx.
"""
import os
import unittest
from unittest import mock

from app import db
from app.database import Account
from tests.support.app import make_test_app
from tests.support.seed import make_account


class _ClientCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        # CSRF is enforced app-wide; these cases are about input validation, not the token.
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()


class NumericQueryParamTests(_ClientCase):
    def test_a_non_numeric_alerts_limit_falls_back_to_the_default(self):
        res = self.client.get('/api/alerts?limit=abc')
        self.assertEqual(res.status_code, 200)

    def test_a_non_numeric_logs_tail_falls_back_to_the_default(self):
        # The route answers [] before parsing anything when there is no file, so the
        # cast is only reached with a real one - a sandbox file, never the live log.
        path = os.path.join(self.t._tmpdir, 'history.log')
        with open(path, 'w') as fh:
            fh.write('line\n')
        with mock.patch('app.routes.logs._log_file_path', return_value=path):
            res = self.client.get('/api/logs/history?tail=abc')
        self.assertEqual(res.status_code, 200)


class ReadinessCheckIdTests(_ClientCase):
    def test_run_answers_404_for_a_list_or_dict_check(self):
        for bad in ([], ['db_write'], {'id': 'db_write'}):
            res = self.client.post('/api/readiness/run', json={'check': bad})
            self.assertEqual(res.status_code, 404, bad)
            self.assertIn('Unknown check', res.get_json()['error'])

    def test_ignore_answers_404_for_a_list_or_dict_check(self):
        for bad in ([], {'id': 'guide_content'}):
            res = self.client.post('/api/readiness/ignore',
                                   json={'check': bad, 'ignored': True})
            self.assertEqual(res.status_code, 404, bad)
            self.assertIn('Unknown check', res.get_json()['error'])


class SyncIntervalValidationTests(_ClientCase):
    def setUp(self):
        super().setUp()
        self.acc_id = make_account('Api').id
        db.session.commit()

    def _save(self, hours):
        return self.client.post(f'/api/accounts/{self.acc_id}',
                                json={'name': 'Api', 'account_type': 'm3u',
                                      'm3u_url': 'http://x.test/a.m3u',
                                      'sync_interval_hours': hours})

    def test_the_json_api_rejects_a_non_numeric_interval_with_a_400(self):
        for bad in ('abc', '0', '-2', '1.5'):
            res = self._save(bad)
            self.assertEqual(res.status_code, 400, bad)
            self.assertIn('Sync interval', res.get_json()['error'])
        db.session.expire_all()
        self.assertIsNone(db.session.get(Account, self.acc_id).sync_interval_hours)

    def test_blank_still_means_follow_the_global_interval(self):
        res = self._save('')
        self.assertEqual(res.status_code, 200)

    def test_the_add_form_rerenders_with_the_error_instead_of_a_500(self):
        res = self.client.post('/accounts/new', data={
            'name': 'New', 'account_type': 'm3u', 'm3u_url': 'http://x.test/b.m3u',
            'sync_interval_hours': 'abc'})
        self.assertEqual(res.status_code, 200)
        self.assertIn('Sync interval must be a positive whole number',
                      res.get_data(as_text=True))
        self.assertEqual(Account.query.filter_by(name='New').count(), 0)


if __name__ == '__main__':
    unittest.main()
