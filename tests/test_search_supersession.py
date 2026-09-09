"""A search nobody is waiting for stops: it gives up the scan slot and aborts its own scan.

Guards dev/docs/BUGS.md 2026-08-16 (the scan-slot queue serializing dead work). The page
already aborts a superseded keystroke's request, but an abort is invisible to a WSGI server -
Werkzeug runs the handler thread to completion and notices the dead peer only when it writes
the response. During a degraded window that meant the single `max_concurrent_unindexed` slot
was spent, in full, on searches the page had already thrown away, while the one the user was
watching queued behind them.

Measured live on the 1.9 GB production database with a real index rebuild running, which is
the load a degraded window actually carries: one degraded airings search alone took 11.5s of
its 15s budget, and three searches 0.25s apart ended with the FIRST one - already abandoned -
returning the only complete answer while both later ones 503'd. The live request's log line
read `Search timed out after 0.228517s of a 15s budget`: it had queued away its whole budget,
been handed the slot with 0.23s left, and reported the resulting abort as a timeout.

Three things have to hold:

* **supersession is observed** - a strictly newer `seq` on the same `(sid, lane)` means the
  page moved on, and nothing else does. A different lane, a different page, or an older seq
  arriving late must never cancel live work;
* **it actually stops the work** - both while queued (the slot goes to the next waiter without
  the search running at all) and while scanning (the statement is aborted mid-flight, which
  only SQLite's progress handler can do);
* **the two real failures stay distinguishable** - "never got a turn" and "ran out of time
  running" name different causes and point at different fixes, so the wait is capped below the
  whole budget and both messages say where the time went.

Record: dev/changelog/678.
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
from app.db_utils import QueryAbandoned, QueryDeadlineExceeded, query_deadline  # noqa: E402
from app.routes import channel_search as route_mod  # noqa: E402
from app.routes.channel_search import (LANE_FACETS, LANE_ROWS,  # noqa: E402
                                       SupersessionRegistry)

SEARCH_URL = '/api/channels/search'
COUNTS_URL = '/api/channels/search/counts'
FACETS_URL = '/api/channels/search/facets'

#: Long enough that a thread genuinely blocks on the condition variable, short enough that a
#: test which waits it out costs nothing.
BLINK = 0.05

#: A budget small enough that the deadline is already blown by the time the first statement
#: reaches the progress handler, without any fake clock (tests/test_search_deadline.py's own).
INSTANT = 1e-9


def _route_clock(*ticks):
    """Replace the `time` MODULE the route module reads, not time.monotonic itself.

    Patching time.monotonic globally would also drive query_deadline's clock and threading's,
    so a sequence scripted for the route's two reads runs dry inside the helper under test.
    Same helper as tests/test_search_scan_gate.py, duplicated from there - importing a private
    helper across test modules couples their setup, and this one is four lines.
    """
    scripted = iter(list(ticks) + [ticks[-1]] * 64)
    fake = mock.Mock()
    fake.monotonic = lambda: next(scripted)
    return mock.patch.object(route_mod, 'time', fake)


class RegistryTests(unittest.TestCase):
    """The registry alone - no app, no database, no HTTP."""

    def setUp(self):
        self.reg = SupersessionRegistry()

    def test_a_newer_request_supersedes_an_older_one_on_the_same_page(self):
        """The whole mechanism in one assertion."""
        older = self.reg.arrive('page-a', 1, LANE_ROWS)
        self.assertFalse(older(), 'a request was superseded before anything newer arrived')
        self.reg.arrive('page-a', 2, LANE_ROWS)
        self.assertTrue(older(), 'the older request never learned it had been superseded')

    def test_the_newest_request_is_never_superseded_by_itself(self):
        newest = self.reg.arrive('page-a', 2, LANE_ROWS)
        self.assertFalse(newest())

    def test_an_older_seq_arriving_late_does_not_un_supersede_anything(self):
        """Requests can arrive out of order under load. The registry keeps the high-water
        mark, or a straggler would resurrect work the page has already replaced - and worse,
        cancel the live request that replaced it."""
        newest = self.reg.arrive('page-a', 5, LANE_ROWS)
        straggler = self.reg.arrive('page-a', 3, LANE_ROWS)
        self.assertFalse(newest(), 'a late older request cancelled the live one')
        self.assertTrue(straggler(), 'the straggler did not read as already superseded')

    def test_the_two_lanes_do_not_cancel_each_other(self):
        """Rows and the facet rail are separate counters on the page and run concurrently by
        design, so a rail request must never cancel the rows fetched alongside it."""
        rows = self.reg.arrive('page-a', 1, LANE_ROWS)
        self.reg.arrive('page-a', 9, LANE_FACETS)
        self.assertFalse(rows())

    def test_one_page_does_not_cancel_another(self):
        """Two tabs, or two people. Both are live work."""
        first = self.reg.arrive('page-a', 1, LANE_ROWS)
        self.reg.arrive('page-b', 99, LANE_ROWS)
        self.assertFalse(first())

    def test_a_caller_that_sends_no_id_is_not_tracked_at_all(self):
        """None, not a predicate that always answers False: it reaches query_deadline as
        'install no abandonment check', so a caller outside this page pays nothing."""
        self.assertIsNone(self.reg.arrive('', 1, LANE_ROWS))
        self.assertIsNone(self.reg.arrive('page-a', None, LANE_ROWS))

    def test_the_registry_is_bounded_and_evicts_the_least_recently_used(self):
        for i in range(SupersessionRegistry.MAX_SESSIONS + 50):
            self.reg.arrive(f'page-{i}', 1, LANE_ROWS)
        self.assertLessEqual(len(self.reg._latest), SupersessionRegistry.MAX_SESSIONS)

    def test_an_evicted_request_reads_as_live_rather_than_superseded(self):
        """The conservative direction. A request whose entry has been evicted runs to
        completion exactly as it does today; the opposite default would cancel live work
        because an unrelated page loaded."""
        evicted = self.reg.arrive('page-0', 1, LANE_ROWS)
        for i in range(1, SupersessionRegistry.MAX_SESSIONS + 50):
            self.reg.arrive(f'page-{i}', 1, LANE_ROWS)
        self.assertFalse(evicted())

    def test_reset_forgets_everything(self):
        """tests/support/app.py calls this between modules - two modules both using a short
        literal sid would otherwise share an entry."""
        older = self.reg.arrive('page-a', 1, LANE_ROWS)
        self.reg.arrive('page-a', 2, LANE_ROWS)
        self.assertTrue(older())
        self.reg.reset()
        self.assertFalse(self.reg.arrive('page-a', 1, LANE_ROWS)())


class DeadlineAbandonmentTests(unittest.TestCase):
    """query_deadline's abandonment hook: the only thing that can stop a running statement."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        acct = seed.make_account(name='Alpha')
        for name in ('US| ESPN2 HD', 'Discovery Channel', 'Sky Sports Action'):
            seed.make_channel(acct, name=name)
        db.session.commit()

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def _count(self):
        from app.database import Channel
        return db.session.query(db.func.count(Channel.id)).scalar()

    def test_an_abandoned_query_is_aborted_mid_statement(self):
        with mock.patch('app.db_utils.QUERY_DEADLINE_CHECK_OPS', 1):
            with self.assertRaises(QueryAbandoned):
                with query_deadline(30, label='channels search', abandoned=lambda: True):
                    self._count()

    def test_abandonment_is_reported_as_itself_not_as_a_timeout(self):
        """The distinction is the point: a request cancelled by its own page did not run too
        long, and reporting it as a timeout puts the wrong cause in the log."""
        with mock.patch('app.db_utils.QUERY_DEADLINE_CHECK_OPS', 1):
            try:
                with query_deadline(30, abandoned=lambda: True):
                    self._count()
            except QueryAbandoned:
                pass
            else:
                self.fail('no abandonment was raised')

    def test_a_deadline_still_fires_as_a_timeout_when_nothing_is_abandoned(self):
        with mock.patch('app.db_utils.QUERY_DEADLINE_CHECK_OPS', 1):
            with self.assertRaises(QueryDeadlineExceeded):
                with query_deadline(1e-9, abandoned=lambda: False):
                    self._count()

    def test_a_query_is_cancellable_with_no_deadline_at_all(self):
        """`seconds` falsy is the documented no-deadline escape hatch, and it used to yield
        straight through with no handler installed. Supplying only an abandonment predicate
        has to keep the handler, or 'cancellable but unbounded' is not expressible."""
        with mock.patch('app.db_utils.QUERY_DEADLINE_CHECK_OPS', 1):
            with self.assertRaises(QueryAbandoned):
                with query_deadline(0, abandoned=lambda: True):
                    self._count()

    def test_the_untracked_case_is_byte_identical_to_before(self):
        """No deadline and no predicate still yields straight through."""
        with query_deadline(0, abandoned=None):
            self.assertEqual(self._count(), 3)

    def test_the_handler_is_cleared_so_the_next_request_is_not_poisoned(self):
        """It lives on the pooled DBAPI connection, not on the block - one left installed
        keeps aborting every later request that checks that connection out."""
        with mock.patch('app.db_utils.QUERY_DEADLINE_CHECK_OPS', 1):
            with self.assertRaises(QueryAbandoned):
                with query_deadline(30, abandoned=lambda: True):
                    self._count()
        self.assertEqual(self._count(), 3, 'the progress handler outlived its block')


