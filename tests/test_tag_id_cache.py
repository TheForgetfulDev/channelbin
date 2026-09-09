"""The per-tag channel-id memoization in app/channel_search.py (dev/changelog/597).

A tag whose pattern is an ordinary word (`live`, `new`) can match a large enough slice of the
EPG catalog that the "does this channel air something matching" half of the tag predicate
makes SQLite's query planner pick it as the driving predicate over far more selective filters
- measured concretely at 4.5s and 14.4s for two real reproductions, both down to ~0.1-0.2s once
that half is answered from a precomputed id set instead of a live subquery (BUGS.md 2026-08-07,
dev/changelog/597).

**What must hold regardless of whether the cache is hit, missed, or switched off**: the
channel-grain and airing-grain results are byte-identical either way. The cache is a fast path
for one half of one predicate, never a second definition of what a tag matches - so every
correctness test here runs the same search twice, once with the cache on and once forced off
via `search.tag_id_cache_enabled: false`, and asserts the two answers agree.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.config_sandbox import ConfigSandbox  # noqa: E402
from tests.support.iocount import IOCounter, all_engines  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import only_hiding  # noqa: E402
from app import db  # noqa: E402
from app.database import EPGEntry, Tag, TagPattern  # noqa: E402
from app.channel_search import (  # noqa: E402
    DimensionFilter, GRAIN_AIRINGS, GRAIN_CHANNELS, SearchContext, SearchState,
    _cached_tag_channel_ids, _tag_predicate, clear_tag_channel_ids_cache, search,
    _tag_channel_ids_cache)
from app.channel_search_rows import _tags_by_channel  # noqa: E402
from app.search_index import rebuild_search_indexes  # noqa: E402

CACHE_ON = {}
CACHE_OFF = {'search': {'tag_id_cache_enabled': False}}


class _CacheTestCase(unittest.TestCase):
    """Three channels chosen to separate the two halves of a tag match:

    * `chan_air` carries the pattern only in something it airs (the cached half).
    * `chan_name` carries the pattern only in its own name (the always-live half).
    * `chan_none` carries it nowhere.

    Both future EPG entries also carry the text 'wembley', so a query for it plus the tag
    filter exercises the airing grain's actual per-row semantics ("does THIS showing carry
    the tag"), not just "does the channel ever air it".
    """

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()

        self.acct = seed.make_account()
        self.chan_air = seed.make_channel(self.acct, name='Channel Air',
                                          last_seen_at=datetime.utcnow())
        self.chan_name = seed.make_channel(self.acct, name='Livecast Prime',
                                           last_seen_at=datetime.utcnow())
        self.chan_none = seed.make_channel(self.acct, name='Channel None',
                                           last_seen_at=datetime.utcnow())

        self._epg(self.chan_air, 'Wembley Livecast Special')
        self._epg(self.chan_name, 'Wembley Report')
        self._epg(self.chan_none, 'Some Other Program')

        self.tag = Tag(name='livecast')
        db.session.add(self.tag)
        db.session.flush()
        db.session.add(TagPattern(tag_id=self.tag.id, pattern='LIVECAST'))
        db.session.commit()

        rebuild_search_indexes('test')

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def _epg(self, channel, title, offset_minutes=-30):
        """One showing that is ON RIGHT NOW and still ends in the future.

        Both halves are load-bearing, the same way `test_channel_search.py::_epg` is. On now,
        because the channel grain's tag predicate asks what a channel is airing at this instant
        (dev/changelog/862). Ending in the future, because the "ever airs" half this file is
        really about is answered from `chan_prog`, which is built over `stop_time >= now` - a
        fixture that drifted wholly into the past would stop testing the cache at all while
        still passing.
        """
        start = datetime.utcnow() + timedelta(minutes=offset_minutes)
        entry = EPGEntry(channel_id=channel.id, title=title, start_time=start,
                         stop_time=start + timedelta(hours=1))
        db.session.add(entry)
        db.session.flush()
        return entry

    def ctx(self, cfg=CACHE_ON):
        return SearchContext.build(cfg)


class ChannelGrainCorrectnessTests(_CacheTestCase):
    """Channel grain: "does this channel carry the tag at all" - name OR anything it airs."""

    def test_cache_on_matches_both_channels(self):
        state = SearchState(filters=(DimensionFilter('tag', ('livecast',)),),
                            standing=only_hiding(), grain=GRAIN_CHANNELS)
        names = sorted(r.name for r in search(state, self.ctx(CACHE_ON)).rows)
        self.assertEqual(names, ['Channel Air', 'Livecast Prime'])

    def test_cache_off_gives_the_identical_answer(self):
        state = SearchState(filters=(DimensionFilter('tag', ('livecast',)),),
                            standing=only_hiding(), grain=GRAIN_CHANNELS)
        names = sorted(r.name for r in search(state, self.ctx(CACHE_OFF)).rows)
        self.assertEqual(names, ['Channel Air', 'Livecast Prime'])

    def test_cached_half_covers_only_the_airs_side_not_the_name_side(self):
        """_cached_tag_channel_ids is deliberately the program-side match only - the
        channel-name match stays live (it is already cheap, see DESIGN-channel-search.md
        §6). chan_name's own name carries the pattern but nothing it airs does."""
        ids = _cached_tag_channel_ids(self.tag, ['LIVECAST'], CACHE_ON)
        self.assertEqual(ids, frozenset({self.chan_air.id}))


