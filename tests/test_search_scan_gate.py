"""The cap on concurrent unindexed search scans: it holds, it sheds loudly, it leaks nothing.

Guards dev/docs/BUGS.md 2026-08-01 (the sync-window search stampede, second half). An account
sync moves the search-index watermark, every search falls back to a LIKE scan over ~1.9M
`epg_entries` joined to 136,202 `channels`, and on 2026-08-01 about ten of those ran at once on
two cores. `tests/test_search_deadline.py` covers the first half - what bounds ONE of them. This
file covers the multiplier, which is what actually saturated the box: the pool exhausted, the
account sync died with `QueuePool limit of size 5 overflow 10 reached`, and the index rebuild
that would have ended the degraded window took 608s against a normal 65-176s because it was
starved of the CPU those scans were holding.

Measured on the live database before the cap (dev/changelog/422): one degraded airing search
answered in 10.6s; eight of them at once ALL failed at the 15s budget, so the stampede's yield
was zero results for eight requests' worth of CPU.

Four things have to hold, and each is a separate way for this to be worse than nothing:

* **the cap holds** - only `max_concurrent_unindexed` scans run at once, and it is read per
  request so changing the setting does not need a restart;
* **it gates the right requests** - a search whose index is usable must not queue behind a
  degraded one, or a healthy app inherits the incident's latency;
* **nothing waits while holding a connection** - a queue made of pooled connections instead of
  threads is the same exhaustion in a different place, not a fix. That is the assertion most
  worth having here;
* **it sheds out loud and leaks no slot** - a shed request is a 503 with a sentence, never an
  empty result, and at limit=1 a slot leaked by an exception would fail the search box closed
  for the life of the process.

Record: dev/changelog/422.
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
from app.db_utils import ConcurrencyGate, ConcurrencyGateTimeout  # noqa: E402
from app.routes import channel_search as route_mod  # noqa: E402

SEARCH_URL = '/api/channels/search'

#: Long enough that a thread genuinely blocks on the condition variable, short enough that a
#: test which waits it out costs nothing. Never used as the ONLY thing separating two threads.
BLINK = 0.05


def _route_clock(*ticks):
    """Replace the `time` MODULE the route module looks at, not time.monotonic itself.

    Patching `time.monotonic` globally would also drive query_deadline's own clock and
    threading's, so a scripted sequence meant for the route's two reads runs dry inside the
    helper it is testing and the request 500s instead of doing what the test is checking.
    Swapping the route's module reference keeps the fake clock where it belongs.
    """
    scripted = iter(list(ticks) + [ticks[-1]] * 64)
    fake = mock.Mock()
    fake.monotonic = lambda: next(scripted)
    return mock.patch.object(route_mod, 'time', fake)


class GateTests(unittest.TestCase):
    """app/db_utils.py::ConcurrencyGate itself, with no app or database involved."""

    def setUp(self):
        self.gate = ConcurrencyGate('test scan')

    def test_a_second_holder_blocks_while_the_first_is_inside(self):
        """The whole point: at limit=1 the second thread does not start work."""
        entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()

        def first():
            with self.gate.slot(1):
                entered.set()
                release.wait(5)

        def second():
            with self.gate.slot(1, timeout=5):
                second_entered.set()

        t1 = threading.Thread(target=first)
        t1.start()
        self.assertTrue(entered.wait(5))
        t2 = threading.Thread(target=second)
        t2.start()
        try:
            self.assertFalse(second_entered.wait(BLINK),
                             'the second holder ran while the first still held the slot')
            release.set()
            self.assertTrue(second_entered.wait(5),
                            'the second holder never got the slot after it was released')
        finally:
            release.set()
            t1.join(5)
            t2.join(5)

    def test_two_holders_fit_at_limit_two(self):
        """The cap is the number, not "one" - a limit of 2 admits 2."""
        both_in = threading.Barrier(2, timeout=5)
        errors = []

        def hold():
            try:
                with self.gate.slot(2, timeout=5):
                    both_in.wait()
            except Exception as exc:      # a BrokenBarrierError means they never overlapped
                errors.append(exc)

        threads = [threading.Thread(target=hold) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(errors, [])

    def test_a_blown_wait_raises_rather_than_running_anyway(self):
        """Shedding is a decision, not a fallthrough. A gate that admitted the caller after
        the timeout would look like it worked and cap nothing."""
        entered = threading.Event()
        release = threading.Event()

        def first():
            with self.gate.slot(1):
                entered.set()
                release.wait(5)

        t1 = threading.Thread(target=first)
        t1.start()
        self.assertTrue(entered.wait(5))
        try:
            with self.assertRaises(ConcurrencyGateTimeout) as caught:
                with self.gate.slot(1, timeout=BLINK):
                    self.fail('the gate admitted a caller whose wait had expired')
            self.assertEqual(caught.exception.limit, 1)
            self.assertEqual(caught.exception.seconds, BLINK)
        finally:
            release.set()
            t1.join(5)

    def test_the_slot_is_released_when_the_block_raises(self):
        """A slot leaked by an exception is permanent for the life of the process, and at
        limit=1 that fails the whole feature closed forever."""
        with self.assertRaises(ValueError):
            with self.gate.slot(1):
                raise ValueError('boom')
        self.assertEqual(self.gate.held, 0)
        with self.gate.slot(1, timeout=BLINK):
            pass

    def test_zero_disables_the_cap(self):
        """The documented escape hatch, and the same shape query_deadline(0) already has."""
        with self.gate.slot(1):
            with self.gate.slot(0):
                self.assertEqual(self.gate.held, 1)   # the inner hold was not counted

    def test_the_limit_is_read_on_every_acquire(self):
        """A BoundedSemaphore fixes its size at construction, so a settings change would need
        a restart to take effect - and this is a knob an operator reaches for while something
        is already going wrong."""
        with self.gate.slot(1):
            with self.assertRaises(ConcurrencyGateTimeout):
                with self.gate.slot(1, timeout=BLINK):
                    pass
            with self.gate.slot(2, timeout=BLINK):
                self.assertEqual(self.gate.held, 2)


class _RouteTestCase(unittest.TestCase):
    """Shared setup for the endpoint cases.

    A test app has never built its search indexes, so a typed `q` is degraded by construction -
    the same readiness failure a mid-sync rebuild produces, and the state the gate exists for.
    """

    def setUp(self):
        self.t = make_test_app()
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        acct = seed.make_account(name='Alpha')
        for name in ('US| ESPN2 HD', 'Discovery Channel', 'Sky Sports Action'):
            ch = seed.make_channel(acct, name=name)
            seed.make_epg_entry(ch, title=f'{name} Tonight')
        db.session.commit()
        # Every case here drives the gate directly, so a slot stranded by a previous failing
        # test would make the next one fail for the wrong reason. The gate is process-wide by
        # design (it protects the box's cores, not one app instance), so it outlives the app.
        self.addCleanup(self._assert_gate_drained)

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def _assert_gate_drained(self):
        self.assertEqual(route_mod.UNINDEXED_SCANS.held, 0,
                         'the request left a scan slot held')

    def settings(self, *, limit=1, degraded_seconds=5, seconds=5):
        """Patch the endpoint's config read.

        The route binds `load_config` at import, so `make_test_app(extra_overrides=...)` and a
        patch of `app.config.load_config` both miss it - CLAUDE.md's "overrides are NOT visible
        to runtime load_config()" rule, in its module-binding form.
        """
        cfg = copy.deepcopy(load_config())
        cfg['search']['timeout_seconds'] = seconds
        cfg['search']['degraded_timeout_seconds'] = degraded_seconds
        cfg['search']['max_concurrent_unindexed'] = limit
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


class SheddingTests(_RouteTestCase):

    def test_a_degraded_search_with_no_slot_is_a_503_that_says_why(self):
        """Shed out loud. A short or empty result is indistinguishable on screen from a real
        answer, which is the silence this project's founding principle exists to stop."""
        self.hold_the_slot()
        with self.settings(degraded_seconds=BLINK):
            resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 503, resp.get_json())
        body = resp.get_json()
        self.assertNotIn('success', body)
        self.assertIn('rebuilt', body['error'])
        self.assertIn('already running', body['error'])
        self.assertNotIn('timed out', body['error'].lower())

    def test_a_healthy_search_does_not_queue_behind_a_degraded_one(self):
        """The gate is for unindexed scans only. An untyped search is not degraded, so it must
        answer while the slot is held - otherwise a healthy app inherits the incident."""
        self.hold_the_slot()
        with self.settings(degraded_seconds=BLINK):
            resp = self.t.client.get(f'{SEARCH_URL}?facets=')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue(resp.get_json()['success'])

    def test_zero_disables_the_cap(self):
        """The documented escape hatch, at the endpoint."""
        self.hold_the_slot()
        with self.settings(limit=0, degraded_seconds=BLINK):
            resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 200, resp.get_json())

    def test_a_shed_request_does_not_strand_the_slot(self):
        """A shed that leaked a slot would break the search box until a restart - the same
        class of defect as the progress handler left on a pooled connection."""
        release = self.hold_the_slot()
        with self.settings(degraded_seconds=BLINK):
            self.assertEqual(self.t.client.get(f'{SEARCH_URL}?q=espn').status_code, 503)
        release.set()
        with self.settings():
            resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual([r['name'] for r in resp.get_json()['rows']], ['US| ESPN2 HD'])

    def test_an_ordinary_search_is_untouched_by_the_shipped_cap(self):
        """The cap must be invisible when nothing is contending: no patch at all, and the
        search answers."""
        resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual([r['name'] for r in resp.get_json()['rows']], ['US| ESPN2 HD'])