class _RouteTestCase(unittest.TestCase):
    """A test app has never built its search indexes, so a typed `q` is degraded by
    construction - the same readiness failure a mid-sync rebuild produces."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        acct = seed.make_account(name='Alpha')
        for name in ('US| ESPN2 HD', 'Discovery Channel', 'Sky Sports Action'):
            ch = seed.make_channel(acct, name=name)
            seed.make_epg_entry(ch, title=f'{name} Tonight')
        db.session.commit()
        route_mod.SEARCH_GENERATIONS.reset()
        self.addCleanup(route_mod.SEARCH_GENERATIONS.reset)
        self.addCleanup(self._assert_gate_drained)

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def _assert_gate_drained(self):
        self.assertEqual(route_mod.UNINDEXED_SCANS.held, 0,
                         'the request left a scan slot held')

    def settings(self, *, limit=1, degraded_seconds=5, seconds=5, aggregate_seconds=2):
        """Patch the endpoint's config read - the route binds `load_config` at import, so
        neither `make_test_app(extra_overrides=...)` nor a patch of `app.config.load_config`
        reaches it (CLAUDE.md's "overrides are NOT visible to runtime load_config()")."""
        cfg = copy.deepcopy(load_config())
        cfg['search']['timeout_seconds'] = seconds
        cfg['search']['degraded_timeout_seconds'] = degraded_seconds
        cfg['search']['max_concurrent_unindexed'] = limit
        cfg['search']['degraded_aggregate_timeout_seconds'] = aggregate_seconds
        return mock.patch.multiple('app.routes.channel_search', load_config=lambda: cfg)

    def hold_the_slot(self):
        """Occupy the single scan slot from another thread for the duration of the test."""
        entered = threading.Event()
        release = threading.Event()

        def hold():
            with route_mod.UNINDEXED_SCANS.slot(1):
                entered.set()
                release.wait(10)

        thread = threading.Thread(target=hold)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(5), 'the holder thread never took the slot')
        return release


