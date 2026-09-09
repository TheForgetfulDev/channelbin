"""Test package.

This exists solely so the test-isolation guards install before any test module is
imported, on every invocation path: `./run_tests.sh`, `python3 -m unittest discover
-s tests`, and `python3 -m unittest tests.test_<module>`. All three import this
package first.

  * netguard   - no test may reach the network (sockets + URL-pointed child spawns).
  * notifyguard - no test may send a real push notification. Separate from netguard
    because it has to stop the send at the source: the push path reads the *real*
    config.yaml at runtime, where pushover is enabled with a live URL.

It also installs three speed optimizations rather than guards, all of which attack the
same thing: the fixed cost the suite pays per throwaway app, times the ~570 of them a
full run builds. They install here, before any test module is imported, so every
invocation path gets them for free.

  * routecache  - memoize werkzeug's per-rule URL compilation across apps (144s -> 83s).
  * jinjacache  - share one compiled-template cache across apps (122s -> 104s).
  * sqlitespeed - no fsync on a temp database that is deleted seconds later (~6s).

None of the three may change what a test observes; routecache and jinjacache each have a
fidelity test asserting exactly that (tests/test_route_cache_fidelity.py,
tests/test_jinja_cache_fidelity.py).
"""
from .support.jinjacache import install as _install_jinjacache
from .support.netguard import install as _install_netguard
from .support.notifyguard import install as _install_notifyguard
from .support.routecache import install as _install_routecache
from .support.sqlitespeed import install as _install_sqlitespeed

_install_netguard()
_install_notifyguard()
_install_routecache()
_install_jinjacache()
_install_sqlitespeed()