class QueueIsMadeOfThreadsNotConnectionsTests(_RouteTestCase):
    """The assertion that separates a fix from a relocation of the same bug."""

    def test_nothing_is_held_open_on_the_session_when_the_request_queues(self):
        """A request waiting on the gate while holding a pooled connection has not fixed the
        exhaustion, it has moved it: the queue is then made of connections, and the account
        sync and index rebuild that END the degraded window still cannot get one. That is
        precisely how the 2026-08-01 incident killed the sync.
        """
        seen = []
        real_slot = route_mod.UNINDEXED_SCANS.slot

        def recording_slot(limit, timeout=None):
            # `db.session` is the scoped proxy and does not forward in_transaction(); calling
            # it returns this thread's real Session. checkedout() is the blunter half of the
            # same question, asked of the pool itself.
            seen.append({'limit': limit,
                         'in_transaction': db.session().in_transaction(),
                         'checked_out': db.engine.pool.checkedout()})
            return real_slot(limit, timeout=timeout)

        with mock.patch.object(route_mod.UNINDEXED_SCANS, 'slot', recording_slot):
            with self.settings():
                resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['limit'], 1, 'a degraded search was not gated')
        self.assertFalse(seen[0]['in_transaction'],
                         'the request entered the gate inside an open transaction')
        self.assertEqual(seen[0]['checked_out'], 0,
                         'the request entered the gate holding a pooled connection')


