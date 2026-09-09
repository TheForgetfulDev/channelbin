"""Tier 2 - CSRF coverage + JSON error envelope (dev/changelog/268, chunk 5).

Two app-wide contracts from CLAUDE.md:

  * Every state-changing route (POST/PUT/DELETE/PATCH) is CSRF-protected app-wide; a
    request with no token must be rejected before the view body runs - never 200, never
    500. This is a mechanical sweep over the whole url_map (the same never-executed-path
    philosophy as the GET smoke sweep), so a newly added mutating route that somehow
    escaped protection would trip it.
  * A rejected JSON/`/api/` request comes back as the `{'error': ...}` envelope with a
    400, not an HTML error page (app/__init__.py _handle_csrf_error).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402

_MUTATING = {'POST', 'PUT', 'DELETE', 'PATCH'}


class CsrfSweepTests(unittest.TestCase):
    """CSRF is active (make_test_app does NOT disable it). Fire every mutating rule with
    no token and a dummy id; the pre-request CSRF check must reject before routing to the
    view, so the id never matters."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _concrete_path(self, rule):
        # Substitute a harmless value for each converter arg.
        path = rule.rule
        for arg in rule.arguments:
            conv = rule._converters.get(arg)
            val = '1' if conv is not None and conv.__class__.__name__.startswith(('Integer', 'Float')) else 'x'
            path = path.replace(f'<{arg}>', val).replace(f'<int:{arg}>', val)\
                       .replace(f'<float:{arg}>', val).replace(f'<path:{arg}>', val)\
                       .replace(f'<string:{arg}>', val)
        return path

    def test_every_mutating_route_rejects_missing_token(self):
        offenders = []
        for rule in self.t.app.url_map.iter_rules():
            methods = (rule.methods or set()) & _MUTATING
            if not methods:
                continue
            path = self._concrete_path(rule)
            method = 'POST' if 'POST' in methods else sorted(methods)[0]
            resp = self.t.client.open(path, method=method)
            # Rejected without executing: CSRF returns 400 (api/json) or 302 (html
            # redirect). It must never 200 (executed) or 500 (crashed).
            if resp.status_code in (200, 500):
                offenders.append(f'{method} {path} -> {resp.status_code}')
        self.assertEqual(offenders, [], f'mutating routes not rejected without CSRF: {offenders}')


class CsrfEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_api_route_returns_error_envelope(self):
        resp = self.t.client.post('/api/guide/channels/1/add')
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIsNotNone(body, 'API CSRF failure must return JSON, not an HTML page')
        self.assertIn('error', body)
        self.assertNotIn('success', body)

    def test_json_accept_header_returns_error_envelope(self):
        # A non-/api/ route asked for JSON via Accept still gets the envelope.
        resp = self.t.client.post('/recordings/1/delete-json',
                                  headers={'Accept': 'application/json'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())


if __name__ == '__main__':
    unittest.main(verbosity=2)
