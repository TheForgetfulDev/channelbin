"""Tier 2 - a search index left BUILDING by a dead process is reconciled at startup.

BUGS.md 2026-08-01 (stranded BUILDING row). STATUS_BUILDING is cleared only by the rebuild
that set it, so a process killed mid-rebuild - ./restart.sh, kill -9, an OOM kill - left the
row set with nobody to clear it. search_index_readiness() then reported that index unusable
for good and every search silently took the unindexed scan over 1.9M rows, recoverable only
if a later account sync happened to rebuild. dev/changelog/425.

**These tests restart for real.** A second create_app() is built against the first app's
database file (see _restart), because the defect is entirely about what a fresh process makes
of state a previous one left behind - patching the reconcile function and asserting it was
called would prove only that the test called it.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app import search_index as SI  # noqa: E402
from app.database import Alert, SearchIndexState  # noqa: E402


def _restart(t):
    """A second app on the first one's database file - what a process restart looks like.

    Redirecting database.path is also what suppresses make_test_app's schema-template copy
    (it only preseeds when the path is the one it created), so this app opens the existing
    file rather than a fresh one. Cleanup order matters: this app's context is pushed on top
    of the first's, so it must be cleaned up first.
    """
    return make_test_app(extra_overrides={'database': {'path': t.db_path}})


def _state(name, status, **kw):
    row = SearchIndexState(name=name, status=status, **kw)
    db.session.add(row)
    db.session.commit()
    return row


def _alerts(source):
    return Alert.query.filter_by(alert_type='SEARCH_INDEX_REBUILD_FAILED',
                                 source=source, dismissed_at=None).all()


class StrandedBuildTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.restarted = None

    def tearDown(self):
        if self.restarted is not None:
            self.restarted.cleanup()
        self.t.cleanup()

    def _restart(self):
        self.restarted = _restart(self.t)
        return self.restarted

    def test_a_stranded_building_row_is_failed_by_a_restart(self):
        """The whole defect: nothing cleared BUILDING, so readiness stayed False forever."""
        _state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_BUILDING,
               rebuilt_at=datetime.utcnow() - timedelta(hours=3), source_watermark='999')
        self._restart()
        row = SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_PROGRAMS).one()
        self.assertEqual(row.status, SI.STATUS_FAILED)

    def test_the_recorded_error_names_the_interruption(self):
        """§Failure paths must be observable - a FAILED state with no reason renders blank."""
        _state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_BUILDING)
        self._restart()
        row = SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_PROGRAMS).one()
        self.assertEqual(row.error, SI.BUILD_INTERRUPTED_ERROR)

    def test_a_restart_raises_the_standing_alert(self):
        _state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_BUILDING)
        self._restart()
        alerts = _alerts(f'search-index:{SI.SEARCH_INDEX_PROGRAMS}')
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, 'ERROR')
        self.assertIn(SI.SEARCH_INDEX_PROGRAMS, alerts[0].title)
        self.assertIn('interrupted', alerts[0].body)

    def test_readiness_stops_calling_it_a_rebuild_in_flight(self):
        """Before the fix this said "being rebuilt right now" for the life of the install."""
        _state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_BUILDING)
        self._restart()
        ready, reason = SI.search_index_readiness(SI.SEARCH_INDEX_PROGRAMS)
        self.assertFalse(ready, 'a half-built index still must not be queried')
        self.assertIn('last failed to rebuild', reason)
        self.assertNotIn('being rebuilt right now', reason)

    def test_the_watermark_is_cleared(self):
        """An interrupted rebuild's watermark describes rows the index does not contain. Left
        in place, anything that later set the status back to OK would read it as fresh."""
        _state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_BUILDING, source_watermark='4242')
        self._restart()
        row = SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_PROGRAMS).one()
        self.assertEqual(row.source_watermark or '', '')

    def test_every_stranded_index_is_reconciled_not_just_the_first(self):
        _state(SI.SEARCH_INDEX_CHANNELS, SI.STATUS_BUILDING)
        _state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_BUILDING)
        self._restart()
        self.assertEqual(
            {r.name: r.status for r in SearchIndexState.query.all()},
            {SI.SEARCH_INDEX_CHANNELS: SI.STATUS_FAILED,
             SI.SEARCH_INDEX_PROGRAMS: SI.STATUS_FAILED})
        self.assertEqual(len(_alerts(f'search-index:{SI.SEARCH_INDEX_CHANNELS}')), 1)
        self.assertEqual(len(_alerts(f'search-index:{SI.SEARCH_INDEX_PROGRAMS}')), 1)

    def test_a_healthy_row_survives_a_restart_untouched(self):
        """Startup must not touch an index that is fine - the reconcile is scoped to BUILDING
        and nothing else. Every column here is one a needless re-record would overwrite."""
        built_at = datetime.utcnow() - timedelta(days=1)
        _state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_OK, rebuilt_at=built_at,
               duration_ms=76133, row_count=410987, source_watermark='410987')
        self._restart()
        row = SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_PROGRAMS).one()
        self.assertEqual(
            (row.status, row.duration_ms, row.row_count, row.source_watermark, row.error),
            (SI.STATUS_OK, 76133, 410987, '410987', None))
        self.assertEqual(row.rebuilt_at, built_at)
        self.assertEqual(_alerts(f'search-index:{SI.SEARCH_INDEX_PROGRAMS}'), [])

    def test_an_already_failed_row_is_not_re_recorded(self):
        """A rebuild that failed on its own terms already recorded why and already alerted;
        overwriting its error with the interruption text would lose the real cause."""
        failed_at = datetime.utcnow() - timedelta(hours=2)
        _state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_FAILED, rebuilt_at=failed_at,
               error='database or disk is full')
        self._restart()
        row = SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_PROGRAMS).one()
        self.assertEqual(row.error, 'database or disk is full')
        self.assertEqual(row.rebuilt_at, failed_at)

    def test_a_fresh_install_is_a_no_op(self):
        """No state rows at all is the brand-new-database case, not a stranded build."""
        self.assertEqual(SearchIndexState.query.count(), 0)
        self._restart()
        self.assertEqual(SearchIndexState.query.count(), 0)
        self.assertEqual(Alert.query.filter_by(
            alert_type='SEARCH_INDEX_REBUILD_FAILED').count(), 0)


class RecoveryTests(unittest.TestCase):
    """Characterization: these hold with or without the create_app() wiring, because they
    call the reconcile directly. They pin what happens after it, which is the half a reader
    of the changelog will ask about - the FAILED state is not a dead end."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_a_later_rebuild_clears_the_state_and_dismisses_the_alert(self):
        _state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_BUILDING)
        SI.reconcile_interrupted_builds()
        self.assertEqual(len(_alerts(f'search-index:{SI.SEARCH_INDEX_PROGRAMS}')), 1)

        SI.rebuild_search_indexes('test', names=(SI.SEARCH_INDEX_PROGRAMS,))

        row = SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_PROGRAMS).one()
        self.assertEqual(row.status, SI.STATUS_OK)
        self.assertIsNone(row.error)
        self.assertEqual(_alerts(f'search-index:{SI.SEARCH_INDEX_PROGRAMS}'), [],
                         'the standing alert must auto-dismiss once the index is rebuilt')
        self.assertTrue(SI.search_index_ready(SI.SEARCH_INDEX_PROGRAMS))

    def test_reconciling_twice_does_not_stack_alerts(self):
        """The second call finds FAILED, not BUILDING, so it is a no-op - and even if the
        first alert were still standing, _raise_or_resolve_standing_alert refreshes it."""
        _state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_BUILDING)
        SI.reconcile_interrupted_builds()
        SI.reconcile_interrupted_builds()
        self.assertEqual(len(_alerts(f'search-index:{SI.SEARCH_INDEX_PROGRAMS}')), 1)


if __name__ == '__main__':
    unittest.main()
