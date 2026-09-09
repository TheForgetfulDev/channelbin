"""Memoize werkzeug's per-Rule URL compilation across the suite's many throwaway apps.

Every DB-backed test builds its own app via make_test_app(), and create_app() registers
~160 url rules. werkzeug compiles each rule eagerly on Map.add(): it parses the rule
string, then generates and `compile()`s Python source for two URL builders. That is
~1,100 builtins.compile() calls per app and measured 96ms of make_test_app()'s 142ms -
about two thirds of the whole suite's wall time, spent producing a byte-identical result
751 times over.

The app shape never varies between tests, so the compiled output is cached on the rule's
identifying attributes and replayed onto each new Rule instance. Measured effect on the
full suite: 144s -> 83s.

TEST-ONLY. Production never imports this; app/ is untouched.

Why this is safe for the pinned werkzeug (3.1.8), and what would break it
------------------------------------------------------------------------
compile() sets exactly five attributes: _parts, _trace, _converters, _build and
_build_unknown. After compile() returns, every use of them in werkzeug is read-only
(routing/matcher.py iterates _parts and calls _converters[...].to_python; routing/map.py
calls _converters[...].to_url) - nothing mutates them, so sharing them between Rule
objects on different Maps cannot leak state. _build/_build_unknown are stored unbound and
re-bound per instance, so a cached builder never closes over the wrong rule. The one real
aliasing is _converters: converter instances hold the Map they were built for, but
BaseConverter only ever assigns self.map and never reads it, so the shared reference is
inert.

All of that is werkzeug-internal and unpinned in requirements.txt (Flask>=3.0), so it is
verified rather than assumed. install() refuses to patch if the attributes have moved, and
tests/test_route_cache_fidelity.py asserts that a memoized app routes and builds URLs
identically to a non-memoized one - that test is what fails first if an upgrade changes
the contract.
"""
import sys

from werkzeug.routing.map import Map
from werkzeug.routing.rules import Rule

_REQUIRED_ATTRS = ('_parts', '_trace', '_converters', '_build', '_build_unknown')

_cache = {}
_real_compile = None


def _cache_key(rule):
    """The attributes compile()'s output actually depends on.

    _parse_rule() reads the rule string, merge_slashes and the map's converters; the
    builder additionally bakes in defaults, and the domain half comes from host/subdomain
    plus the map's host_matching. Anything outside this tuple must not influence compile()
    - if it ever does, the fidelity test is what catches it.
    """
    defaults = rule.defaults or {}
    return (rule.rule, rule.subdomain, rule.host, rule.merge_slashes,
            rule.map.host_matching,
            tuple(sorted(((k, repr(v)) for k, v in defaults.items()))))


def _compile_memo(self):
    key = _cache_key(self)
    hit = _cache.get(key)
    if hit is None:
        _real_compile(self)
        _cache[key] = (self._parts, self._trace, self._converters,
                       self._build.__func__, self._build_unknown.__func__)
        return
    parts, trace, converters, build, build_unknown = hit
    self._parts = parts
    self._trace = trace
    self._converters = converters
    # Re-bind per instance: a cached builder must never stay bound to the rule that
    # first compiled it, or it would build URLs against another app's Map.
    self._build = build.__get__(self, None)
    self._build_unknown = build_unknown.__get__(self, None)


def install():
    """Patch Rule.compile, or leave werkzeug alone and say so loudly on stderr.

    Never fails the run: this is a speed optimization, and a suite that refuses to start
    because an unrelated dependency moved an underscore attribute would be worse than a
    slow one. But it does not degrade silently either (CLAUDE.md: nothing silent) - if the
    contract this relies on is gone, that is printed, because the alternative is
    wondering for weeks why the suite got slow again.
    """
    global _real_compile
    if _real_compile is not None:
        return True

    # Contract check against the installed werkzeug, not against assumption: compile a
    # real rule the real way and confirm the five attributes we replay still exist.
    try:
        probe_map = Map([Rule('/probe/<int:n>', endpoint='probe')])
        probe = next(iter(probe_map.iter_rules()))
        missing = [a for a in _REQUIRED_ATTRS if not hasattr(probe, a)]
    except Exception as exc:  # noqa: BLE001 - any failure here means "don't patch"
        missing = [f'probe raised {type(exc).__name__}: {exc}']
    if missing:
        print(f'[routecache] NOT installed: werkzeug Rule contract changed ({missing}). The '
              f'test suite will still run correctly, just ~60s slower. Update '
              f'tests/support/routecache.py for this werkzeug version.', file=sys.stderr)
        return False

    _real_compile = Rule.compile
    Rule.compile = _compile_memo
    return True


def uninstall():
    """Restore the real compile() and drop the cache. Used by the fidelity test to build
    a known-unmemoized app to compare against."""
    global _real_compile
    if _real_compile is None:
        return
    Rule.compile = _real_compile
    _real_compile = None
    _cache.clear()


def is_installed():
    return _real_compile is not None
