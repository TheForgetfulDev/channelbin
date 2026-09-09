"""Including past showings costs the NUMBERS, never the answer.

Guards dev/docs/BUGS.md 2026-08-16 @ 02:41:18 PM ET. "Show airings that have ended" is a
checkbox in the standing-options rail, on by default, and turning it off is one click. It puts
every already-ended showing back in scope - 1.35M of 2.23M rows on the live database - and
nothing can narrow that: both program indexes are built over `chan_prog`, which holds FUTURE
showings only by construction. So every aggregate over that state reads the whole table.

Measured over HTTP on the live database with the index perfectly healthy, before this fix: the
facet rail went 7.6s -> 24.4s (the tag facet alone 1.9s -> 14.2s, because `_tag_predicate`
drops its cached channel-id prefilter for that same future-only reason), blew the 20s budget,
and returned a bare 503 reading "Try a narrower search, or try again in a moment." Both halves
of that sentence are false: there is usually nothing typed to narrow, and it never succeeds on
a retry. A caller that did not pass `counts=0&facets=` - which is every caller of this URL
except the search page itself - lost a 0.1s row page along with it.

The policy is dev/changelog/676's, applied to a second cause. The rows always run; the total,
the "Hide X" counts and the facet rail are OPTIONAL aggregates, attempted on a bounded budget
and reported as `declined` with a reason when they cannot be had.

Four things have to hold:

* **the rows still come back, complete, as a 200** - the dead end is the defect;
* **the aggregates are attempted, not refused.** Reading every row of a small install's EPG is
  instant, and there is no shape test that could separate the answerable requests from the
  rest: with the past included the rail measures 1.2s to 24.4s in a continuous spread as
  filters narrow it. Only the cost can decide;
* **the reason is the server's, and it is the RIGHT one.** "Degraded" and "full scan" are
  different facts with different remedies - one clears itself in minutes, the other lasts
  exactly as long as the option stays off - and the page must not re-derive either;
* **nothing about the ordinary path moves.** Past-on, the channel grain, and a healthy
  aggregate all keep the full budget and their 503.

Record: dev/changelog/681.
"""
import copy
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.channel_search import (GRAIN_AIRINGS, GRAIN_CHANNELS, STANDING_BY_KEY,  # noqa: E402
                                SearchState, full_scan_reason)
from app.config import load_config  # noqa: E402
from app.routes import channel_search as route_mod  # noqa: E402
from app.search_index import rebuild_search_indexes  # noqa: E402

SEARCH_URL = '/api/channels/search'
COUNTS_URL = '/api/channels/search/counts'
FACETS_URL = '/api/channels/search/facets'

#: The airing grain with ended showings INCLUDED, and everything else left as the grain's
#: defaults. Spelled as an explicit list because absent means "use the defaults" and an
#: option that hides by default cannot be switched off any other way.
#:
#: Named for what it does to the RESULT rather than for the checkbox, because the checkbox
#: changed direction: it read "Show airings that have ended" and now reads "Show airings
#: that have ended" (dev/changelog/778). `showpast` present is what puts the past back.
PAST_INCLUDED = ('grain=airings&standing=showhidden&standing=shownoepg'
                 '&standing=showuntested&standing=grpdedup&standing=showpast')
PAST_EXCLUDED = PAST_INCLUDED.replace('&standing=showpast', '')