class QueuedWorkTests(_RouteTestCase):
    """The headline: dead work in the queue stops being dead work ahead of live work."""

    def test_a_request_superseded_while_queued_gives_up_the_slot_without_searching(self):
        """The measured incident, in one test. The first of three rapid searches held the
        only slot and produced the only complete answer, for a search the page had already
        replaced twice - so the request the user was watching starved behind it."""
        release = self.hold_the_slot()
        searched = []
        real_search = route_mod.search

        def counting_search(*a, **kw):
            searched.append(True)
            return real_search(*a, **kw)

        answer = {}

        def queued_request():
            with self.settings(degraded_seconds=5):
                answer['resp'] = self.t.client.get(
                    f'{SEARCH_URL}?q=espn&sid=page-a&seq=1')

        with mock.patch.object(route_mod, 'search', counting_search):
            thread = threading.Thread(target=queued_request)
            thread.start()
            try:
                # It is now blocked in the gate behind the holder. The page moves on, exactly
                # as a newer keystroke's request does when it registers its own seq.
                thread.join(BLINK * 4)
                self.assertTrue(thread.is_alive(), 'the request never queued for the slot')
                route_mod.SEARCH_GENERATIONS.arrive('page-a', 2, LANE_ROWS)
                release.set()
                thread.join(10)
            finally:
                release.set()

        self.assertEqual(answer['resp'].status_code, 409, answer['resp'].get_json())
        self.assertEqual(searched, [],
                         'the superseded request ran its search anyway, which is the whole '
                         'defect: it spent the slot on an answer nobody would read')

    def test_a_superseded_request_answers_a_sentence_not_an_empty_result(self):
        """A blank 200 would be indistinguishable on screen from "nothing matched"."""
        route_mod.SEARCH_GENERATIONS.arrive('page-a', 7, LANE_ROWS)
        with self.settings():
            resp = self.t.client.get(f'{SEARCH_URL}?q=espn&sid=page-a&seq=1')
        self.assertEqual(resp.status_code, 409, resp.get_json())
        body = resp.get_json()
        self.assertNotIn('success', body)
        self.assertIn('superseded', body['error'].lower())

    def test_a_request_superseded_while_scanning_is_aborted_mid_statement(self):
        """Queue abandonment alone would not have fixed the measured case: the dead request
        was already RUNNING and holding the slot. Only the progress handler can stop that."""
        real_search = route_mod.search

        def superseding_search(*a, **kw):
            # Inside the slot and inside the armed deadline: the page moves on after this
            # request has already started scanning, which is the case the queue check above
            # cannot reach.
            route_mod.SEARCH_GENERATIONS.arrive('page-a', 2, LANE_ROWS)
            return real_search(*a, **kw)

        with mock.patch('app.db_utils.QUERY_DEADLINE_CHECK_OPS', 1), \
                mock.patch.object(route_mod, 'search', superseding_search):
            with self.settings():
                resp = self.t.client.get(f'{SEARCH_URL}?q=espn&sid=page-a&seq=1')
        self.assertEqual(resp.status_code, 409, resp.get_json())

    def test_the_live_request_still_answers(self):
        """Cancelling the dead ones must not cancel the one the page is waiting for."""
        with self.settings():
            resp = self.t.client.get(f'{SEARCH_URL}?q=espn&sid=page-a&seq=1')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue(resp.get_json()['success'])

    def test_a_caller_that_sends_no_id_behaves_exactly_as_before(self):
        """This URL contract is an API other pages link into. A caller that does not track
        its own requests is never superseded - it is not an error to omit these."""
        route_mod.SEARCH_GENERATIONS.arrive('page-a', 99, LANE_ROWS)
        with self.settings():
            resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 200, resp.get_json())

    def test_a_malformed_seq_is_ignored_rather_than_rejected(self):
        """It describes the caller's bookkeeping, not the search. Refusing to search over it
        would trade a slow answer for no answer."""
        with self.settings():
            resp = self.t.client.get(f'{SEARCH_URL}?q=espn&sid=page-a&seq=banana')
        self.assertEqual(resp.status_code, 200, resp.get_json())

    def test_the_session_id_never_reaches_the_address_bar(self):
        """`query_string` is what the page puts back in the URL. A frozen sid in a shared
        link would cancel the recipient's search against a page load that ended long ago."""
        with self.settings():
            resp = self.t.client.get(f'{SEARCH_URL}?q=espn&sid=page-a&seq=1')
        qs = resp.get_json()['query_string']
        self.assertNotIn('sid', qs)
        self.assertNotIn('seq', qs)


