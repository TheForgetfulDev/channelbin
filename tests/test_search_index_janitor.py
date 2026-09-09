"""Tier 2 - the search-index janitor (app/search_index.py::run_index_janitor).

Guards dev/changelog/680 and dev/docs/BUGS.md 2026-08-16 (the index-janitor entry).

The defect: `rebuild_search_indexes()` was reachable from three places only - a sync's
`finally`, EPG cleanup, and the Maintenance button - so every path that separates staleness
from a live sync thread left the repair to nobody. Measured live on 2026-08-15: account 2
committed its channel upserts at 15:23:06 UTC, the process was restarted at 15:27:00, and
`rebuild_needed` was True in a thread that no longer existed. Both indexes then sat stale
for 6.5 hours (worst case ~22, the sync interval) with every EPG search running the LIKE
fallback over 2.2M rows.

What is asserted, in order:

  1. **The grace window is real in both directions** - a freshly-stale index is left alone
     for the owner that should fix it, and one nobody fixed is repaired.
  2. **It asks admission rather than reading it.** A janitor that checks who is running and
     then starts has rebuilt the exact check-then-act race dev/changelog/679 closed, so the
     refusal path is asserted through a really-held ticket, not a mocked predicate.
  3. **A failing index is throttled to one attempt per grace window**, not one per tick -
     a programs rebuild is ~76s of CPU and a tick is 10 minutes.
  4. **An index someone else is already rebuilding is not "stale with no owner"**, and a
     tick that does nothing is not recorded as a job run.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_search_index_janitor
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import admission, db  # noqa: E402
from app import scheduler as sched  # noqa: E402
from app import search_index as SI  # noqa: E402
from app.accounts import _upsert_channels  # noqa: E402
from app.database import JobRun, M3uAccount, SearchIndexState  # noqa: E402
from tests.support import make_test_app  # noqa: E402

GRACE = 15


def _stream(sid, **overrides):
    base = {
        'stream_id': sid,
        'name': f'Ch{sid}',
        '_stream_url': f'http://provider.test/live/u/p/{sid}.ts',
        'category_id': '7',
        'category_name': 'Sports',
        'epg_channel_id': f'ch{sid}.test',
    }
    base.update(overrides)
    return base


class _Held:
    """Hold an admission ticket for a `with` block - the real registry, not a stub, because
    the whole point of the janitor's refusal path is that it goes through try_start."""

    def __init__(self, kind, label='held'):
        self.kind = kind
        self.label = label
        self.ticket = None

    def __enter__(self):
        self.ticket = admission.try_start(self.kind, self.label, force=True)
        return self.ticket

    def __exit__(self, *exc):
        admission.release(self.ticket)
        return False


