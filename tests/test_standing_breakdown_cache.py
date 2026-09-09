"""The airing grain's unfiltered standing-breakdown memoization in app/channel_search.py
(dev/changelog/598).

The airing grain's default (unfiltered) first-page load was ~2.1-2.3s, ~1.6s of it one
statement - `_standing_breakdown_compute()`, a `GROUP BY` over the ~1.6M-row
`epg_entries`/`channels` join that answers the total match count and how many rows each of
the four default "Hide X" toggles removes. Two indexes and `ANALYZE` were already measured
against this exact statement and lost (`dev/changelog/414`, `420`) - the fix here is the
caching lever `dev/docs/DESIGN-channel-search.md` §10 names, exactly like the tag-search cache
before it (`tests/test_tag_id_cache.py`, `dev/changelog/597`): watermark-invalidated, keyed by
what actually changes the answer.

**What must hold regardless of whether the cache is hit, missed, or its TTL has passed**: the
`(standing_hidden, total)` answer is byte-identical to what a live, uncached computation would
give right now. The cache is a fast path for one specific case (the airing grain with no text
query and no dimension filters), never a second definition of what the breakdown counts - so
the correctness tests here always compare a cached answer against
`_standing_breakdown_compute()` computed directly.
"""
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.config_sandbox import ConfigSandbox  # noqa: E402
from tests.support.iocount import IOCounter, all_engines  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import only_hiding  # noqa: E402
from app import db  # noqa: E402
from app.accounts import NORM_DISABLED, NORM_MPEGTS  # noqa: E402
from app.channel_search import (  # noqa: E402
    DimensionFilter, GRAIN_AIRINGS, GRAIN_CHANNELS, SearchContext, SearchState,
    _cached_standing_breakdown, _standing_breakdown, _standing_breakdown_cache,
    _standing_breakdown_compute, clear_standing_breakdown_cache, dimension_predicates,
    text_predicates)

CACHE_ON = {}


