"""MANDATORY scaling regression (CLAUDE.md "## Testing";
dev/changelog/268, chunk 3). Guards the whole "innocuous helper called once per row
silently reintroduces O(N) disk I/O" defect class.

Pins BUGS.md 2026-07-15 10:34: `/api/guide/epg` and `/api/guide/search` took ~11s because
`load_config()` (a full disk read + YAML parse, uncached) was invoked once per program row.
The invariant is explicit in that entry: both endpoints must call `load_config()` a number of
times that does NOT scale with the number of program/result rows.

Strategy: build two throwaway apps seeded with a small vs. large number of EPG programs on one
guide channel, count `load_config()` calls made during each endpoint request, and assert the
count is identical (row-count-independent). Counting patches every module-bound `load_config`
name a request can reach (routes.guide + accounts - the two the BUGS.md entry names), so a
per-row call through `normalize_url`/`render_filename_template` is caught wherever it hides.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402

# Module namespaces that hold their own bound reference to load_config (each did
# `from ..config import load_config` / `from .config import load_config`, so patching
# app.config.load_config alone would miss them).
import app.routes.guide as guide_mod  # noqa: E402
import app.accounts as accounts_mod  # noqa: E402


class _CountingLoadConfig:
    """Patch every load_config binding a guide/search request can reach, counting calls
    while delegating to the real implementation. Restores originals on exit."""

    _TARGETS = (guide_mod, accounts_mod)

    def __init__(self):
        self.count = 0

    def __enter__(self):
        self._originals = [(m, m.load_config) for m in self._TARGETS]
        real = self._originals[0][1]

        def counting(*args, **kwargs):
            self.count += 1
            return real(*args, **kwargs)

        for m, _ in self._originals:
            m.load_config = counting
        return self

    def __exit__(self, *exc):
        for m, orig in self._originals:
            m.load_config = orig
        return False


def _seed_guide_channel_with_programs(n_programs):
    """One in-guide channel with n_programs future EPG rows, all titled 'News' (so the
    shallow search for 'News' matches every one). Committed."""
    acc = seed.make_account()
    ch = seed.make_channel(acc, name='News Channel', in_guide=True)
    base = datetime.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for i in range(n_programs):
        start = base + timedelta(minutes=30 * i)
        db.session.add(seed.EPGEntry(
            channel_id=ch.id, title='News',
            start_time=start, stop_time=start + timedelta(minutes=30)))
    db.session.commit()
    return ch


def _epg_window_args(hours=48):
    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    start = now.strftime('%Y-%m-%dT%H:%M:%S')
    end = (now + timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%S')
    return {'start': start, 'end': end}


class GuideScalingTests(unittest.TestCase):
    """load_config() call count must be independent of the number of program rows."""

    def _count_for(self, path_builder, n_programs):
        t = make_test_app()
        try:
            _seed_guide_channel_with_programs(n_programs)
            path = path_builder()
            with _CountingLoadConfig() as counter:
                resp = t.client.get(path)
            self.assertEqual(resp.status_code, 200,
                             f'{path} returned {resp.status_code}, not 200')
            return counter.count
        finally:
            t.cleanup()

    def _assert_row_independent(self, path_builder):
        small = self._count_for(path_builder, 20)
        large = self._count_for(path_builder, 300)
        # A per-row load_config() would make `large` ~15× `small`. Require exact
        # equality: the count must not depend on row count at all.
        self.assertEqual(
            small, large,
            f'load_config() call count scales with row count '
            f'({small} at 20 rows vs {large} at 300 rows) - a disk-I/O helper is being '
            f'invoked per program row (BUGS.md 2026-07-15 10:34).')

    def test_epg_api_load_config_does_not_scale_with_rows(self):
        self._assert_row_independent(lambda: '/api/guide/epg?' + '&'.join(
            f'{k}={v}' for k, v in _epg_window_args().items()))

    # The sibling case for the airing search that replaced /api/guide/search
    # (dev/changelog/416) lives in tests/test_scaling_pages.py, which is where every
    # channel-search surface's row-scaling guard is - it counts SQL statements as well as
    # config parses, which is the stronger form of the same invariant.


if __name__ == '__main__':
    unittest.main(verbosity=2)