class SharedBudgetTests(_RouteTestCase):
    """Waiting for a slot is charged against the request's budget, not added to it."""

    def test_the_deadline_gets_what_the_wait_left_of_the_budget(self):
        """One number bounds the whole handler. Charged separately, a request that queued for
        14 of its 15 seconds would then be allowed another 15 - the unbounded occupancy this
        set of changes exists to end."""
        release = self.hold_the_slot()
        threading.Timer(BLINK * 4, release.set).start()
        seen = []
        real_deadline = route_mod.query_deadline

        def recording_deadline(seconds, label='', abandoned=None):
            seen.append(seconds)
            return real_deadline(seconds, label=label, abandoned=abandoned)

        with mock.patch.object(route_mod, 'query_deadline', recording_deadline):
            with self.settings(degraded_seconds=5):
                resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(len(seen), 1)
        self.assertLess(seen[0], 5, 'the query got the full budget on top of its wait')
        self.assertGreater(seen[0], 0, 'the deadline was armed with a non-positive value, '
                                       'which query_deadline reads as no deadline at all')

    def test_a_budget_spent_entirely_on_waiting_still_leaves_a_deadline_armed(self):
        """The clamp. query_deadline reads a non-positive value as "unbounded", so a request
        that queued away its whole budget would come out the other side with nothing bounding
        it - the one case that most needs bounding.
        """
        seen = []
        real_deadline = route_mod.query_deadline

        def recording_deadline(seconds, label='', abandoned=None):
            seen.append(seconds)
            return real_deadline(seconds, label=label, abandoned=abandoned)

        with mock.patch.object(route_mod, 'query_deadline', recording_deadline), \
                _route_clock(0.0, 99.0), mock.patch('app.db_utils.QUERY_DEADLINE_CHECK_OPS', 1):
            with self.settings(degraded_seconds=5):
                resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 0)
        self.assertEqual(resp.status_code, 503, 'an exhausted budget answered as a success')
        self.assertIn('timed out', resp.get_json()['error'].lower())

    def test_the_timeout_message_quotes_the_whole_budget_not_the_remainder(self):
        """`exc.seconds` is whatever was left after the wait; the reader's setting says 5.
        Reporting the remainder would name a number that appears nowhere they can act on."""
        # A request that queued away its entire 5s budget: the deadline is armed with the
        # clamp's floor, so it fires at once and the message has to name 5, not 1e-06.
        with _route_clock(0.0, 5.0), mock.patch('app.db_utils.QUERY_DEADLINE_CHECK_OPS', 1):
            with self.settings(degraded_seconds=5):
                resp = self.t.client.get(f'{SEARCH_URL}?q=espn')
        self.assertEqual(resp.status_code, 503, resp.get_json())
        error = resp.get_json()['error']
        self.assertIn('after 5s', error)
        self.assertNotIn('1e-06', error)


if __name__ == '__main__':
    unittest.main()
