"""The search request's time budget: it fires, it says why, and it leaves nothing behind.

Guards dev/docs/BUGS.md 2026-08-01 05:xx (the sync-window search stampede). During an account
sync the search indexes are stale and every search falls back to a LIKE scan over ~1.9M
`epg_entries` joined to 136,202 `channels`. On 2026-08-01 about ten of those piled onto two
cores, exhausted the SQLAlchemy pool, killed the sync that had opened the window, and one
request ran for roughly nine minutes. Nothing in the app bounded it: a Python-side check
between statements cannot stop a statement already running, and neither can the client
hanging up, because Werkzeug runs a handler thread to completion.

`app/db_utils.py::query_deadline` bounds it with SQLite's own progress handler, which can.
Three things have to hold, and each is a separate way for this to be worse than useless:

* **it fires** - a request that outruns the budget is a 503 with a sentence, never a short
  result that looks like a real answer;
* **it says why** - when the search is running unindexed, the message carries the engine's
  own `degraded_reason()` rather than a second wording invented at the endpoint. That same
  reason also picks WHICH budget applies: a search whose index is usable gets the 60s
  backstop (it has to clear the slowest legitimate request, which since dev/changelog/420 is
  the airing rail's Today chip at 40s, not the 32-64s first paint this was originally sized
  around), a degraded one gets 15s. A single number cannot serve both, and the 2x2 below is
  what pins that;
* **it cleans up** - the progress handler lives on the *pooled* DBAPI connection, so one left
  installed would abort every later request that checks that connection out, turning a slow
  search into a permanently broken app. That is the assertion most worth having here.

`QUERY_DEADLINE_CHECK_OPS` is patched to 1 throughout. The handler runs every N SQLite VM
operations, and a query over a dozen seeded rows finishes in far fewer than the production
20,000 - without the patch the handler would never be invoked and every test here would pass
vacuously. The budget is likewise a nanosecond, so the deadline is already past when the
first check runs and the abort is deterministic rather than a race with the clock.

Record: dev/changelog/418.
"""
import copy
import os
import sys
import unittest
from unittest import mock

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.db_utils import QueryDeadlineExceeded, query_deadline  # noqa: E402

SEARCH_URL = '/api/channels/search'

#: Small enough that time.monotonic() has already passed it by the first check, so the abort
#: does not depend on how fast this machine runs a dozen rows.
INSTANT = 1e-9


class _DeadlineTestCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        acct = seed.make_account(name='Alpha')
        for name in ('US| ESPN2 HD', 'Discovery Channel', 'Sky Sports Action'):
            ch = seed.make_channel(acct, name=name)
            seed.make_epg_entry(ch, title=f'{name} Tonight')
        db.session.commit()

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def budget(self, seconds, degraded_seconds=None):
        """Patch the endpoint's config read AND the check interval, together.

        The route binds `load_config` at import, so `make_test_app(extra_overrides=...)` and
        a patch of `app.config.load_config` both miss it - CLAUDE.md's "overrides are NOT
        visible to runtime load_config()" rule, in its module-binding form.

        Both budgets default to the same value so a test that does not care which one is
        picked cannot pass by accident on the other.
        """
        cfg = copy.deepcopy(load_config())
        cfg['search']['timeout_seconds'] = seconds
        cfg['search']['degraded_timeout_seconds'] = (
            seconds if degraded_seconds is None else degraded_seconds)
        return mock.patch.multiple(
            'app.routes.channel_search', load_config=lambda: cfg), mock.patch(
                'app.db_utils.QUERY_DEADLINE_CHECK_OPS', 1)


