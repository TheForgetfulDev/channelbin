"""Tier 2 - the manual search-index rebuild trigger, and the lock that a second caller needs.

BUGS.md 2026-08-01 (no manual rebuild trigger). rebuild_search_indexes() had exactly one
caller - the account sync close-out - so an index left FAILED or stranded could only be
repaired by waiting for a full sync, while every search took the unindexed scan over 1.9M
rows. That scan is what pegged both cores for 16 minutes on 2026-08-01. dev/changelog/426.

Two things are being guarded here, and they are not the same weight:

* **The lock** (ConcurrentRebuildTests) is the real regression guard. Adding a second caller
  made concurrent rebuilds possible for the first time, and two interleaved runs of the
  programs rebuild leave a half-populated index that _record_state(STATUS_OK) declares fresh.
* **The route and status cases** are characterization of new behavior - without the change
  they 404, which proves little on its own. The exceptions are the two refusal cases and the
  stale-is-not-ready case, which fail for their own substantive reasons when only their guard
  clause is removed.

The rebuild runs on a real thread here rather than a patched one: it is spawned by the route
precisely so the request does not block for the 76s a production programs rebuild measured,
and a synchronous stand-in would not exercise that.
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import search_index as SI  # noqa: E402
from app.database import Alert, SearchIndexState  # noqa: E402

REBUILD_URL = '/api/settings/search-index/rebuild'
STATUS_URL = '/api/settings/search-index'


def _join_rebuild(timeout=30):
    """Wait out the worker the route started. It is found by the name the route gives it,
    so nothing has to be patched to make the spawn observable; if it already finished,
    enumerate() simply does not list it."""
    for th in threading.enumerate():
        if th.name == 'search-index-rebuild':
            th.join(timeout)
            return not th.is_alive()
    return True


def _seed_searchable():
    """Enough source rows that both indexes have something to build from."""
    acct = seed.make_account()
    ch = seed.make_channel(acct, name='US| ESPN2 HD', in_guide=True)
    seed.make_epg_entry(ch, title='SportsCenter')
    db.session.commit()
    return acct, ch


class ManualRebuildRouteTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        # CSRF-protected app-wide like every other mutating route; the token round-trip is
        # covered by tests/test_csrf_envelope.py, not re-proved here.
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client

    def tearDown(self):
        _join_rebuild()
        self.t.cleanup()

    def test_the_route_actually_rebuilds_both_indexes(self):
        """The whole point: recovery without waiting for an account sync."""
        _seed_searchable()
        resp = self.client.post(REBUILD_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        self.assertTrue(_join_rebuild(), 'the rebuild worker did not finish')
        # The worker commits in its own app context, so the outer session's identity map
        # still holds pre-rebuild rows.
        db.session.expire_all()
        for name in SI.SEARCH_INDEX_NAMES:
            row = SearchIndexState.query.filter_by(name=name).one()
            self.assertEqual(row.status, SI.STATUS_OK, f'{name} did not reach OK')
        self.assertTrue(SI.search_index_ready())

    def test_the_request_does_not_wait_for_the_rebuild(self):
        """A programs rebuild measured 76.1s at 411k rows; holding the request thread for
        that long is what the background worker exists to avoid."""
        _seed_searchable()
        started = threading.Event()
        release = threading.Event()
        original = SI._rebuild_one

        def _blocking(name, reason):
            started.set()
            release.wait(10)
            return original(name, reason)

        SI._rebuild_one = _blocking
        try:
            resp = self.client.post(REBUILD_URL)
            self.assertEqual(resp.status_code, 200)
            # The response came back while the worker is still inside the rebuild.
            self.assertTrue(started.wait(5), 'the worker never started')
        finally:
            release.set()
            _join_rebuild()
            SI._rebuild_one = original

    def test_a_rebuild_already_running_is_refused(self):
        """Two rebuilds at once corrupt the programs index; the second must not be queued."""
        SI._rebuilding = True
        try:
            resp = self.client.post(REBUILD_URL)
        finally:
            SI._rebuilding = False
        self.assertEqual(resp.status_code, 409)
        self.assertIn('already running', resp.get_json()['error'])

    def test_a_syncing_account_is_refused_and_named(self):
        """A sync ends with its own rebuild, so a manual one mid-sync burns ~76s of CPU on a
        result its next insert makes stale - inside the exact window this refusal protects."""
        acct = seed.make_account(name='Provider Two')
        acct.status = 'SYNCING'
        db.session.commit()
        resp = self.client.post(REBUILD_URL)
        self.assertEqual(resp.status_code, 409)
        self.assertIn('Provider Two', resp.get_json()['error'])
        # And nothing was started behind the refusal.
        self.assertIsNone(SearchIndexState.query.filter_by(
            name=SI.SEARCH_INDEX_CHANNELS).first())

    def test_the_rebuild_route_is_post_only(self):
        """§Enforcement lives server-side - a state-changing action must not be reachable by
        a GET a browser can be tricked into making."""
        self.assertEqual(self.client.get(REBUILD_URL).status_code, 405)

    def test_a_successful_rebuild_dismisses_the_standing_alert(self):
        """Recovery needs no new code: the alert item 6 raises is auto-dismissed by the next
        successful rebuild (dev/changelog/425)."""
        _seed_searchable()
        SI._alert_rebuild_failed(SI.SEARCH_INDEX_PROGRAMS, 'boom', active=True)
        db.session.expire_all()
        self.assertEqual(Alert.query.filter_by(
            alert_type='SEARCH_INDEX_REBUILD_FAILED', dismissed_at=None).count(), 1)

        self.assertEqual(self.client.post(REBUILD_URL).status_code, 200)
        self.assertTrue(_join_rebuild())
        db.session.expire_all()
        self.assertEqual(Alert.query.filter_by(
            alert_type='SEARCH_INDEX_REBUILD_FAILED', dismissed_at=None).count(), 0)


class IndexStatusEndpointTests(unittest.TestCase):
    """The first surface that renders search_index_state at all - before it, a degraded
    search was knowable only from an alert firing."""

    def setUp(self):
        self.t = make_test_app()
        # CSRF-protected app-wide like every other mutating route; the token round-trip is
        # covered by tests/test_csrf_envelope.py, not re-proved here.
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client

    def tearDown(self):
        _join_rebuild()
        self.t.cleanup()

    def _indexes(self):
        resp = self.client.get(STATUS_URL)
        self.assertEqual(resp.status_code, 200)
        return {ix['name']: ix for ix in resp.get_json()['indexes']}

    def test_every_index_is_reported(self):
        ix = self._indexes()
        self.assertEqual(set(ix), set(SI.SEARCH_INDEX_NAMES))
        for entry in ix.values():
            self.assertTrue(entry['label'], 'each index needs a human label')

    def test_a_never_built_index_is_reported_as_not_ready(self):
        entry = self._indexes()[SI.SEARCH_INDEX_CHANNELS]
        self.assertEqual(entry['status'], 'NEVER_BUILT')
        self.assertFalse(entry['ready'])
        self.assertIn('never been built', entry['reason'])

    def test_a_stale_index_is_ok_but_not_ready(self):
        """The reason readiness is asked per index instead of being read off `status`: a
        stale index still says OK, and rendering that as healthy is the silent failure."""
        acct, ch = _seed_searchable()
        SI.rebuild_search_indexes('test')
        self.assertTrue(self._indexes()[SI.SEARCH_INDEX_PROGRAMS]['ready'])

        seed.make_epg_entry(ch, title='Later Program', offset_minutes=120)
        db.session.commit()

        entry = self._indexes()[SI.SEARCH_INDEX_PROGRAMS]
        self.assertEqual(entry['status'], SI.STATUS_OK)
        self.assertFalse(entry['ready'], 'a moved watermark means the index is stale')
        self.assertIn('stale', entry['reason'])

    def test_a_built_index_reports_its_numbers(self):
        _seed_searchable()
        SI.rebuild_search_indexes('test')
        entry = self._indexes()[SI.SEARCH_INDEX_CHANNELS]
        self.assertEqual(entry['status'], SI.STATUS_OK)
        self.assertTrue(entry['ready'])
        self.assertEqual(entry['row_count'], 1)
        self.assertIsNotNone(entry['duration_ms'])
        self.assertIsNotNone(entry['rebuilt_at'])
        self.assertIsNone(entry['error'])

    def test_a_failed_index_reports_its_error(self):
        """§Failure paths must be observable - a FAILED index with no reason shown is the
        blank-detail-page defect in another shape."""
        row = SearchIndexState(name=SI.SEARCH_INDEX_PROGRAMS, status=SI.STATUS_FAILED,
                               error='disk full')
        db.session.add(row)
        db.session.commit()
        entry = self._indexes()[SI.SEARCH_INDEX_PROGRAMS]
        self.assertEqual(entry['status'], SI.STATUS_FAILED)
        self.assertFalse(entry['ready'])
        self.assertEqual(entry['error'], 'disk full')


class ConcurrentRebuildTests(unittest.TestCase):
    """The real guard. REBUILD_SQL empties chan_prog and stages into temp.chan_prog_stage,
    and rebuild_units() reads its chunk bounds from MAX(id) FROM chan_prog between commits -
    so a second run interleaved with the first moves those bounds out from under it and
    leaves a half-populated index that _record_state(STATUS_OK) then declares fresh."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_two_rebuilds_never_overlap(self):
        _seed_searchable()

        counter_lock = threading.Lock()
        state = {'active': 0, 'overlapped': False}
        original = SI._rebuild_one

        def _watched(name, reason):
            with counter_lock:
                state['active'] += 1
                if state['active'] > 1:
                    state['overlapped'] = True
            # Wide enough that an unserialized second caller lands inside this window.
            time.sleep(0.05)
            try:
                return original(name, reason)
            finally:
                with counter_lock:
                    state['active'] -= 1

        def _worker():
            with self.t.app.app_context():
                SI.rebuild_search_indexes('concurrency test')

        SI._rebuild_one = _watched
        try:
            threads = [threading.Thread(target=_worker, name=f'rebuild-{i}')
                       for i in range(2)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(30)
                self.assertFalse(th.is_alive(), 'a rebuild thread hung')
        finally:
            SI._rebuild_one = original

        self.assertFalse(state['overlapped'],
                         'two rebuilds ran at once - the programs index can be left '
                         'half-populated and marked OK')

    def test_rebuild_in_progress_tracks_the_lock(self):
        """What the route's refusal reads. False outside a rebuild, True inside one."""
        self.assertFalse(SI.rebuild_in_progress())
        seen = []
        original = SI._rebuild_one

        def _watched(name, reason):
            seen.append(SI.rebuild_in_progress())
            return original(name, reason)

        _seed_searchable()
        SI._rebuild_one = _watched
        try:
            SI.rebuild_search_indexes('test')
        finally:
            SI._rebuild_one = original
        self.assertTrue(seen and all(seen), 'rebuild_in_progress() was False mid-rebuild')
        self.assertFalse(SI.rebuild_in_progress(), 'the flag was left set')


if __name__ == '__main__':
    unittest.main()
