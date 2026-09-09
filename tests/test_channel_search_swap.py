"""Channel search on the FTS5 index: parity with LIKE, and every way it degrades back to it.

Guards the swap in dev/changelog/365. The defect class this file exists for is *silent wrong
results*: the index is a cache of the channel table's text, and a query answered from a cache
that no longer matches its source is wrong in a way nothing on the page looks wrong about. So
the parity tests here matter less than the fallback tests - parity failing is loud, a stale
index answering confidently is not.

Two known, deliberate divergences from LIKE, both characterized (not guarded) below:
unicode case folding, where FTS is a strict superset, and the sub-3-character trigram cliff,
which is what the LIKE fallback exists for.
"""
import logging
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import show_all_query  # noqa: E402
from app import db  # noqa: E402
from app.database import Channel, EPGEntry, SearchIndexState  # noqa: E402
from app.search_index import (  # noqa: E402
    SEARCH_INDEX_CHANNELS, SEARCH_INDEX_PROGRAMS, STATUS_FAILED, TRIGRAM_MIN_CHARS,
    apply_channel_search, rebuild_search_indexes, search_index_readiness, source_watermark)

# Deliberately includes the shapes a word-based tokenizer would break on: a leading prefix
# and pipe (`US| ESPN2 HD` is the real-world channel-name shape), punctuation, and spaces.
CORPUS = [
    'US| ESPN2 HD',
    'ESPN Deportes',
    'Discovery Channel',
    'A & E',
    '24/7 News',
    'BBC World News',
    'FÚTBOL Total',
]


class _SearchTestCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acc = seed.make_account()
        for name in CORPUS:
            seed.make_channel(self.acc, name=name, last_seen_at=datetime.utcnow())
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def build(self):
        rebuild_search_indexes('test')
        readiness = search_index_readiness(SEARCH_INDEX_CHANNELS)
        self.assertTrue(readiness[0], f'index should be ready after a rebuild: {readiness[1]}')
        return readiness

    def searched(self, q, **kw):
        return sorted(c.name for c in apply_channel_search(Channel.query, q, **kw))

    def liked(self, q):
        return sorted(c.name for c in Channel.query.filter(Channel.name.ilike(f'%{q}%')))


class ParityTests(_SearchTestCase):
    """The index must return exactly what LIKE returned, or the swap changed behavior."""

    def test_indexed_results_match_like_across_the_corpus(self):
        readiness = self.build()
        for q in ['espn', 'ESPN', 'EsPn', 'Discovery', 'news', 'NEWS', ' & ', '24/7',
                  'World News', 'SPN2', 'US|', 'zzz-no-match']:
            with self.subTest(q=q):
                self.assertEqual(self.searched(q, readiness=readiness), self.liked(q))

    def test_matches_mid_word(self):
        """`SPN2` is inside `ESPN2`. A word-based tokenizer would return nothing here, which
        is the entire reason the trigram tokenizer was chosen over the default."""
        self.assertEqual(self.searched('SPN2', readiness=self.build()), ['US| ESPN2 HD'])

    def test_empty_query_returns_everything(self):
        readiness = self.build()
        self.assertEqual(len(self.searched('', readiness=readiness)), len(CORPUS))

    def test_stream_url_is_not_searched(self):
        """ch_fts also indexes stream_url/epg_channel_id/category_name, so an unscoped MATCH
        would silently ship "search more than the channel name" - a different feature. The
        seeded stream URLs all contain 'example.test'; no channel name does."""
        self.assertEqual(self.searched('example.test', readiness=self.build()), [])
        self.assertEqual(self.liked('example.test'), [])

    def test_ordering_and_pagination_are_unchanged(self):
        readiness = self.build()
        for ch in Channel.query.all():          # spread across categories to make sort visible
            ch.category_name = 'B' if 'News' in ch.name else 'A'
        db.session.commit()
        ordered = apply_channel_search(Channel.query, 'e', readiness=readiness).order_by(
            Channel.category_name, Channel.name)
        expect = [c.name for c in Channel.query.filter(Channel.name.ilike('%e%')).order_by(
            Channel.category_name, Channel.name)]
        self.assertEqual([c.name for c in ordered], expect)
        page1 = ordered.paginate(page=1, per_page=2, error_out=False)
        page2 = ordered.paginate(page=2, per_page=2, error_out=False)
        self.assertEqual([c.name for c in page1.items], expect[:2])
        self.assertEqual([c.name for c in page2.items], expect[2:4])