class _JanitorCase(unittest.TestCase):
    """A test app whose channels index starts FRESH, so staleness in a test is something
    the test caused rather than the empty state every new database begins in."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Janitor', status='OK',
                                  m3u_url='http://provider.test/playlist.m3u8')
        db.session.add(self.account)
        db.session.commit()
        self.streams = [_stream(i) for i in range(1, 4)]
        _upsert_channels(self.account, self.streams)
        db.session.commit()
        SI.rebuild_search_indexes('test setup')
        SI._stale_since.clear()

    def tearDown(self):
        admission.reset_for_tests()
        SI._stale_since.clear()
        self.t.cleanup()

    def _make_stale(self):
        """Rename a channel through the real upsert path, which moves the indexed text and
        therefore the watermark - the same way a provider rename does in production."""
        _upsert_channels(self.account, [_stream(1, name='Renamed HD')] + self.streams[1:])
        db.session.commit()
        db.session.expire_all()
        self.assertFalse(SI.search_index_readiness(SI.SEARCH_INDEX_CHANNELS)[0])

    def _backdate(self, minutes, name=SI.SEARCH_INDEX_CHANNELS):
        """Pretend the clock entry was written `minutes` ago."""
        SI._stale_since[name] = datetime.utcnow() - timedelta(minutes=minutes)

    def _ready(self, name=SI.SEARCH_INDEX_CHANNELS):
        return SI.search_index_readiness(name)[0]


class GraceWindowTests(_JanitorCase):
    def test_a_fresh_index_is_left_alone_and_keeps_no_clock(self):
        with mock.patch.object(SI, 'rebuild_search_indexes') as rebuild:
            outcome = SI.run_index_janitor(GRACE)
        rebuild.assert_not_called()
        self.assertEqual(outcome['due'], [])
        self.assertNotIn(SI.SEARCH_INDEX_CHANNELS, SI._stale_since)

    def test_the_first_tick_starts_the_clock_and_rebuilds_nothing(self):
        """The owner that should repair this - a sync's own close-out rebuild - has not had
        its chance yet. Repairing on sight would race every ordinary sync."""
        self._make_stale()
        with mock.patch.object(SI, 'rebuild_search_indexes') as rebuild:
            SI.run_index_janitor(GRACE)
        rebuild.assert_not_called()
        self.assertIn(SI.SEARCH_INDEX_CHANNELS, SI._stale_since)

    def test_consecutive_ticks_inside_the_grace_window_rebuild_nothing(self):
        """No backdating here on purpose: this is the real cadence, several ticks arriving
        while the window is still open, and it is what proves the clock is compared against
        rather than merely written."""
        self._make_stale()
        with mock.patch.object(SI, 'rebuild_search_indexes') as rebuild:
            for _ in range(4):
                SI.run_index_janitor(GRACE)
        rebuild.assert_not_called()

    def test_a_tick_one_minute_short_of_the_window_rebuilds_nothing(self):
        self._make_stale()
        SI.run_index_janitor(GRACE)
        self._backdate(GRACE - 1)
        with mock.patch.object(SI, 'rebuild_search_indexes') as rebuild:
            SI.run_index_janitor(GRACE)
        rebuild.assert_not_called()

    def test_an_index_stale_past_the_grace_window_is_rebuilt(self):
        """The headline: nobody came, so the janitor does it."""
        self._make_stale()
        SI.run_index_janitor(GRACE)
        self._backdate(GRACE)
        outcome = SI.run_index_janitor(GRACE)
        self.assertEqual(outcome['due'], [SI.SEARCH_INDEX_CHANNELS])
        self.assertTrue(outcome['results'][SI.SEARCH_INDEX_CHANNELS])
        self.assertTrue(self._ready(), 'the degraded window must actually be closed')

    def test_a_repaired_index_drops_its_clock_entry(self):
        self._make_stale()
        SI.run_index_janitor(GRACE)
        self._backdate(GRACE)
        SI.run_index_janitor(GRACE)
        SI.run_index_janitor(GRACE)
        self.assertNotIn(SI.SEARCH_INDEX_CHANNELS, SI._stale_since)

    def test_an_index_repaired_by_somebody_else_drops_its_clock_entry(self):
        """A sync arriving mid-grace is the expected outcome, not an edge case - the clock
        has to reset, or the next staleness inherits a window that already elapsed."""
        self._make_stale()
        SI.run_index_janitor(GRACE)
        SI.rebuild_search_indexes('a sync close-out')
        with mock.patch.object(SI, 'rebuild_search_indexes') as rebuild:
            SI.run_index_janitor(GRACE)
        rebuild.assert_not_called()
        self.assertNotIn(SI.SEARCH_INDEX_CHANNELS, SI._stale_since)

    def test_a_grace_of_zero_disables_the_janitor(self):
        self._make_stale()
        self._backdate(600)
        with mock.patch.object(SI, 'rebuild_search_indexes') as rebuild:
            outcome = SI.run_index_janitor(0)
        rebuild.assert_not_called()
        self.assertEqual(outcome['due'], [])


class AdmissionTests(_JanitorCase):
    """The janitor is the one caller of rebuild_search_indexes that asks rather than forces.
    Every assertion here holds a REAL ticket, because a stubbed "is anything running?" check
    would pass just as happily against the check-then-act shape dev/changelog/679 removed."""

    def _due(self):
        self._make_stale()
        SI.run_index_janitor(GRACE)
        self._backdate(GRACE)

    def test_a_running_sync_refuses_the_janitor(self):
        self._due()
        with _Held(admission.KIND_SYNC, 'account 2'):
            outcome = SI.run_index_janitor(GRACE)
        self.assertTrue(outcome['refused'])
        self.assertEqual(outcome['results'], {})
        self.assertFalse(self._ready(), 'a refused janitor must not look like a rebuild')

    def test_a_running_rebuild_refuses_the_janitor(self):
        self._due()
        with _Held(admission.KIND_REBUILD, 'manual rebuild from Maintenance'):
            outcome = SI.run_index_janitor(GRACE)
        self.assertTrue(outcome['refused'])
        self.assertEqual(outcome['results'], {})

    def test_maintenance_does_not_refuse_the_janitor(self):
        """Maintenance yields to a rebuild, not the other way round - a janitor that stood
        down for the nightly prune would be blocked at exactly the wrong time."""
        self._due()
        with _Held(admission.KIND_MAINTENANCE, 'database maintenance'):
            outcome = SI.run_index_janitor(GRACE)
        self.assertFalse(outcome['refused'])
        self.assertTrue(self._ready())

    def test_it_asks_refusably_rather_than_forcing(self):
        self._due()
        with mock.patch.object(SI, 'rebuild_search_indexes', return_value={}) as rebuild:
            SI.run_index_janitor(GRACE)
        self.assertTrue(rebuild.call_args.kwargs['refusable'])

    def test_a_refusal_leaves_no_ticket_behind(self):
        """A leaked ticket blocks its dependents for the life of the process."""
        self._due()
        with _Held(admission.KIND_SYNC, 'account 2'):
            SI.run_index_janitor(GRACE)
        self.assertEqual(admission.active_kinds(), set())

    def test_a_completed_rebuild_leaves_no_ticket_behind(self):
        self._due()
        SI.run_index_janitor(GRACE)
        self.assertEqual(admission.active_kinds(), set())


class ThrottleTests(_JanitorCase):
    def test_a_failing_index_is_retried_once_per_grace_window_not_once_per_tick(self):
        """A programs rebuild is ~76s of CPU; retrying it every 10-minute tick because it
        keeps failing would be worse than the staleness it is trying to repair."""
        self._make_stale()
        SI.run_index_janitor(GRACE)
        self._backdate(GRACE)
        failed = {SI.SEARCH_INDEX_CHANNELS: False}
        with mock.patch.object(SI, 'rebuild_search_indexes', return_value=failed) as rebuild:
            SI.run_index_janitor(GRACE)
            self.assertEqual(rebuild.call_count, 1)
            SI.run_index_janitor(GRACE)
            SI.run_index_janitor(GRACE)
            self.assertEqual(rebuild.call_count, 1, 'the clock must restart after an attempt')
            self._backdate(GRACE)
            SI.run_index_janitor(GRACE)
            self.assertEqual(rebuild.call_count, 2)

    def test_a_refused_attempt_is_retried_on_the_next_tick(self):
        """A refusal is different from a failure: nothing ran, the blocker may be gone a
        tick later, and waiting another full grace window would extend the very window this
        job exists to cap."""
        self._make_stale()
        SI.run_index_janitor(GRACE)
        self._backdate(GRACE)
        with _Held(admission.KIND_SYNC, 'account 2'):
            SI.run_index_janitor(GRACE)
        outcome = SI.run_index_janitor(GRACE)
        self.assertEqual(outcome['due'], [SI.SEARCH_INDEX_CHANNELS])
        self.assertTrue(self._ready())


class JanitorScopeTests(_JanitorCase):
    def test_an_index_being_rebuilt_is_not_counted_as_ownerless(self):
        """BUILDING reads as "not ready" to every search, but it is the one flavor of
        unusable that already has somebody working on it."""
        self._make_stale()
        state = SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_CHANNELS).first()
        state.status = SI.STATUS_BUILDING
        db.session.commit()
        with mock.patch.object(SI, 'rebuild_search_indexes') as rebuild:
            SI.run_index_janitor(GRACE)
        rebuild.assert_not_called()
        self.assertNotIn(SI.SEARCH_INDEX_CHANNELS, SI._stale_since)

    def test_only_the_ownerless_index_is_rebuilt(self):
        """The programs index is fresh here, and rebuilding it anyway would spend ~76s
        re-deriving an index that was already correct."""
        self._make_stale()
        SI.run_index_janitor(GRACE)
        self._backdate(GRACE)
        with mock.patch.object(SI, 'rebuild_search_indexes', return_value={}) as rebuild:
            SI.run_index_janitor(GRACE)
        self.assertEqual(tuple(rebuild.call_args.kwargs['names']), (SI.SEARCH_INDEX_CHANNELS,))

    def test_a_never_built_index_is_repaired_like_any_other(self):
        """A fresh install that has synced but never rebuilt is degraded for the same
        reason and by the same amount as a stale one, so it gets the same repair."""
        SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_CHANNELS).delete()
        db.session.commit()
        self.assertIn('never been built',
                      SI.search_index_readiness(SI.SEARCH_INDEX_CHANNELS)[1])
        SI.run_index_janitor(GRACE)
        self._backdate(GRACE)
        SI.run_index_janitor(GRACE)
        self.assertTrue(self._ready())

    def test_the_reason_reaches_the_log(self):
        """Product principle 1: a rebuild nobody asked for has to say what it was for."""
        self._make_stale()
        SI.run_index_janitor(GRACE)
        self._backdate(GRACE)
        with self.assertLogs('app.search_index', level='INFO') as logs:
            SI.run_index_janitor(GRACE)
        line = '\n'.join(logs.output)
        self.assertIn('janitor', line)
        self.assertIn(SI.SEARCH_INDEX_CHANNELS, line)
        self.assertIn('stale', line)


class SchedulerWiringTests(_JanitorCase):
    """The job exists, fires on an interval, and reads its grace at run time."""

    def test_the_job_is_registered_on_an_interval(self):
        self.assertIn('schedule_index_janitor', dir(sched))
        with mock.patch.object(sched, '_add_job') as add_job, \
                mock.patch.object(sched, '_scheduler') as scheduler:
            scheduler.get_job.return_value = None
            sched.schedule_index_janitor(self.t.app)
        kwargs = add_job.call_args.kwargs
        self.assertEqual(kwargs['id'], 'search_index_janitor')
        self.assertEqual(kwargs['trigger'], 'interval')
        self.assertEqual(kwargs['minutes'], sched._INDEX_JANITOR_INTERVAL_MINUTES)

    def test_a_live_job_is_not_re_registered(self):
        """Re-registering on every boot would push the next tick a fresh 10 minutes out,
        which on a restart-loop means the janitor never runs at all."""
        with mock.patch.object(sched, '_add_job') as add_job, \
                mock.patch.object(sched, '_scheduler') as scheduler:
            scheduler.get_job.return_value = mock.Mock(next_run_time=datetime.utcnow())
            sched.schedule_index_janitor(self.t.app)
        add_job.assert_not_called()

    def test_a_no_op_tick_records_no_job_run(self):
        """Ticks are 10 minutes apart and almost all of them do nothing; recording those
        would report the /jobs 'expected runtime' of a job that never ran."""
        with mock.patch.object(sched, '_app', self.t.app):
            sched._index_janitor_job()
        self.assertEqual(JobRun.query.filter_by(job_id='search_index_janitor').count(), 0)

    def test_a_tick_that_rebuilds_records_a_job_run(self):
        self._make_stale()
        with mock.patch.object(sched, '_app', self.t.app):
            sched._index_janitor_job()
            SI._stale_since[SI.SEARCH_INDEX_CHANNELS] = (
                datetime.utcnow() - timedelta(minutes=600))
            sched._index_janitor_job()
        runs = JobRun.query.filter_by(job_id='search_index_janitor').all()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].outcome, 'SUCCESS')
        self.assertTrue(self._ready())

    def test_the_grace_is_read_at_run_time(self):
        """Registered unconditionally, so turning it off in Settings has to take effect on
        the next tick rather than at the next restart."""
        self._make_stale()
        cfg = {'search': {'index_janitor_grace_minutes': 0}}
        with mock.patch.object(sched, '_app', self.t.app), \
                mock.patch('app.config.load_config', return_value=cfg), \
                mock.patch.object(SI, 'rebuild_search_indexes') as rebuild:
            sched._index_janitor_job()
        rebuild.assert_not_called()


if __name__ == '__main__':
    unittest.main()