class AggregateLaneTests(_RouteTestCase):
    """The rail and the totals cancel too - while degraded they take the same scan slot."""

    def test_a_superseded_facet_request_is_cancelled(self):
        route_mod.SEARCH_GENERATIONS.arrive('page-a', 5, LANE_FACETS)
        with self.settings():
            resp = self.t.client.get(f'{FACETS_URL}?q=espn&sid=page-a&seq=1')
        self.assertEqual(resp.status_code, 409, resp.get_json())

    def test_a_superseded_counts_request_is_cancelled(self):
        route_mod.SEARCH_GENERATIONS.arrive('page-a', 5, LANE_ROWS)
        with self.settings():
            resp = self.t.client.get(f'{COUNTS_URL}?q=espn&sid=page-a&seq=1')
        self.assertEqual(resp.status_code, 409, resp.get_json())

    def test_counts_shares_the_row_lane(self):
        """The page sequences /counts against the row request it describes, so a newer ROW
        request is what invalidates it. Given its own lane it would never be cancelled."""
        route_mod.SEARCH_GENERATIONS.arrive('page-a', 5, LANE_ROWS)
        with self.settings():
            rows = self.t.client.get(f'{SEARCH_URL}?q=espn&sid=page-a&seq=1')
            counts = self.t.client.get(f'{COUNTS_URL}?q=espn&sid=page-a&seq=1')
        self.assertEqual(rows.status_code, 409)
        self.assertEqual(counts.status_code, 409)

    def test_a_cancelled_aggregate_is_not_reported_as_declined(self):
        """`declined` means "too expensive to answer here". Cancelled work is a different
        fact, and collapsing the two would tell the page a number is unavailable when
        really its own newer request is about to supply it."""
        route_mod.SEARCH_GENERATIONS.arrive('page-a', 5, LANE_FACETS)
        with self.settings():
            resp = self.t.client.get(f'{FACETS_URL}?q=espn&sid=page-a&seq=1')
        self.assertNotIn('declined', resp.get_json())

    def test_a_live_facet_request_is_unaffected(self):
        with self.settings():
            resp = self.t.client.get(f'{FACETS_URL}?q=espn&sid=page-a&seq=1')
        self.assertEqual(resp.status_code, 200, resp.get_json())