class _CacheTestCase(unittest.TestCase):
    """A duplicate cluster (`dup`), an un-normalized URL (`notnorm`), a channel-kind group
    with an overlapping showing (`grpdedup`), and one already-ended showing (`past`) - the
    four default-on toggles, each with something real to hide, so a cache hit and a live
    computation can be compared on a non-trivial answer rather than a vacuous (0, {})."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()

        self.acct = seed.make_account(name='Alpha', url_normalization=NORM_MPEGTS,
                                      last_sync_at=datetime.utcnow())
        now = datetime.utcnow()

        self.dup_a = seed.make_channel(self.acct, name='Dup A', health_score=90.0,
                                       last_seen_at=now)
        self.dup_b = seed.make_channel(self.acct, name='Dup B', health_score=10.0,
                                       last_seen_at=now)
        self.dup_a.stream_url = self.dup_b.stream_url = 'http://example.test/live/shared'
        self.dup_a.is_duplicate_stream_url = True
        self.dup_b.is_duplicate_stream_url = True

        self.unnorm = seed.make_channel(self.acct, name='Radio Mount', health_score=70.0,
                                        last_seen_at=now, url_normalizable=False)

        self.grp_a = seed.make_channel(self.acct, name='Group A', health_score=80.0,
                                       last_seen_at=now)
        self.grp_b = seed.make_channel(self.acct, name='Group B', health_score=20.0,
                                       last_seen_at=now)
        seed.make_group(name='Fox', members=[self.grp_a, self.grp_b])

        self.plain = seed.make_channel(self.acct, name='Plain Channel', health_score=50.0,
                                       last_seen_at=now)

        # 'past' bait: already ended.
        seed.make_epg_entry(self.plain, title='Yesterday', offset_minutes=-120,
                            duration_minutes=30)
        # A future showing on every channel, so each is a real row `_standing_breakdown`
        # counts as "still visible" before its own toggle removes it.
        for ch in (self.dup_a, self.dup_b, self.unnorm, self.grp_a, self.grp_b, self.plain):
            seed.make_epg_entry(ch, title='Tonight', offset_minutes=30, duration_minutes=60)
        # grpdedup bait: the SAME program on both group members at the same time.
        seed.make_epg_entry(self.grp_a, title='Shared Special', offset_minutes=90,
                            duration_minutes=30)
        seed.make_epg_entry(self.grp_b, title='Shared Special', offset_minutes=90,
                            duration_minutes=30)

        db.session.commit()

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def ctx(self, cfg=None):
        return SearchContext.build(cfg if cfg is not None else CACHE_ON)

    def unfiltered_airings(self):
        return SearchState(grain=GRAIN_AIRINGS)

    def narrowing_for(self, state, ctx):
        return text_predicates(state, ctx) + dimension_predicates(state, ctx)


class CorrectnessTests(_CacheTestCase):
    """A cached answer and a live one must agree - on the exact scenario above, which has
    something real for all four default toggles to hide."""

    def test_the_cache_answer_matches_a_live_computation(self):
        state = self.unfiltered_airings()
        ctx = self.ctx()
        narrowing = self.narrowing_for(state, ctx)
        live = _standing_breakdown_compute(state, ctx, narrowing)
        cached = _standing_breakdown(state, ctx, narrowing)
        self.assertEqual(live, cached)
        # Something real was found, or this test is not exercising what it claims to.
        self.assertGreater(live[1], 0)
        self.assertTrue(live[0])

    def test_a_second_call_returns_the_identical_answer(self):
        state = self.unfiltered_airings()
        ctx = self.ctx()
        narrowing = self.narrowing_for(state, ctx)
        first = _standing_breakdown(state, ctx, narrowing)
        second = _standing_breakdown(state, ctx, narrowing)
        self.assertEqual(first, second)


class ScopeTests(_CacheTestCase):
    """The cache only ever applies to the airing grain's genuinely unfiltered case -
    anything else always computes live, unchanged."""

    def test_a_text_query_is_never_cached(self):
        state = SearchState(grain=GRAIN_AIRINGS, q='tonight')
        ctx = self.ctx()
        narrowing = self.narrowing_for(state, ctx)
        self.assertIsNone(_cached_standing_breakdown(state, ctx, narrowing))
        self.assertEqual(_standing_breakdown_cache, {})

    def test_a_dimension_filter_is_never_cached(self):
        state = SearchState(grain=GRAIN_AIRINGS,
                            filters=(DimensionFilter('acct', (str(self.acct.id),)),))
        ctx = self.ctx()
        narrowing = self.narrowing_for(state, ctx)
        self.assertIsNone(_cached_standing_breakdown(state, ctx, narrowing))
        self.assertEqual(_standing_breakdown_cache, {})

    def test_the_channel_grain_is_never_cached(self):
        state = SearchState(grain=GRAIN_CHANNELS)
        ctx = self.ctx()
        narrowing = self.narrowing_for(state, ctx)
        self.assertIsNone(_cached_standing_breakdown(state, ctx, narrowing))
        self.assertEqual(_standing_breakdown_cache, {})

    def test_the_unfiltered_airing_case_populates_the_cache(self):
        state = self.unfiltered_airings()
        ctx = self.ctx()
        narrowing = self.narrowing_for(state, ctx)
        _standing_breakdown(state, ctx, narrowing)
        self.assertEqual(len(_standing_breakdown_cache), 1)


class HitMissTests(_CacheTestCase):

    def test_a_hit_costs_far_fewer_queries_than_a_miss(self):
        state = self.unfiltered_airings()
        ctx = self.ctx()
        narrowing = self.narrowing_for(state, ctx)
        with IOCounter(all_engines()) as first:
            _standing_breakdown(state, ctx, narrowing)
        with IOCounter(all_engines()) as second:
            _standing_breakdown(state, ctx, narrowing)
        self.assertLess(second.queries, first.queries,
                        'an unchanged cache entry should cost far less than the real query')

    def test_different_active_standing_options_are_different_cache_entries(self):
        """The cache key carries the exact toggle combination - turning one off must not
        serve the answer for a different set of toggles."""
        ctx = self.ctx()
        full = SearchState(grain=GRAIN_AIRINGS)
        narrowed = SearchState(grain=GRAIN_AIRINGS, standing=only_hiding('showdup'))
        full_answer = _standing_breakdown(full, ctx, self.narrowing_for(full, ctx))
        narrowed_answer = _standing_breakdown(narrowed, ctx, self.narrowing_for(narrowed, ctx))
        self.assertNotEqual(full_answer, narrowed_answer)
        self.assertEqual(len(_standing_breakdown_cache), 2)


class WatermarkInvalidationTests(_CacheTestCase):

    def test_a_new_epg_entry_invalidates_the_cache(self):
        state = self.unfiltered_airings()
        ctx = self.ctx()
        narrowing = self.narrowing_for(state, ctx)
        before = _standing_breakdown(state, ctx, narrowing)

        seed.make_epg_entry(self.plain, title='Late Add', offset_minutes=45,
                            duration_minutes=30)
        db.session.commit()

        with IOCounter(all_engines()) as c:
            after = _standing_breakdown(state, ctx, narrowing)
        self.assertGreater(c.queries, 1, 'a moved watermark must recompute, not hit')
        self.assertNotEqual(before, after)
        self.assertEqual(after[1], before[1] + 1)

    def test_a_new_channel_invalidates_the_cache(self):
        state = self.unfiltered_airings()
        ctx = self.ctx()
        narrowing = self.narrowing_for(state, ctx)
        before = _standing_breakdown(state, ctx, narrowing)

        extra = seed.make_channel(self.acct, name='Fresh', health_score=60.0,
                                  last_seen_at=datetime.utcnow())
        seed.make_epg_entry(extra, title='Fresh Tonight', offset_minutes=30,
                            duration_minutes=60)
        db.session.commit()

        with IOCounter(all_engines()) as c:
            after = _standing_breakdown(state, ctx, narrowing)
        self.assertGreater(c.queries, 1, 'a moved watermark must recompute, not hit')
        self.assertEqual(after[1], before[1] + 1)


class TTLTests(_CacheTestCase):
    """The backstop for what the channels/programs watermark cannot see at all: a health
    check's score update (bumps health_score_updated_at, not last_seen_at - app/health_score.py)
    and a channel-group membership edit (ChannelGroupMember/ChannelGroup carry no watermark of
    their own). Both feed the default-on `dup`/`grpdedup` toggles, and `past` is wall-clock
    driven with no data watermark by definition - the TTL is what actually bounds all three."""

    def test_within_the_ttl_a_health_score_change_still_serves_the_old_answer(self):
        state = self.unfiltered_airings()
        ctx = self.ctx()
        narrowing = self.narrowing_for(state, ctx)
        before = _standing_breakdown(state, ctx, narrowing)

        # Changes which duplicate `dup` would keep - but touches neither channels.last_seen_at
        # nor epg_entries, so the watermark this cache checks does not move.
        self.dup_a.health_score = 5.0
        db.session.commit()

        with patch('app.channel_search.time.monotonic', return_value=100.0):
            _standing_breakdown(state, ctx, narrowing)  # seed the cache at t=100
        with patch('app.channel_search.time.monotonic', return_value=150.0):
            still_cached = _standing_breakdown(state, ctx, narrowing)
        self.assertEqual(still_cached, before)

    def test_past_the_ttl_it_recomputes(self):
        state = self.unfiltered_airings()
        ctx = self.ctx()
        narrowing = self.narrowing_for(state, ctx)

        with patch('app.channel_search.time.monotonic', return_value=100.0):
            _standing_breakdown(state, ctx, narrowing)
        self.dup_a.health_score = 5.0
        db.session.commit()
        with patch('app.channel_search.time.monotonic', return_value=100.0 + 301.0):
            with IOCounter(all_engines()) as c:
                after_ttl = _standing_breakdown(state, ctx, narrowing)
        self.assertGreater(c.queries, 1, 'an expired TTL must recompute, not hit')
        live = _standing_breakdown_compute(state, ctx, narrowing)
        self.assertEqual(after_ttl, live)

    def test_the_ttl_is_configurable(self):
        state = self.unfiltered_airings()
        narrowing = self.narrowing_for(state, self.ctx({'search': {
            'standing_breakdown_cache_ttl_seconds': 10}}))

        with patch('app.channel_search.time.monotonic', return_value=100.0):
            ctx = self.ctx({'search': {'standing_breakdown_cache_ttl_seconds': 10}})
            _standing_breakdown(state, ctx, narrowing)
        with patch('app.channel_search.time.monotonic', return_value=111.0):
            ctx = self.ctx({'search': {'standing_breakdown_cache_ttl_seconds': 10}})
            with IOCounter(all_engines()) as c:
                _standing_breakdown(state, ctx, narrowing)
        self.assertGreater(c.queries, 1, 'an 11s-old entry must miss an explicit 10s TTL')


class NormalizingAccountKeyTests(_CacheTestCase):
    """`ctx.normalizing_account_ids` feeds the `notnorm` toggle, and it comes from account
    config the channels-table watermark cannot see - so it is folded into the cache key
    directly instead of leaning on the TTL for it."""

    def test_flipping_an_accounts_normalization_mode_is_a_different_cache_entry(self):
        state = self.unfiltered_airings()
        ctx_on = self.ctx()
        narrowing = self.narrowing_for(state, ctx_on)
        with_norm = _standing_breakdown(state, ctx_on, narrowing)

        self.acct.url_normalization = NORM_DISABLED
        db.session.commit()
        ctx_off = self.ctx()
        with IOCounter(all_engines()) as c:
            without_norm = _standing_breakdown(state, ctx_off, self.narrowing_for(state, ctx_off))
        self.assertGreater(c.queries, 1, 'a different normalizing-account set must not hit')
        self.assertNotEqual(with_norm, without_norm)
        self.assertEqual(len(_standing_breakdown_cache), 2)


class ClearCacheTests(_CacheTestCase):

    def test_clear_empties_the_cache(self):
        state = self.unfiltered_airings()
        ctx = self.ctx()
        _standing_breakdown(state, ctx, self.narrowing_for(state, ctx))
        self.assertNotEqual(_standing_breakdown_cache, {})
        clear_standing_breakdown_cache()
        self.assertEqual(_standing_breakdown_cache, {})


class SettingsIntegrationTests(ConfigSandbox):
    """Integration: saving a new TTL through Settings must clear whatever is already
    cached, so a SHORTER ttl takes effect immediately rather than after the old entry's
    original (longer) TTL happens to expire on its own."""

    def setUp(self):
        super().setUp()
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()
        clear_standing_breakdown_cache()

    def _csrf(self):
        import re
        html = self.client.get('/settings').get_data(as_text=True)
        m = re.search(r'name="csrf-token" content="([^"]+)"', html)
        return m.group(1) if m else None

    def test_saving_the_ttl_field_clears_whatever_was_cached(self):
        _standing_breakdown_cache[(('showdup',), ())] = (('x', 'y'), 0.0, ({}, 0))
        self.assertNotEqual(_standing_breakdown_cache, {})

        tok = self._csrf()
        resp = self.client.post('/api/settings/field',
                                json={'path': 'search.standing_breakdown_cache_ttl_seconds',
                                      'value': 60},
                                headers={'X-CSRFToken': tok})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_standing_breakdown_cache, {})


if __name__ == '__main__':
    unittest.main()
