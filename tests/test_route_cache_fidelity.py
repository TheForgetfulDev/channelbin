"""tests/support/routecache.py must be invisible to everything except the clock.

routecache memoizes werkzeug's per-Rule compile() across the throwaway apps the suite
builds, replaying five underscore-prefixed attributes onto each new Rule instead of
recompiling. That is a monkeypatch of third-party internals, and werkzeug is unpinned
(requirements.txt says Flask>=3.0), so the assumptions it rests on are asserted here
rather than trusted: a memoized app must route and build URLs exactly like one built with
werkzeug's real compile().

If this file goes red after a dependency bump, the fix is to update (or delete)
tests/support/routecache.py for the new werkzeug - never to relax these assertions. The
suite is correct without the optimization, just ~60s slower.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import routecache  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402


def _sample_args(rule):
    """Concrete values for a rule's placeholders, typed to match its converters."""
    args = {}
    for name, converter in rule._converters.items():
        kind = type(converter).__name__
        if 'Number' in kind or 'Integer' in kind or 'Float' in kind:
            args[name] = 1
        elif 'Path' in kind:
            args[name] = 'a/b'
        else:
            args[name] = 'x'
    return args


def _fingerprint(app):
    """Everything observable about the app's routing table, as comparable data."""
    adapter = app.url_map.bind('localhost')
    rows = []
    for rule in app.url_map.iter_rules():
        entry = {
            'rule': rule.rule,
            'endpoint': rule.endpoint,
            'methods': sorted(rule.methods or ()),
            'defaults': repr(rule.defaults),
            'converters': sorted((k, type(v).__name__) for k, v in rule._converters.items()),
            'trace': list(rule._trace),
        }
        try:
            entry['built'] = adapter.build(rule.endpoint, _sample_args(rule),
                                           method=next(iter(sorted(rule.methods or ['GET']))))
        except Exception as exc:  # noqa: BLE001 - the failure itself is the comparable value
            entry['built'] = f'<{type(exc).__name__}: {exc}>'
        rows.append(entry)
    return sorted(rows, key=lambda r: (r['rule'], r['endpoint']))


class RouteCacheFidelityTests(unittest.TestCase):
    """A memoized app and a real-compile app must be indistinguishable."""

    def setUp(self):
        self.was_installed = routecache.is_installed()

    def tearDown(self):
        if self.was_installed and not routecache.is_installed():
            routecache.install()

    def _build_fingerprint(self, memoized):
        if memoized:
            routecache.install()
        else:
            routecache.uninstall()
        t = make_test_app()
        try:
            return _fingerprint(t.app)
        finally:
            t.cleanup()

    def test_memoized_routing_table_matches_real_compile(self):
        memo = self._build_fingerprint(memoized=True)
        real = self._build_fingerprint(memoized=False)
        self.assertEqual(len(memo), len(real),
                         f'route count differs: {len(memo)} memoized vs {len(real)} real')
        for m, r in zip(memo, real):
            self.assertEqual(m, r, f'memoized rule differs from a real-compiled one:\n'
                                   f'  memoized: {m}\n  real:     {r}')

    def test_memoized_matching_resolves_the_same_endpoints(self):
        """Building is one direction; matching is the other, and it reads _parts."""
        routecache.uninstall()
        t_real = make_test_app()
        try:
            paths = []
            adapter = t_real.app.url_map.bind('localhost')
            for rule in t_real.app.url_map.iter_rules():
                if 'GET' not in (rule.methods or ()):
                    continue
                try:
                    paths.append(adapter.build(rule.endpoint, _sample_args(rule), method='GET'))
                except Exception:  # noqa: BLE001 - unbuildable rules are not part of this check
                    continue
            expected = [self._match(t_real.app, p) for p in paths]
        finally:
            t_real.cleanup()

        routecache.install()
        t_memo = make_test_app()
        try:
            got = [self._match(t_memo.app, p) for p in paths]
        finally:
            t_memo.cleanup()

        self.assertTrue(paths, 'no buildable GET routes found - the check would be vacuous')
        for path, e, g in zip(paths, expected, got):
            self.assertEqual(e, g, f'{path} matched {g} under the route cache but {e} '
                                   f'under werkzeug\'s real compile()')

    @staticmethod
    def _match(app, path):
        adapter = app.url_map.bind('localhost')
        try:
            return adapter.match(path, method='GET')
        except Exception as exc:  # noqa: BLE001 - the failure is the comparable value
            return f'<{type(exc).__name__}>'

    def test_cached_builders_are_rebound_per_rule(self):
        """A replayed _build must belong to the rule it was replayed onto.

        This is the aliasing bug the memoization could plausibly introduce: leaving the
        cached bound method attached to whichever rule compiled first would silently build
        URLs against a torn-down app's Map.
        """
        routecache.install()
        t = make_test_app()
        try:
            for rule in t.app.url_map.iter_rules():
                self.assertIs(rule._build.__self__, rule,
                              f'{rule.rule}: _build is bound to another Rule instance')
                self.assertIs(rule._build_unknown.__self__, rule,
                              f'{rule.rule}: _build_unknown is bound to another Rule instance')
                self.assertIs(rule.map, t.app.url_map,
                              f'{rule.rule}: rule escaped its own Map')
        finally:
            t.cleanup()


if __name__ == '__main__':
    unittest.main(verbosity=2)
