"""Rows are not optional; the numbers around them are.

Guards dev/docs/BUGS.md 2026-08-16. When the search index is unusable - which happens on
every account sync, ~8x a day - a typed search falls back to a LIKE scan over the whole
`epg_entries` table. Before this, ONE keystroke fired three of those scans through a single
concurrency slot on one flat budget: the row page, the standing-breakdown COUNT, and a facet
request that re-ran the row page a second time before adding a grouped scan on top. Measured
on the live database that is ~14.6s of CPU (3.4 + 3.6 + 3.4 + 3.8), which is why a 15s budget
returned a 503 and nothing at all.

The policy: the rows always run, and every number around them - the total, the per-standing
-option hidden counts, the facet rail - is an OPTIONAL aggregate that is attempted cheaply,
off the critical path, and reported as `declined` when it cannot be had. A stale window costs
the answer's completeness, not its existence.

Four things have to hold, and each is a separate way for this to be worse than nothing:

* **the rows still come back, complete**, and the request is a 200 - a degraded search that
  answered nothing is the defect being fixed;
* **the aggregates are attempted, not refused.** "Never built" is one of the four degraded
  states, so a fresh install is permanently degraded; a blanket refusal would leave a
  300-channel install with no totals forever for counts costing microseconds. The COST
  decides, which is what makes the policy self-tuning;
* **an optional aggregate never delays the rows** - it takes a non-blocking slot, so it is
  shed instantly rather than queued ahead of the request the user is waiting on;
* **every decline is visible.** A declined number is reported as declined, never as a zero
  and never as a spinner that runs forever. That is product principle 1, and it is the whole
  reason this is not simply "drop the counts when busy".

The healthy path is deliberately unchanged and asserted to be: full budget, ordinary slot,
503 on failure. A caller that asked for a total on a healthy index is owed an error, not a
shrug.

Record: dev/changelog/676.
"""
import copy
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.db_utils import QueryDeadlineExceeded  # noqa: E402
from app.routes import channel_search as route_mod  # noqa: E402
from app.search_index import rebuild_search_indexes  # noqa: E402

SEARCH_URL = '/api/channels/search'
COUNTS_URL = '/api/channels/search/counts'
FACETS_URL = '/api/channels/search/facets'

BLINK = 0.05