class DeadlineFiresTests(_DeadlineTestCase):

    def test_blown_budget_is_a_503_with_a_message(self):
        """A request that outruns the budget fails loudly - never a 200 with fewer rows.

        A short result is indistinguishable on screen from a real answer, which is the exact
        silence the project's founding principle exists to stop.
        """
        cfg_patch, ops_patch = self.budget(INSTANT)
        with cfg_patch, ops_patch:
            resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 503, resp.get_json())
        body = resp.get_json()
        self.assertNotIn('success', body)
        self.assertIn('timed out', body['error'].lower())

    def test_message_names_the_stale_index_when_the_search_is_degraded(self):
        """The reason is the engine's own wording, not a second sentence at the endpoint.

        A test app has never built its search indexes, which is one of the four readiness
        failures - the same class of state a mid-sync rebuild produces.
        """
        cfg_patch, ops_patch = self.budget(INSTANT)
        with cfg_patch, ops_patch:
            resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        error = resp.get_json()['error']
        self.assertIn('search index', error)
        self.assertIn('without its index', error)

    def test_message_is_generic_when_the_search_is_not_degraded(self):
        """No typed query means no unindexed scan, so the message must not blame the index.

        Doubles as the prepare-abort case: with no `q` the first statement inside the budget
        is the standing breakdown's window-function CASE, which aborts while SQLite is still
        PREPARING it and therefore reports something other than `interrupted`. An earlier
        draft discriminated on that word and turned this exact request into a 500.
        """
        cfg_patch, ops_patch = self.budget(INSTANT)
        with cfg_patch, ops_patch:
            resp = self.t.client.get(f'{SEARCH_URL}?facets=')
        error = resp.get_json()['error']
        self.assertIn('timed out', error.lower())
        self.assertNotIn('search index', error)

    def test_a_degraded_search_is_bounded_by_the_degraded_budget(self):
        """The two budgets are not interchangeable, and the reason is what picks between them.

        A test app has never built its search indexes, so a typed query runs unindexed - the
        sync-window state. Only the degraded budget is armed here, so a 503 can only mean the
        route read the right one.
        """
        cfg_patch, ops_patch = self.budget(0, degraded_seconds=INSTANT)
        with cfg_patch, ops_patch:
            resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 503, resp.get_json())

    def test_a_healthy_search_is_not_bounded_by_the_degraded_budget(self):
        """The other half. An untyped search is not degraded, so the tight budget must not
        reach it - shipped, the tight one is 15s and legitimate healthy requests still run
        longer than that (the airing rail's Today chip is 40s, dev/changelog/420)."""
        cfg_patch, ops_patch = self.budget(0, degraded_seconds=INSTANT)
        with cfg_patch, ops_patch:
            resp = self.t.client.get(f'{SEARCH_URL}?facets=')
        self.assertEqual(resp.status_code, 200, resp.get_json())

    def test_a_healthy_search_is_bounded_by_the_backstop(self):
        """And the backstop still applies to it - nothing is unbounded."""
        cfg_patch, ops_patch = self.budget(INSTANT, degraded_seconds=0)
        with cfg_patch, ops_patch:
            resp = self.t.client.get(f'{SEARCH_URL}?facets=')
        self.assertEqual(resp.status_code, 503, resp.get_json())

    def test_a_degraded_search_is_not_bounded_by_the_backstop(self):
        """Completing the 2x2: the backstop is not consulted once a search is degraded."""
        cfg_patch, ops_patch = self.budget(INSTANT, degraded_seconds=0)
        with cfg_patch, ops_patch:
            resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 200, resp.get_json())

    def test_zero_disables_the_deadline(self):
        """The documented escape hatch. Same request, same check interval, no deadline."""
        cfg_patch, ops_patch = self.budget(0)
        with cfg_patch, ops_patch:
            resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue(resp.get_json()['success'])

    def test_ordinary_search_is_untouched_by_the_shipped_budget(self):
        """The default budget must be invisible: no patch at all, and the search answers."""
        resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual([r['name'] for r in resp.get_json()['rows']], ['US| ESPN2 HD'])