class ChannelGrainNowScopeTests(_CacheTestCase):
    """The channel grain's "airs something matching" half asks about the program on RIGHT NOW
    (dev/changelog/862), so it agrees with the row's own `Now airing` column and with what a
    typed query on the same grain means.

    A fourth channel is added carrying the pattern only in a showing three hours out. It must
    be absent from the channel grain in every form the page can ask - the filter, the facet
    count and the row badge - while the Guide (EPG) grain's channel badge, which describes a
    row's channel rather than the instant, must still carry it.
    """

    def setUp(self):
        super().setUp()
        self.chan_later = seed.make_channel(self.acct, name='Channel Later',
                                            last_seen_at=datetime.utcnow())
        self._epg(self.chan_later, 'Tonight Livecast Gala', offset_minutes=180)
        db.session.commit()
        rebuild_search_indexes('test')

    def _names(self, cfg):
        state = SearchState(filters=(DimensionFilter('tag', ('livecast',)),),
                            standing=only_hiding(), grain=GRAIN_CHANNELS)
        return sorted(r.name for r in search(state, self.ctx(cfg)).rows)

    def test_a_tag_carried_only_by_a_later_showing_does_not_match(self):
        for cfg, label in ((CACHE_ON, 'cache on'), (CACHE_OFF, 'cache off')):
            with self.subTest(label):
                self.assertEqual(self._names(cfg), ['Channel Air', 'Livecast Prime'])

    def test_the_row_badge_answers_per_grain_the_way_the_filter_does(self):
        """The badge and the filter have to agree on each grain or the page contradicts
        itself: `Channel Later` is tagged on Guide (EPG), where the badge describes its
        channel, and untagged on Channels, where it describes the row."""
        ctx = self.ctx(CACHE_ON)
        ids = [self.chan_air.id, self.chan_later.id]
        self.assertEqual(sorted(_tags_by_channel(ids, ctx, now_scoped=True)),
                         [self.chan_air.id])
        self.assertEqual(sorted(_tags_by_channel(ids, ctx, now_scoped=False)), sorted(ids))

    def test_the_now_half_reaches_the_query_as_ids_not_as_a_subquery(self):
        """The scan behind this costs the same every time it runs and one request asks the
        question about ten times (rows, standing breakdown, every facet), so it is resolved
        once per request. Left embedded it would run inside every facet aggregate."""
        ctx = self.ctx(CACHE_ON)
        pred = str(_tag_predicate(self.tag, True, GRAIN_CHANNELS, cfg=CACHE_ON, ctx=ctx))
        self.assertNotIn('epg_entries', pred)
        self.assertNotIn('chan_prog', pred)
        # And the ids it carries are the now-scoped set, which is strictly narrower here than
        # the "ever airs" one the airing grain still uses - so the shapes matching is not the
        # same thing as the answers matching.
        self.assertEqual(ctx.now_tag_channel_ids(self.tag), (self.chan_air.id,))
        self.assertIn(self.chan_later.id,
                      _cached_tag_channel_ids(self.tag, ['LIVECAST'], CACHE_ON))

    def test_a_second_request_inside_the_ttl_does_not_scan_again(self):
        first_ctx, second_ctx = self.ctx(CACHE_ON), self.ctx(CACHE_ON)
        with IOCounter(all_engines()) as first:
            first_ctx.now_tag_channel_ids(self.tag)
        with IOCounter(all_engines()) as second:
            second_ctx.now_tag_channel_ids(self.tag)
        self.assertGreater(first.queries, 0)
        self.assertEqual(second.queries, 0,
                         'inside the TTL a second request must not touch the database')

    def test_an_edited_pattern_is_a_miss_even_inside_the_ttl(self):
        """This cache expires on a wall clock, not on a watermark, so nothing about an edited
        tag makes an entry look stale by itself - the key has to carry the patterns."""
        self.assertEqual(self.ctx(CACHE_ON).now_tag_channel_ids(self.tag),
                         (self.chan_air.id,))
        db.session.add(TagPattern(tag_id=self.tag.id, pattern='WEMBLEY'))
        db.session.commit()
        self.assertEqual(sorted(self.ctx(CACHE_ON).now_tag_channel_ids(self.tag)),
                         sorted([self.chan_air.id, self.chan_name.id]))