class _Base(unittest.TestCase):
    """A corpus with future showings on the airing grain.

    Every request here is UNTYPED unless a test says otherwise, and that is deliberate: it is
    the exact shape that failed in production (a bare `?grain=airings` with the past included),
    and `degraded_reason()` short-circuits on an empty `q`, so a never-built index cannot
    contaminate the result. Full scan is then the only reason anything is declined.
    """

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        acct = seed.make_account()
        for i in range(6):
            ch = seed.make_channel(acct, stream_id=100 + i, name=f'ESPN {i}', in_guide=True)
            seed.make_epg_entry(ch, title=f'ESPN Tonight {i}', offset_minutes=30)
        db.session.commit()

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def settings(self, **over):
        """Patch the endpoint's config read - the route binds `load_config` at import, so
        `make_test_app(extra_overrides=...)` and a patch of `app.config.load_config` both miss
        it (CLAUDE.md's "overrides are NOT visible to runtime load_config()" rule)."""
        cfg = copy.deepcopy(load_config())
        cfg['search'].update(over)
        return mock.patch.multiple('app.routes.channel_search', load_config=lambda: cfg)

    def get(self, url, qs=PAST_INCLUDED, expect=200):
        resp = self.t.client.get(f'{url}?{qs}')
        self.assertEqual(resp.status_code, expect, resp.get_data(as_text=True)[:400])
        return resp.get_json()

    def make_healthy(self):
        rebuild_search_indexes('test')
        db.session.commit()


class FullScanReasonTests(_Base):
    """The one condition, as pure state. It must be answerable without a database, because the
    route asks before it opens a connection."""

    def state(self, **kw):
        params = {'grain': GRAIN_AIRINGS,
                  'standing': ['showhidden', 'shownoepg', 'showuntested', 'grpdedup',
                               'showpast']}
        params.update(kw)
        return SearchState.from_params(_Params(params))

    def test_the_airing_grain_with_the_past_included_is_a_full_scan(self):
        self.assertTrue(full_scan_reason(self.state()))

    def test_hiding_the_past_is_not(self):
        self.assertEqual(
            full_scan_reason(self.state(standing=['showhidden', 'shownoepg',
                                                  'showuntested', 'grpdedup'])), '')

    def test_the_airing_grain_defaults_are_not(self):
        """`past` is default-ON, so the grain's own first paint must never be a full scan -
        if it were, this policy would decline the numbers on the ordinary view."""
        self.assertEqual(full_scan_reason(SearchState.from_params(
            _Params({'grain': GRAIN_AIRINGS}))), '')

    def test_the_channel_grain_is_never_a_full_scan(self):
        """`past` is scoped to the airing grain, so it is absent from every channel-grain
        state - reading that absence as "the past is included" would decline the numbers on
        the page's default view."""
        self.assertEqual(full_scan_reason(SearchState.from_params(
            _Params({'grain': GRAIN_CHANNELS}))), '')
        self.assertEqual(full_scan_reason(SearchState.from_params(
            _Params({'grain': GRAIN_CHANNELS, 'standing': ['']}))), '')

    def test_it_needs_no_database(self):
        """The route calls this before opening a connection, beside `probe_degraded_reason()`
        and for the same reason. A version that queried anything would put the answer behind
        the pooled connection this whole area exists to stop holding."""
        with mock.patch.object(db, 'session', None):
            self.assertTrue(full_scan_reason(self.state()))

    def test_the_reason_names_the_control_that_causes_it(self):
        """The remedy for a full scan is a checkbox the reader can find, unlike a degraded
        window where the remedy is to wait. The label is read off the registry here so that
        renaming the option fails this test rather than silently leaving the sentence
        pointing at a control that no longer exists under that name."""
        self.assertIn(STANDING_BY_KEY['showpast'].label, full_scan_reason(self.state()))


class _Params(dict):
    """The `getlist` mapping `SearchState.from_params` wants, without a request context."""

    def getlist(self, key):
        value = self.get(key)
        if value is None:
            return []
        return list(value) if isinstance(value, list) else [value]