class FallbackTests(_SearchTestCase):
    """Every way the index becomes unusable must degrade to LIKE, never to zero rows."""

    def test_short_query_falls_back_and_still_matches(self):
        """Below 3 characters FTS5's trigram tokenizer matches *nothing* and does not error.
        Without the fallback this returns an empty list while looking perfectly healthy -
        the single most likely way for the FTS5 swap to ship a silent wrong-results bug."""
        readiness = self.build()
        self.assertLess(2, TRIGRAM_MIN_CHARS + 1)
        self.assertEqual(self.searched('HD', readiness=readiness), self.liked('HD'))
        self.assertEqual(self.searched('HD', readiness=readiness), ['US| ESPN2 HD'])

    def test_never_built_index_falls_back(self):
        readiness = search_index_readiness(SEARCH_INDEX_CHANNELS)
        self.assertFalse(readiness[0])
        self.assertIn('never been built', readiness[1])
        self.assertEqual(self.searched('espn', readiness=readiness), self.liked('espn'))

    def test_failed_rebuild_falls_back(self):
        self.build()
        state = SearchIndexState.query.filter_by(name=SEARCH_INDEX_CHANNELS).first()
        state.status = STATUS_FAILED
        db.session.commit()
        ready, reason = search_index_readiness(SEARCH_INDEX_CHANNELS)
        self.assertFalse(ready)
        self.assertIn('failed to rebuild', reason)

    def test_new_channel_makes_the_index_stale_and_search_still_finds_it(self):
        """The sync commits channel upserts before the EPG fetch, so a sync that fails at the
        fetch leaves committed channels the index has never seen. Status still reads OK."""
        self.build()
        seed.make_channel(self.acc, name='NEW ESPN CHANNEL', last_seen_at=datetime.utcnow())
        db.session.commit()
        ready, reason = search_index_readiness(SEARCH_INDEX_CHANNELS)
        self.assertFalse(ready, 'a channel added since the rebuild must read as stale')
        self.assertIn('stale', reason)
        self.assertIn('NEW ESPN CHANNEL',
                      self.searched('espn', readiness=(ready, reason)))

    def test_renamed_channel_does_not_match_its_old_name(self):
        """The nastier half of staleness: FTS stores the tokenized text, so a stale index goes
        on matching the name the channel used to have. A miss is visible; this is not.

        The rename stamps search_text_updated_at because that is what a real rename does -
        _upsert_channels stamps it for exactly the four columns ch_fts indexes, and the
        watermark reads it rather than last_seen_at, which moved on every matched row of every
        sync and so declared the index stale after syncs that changed nothing
        (dev/changelog/674)."""
        self.build()
        ch = Channel.query.filter_by(name='Discovery Channel').first()
        ch.name = 'Science Channel'
        ch.last_seen_at = datetime.utcnow()
        ch.search_text_updated_at = datetime.utcnow()
        db.session.commit()
        readiness = search_index_readiness(SEARCH_INDEX_CHANNELS)
        self.assertFalse(readiness[0])
        self.assertEqual(self.searched('Discovery', readiness=readiness), [])
        self.assertEqual(self.searched('Science', readiness=readiness), ['Science Channel'])

    def test_deleted_channel_never_matches_even_while_still_indexed(self):
        """Deletion needs no staleness handling: the helper selects bare rowids, and the
        caller's IN drops any whose channel row is gone. This is why COUNT(*) is not in the
        watermark."""
        readiness = self.build()
        db.session.delete(Channel.query.filter_by(name='ESPN Deportes').first())
        db.session.commit()
        self.assertEqual(self.searched('espn', readiness=readiness), ['US| ESPN2 HD'])

    def test_readiness_is_derived_when_the_caller_omits_it(self):
        self.build()
        self.assertEqual(self.searched('espn'), self.liked('espn'))

    def test_watermark_moves_with_the_source(self):
        before = source_watermark(SEARCH_INDEX_CHANNELS)
        seed.make_channel(self.acc, name='Another', last_seen_at=datetime.utcnow())
        db.session.commit()
        self.assertNotEqual(before, source_watermark(SEARCH_INDEX_CHANNELS))


class FallbackLoggingTests(_SearchTestCase):
    """The "don't hide things" principle: a search that quietly got slower must say why."""

    def test_degraded_index_logs_at_info_with_the_reason(self):
        with self.assertLogs('app.search_index', level=logging.INFO) as cm:
            apply_channel_search(Channel.query, 'espn',
                                 readiness=(False, 'the channels search index is stale')).all()
        self.assertTrue(any('using LIKE' in m and 'stale' in m for m in cm.output), cm.output)

    def test_short_query_logs_at_debug(self):
        with self.assertLogs('app.search_index', level=logging.DEBUG) as cm:
            apply_channel_search(Channel.query, 'HD', readiness=self.build()).all()
        self.assertTrue(any('using LIKE' in m and 'trigram minimum' in m for m in cm.output),
                        cm.output)


