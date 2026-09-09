"""Tier 2 - Xtream debug endpoints are gated server-side (dev/changelog/268, chunk 5).

BUGS.md 2026-07-16 02:42 PM: the xtream_debug_mode flag hid the *buttons* but the
/dump and /sync-from-dump routes had no server-side check, so hitting the URLs directly
ran regardless. Both must abort(404) when the flag is off, and pass the gate when on.

The form-POST spelling of these two was deleted in dev/changelog/456 along with the rest of
the list page's form routes; the JSON API pair asserted here is what both Accounts surfaces
call, and `_require_xtream_debug` under them is the same gate it always was.

CSRF is disabled here so the request reaches the view body - the point under test is the
debug gate, not CSRF (covered separately in test_csrf_envelope). The flag value is driven
by patching load_config in the accounts module rather than trusting the live config.yaml.

dev/changelog/547 added a per-account override (Account.xtream_debug_override) so the gate
passes for one account even with the global flag off - PerAccountOverrideTests covers that.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402


class XtreamDebugGateTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acc = seed.make_account()   # an M3U account - never an xtream account
        db.session.commit()
        import app.routes.accounts as acct
        self._acct = acct
        self._real_load = acct.load_config

    def tearDown(self):
        self._acct.load_config = self._real_load
        self.t.cleanup()

    def _set_flag(self, value):
        from app.config import load_config as real, _deep_merge
        self._acct.load_config = lambda *a, **k: _deep_merge(
            real(), {'debug': {'xtream_debug_mode': value}})

    def _paths(self):
        return (f'/api/accounts/{self.acc.id}/dump',
                f'/api/accounts/{self.acc.id}/sync-from-dump')

    def test_dump_404s_when_flag_off(self):
        self._set_flag(False)
        for path in self._paths():
            resp = self.t.client.post(path, json={})
            self.assertEqual(resp.status_code, 404, f'{path} should 404 with debug off')

    def test_routes_pass_gate_when_flag_on(self):
        self._set_flag(True)
        # Gate passes; the M3U account then trips the "not an Xtream account" guard and is
        # refused with a 400 - proving the request got PAST the 404 gate without any
        # network. The two codes differing is what makes the flag-off case above evidence
        # of the gate rather than evidence of a missing route.
        for path in self._paths():
            resp = self.t.client.post(path, json={})
            self.assertEqual(resp.status_code, 400, f'{path} should pass the gate with debug on')
            self.assertIn('Not an Xtream account', resp.get_json()['error'])


class XtreamDebugPerAccountOverrideTests(unittest.TestCase):
    """dev/changelog/547 - Account.xtream_debug_override lets one account use the dump/
    replay tools with the global flag off, without touching every other account.

    Uses M3U accounts throughout, same as XtreamDebugGateTests above: gate-pass is proven
    by reaching the "Not an Xtream account" 400 rather than 404, without needing a real
    Xtream account or triggering any actual dump/sync work."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acc = seed.make_account(name='Under vetting')
        db.session.commit()
        import app.routes.accounts as acct
        self._acct = acct
        self._real_load = acct.load_config
        self._set_flag(False)   # global flag off for every test in this class

    def tearDown(self):
        self._acct.load_config = self._real_load
        self.t.cleanup()

    def _set_flag(self, value):
        from app.config import load_config as real, _deep_merge
        self._acct.load_config = lambda *a, **k: _deep_merge(
            real(), {'debug': {'xtream_debug_mode': value}})

    def _paths(self, account_id):
        return (f'/api/accounts/{account_id}/dump',
                f'/api/accounts/{account_id}/sync-from-dump')

    def test_override_off_still_404s_with_global_off(self):
        self.acc.xtream_debug_override = False
        db.session.commit()
        for path in self._paths(self.acc.id):
            resp = self.t.client.post(path, json={})
            self.assertEqual(resp.status_code, 404)

    def test_override_on_passes_gate_with_global_off(self):
        self.acc.xtream_debug_override = True
        db.session.commit()
        for path in self._paths(self.acc.id):
            resp = self.t.client.post(path, json={})
            self.assertEqual(resp.status_code, 400, f'{path} should pass the gate with the override on')
            self.assertIn('Not an Xtream account', resp.get_json()['error'])

    def test_other_accounts_unaffected_by_this_accounts_override(self):
        other = seed.make_account(name='Other M3U')
        db.session.commit()
        self.acc.xtream_debug_override = True
        db.session.commit()
        for path in self._paths(other.id):
            resp = self.t.client.post(path, json={})
            self.assertEqual(resp.status_code, 404,
                              'a different account\'s override must not leak to this one')


if __name__ == '__main__':
    unittest.main(verbosity=2)