class RowsSurviveTheirOwnAggregatesTests(_Base):
    """The dead end: a 0.1s row page thrown away for numbers nobody was owed."""

    def test_the_row_endpoint_answers_and_declines_the_numbers(self):
        payload = self.get(SEARCH_URL)
        self.assertTrue(payload['rows'], 'a full-scan search returned no rows')
        self.assertEqual(payload['declined'], ['counts', 'facets'])
        self.assertIsNone(payload['total'])
        self.assertIsNone(payload['pages'])
        self.assertIsNone(payload['standing_hidden'])
        self.assertEqual(payload['facets'], {})
        self.assertEqual(payload['facets_counted'], [])

    def test_the_row_endpoint_says_why(self):
        payload = self.get(SEARCH_URL)
        self.assertEqual(payload['declined_reason'],
                         full_scan_reason(SearchState.from_params(
                             _Params({'grain': GRAIN_AIRINGS,
                                      'standing': ['showhidden', 'shownoepg',
                                                   'showuntested', 'grpdedup',
                                                   'showpast']}))))

    def test_the_facet_rail_is_declined_even_when_explicitly_asked_for(self):
        """Enforcement is server-side: naming dimensions does not opt a caller out of the
        policy protecting the box."""
        payload = self.get(SEARCH_URL, PAST_INCLUDED + '&facets=acct&facets=tag')
        self.assertEqual(payload['facets'], {})
        self.assertIn('facets', payload['declined'])

    def test_hiding_the_past_declines_nothing(self):
        payload = self.get(SEARCH_URL, PAST_EXCLUDED)
        self.assertEqual(payload['declined'], [])
        self.assertEqual(payload['declined_reason'], '')
        self.assertIsInstance(payload['total'], int)
        self.assertTrue(payload['facets_counted'])

    def test_the_channel_grain_declines_nothing(self):
        payload = self.get(SEARCH_URL, 'grain=channels')
        self.assertEqual(payload['declined'], [])
        self.assertIsInstance(payload['total'], int)

    def test_the_rows_are_the_rows_the_full_pipeline_would_have_returned(self):
        """Completeness of the ANSWER is what is traded away, never its correctness.

        CHARACTERIZATION, not a regression guard: it passes against the pre-fix code too,
        because the rows were always correct on a corpus small enough not to time out. It is
        here to fail if a later cost-cutting pass starts trading correctness for speed, which
        is the one trade this policy may not make."""
        with self.settings(full_scan_aggregate_timeout_seconds=0):
            declined = self.get(SEARCH_URL)
        counted = self.get(SEARCH_URL, PAST_EXCLUDED)
        self.assertEqual([r['id'] for r in declined['rows']],
                         [r['id'] for r in counted['rows']])

    def test_declined_reason_is_always_present_even_when_empty(self):
        """Absent-vs-empty is a distinction this page has already been bitten by once
        (`facets`), and one is enough."""
        for url, qs in ((SEARCH_URL, PAST_EXCLUDED), (COUNTS_URL, PAST_EXCLUDED), (FACETS_URL, PAST_EXCLUDED)):
            with self.subTest(url=url):
                self.assertEqual(self.get(url, qs)['declined_reason'], '')


