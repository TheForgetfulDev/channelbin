"""Tier 2 - search-index readiness on /api/nav-status, and the one speller behind it.

dev/changelog/427. During an account sync the indexes go stale and every search falls back
to a LIKE scan over 1.9M rows; on 2026-08-01 ten of those at once pegged both cores for 16
minutes and killed the sync by exhausting the connection pool. The page could only say so
AFTER a slow request came back (the `unindexed` badge, dev/changelog/418). This carries the
same readiness answer on the nav-status poll the page already runs, so the notice can be up
before anything is typed.

The assertion that matters most here is WORDING IDENTITY. There are now two surfaces showing
the user why search is slow, and the whole reason readiness_map() was extracted is that they
must show the same sentence - a second copy of the wording is how the badge and the notice
end up disagreeing. ReasonWordingTests is what stops that; the rest is characterization of a
new payload key.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import search_index as SI  # noqa: E402
from app.channel_search import SearchContext  # noqa: E402
from app.database import SearchIndexState  # noqa: E402

NAV_URL = '/api/nav-status'


def _seed_searchable():
    acct = seed.make_account()
    ch = seed.make_channel(acct, name='US| ESPN2 HD', in_guide=True)
    seed.make_epg_entry(ch, title='SportsCenter')
    db.session.commit()
    return acct, ch


def _mark(name, status=SI.STATUS_OK, watermark=None, error=None):
    """Put one index into a given recorded state, watermark included, without rebuilding
    it - every degraded case below is a state row, not a half-built index."""
    row = SearchIndexState.query.filter_by(name=name).first()
    if row is None:
        row = SearchIndexState(name=name)
        db.session.add(row)
    row.status = status
    row.error = error
    row.source_watermark = (SI.source_watermark(name) if watermark is None else watermark)
    db.session.commit()
    return row


def _all_ready():
    for name in SI.SEARCH_INDEX_NAMES:
        _mark(name)


class NavStatusPayloadTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.client
        with self.t.app.app_context():
            _seed_searchable()

    def tearDown(self):
        self.t.cleanup()

    def _search(self):
        resp = self.client.get(NAV_URL)
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()['search']

    def test_nav_status_carries_both_answers(self):
        """The two keys are the two index SETS a field selection can need, not two indexes."""
        with self.t.app.app_context():
            _all_ready()
        payload = self._search()
        self.assertEqual(set(payload), {'channels', 'programs'})
        for key, answer in payload.items():
            self.assertEqual(set(answer), {'ready', 'reason'}, f'{key} has the wrong shape')
            self.assertIsInstance(answer['ready'], bool)
            self.assertIsInstance(answer['reason'], str)

    def test_all_indexes_healthy_reports_ready_with_no_reason(self):
        with self.t.app.app_context():
            _all_ready()
        payload = self._search()
        self.assertTrue(payload['channels']['ready'])
        self.assertTrue(payload['programs']['ready'])
        self.assertEqual(payload['programs']['reason'], '')

    def test_a_bad_programs_index_leaves_the_channels_answer_ready(self):
        """The point of two answers: a channel-name-only search must not be warned about a
        program index it never touches."""
        with self.t.app.app_context():
            _mark(SI.SEARCH_INDEX_CHANNELS)
            _mark(SI.SEARCH_INDEX_PROGRAMS, status=SI.STATUS_FAILED, error='boom')
        payload = self._search()
        self.assertTrue(payload['channels']['ready'])
        self.assertFalse(payload['programs']['ready'])

    def test_a_bad_channels_index_degrades_both_answers(self):
        """`programs` covers BOTH indexes, so it is False whenever either one is."""
        with self.t.app.app_context():
            _mark(SI.SEARCH_INDEX_CHANNELS, status=SI.STATUS_FAILED, error='boom')
            _mark(SI.SEARCH_INDEX_PROGRAMS)
        payload = self._search()
        self.assertFalse(payload['channels']['ready'])
        self.assertFalse(payload['programs']['ready'])


class DegradedStateTests(unittest.TestCase):
    """All four ways search_index_readiness() can say no, per its own docstring. Each has to
    reach the wire, because each is a real several-minute window: a sync moves the watermark
    8x/day, and BUILDING is the rebuild that ends it."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.client
        with self.t.app.app_context():
            _seed_searchable()

    def tearDown(self):
        self.t.cleanup()

    def _programs_answer(self):
        return self.client.get(NAV_URL).get_json()['search']['programs']

    def test_never_built_is_reported(self):
        with self.t.app.app_context():
            SearchIndexState.query.delete()
            db.session.commit()
        answer = self._programs_answer()
        self.assertFalse(answer['ready'])
        self.assertIn('never been built', answer['reason'])

    def test_building_is_reported(self):
        with self.t.app.app_context():
            _mark(SI.SEARCH_INDEX_CHANNELS)
            _mark(SI.SEARCH_INDEX_PROGRAMS, status=SI.STATUS_BUILDING)
        answer = self._programs_answer()
        self.assertFalse(answer['ready'])
        self.assertIn('being rebuilt', answer['reason'])

    def test_failed_is_reported(self):
        with self.t.app.app_context():
            _mark(SI.SEARCH_INDEX_CHANNELS)
            _mark(SI.SEARCH_INDEX_PROGRAMS, status=SI.STATUS_FAILED, error='disk full')
        answer = self._programs_answer()
        self.assertFalse(answer['ready'])
        self.assertIn('last failed to rebuild', answer['reason'])

    def test_stale_is_reported(self):
        """The sync-window case - the one this whole batch exists for."""
        with self.t.app.app_context():
            _mark(SI.SEARCH_INDEX_CHANNELS)
            _mark(SI.SEARCH_INDEX_PROGRAMS, watermark='something-older')
        answer = self._programs_answer()
        self.assertFalse(answer['ready'])
        self.assertIn('stale', answer['reason'])


