"""The one counter for per-request I/O scaling guards (SQL statements + config.yaml parses).

Canonical home for what used to be two copies - `_IOCounter` in test_scaling_pages.py and
`_CountingParse` in test_config_cache.py - which drifted into two copies of the same fragile
mechanism (CLAUDE.md §Coding Standards, search-before-you-write).

**Parse counting is scoped to the thread that opened the counter.** The seam it patches,
`app.config._parse_config_file`, is process-global, so a background thread calling
load_config() during the measured window used to land in the count. That is exactly what
made these tests flake roughly 1 run in 3: a leaked WatchdogThread polls load_config() about
once a second, and it landed in whichever measurement window was open at the time - which is
why failures appeared in both directions and never reproduced in isolation. The leak itself
is fixed (tests/support/app.py tears down live recordings; test_contention no longer starts
one), but the guard must not depend on no other thread ever existing again.

**Pass every engine, not just `db.engine`.** There are two pools on dvr.db since
dev/changelog/423, and `db.session` routes to the background one whenever there is no request
context - which is precisely the situation a test body is in. So `IOCounter(db.engine)` around
a direct `search(...)` call counts **zero**, silently, and the guard passes while measuring
nothing. `all_engines()` below is the right argument in every case; the single-engine form is
kept only because a caller may legitimately want to watch one.

Usage:

    with IOCounter(all_engines()) as c:
        resp = client.get('/')
    c.queries        # SQL statements executed on those engines during the block
    c.statements     # the statement text of each, for shape assertions
    c.config_parses  # real config.yaml parses on THIS thread during the block
"""
import threading

from sqlalchemy import event

import app.config as config_mod


def all_engines():
    """Every engine `db.session` might route to, for IOCounter. Requires an app context."""
    from app import db
    return list(db.engines.values())


class IOCounter:
    """Count SQL statements on `engines` and real config.yaml parses while active.

    `engines` may be a single Engine, an iterable of Engines, or None for a config-only
    measurement (no SQL listener is attached).
    """

    def __init__(self, engines=None):
        if engines is None:
            self._engines = []
        elif hasattr(engines, '__iter__'):
            self._engines = list(engines)
        else:
            self._engines = [engines]
        self.queries = 0
        # Kept alongside the count so a test can assert on the *shape* of a statement and
        # not merely how many ran - which is what a "predicate is on the wrong side of the
        # join" defect needs, since it changes the plan without changing the query count.
        self.statements = []
        self.config_parses = 0
        self._owner_thread = None

    def __enter__(self):
        self._owner_thread = threading.get_ident()

        def on_execute(conn, cursor, statement, parameters, context, executemany):
            self.queries += 1
            self.statements.append(statement)

        self._on_execute = on_execute
        for engine in self._engines:
            event.listen(engine, 'before_cursor_execute', on_execute)

        self._orig_parse = config_mod._parse_config_file

        def counting_parse():
            # Only the measuring thread's parses are the subject of the assertion;
            # a background thread's poll is noise, not a per-row disk read.
            if threading.get_ident() == self._owner_thread:
                self.config_parses += 1
            return self._orig_parse()

        config_mod._parse_config_file = counting_parse
        return self

    def __exit__(self, *exc):
        for engine in self._engines:
            event.remove(engine, 'before_cursor_execute', self._on_execute)
        config_mod._parse_config_file = self._orig_parse
        return False