class AggregatesAreAttemptedNotRefusedTests(_Base):
    """A blanket "the past is included, so no numbers" would leave every small install with no
    totals for aggregates costing microseconds."""

    def test_a_cheap_full_scan_count_is_served_rather_than_declined(self):
        payload = self.get(COUNTS_URL)
        self.assertEqual(payload['declined'], [])
        self.assertEqual(payload['total'], 6)

    def test_a_cheap_full_scan_facet_rail_is_served_rather_than_declined(self):
        payload = self.get(FACETS_URL, PAST_INCLUDED + '&facets=acct')
        self.assertEqual(payload['declined'], [])
        self.assertIn('acct', payload['facets_counted'])

    def test_a_zero_budget_declines_without_attempting(self):
        """0 is the operator's off switch, and the deterministic lever a test uses to reach
        the declined branch without racing a real timeout."""
        with self.settings(full_scan_aggregate_timeout_seconds=0):
            counts = self.get(COUNTS_URL)
            facets = self.get(FACETS_URL, PAST_INCLUDED + '&facets=acct')
        self.assertEqual(counts['declined'], ['counts'])
        self.assertIsNone(counts['total'])
        self.assertIsNone(counts['standing_hidden'])
        self.assertEqual(facets['declined'], ['facets'])
        self.assertEqual(facets['facets'], {})
        self.assertEqual(facets['facets_counted'], [])

    def test_a_declined_aggregate_is_a_200_not_a_503(self):
        """The rows are already on screen and correct, so this is an incomplete answer rather
        than a failed request - a 503 would put an error toast over a working page, which is
        precisely the outcome the `declined` contract exists to prevent."""
        with self.settings(full_scan_aggregate_timeout_seconds=0):
            resp = self.t.client.get(f'{COUNTS_URL}?{PAST_INCLUDED}')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        # Asserted together: a bare "it was a 200" also passes against the old code, which
        # answered 200 by computing the count and knowing nothing about declining.
        self.assertEqual(payload['declined'], ['counts'])

    def test_a_declined_aggregate_says_why(self):
        with self.settings(full_scan_aggregate_timeout_seconds=0):
            for url in (COUNTS_URL, FACETS_URL):
                with self.subTest(url=url):
                    self.assertIn('Show airings that have ended',
                                  self.get(url)['declined_reason'])

    def test_the_zero_budget_does_not_touch_a_past_hiding_request(self):
        """`full_scan_aggregate_timeout_seconds` governs full-scan requests only. An ordinary
        airing search must still be computed on the full budget."""
        with self.settings(full_scan_aggregate_timeout_seconds=0):
            payload = self.get(COUNTS_URL, PAST_EXCLUDED)
        self.assertEqual(payload['declined'], [])
        self.assertEqual(payload['total'], 6)

    def test_the_zero_budget_does_not_touch_the_channel_grain(self):
        with self.settings(full_scan_aggregate_timeout_seconds=0):
            payload = self.get(COUNTS_URL, 'grain=channels')
        self.assertEqual(payload['declined'], [])

    def test_a_declined_aggregate_leaves_no_scan_slot_held(self):
        """Teardown releases everything the create path acquired. A leaked slot would block
        every degraded search for the life of the process."""
        with self.settings(full_scan_aggregate_timeout_seconds=0):
            self.get(COUNTS_URL)
            self.get(FACETS_URL, PAST_INCLUDED + '&facets=acct')
        self.assertEqual(route_mod.UNINDEXED_SCANS.held, 0)


class DegradedAndFullScanAreDifferentFactsTests(_Base):
    """Both can hold at once. Which one is reported decides what the reader is told to do."""

    def test_degraded_wins_when_both_hold(self):
        """A degraded window clears itself within minutes (dev/changelog/680), so "wait" is
        the actionable remedy; the full scan lasts exactly as long as the option stays off and
        will still be there afterwards. Naming the permanent one would send the reader to
        change a control that was not what stopped this particular request."""
        # Typed and never indexed: degraded_reason() is non-empty, and the past is included.
        payload = self.get(SEARCH_URL, PAST_INCLUDED + '&q=espn')
        self.assertEqual(payload['declined'], ['counts', 'facets'])
        self.assertEqual(payload['declined_reason'], payload['degraded'])
        self.assertNotIn('Show airings that have ended', payload['declined_reason'])

    def test_the_full_scan_reason_is_reported_once_the_index_is_healthy(self):
        self.make_healthy()
        payload = self.get(SEARCH_URL, PAST_INCLUDED + '&q=espn')
        self.assertEqual(payload['degraded'], '')
        self.assertIn('Show airings that have ended', payload['declined_reason'])

    def test_each_budget_governs_only_its_own_state(self):
        """Two keys because they are two decisions. Zeroing one must not decline the other's
        requests, or an operator tuning a degraded window silently changes what a healthy
        page shows."""
        self.make_healthy()
        with self.settings(degraded_aggregate_timeout_seconds=0):
            self.assertEqual(self.get(COUNTS_URL)['declined'], [])
        with self.settings(full_scan_aggregate_timeout_seconds=0):
            self.assertEqual(self.get(COUNTS_URL, PAST_EXCLUDED)['declined'], [])


if __name__ == '__main__':
    unittest.main()