class CombinedEpgSearchTests(_SearchTestCase):
    """include_epg is off by default and nothing ships with it on yet - it is what the search
    revamp switches on. Tested here so it does not arrive unguarded."""

    def setUp(self):
        super().setUp()
        self.bbc = Channel.query.filter_by(name='BBC World News').first()
        seed.make_epg_entry(self.bbc, title='Antiques Roadshow', offset_minutes=30)
        db.session.commit()

    def test_off_by_default(self):
        self.build()
        self.assertEqual(self.searched('Antiques'), [])

    def test_matches_a_channel_by_what_it_airs(self):
        rebuild_search_indexes('test')
        readiness = search_index_readiness(SEARCH_INDEX_CHANNELS, SEARCH_INDEX_PROGRAMS)
        self.assertTrue(readiness[0], readiness[1])
        self.assertEqual(
            self.searched('Antiques', readiness=readiness, include_epg=True),
            ['BBC World News'])

    def test_fallback_path_agrees_with_the_indexed_path(self):
        rebuild_search_indexes('test')
        indexed = self.searched(
            'Antiques',
            readiness=search_index_readiness(SEARCH_INDEX_CHANNELS, SEARCH_INDEX_PROGRAMS),
            include_epg=True)
        fell_back = self.searched('Antiques', readiness=(False, 'forced'), include_epg=True)
        self.assertEqual(indexed, fell_back)
        self.assertEqual(fell_back, ['BBC World News'])

    def test_past_programs_are_not_matched(self):
        """chan_prog is built from future entries only, so the LIKE fallback has to use the
        same window or the two disagree about what a channel is airing."""
        old = EPGEntry.query.first()
        old.start_time = datetime.utcnow() - timedelta(hours=4)
        old.stop_time = datetime.utcnow() - timedelta(hours=3)
        db.session.commit()
        rebuild_search_indexes('test')
        readiness = search_index_readiness(SEARCH_INDEX_CHANNELS, SEARCH_INDEX_PROGRAMS)
        self.assertEqual(self.searched('Antiques', readiness=readiness, include_epg=True), [])
        self.assertEqual(self.searched('Antiques', readiness=(False, 'forced'),
                                       include_epg=True), [])


class CharacterizationTests(_SearchTestCase):
    """NOT regression guards. These document where FTS and LIKE genuinely differ, so a future
    reader does not "fix" a divergence that was measured and accepted."""

    def test_unicode_case_folding_is_a_superset_of_like(self):
        """FTS5 folds unicode case; SQLite's LIKE folds ASCII only. FTS therefore finds
        `FÚTBOL Total` for the query `fútbol` and LIKE does not. Strictly more results, never
        fewer - an improvement, and the reason no case_sensitive option is wanted."""
        readiness = self.build()
        self.assertEqual(self.searched('fútbol', readiness=readiness), ['FÚTBOL Total'])
        self.assertEqual(self.liked('fútbol'), [])

    def test_two_character_query_would_match_nothing_without_the_fallback(self):
        """The trigram cliff itself, shown directly: a 2-character MATCH returns zero rows and
        raises nothing. test_short_query_falls_back_and_still_matches is the actual guard."""
        self.build()
        rows = db.session.execute(
            db.text('SELECT rowid FROM ch_fts WHERE ch_fts MATCH \'{name} : "HD"\'')).all()
        self.assertEqual(rows, [])


class RouteTests(_SearchTestCase):
    """The route is where enforcement lives - the helper being right is not enough.

    That route is `/api/channels/search`: the Browse tab renders its rows from JSON, so
    `/channels` itself now filters nothing (dev/changelog/400). The `show*` keys are all
    passed because the standing options that hide by default hide rows for reasons that have
    nothing to do with the index, and this file is about index parity.
    """

    def _names(self, query):
        resp = self.t.app.test_client().get(
            f'/api/channels/search?{query}&facets=&{show_all_query()}')
        self.assertEqual(resp.status_code, 200)
        return {row['name'] for row in resp.get_json()['rows']}

    def test_the_search_returns_the_same_channels_indexed_or_not(self):
        self.build()
        indexed = self._names('q=espn')
        self.assertIn('ESPN Deportes', indexed)
        self.assertNotIn('Discovery Channel', indexed)

        seed.make_channel(self.acc, name='Stale ESPN Add', last_seen_at=datetime.utcnow())
        db.session.commit()
        stale = self._names('q=espn')
        self.assertIn('Stale ESPN Add', stale)
        self.assertIn('ESPN Deportes', stale)

    def test_short_query_still_filters(self):
        self.build()
        rows = self._names('q=HD')
        self.assertIn('US| ESPN2 HD', rows)
        self.assertNotIn('Discovery Channel', rows)


if __name__ == '__main__':
    unittest.main()