class _Base(unittest.TestCase):
    """A corpus with a typed term that matches, on both grains.

    `q` is load-bearing in every test here: `degraded_reason()` returns '' for an empty
    query, so an untyped search is NEVER degraded no matter what the index is doing.
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
        """Patch the endpoint's config read.

        The route binds `load_config` at import, so `make_test_app(extra_overrides=...)` and
        a patch of `app.config.load_config` both miss it - CLAUDE.md's "overrides are NOT
        visible to runtime load_config()" rule, in its module-binding form.
        """
        cfg = copy.deepcopy(load_config())
        cfg['search'].update(over)
        return mock.patch.multiple('app.routes.channel_search', load_config=lambda: cfg)

    def get(self, url, qs='q=espn', expect=200):
        resp = self.t.client.get(f'{url}?{qs}')
        self.assertEqual(resp.status_code, expect, resp.get_data(as_text=True)[:400])
        return resp.get_json()

    def make_healthy(self):
        """Build the indexes so `probe_degraded_reason()` comes back empty."""
        rebuild_search_indexes('test')
        db.session.commit()


class RowsAreNeverOptionalTests(_Base):
    """The half the user is actually waiting on."""

    def test_a_degraded_search_returns_its_rows_and_declines_the_numbers(self):
        payload = self.get(SEARCH_URL)
        self.assertTrue(payload['rows'], 'a degraded search returned no rows')
        self.assertEqual(payload['declined'], ['counts', 'facets'])
        self.assertIsNone(payload['total'])
        self.assertIsNone(payload['pages'])
        self.assertIsNone(payload['standing_hidden'])
        self.assertEqual(payload['facets'], {})
        self.assertEqual(payload['facets_counted'], [])

    def test_the_rows_are_the_same_rows_the_full_pipeline_would_have_returned(self):
        """Completeness of the ANSWER is what is being traded away, never its correctness -
        declining the counts must not quietly change which rows match.

        CHARACTERIZATION, not a regression guard: this passes against the pre-fix code too,
        because the rows were always correct. It is here to fail if a future cost-cutting
        pass starts trading correctness for speed, which is the one trade this policy is not
        allowed to make."""
        degraded = self.get(SEARCH_URL)
        self.make_healthy()
        healthy = self.get(SEARCH_URL)
        self.assertEqual([r['id'] for r in degraded['rows']],
                         [r['id'] for r in healthy['rows']])

    def test_the_facet_rail_is_declined_even_when_explicitly_asked_for(self):
        """Enforcement is server-side. A caller naming dimensions does not get to opt out of
        the policy protecting the box."""
        payload = self.get(SEARCH_URL, 'q=espn&facets=acct&facets=tag')
        self.assertEqual(payload['facets'], {})
        self.assertIn('facets', payload['declined'])

    def test_a_healthy_search_declines_nothing(self):
        self.make_healthy()
        payload = self.get(SEARCH_URL)
        self.assertEqual(payload['declined'], [])
        self.assertIsInstance(payload['total'], int)
        self.assertTrue(payload['facets_counted'])

    def test_an_untyped_search_is_never_degraded_so_nothing_is_declined(self):
        """The unfiltered first paint is not a degraded request even on a never-built index,
        because `degraded_reason()` short-circuits on an empty `q`."""
        payload = self.get(SEARCH_URL, 'q=')
        self.assertEqual(payload['declined'], [])
        self.assertIsInstance(payload['total'], int)


class AggregatesAreAttemptedNotRefusedTests(_Base):
    """The finding that shaped the policy: a blanket "degraded means no counts" would leave
    every small install with no numbers forever."""

    def test_a_cheap_degraded_count_is_served_rather_than_declined(self):
        payload = self.get(COUNTS_URL)
        self.assertEqual(payload['declined'], [])
        self.assertEqual(payload['total'], 6)

    def test_a_cheap_degraded_facet_rail_is_served_rather_than_declined(self):
        payload = self.get(FACETS_URL, 'q=espn&facets=acct')
        self.assertEqual(payload['declined'], [])
        self.assertIn('acct', payload['facets_counted'])

    def test_a_zero_budget_declines_without_attempting(self):
        """0 is the operator's off switch, and the deterministic lever a test uses to reach
        the declined branch without racing a real timeout."""
        with self.settings(degraded_aggregate_timeout_seconds=0):
            counts = self.get(COUNTS_URL)
            facets = self.get(FACETS_URL, 'q=espn&facets=acct')
        self.assertEqual(counts['declined'], ['counts'])
        self.assertIsNone(counts['total'])
        self.assertIsNone(counts['standing_hidden'])
        self.assertEqual(facets['declined'], ['facets'])
        self.assertEqual(facets['facets'], {})
        self.assertEqual(facets['facets_counted'], [])

    def test_a_declined_aggregate_is_a_200_not_a_503(self):
        """The rows are already on screen and correct, so this is an incomplete answer, not
        a failed request - answering 503 would put an error toast over a working page."""
        with self.settings(degraded_aggregate_timeout_seconds=0):
            resp = self.t.client.get(f'{COUNTS_URL}?q=espn')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        # Asserted together: a bare "it was a 200" also passes against the OLD code, which
        # answered 200 by simply computing the count and knowing nothing about declining.
        self.assertEqual(payload['declined'], ['counts'])

    def test_the_zero_budget_does_not_touch_the_healthy_path(self):
        """`degraded_aggregate_timeout_seconds` governs degraded requests only - a healthy
        counts request must still be computed with the full budget."""
        self.make_healthy()
        with self.settings(degraded_aggregate_timeout_seconds=0):
            payload = self.get(COUNTS_URL)
        self.assertEqual(payload['declined'], [])
        self.assertEqual(payload['total'], 6)


class OptionalAggregatesNeverDelayRowsTests(_Base):
    """The non-blocking slot. An optional number that queued would reintroduce exactly the
    latency the policy exists to remove."""

    def hold_the_slot(self):
        """Occupy the single scan slot from another thread for the duration of the test."""
        entered = threading.Event()
        release = threading.Event()

        def hold():
            with route_mod.UNINDEXED_SCANS.slot(1, timeout=5):
                entered.set()
                release.wait(10)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        self.assertTrue(entered.wait(5), 'the holder never took the slot')
        return release, t

    def test_a_degraded_aggregate_sheds_instantly_when_the_slot_is_taken(self):
        """Not merely "eventually gives up" - it must not wait at all. A generous budget is
        used deliberately: if the aggregate queued, this would block on it for 30s."""
        release, t = self.hold_the_slot()
        try:
            with self.settings(max_concurrent_unindexed=1,
                               degraded_aggregate_timeout_seconds=30):
                payload = self.get(COUNTS_URL)
        finally:
            release.set()
            t.join(5)
        self.assertEqual(payload['declined'], ['counts'])
        self.assertIsNone(payload['total'])

    def test_the_shed_aggregate_leaks_no_slot(self):
        release, t = self.hold_the_slot()
        try:
            with self.settings(max_concurrent_unindexed=1,
                               degraded_aggregate_timeout_seconds=30):
                self.get(COUNTS_URL)
                self.get(FACETS_URL, 'q=espn&facets=acct')
        finally:
            release.set()
            t.join(5)
        self.assertEqual(route_mod.UNINDEXED_SCANS.held, 0,
                         'a declined aggregate left a scan slot held')

    def test_the_rows_still_get_their_slot_by_waiting_for_it(self):
        """The other side of the same coin: rows are NOT optional, so the row endpoint still
        queues for its slot rather than shedding instantly.

        CHARACTERIZATION, not a regression guard: this passes against the pre-fix code too,
        since waiting is what the row endpoint always did. It is here so that a later pass
        cannot make rows non-blocking by copying the aggregate path - shedding the rows is
        the behavior this whole item exists to stop."""
        release, t = self.hold_the_slot()
        done = threading.Event()
        out = {}

        def ask():
            with self.t.app.test_request_context():
                pass
            out['payload'] = self.t.client.get(f'{SEARCH_URL}?q=espn').get_json()
            done.set()

        with self.settings(max_concurrent_unindexed=1, degraded_timeout_seconds=10):
            asker = threading.Thread(target=ask, daemon=True)
            asker.start()
            self.assertFalse(done.wait(BLINK),
                             'the row request did not wait for the busy slot')
            release.set()
            self.assertTrue(done.wait(10), 'the row request never got the slot')
            asker.join(5)
        t.join(5)
        self.assertTrue(out['payload']['rows'])


class TheFacetRailHasItsOwnEndpointTests(_Base):
    """The duplicate row query. `fetchFacets()` used to hit the ROW endpoint with
    `facets=<dims>&counts=0`, which built the whole LIMIT-100 page and discarded it - so
    every keystroke paid the page query twice, degraded or not."""

    def test_the_facets_endpoint_carries_the_rail_and_nothing_else(self):
        self.make_healthy()
        payload = self.get(FACETS_URL, 'q=espn&facets=acct')
        self.assertEqual(set(payload), {'success', 'facets', 'facets_counted',
                                        'declined', 'declined_reason'})
        self.assertNotIn('rows', payload)

    def test_it_agrees_with_what_the_bundled_response_says(self):
        """Same numbers, one query shape cheaper - a split that changed the answer would be
        a regression dressed as an optimization."""
        self.make_healthy()
        bundled = self.get(SEARCH_URL, 'q=espn&facets=acct')
        alone = self.get(FACETS_URL, 'q=espn&facets=acct')
        self.assertEqual(alone['facets'], bundled['facets'])
        self.assertEqual(alone['facets_counted'], bundled['facets_counted'])

    def test_it_400s_on_an_unanswerable_state_like_the_other_endpoints(self):
        payload = self.get(FACETS_URL, 'grain=nope', expect=400)
        self.assertIn('error', payload)

    def test_it_honours_the_requested_dimension_set(self):
        self.make_healthy()
        payload = self.get(FACETS_URL, 'q=espn&facets=acct')
        self.assertEqual(payload['facets_counted'], ['acct'])


class HealthyPathIsUnchangedTests(_Base):
    """A healthy aggregate that cannot answer is still an error. The `declined` contract is
    for the degraded window only - inheriting it everywhere would turn a real failure into a
    silently missing number, which is the defect one layer down."""

    def blow_the_budget(self):
        """Make the aggregate itself trip its deadline, deterministically.

        Racing a real `timeout_seconds=0.001` against a six-row corpus is a coin flip - the
        count usually finishes first - so the failure is injected at the one place both
        paths funnel through instead.
        """
        return mock.patch.object(
            route_mod, 'search_counts',
            side_effect=QueryDeadlineExceeded(1, 'counts'))

    def test_a_healthy_aggregate_that_blows_its_budget_is_a_503_not_a_decline(self):
        """The `declined` contract is for the degraded window ONLY. A caller that asked for
        a total on a healthy index and cannot have one is owed an error - inheriting the
        soft answer here would turn a real failure into a silently missing number.

        Passes against the pre-fix code as well (everything 503'd back then), so it is a
        guard against the soft-answer path over-reaching rather than against the original
        defect.
        Its pair below is the one that fails without the fix."""
        self.make_healthy()
        with self.blow_the_budget():
            resp = self.t.client.get(f'{COUNTS_URL}?q=espn')
        self.assertEqual(resp.status_code, 503)
        self.assertIn('error', resp.get_json())

    def test_the_same_failure_while_degraded_is_a_declined_200(self):
        """The pair to the test above: identical failure, opposite answer, and the index
        state is the only thing that differs."""
        with self.blow_the_budget():
            resp = self.t.client.get(f'{COUNTS_URL}?q=espn')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload['declined'], ['counts'])
        self.assertIsNone(payload['total'])

    def test_a_healthy_facets_request_gets_the_full_budget_not_the_degraded_one(self):
        """The degraded knob must be invisible to a healthy request - if the healthy path
        read it, setting it to 0 would silently stop counting the rail app-wide."""
        self.make_healthy()
        with self.settings(degraded_aggregate_timeout_seconds=0):
            payload = self.get(FACETS_URL, 'q=espn&facets=acct')
        self.assertEqual(payload['declined'], [])
        self.assertIn('acct', payload['facets_counted'])


if __name__ == '__main__':
    unittest.main()