class AiringGrainCorrectnessTests(_CacheTestCase):
    """Airing grain: "does THIS showing carry the tag" - a text query narrows the same way
    the real 4.5s/14.4s reproductions did, so this is the actual pathological shape, not a
    simplified stand-in for it."""

    def _titles(self, cfg):
        state = SearchState(q='wembley', filters=(DimensionFilter('tag', ('livecast',)),),
                            grain=GRAIN_AIRINGS)
        return sorted(r.title for r in search(state, self.ctx(cfg)).rows)

    def test_only_the_row_that_actually_carries_both_matches(self):
        """chan_name's entry ('Wembley Report') matches the text query but not the tag -
        the airing grain must not fall back to "the channel airs the tag somewhere"."""
        self.assertEqual(self._titles(CACHE_ON), ['Wembley Livecast Special'])

    def test_cache_off_gives_the_identical_answer(self):
        self.assertEqual(self._titles(CACHE_OFF), ['Wembley Livecast Special'])


class BareTagFilterUsesTheCacheTests(_CacheTestCase):
    """The first of the two original reproductions - `f.tag=` with NO text query at all - is
    exactly the case a naive reuse of `airing_narrowing_decision` silently defeats: that
    function answers "no positive terms" for an empty query, which is a fact about the
    query's own typed terms and has nothing to do with whether the tag's own cached set is
    usable. `_tag_predicate` must gate the cached prefilter on `hides_past` alone. Caught live
    against the production database (dev/changelog/597) - the small fixture here returns the
    same *rows* whichever gate is used (this was always a performance bug, not a correctness
    one), so the structural check below is what actually guards it."""

    def test_the_cached_prefilter_is_used_even_with_no_text_query(self):
        used = _tag_predicate(self.tag, True, GRAIN_AIRINGS, narrow=False, cfg=CACHE_ON,
                              hides_past=True)
        unused = _tag_predicate(self.tag, True, GRAIN_AIRINGS, narrow=False, cfg=CACHE_ON,
                                hides_past=False)
        self.assertIn('channel_id IN', str(used))
        self.assertNotIn('channel_id IN', str(unused))

    def test_a_bare_tag_filter_with_no_text_query_still_returns_the_right_row(self):
        state = SearchState(filters=(DimensionFilter('tag', ('livecast',)),),
                            grain=GRAIN_AIRINGS)
        titles = sorted(r.title for r in search(state, self.ctx(CACHE_ON)).rows)
        self.assertEqual(titles, ['Wembley Livecast Special'])


class InvalidationTests(_CacheTestCase):

    def test_a_second_call_with_nothing_changed_is_a_cache_hit(self):
        """A hit still costs one cheap `SELECT MAX(id)` watermark check (that IS the
        invalidation signal), but must never re-run the per-pattern chan_prog_fts lookup a
        miss does - which is the whole point of caching it."""
        with IOCounter(all_engines()) as first:
            ids1 = _cached_tag_channel_ids(self.tag, ['LIVECAST'], CACHE_ON)
        with IOCounter(all_engines()) as second:
            ids2 = _cached_tag_channel_ids(self.tag, ['LIVECAST'], CACHE_ON)
        self.assertLess(second.queries, first.queries,
                        'an unchanged tag should cost far less than a fresh computation')
        self.assertEqual(second.queries, 1, 'a hit should cost only the watermark check')
        self.assertEqual(ids1, ids2)

    def test_a_landed_epg_sync_invalidates_the_cache(self):
        _cached_tag_channel_ids(self.tag, ['LIVECAST'], CACHE_ON)
        self._epg(self.chan_none, 'Breaking Livecast Update')
        rebuild_search_indexes('test')
        with IOCounter(all_engines()) as c:
            ids = _cached_tag_channel_ids(self.tag, ['LIVECAST'], CACHE_ON)
        self.assertGreater(c.queries, 0, 'the watermark moved, so this must recompute')
        self.assertEqual(ids, frozenset({self.chan_air.id, self.chan_none.id}))

    def test_an_edited_pattern_invalidates_the_cache_with_no_watermark_change(self):
        _cached_tag_channel_ids(self.tag, ['LIVECAST'], CACHE_ON)
        # A different pattern, same tag id, same underlying data - only the KEY changed.
        ids = _cached_tag_channel_ids(self.tag, ['WEMBLEY'], CACHE_ON)
        self.assertEqual(ids, frozenset({self.chan_air.id, self.chan_name.id}))

    def test_clear_empties_the_cache(self):
        _cached_tag_channel_ids(self.tag, ['LIVECAST'], CACHE_ON)
        self.assertNotEqual(_tag_channel_ids_cache, {})
        clear_tag_channel_ids_cache()
        self.assertEqual(_tag_channel_ids_cache, {})