class HandlerIsClearedTests(_DeadlineTestCase):

    def test_a_timed_out_request_does_not_poison_the_next_one(self):
        """The pooled-connection leak, at the endpoint.

        The test app's UI pool is a QueuePool driven from one thread, so the request
        after a timeout gets the same DBAPI connection back. A progress handler left on it
        carries a deadline already in the past, so every statement on that connection would
        abort - a slow search would break the app until a restart rather than until the sync
        finished.
        """
        cfg_patch, ops_patch = self.budget(INSTANT)
        with cfg_patch, ops_patch:
            self.assertEqual(self.t.client.get(f'{SEARCH_URL}?q=espn').status_code, 503)
        resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual([r['name'] for r in resp.get_json()['rows']], ['US| ESPN2 HD'])

    def test_repeated_timeouts_do_not_exhaust_the_pool(self):
        """More timeouts than the pool holds, then a normal request still answers.

        A deadline that fired mid-transaction without rolling back would leave its connection
        checked out, so this is the fix moving the pool exhaustion rather than ending it.

        The loop count is derived from the pool's actual ceiling, never typed: the ceiling is
        a config value now (dev/changelog/423), and a hardcoded 20 would quietly stop
        exceeding it the first time someone raised `database.max_overflow`.
        """
        pool = db.engines[None].pool
        attempts = pool.size() + pool._max_overflow + 5
        cfg_patch, ops_patch = self.budget(INSTANT)
        with cfg_patch, ops_patch:
            for _ in range(attempts):
                self.assertEqual(self.t.client.get(f'{SEARCH_URL}?q=espn').status_code, 503)
        self.assertEqual(self.t.client.get(f'{SEARCH_URL}?q=espn').status_code, 200)


class QueryDeadlineHelperTests(_DeadlineTestCase):
    """Unit-level cover for app/db_utils.py::query_deadline itself.

    Characterization of the helper rather than a guard on the endpoint's behavior - the
    endpoint cases above are what fail when the route wiring is reverted. These pin the
    contract the route depends on: which exception type comes out, that an unrelated error
    passes through untouched, and that the handler is off the raw connection afterwards.
    """

    def test_raises_query_deadline_exceeded_not_operational_error(self):
        with mock.patch('app.db_utils.QUERY_DEADLINE_CHECK_OPS', 1):
            with self.assertRaises(QueryDeadlineExceeded) as caught:
                with query_deadline(INSTANT, label='channels search'):
                    db.session.execute(text('SELECT COUNT(*) FROM channels')).scalar()
        self.assertIn('channels search', str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, OperationalError)

    def test_an_abort_during_statement_preparation_is_still_a_deadline(self):
        """Where the abort lands must not change what comes out of the helper.

        Measured on SQLite 3.45.1: a statement aborted during `sqlite3_step` reports
        `interrupted`, while one aborted while it is still being prepared reports
        `expected 0 columns for '' but got 5` from the subquery column-name resolver. The
        helper keys off its own handler having fired, never off the message, precisely so
        both come out as QueryDeadlineExceeded. The statement below carries a windowed
        subquery and has not been prepared before, which is the prepare-abort shape.
        """
        sql = ('SELECT COUNT(*) FROM (SELECT id, row_number() OVER (ORDER BY id) AS rn '
               'FROM channels) AS x WHERE x.rn > 0')
        with mock.patch('app.db_utils.QUERY_DEADLINE_CHECK_OPS', 1):
            with self.assertRaises(QueryDeadlineExceeded):
                with query_deadline(INSTANT):
                    db.session.execute(text(sql)).scalar()

    def test_an_unrelated_operational_error_passes_through(self):
        """Only OUR interrupt is translated. Anything else is somebody else's problem."""
        with self.assertRaises(OperationalError) as caught:
            with query_deadline(30, label='channels search'):
                db.session.execute(text('SELECT 1 FROM no_such_table_here')).scalar()
        self.assertIn('no such table', str(caught.exception))

    def test_handler_is_off_the_raw_connection_after_a_timeout(self):
        """Checked on the connection object itself, so it does not rely on pool behavior."""
        raw = db.session.connection().connection.dbapi_connection
        with mock.patch('app.db_utils.QUERY_DEADLINE_CHECK_OPS', 1):
            with self.assertRaises(QueryDeadlineExceeded):
                with query_deadline(INSTANT):
                    db.session.execute(text('SELECT COUNT(*) FROM channels')).scalar()
        self.assertEqual(raw.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 3)

    def test_handler_is_off_the_raw_connection_after_an_unrelated_error(self):
        raw = db.session.connection().connection.dbapi_connection
        with self.assertRaises(OperationalError):
            with query_deadline(30):
                db.session.execute(text('SELECT 1 FROM no_such_table_here')).scalar()
        db.session.rollback()
        self.assertEqual(raw.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 3)


if __name__ == '__main__':
    unittest.main()