class StarvationReportingTests(_RouteTestCase):
    """"Never got a turn" and "ran out of time running" are different causes."""

    def test_the_wait_is_capped_below_the_whole_budget(self):
        """Without a reserve a request could be handed the slot with a rounding error left,
        arm a deadline of that size and report the resulting abort as a timeout - naming the
        wrong cause. Measured live: `Search timed out after 0.228517s of a 15s budget`."""
        seen = []
        real_slot = route_mod.UNINDEXED_SCANS.slot

        def recording_slot(limit, timeout=None):
            seen.append(timeout)
            return real_slot(limit, timeout=timeout)

        with mock.patch.object(route_mod.UNINDEXED_SCANS, 'slot', recording_slot):
            with self.settings(degraded_seconds=10):
                self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(len(seen), 1)
        self.assertLess(seen[0], 10, 'the wait was allowed to consume the whole budget, so a '
                                     'starved request cannot be told from a slow one')
        self.assertGreater(seen[0], 0)

    def test_a_starved_request_says_it_never_started(self):
        self.hold_the_slot()
        with self.settings(degraded_seconds=BLINK):
            resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 503, resp.get_json())
        self.assertIn('never started', resp.get_json()['error'])

    def test_a_timeout_that_queued_says_how_much_of_the_budget_went_to_the_queue(self):
        """Quoting the budget alone was true but not the whole truth: a request that spent
        most of its budget behind another search is not describable as "too broad", and
        saying so sends its reader to narrow a query that was never the problem."""
        logged = []
        # A request handed the slot with its whole 5s budget already spent queuing: the
        # deadline is armed at the clamp's floor and fires on the first statement.
        with mock.patch.object(route_mod.log, 'warning',
                               lambda *a, **kw: logged.append(a[0] % a[1:])), \
                mock.patch('app.db_utils.QUERY_DEADLINE_CHECK_OPS', 1), \
                _route_clock(0.0, 5.0):
            with self.settings(degraded_seconds=5):
                resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 503, resp.get_json())
        self.assertIn('waiting for a turn', resp.get_json()['error'])
        self.assertTrue(any('queued for a scan slot' in line for line in logged), logged)

    def test_a_timeout_that_never_queued_does_not_invent_a_wait(self):
        """The common case is a genuinely slow query with an empty queue. Reporting a wait
        of 0.0s there would be noise in the one message that has to stay readable."""
        with mock.patch('app.db_utils.QUERY_DEADLINE_CHECK_OPS', 1):
            with self.settings(degraded_seconds=INSTANT):
                resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 503, resp.get_json())
        error = resp.get_json()['error']
        self.assertIn('timed out', error.lower())
        self.assertNotIn('waiting for a turn', error)


if __name__ == '__main__':
    unittest.main()