class ConfigToggleTests(_CacheTestCase):

    def test_disabled_always_returns_none(self):
        self.assertIsNone(_cached_tag_channel_ids(self.tag, ['LIVECAST'], CACHE_OFF))

    def test_disabled_never_populates_the_cache(self):
        _cached_tag_channel_ids(self.tag, ['LIVECAST'], CACHE_OFF)
        self.assertEqual(_tag_channel_ids_cache, {})

    def test_missing_search_block_defaults_to_enabled(self):
        """A config predating this setting (or a hand-built SearchContext without a `search`
        block) must not silently disable the cache - the shipped default is on."""
        self.assertIsNotNone(_cached_tag_channel_ids(self.tag, ['LIVECAST'], {}))

    def test_a_tag_with_no_patterns_matches_nothing_cache_on_or_off(self):
        empty_tag = Tag(name='unfinished')
        db.session.add(empty_tag)
        db.session.commit()
        for cfg in (CACHE_ON, CACHE_OFF):
            pred = _tag_predicate(empty_tag, True, GRAIN_CHANNELS, cfg=cfg)
            self.assertIs(pred, db.false())


class TagDeleteEvictsCacheTests(unittest.TestCase):
    """Integration: deleting a tag through the real route must not leave its cache entry
    behind under a tag id nothing looks up again."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        for tag in Tag.query.all():
            db.session.delete(tag)
        db.session.commit()
        self.acct = seed.make_account()
        self.chan = seed.make_channel(self.acct, name='Channel Air',
                                      last_seen_at=datetime.utcnow())
        entry = EPGEntry(channel_id=self.chan.id, title='Livecast Special',
                         start_time=datetime.utcnow() + timedelta(minutes=30),
                         stop_time=datetime.utcnow() + timedelta(hours=1, minutes=30))
        db.session.add(entry)
        self.tag = Tag(name='livecast')
        db.session.add(self.tag)
        db.session.flush()
        db.session.add(TagPattern(tag_id=self.tag.id, pattern='LIVECAST'))
        db.session.commit()
        rebuild_search_indexes('test')

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_delete_evicts_the_tag_from_the_cache(self):
        _cached_tag_channel_ids(self.tag, ['LIVECAST'], CACHE_ON)
        self.assertIn(self.tag.id, _tag_channel_ids_cache)

        resp = self.client.delete(f'/api/tags/{self.tag.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(self.tag.id, _tag_channel_ids_cache)


class SettingsToggleClearsCacheTests(ConfigSandbox):
    """Integration: flipping the Settings toggle off must actually free the cached RAM, not
    just stop it from growing - and back on should start from a clean slate."""

    def setUp(self):
        super().setUp()
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()
        clear_tag_channel_ids_cache()

    def _csrf(self):
        import re
        html = self.client.get('/settings').get_data(as_text=True)
        m = re.search(r'name="csrf-token" content="([^"]+)"', html)
        return m.group(1) if m else None

    def test_flipping_the_field_off_clears_whatever_was_cached(self):
        _tag_channel_ids_cache[123] = ((), 'x', frozenset())
        self.assertNotEqual(_tag_channel_ids_cache, {})

        tok = self._csrf()
        resp = self.client.post('/api/settings/field',
                                json={'path': 'search.tag_id_cache_enabled', 'value': False},
                                headers={'X-CSRFToken': tok})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_tag_channel_ids_cache, {})


if __name__ == '__main__':
    unittest.main()
