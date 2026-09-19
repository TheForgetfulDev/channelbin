"""A trailing slash resolves to the same route as the bare path (BUGS.md 2026-09-18
05:14 PM, dev/changelog/1030).

Werkzeug matches a rule written without a trailing slash only in that exact spelling, so
'/accounts/' 404'd while '/accounts' served the page - on every route in the app, since
none is written with a trailing slash. `app.url_map.strict_slashes = False` in
create_app() fixes that; these tests cover every rule rather than a sample, so a route
added later inherits the check.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.exceptions import NotFound  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402


class TrailingSlashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = make_test_app()

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def test_every_parameterless_rule_matches_with_a_trailing_slash(self):
        adapter = self.t.app.url_map.bind('localhost')
        checked = 0
        for rule in self.t.app.url_map.iter_rules():
            if '<' in rule.rule or rule.rule == '/':
                continue
            method = next(iter(sorted(rule.methods - {'HEAD', 'OPTIONS'})))
            with self.subTest(rule=rule.rule, method=method):
                try:
                    endpoint, _ = adapter.match(rule.rule + '/', method=method)
                except NotFound:
                    self.fail(f'{rule.rule}/ is a 404')
                self.assertEqual(endpoint, rule.endpoint)
            checked += 1
        self.assertGreater(checked, 50)

    def test_accounts_page_with_trailing_slash_is_served(self):
        self.assertEqual(self.t.client.get('/accounts/').status_code, 200)

    def test_parameterized_route_with_trailing_slash_is_served(self):
        with self.t.app.app_context():
            acct = seed.make_account()
            db.session.commit()
            acct_id = acct.id
        self.assertEqual(self.t.client.get(f'/accounts/{acct_id}/').status_code, 200)


if __name__ == '__main__':
    unittest.main()