class ReasonWordingTests(unittest.TestCase):
    """ONE SPELLER. The notice, the `unindexed` badge's tooltip and the server log all show
    the engine's reason string; if any surface re-types it they drift apart silently."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.client
        with self.t.app.app_context():
            _seed_searchable()

    def tearDown(self):
        self.t.cleanup()

    def test_the_wire_reason_is_byte_identical_to_the_engine(self):
        for status, watermark in ((SI.STATUS_FAILED, None),
                                  (SI.STATUS_BUILDING, None),
                                  (SI.STATUS_OK, 'something-older')):
            with self.subTest(status=status, watermark=watermark):
                with self.t.app.app_context():
                    _mark(SI.SEARCH_INDEX_CHANNELS)
                    _mark(SI.SEARCH_INDEX_PROGRAMS, status=status, watermark=watermark)
                    expected = SI.search_index_readiness(*SI.SEARCH_INDEX_NAMES)
                answer = self.client.get(NAV_URL).get_json()['search']['programs']
                self.assertEqual(answer['ready'], expected[0])
                self.assertEqual(answer['reason'], expected[1])

    def test_readiness_map_covers_exactly_the_two_sets_index_names_can_return(self):
        """If _index_names() ever gains a third answer, readiness_map() has to gain it too
        or SearchContext falls through to a live per-request lookup nobody notices."""
        from app.channel_search import FIELDS, _index_names
        produced = {_index_names((f,)) for f in FIELDS}
        with self.t.app.app_context():
            self.assertEqual(set(SI.readiness_map()), set(SI.READINESS_SETS))
        self.assertTrue(produced.issubset(set(SI.READINESS_SETS)),
                        f'_index_names can return {produced - set(SI.READINESS_SETS)}')

    def test_search_context_readiness_equals_readiness_map(self):
        """The extraction must not have forked the two callers. A SearchContext whose map
        disagreed with nav-status would warn about one thing and degrade over another."""
        with self.t.app.app_context():
            _mark(SI.SEARCH_INDEX_CHANNELS)
            _mark(SI.SEARCH_INDEX_PROGRAMS, watermark='something-older')
            ctx = SearchContext.build()
            self.assertEqual(ctx.readiness, SI.readiness_map())


if __name__ == '__main__':
    unittest.main()
