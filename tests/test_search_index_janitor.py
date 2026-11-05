"""Tier 2 - the search-index janitor (app/search_index.py::run_index_janitor).

Guards dev/changelog/680 and dev/docs/BUGS.md 2026-08-16 (the index-janitor entry).

The defect: `rebuild_search_indexes()` was reachable from three places only - a sync's
`finally`, EPG cleanup, and the Maintenance button - so every path that separates staleness
from a live sync thread left the repair to nobody. Measured live on 2026-08-15: account 2
committed its channel upserts at 15:23:06 UTC, the process was restarted at 15:27:00, and
`rebuild_needed` was True in a thread that no longer existed. Both indexes then sat stale
for 6.5 hours (worst case ~22, the sync interval) with every EPG search running the LIKE
fallback over 2.2M rows.

A 15-minute grace window kept the job from acting on its first look until dev/changelog/1431,
when a 96-row EPG key edit left program search unindexed for 27 minutes.

What is asserted, in order:

  1. **It repairs on its first look**, and leaves a healthy index alone.
  2. **It asks admission rather than reading it.** A janitor that checks who is running and
     then starts has rebuilt the exact check-then-act race dev/changelog/679 closed, so the
     refusal path is asserted through a really-held ticket, not a mocked predicate.
  3. **An attempt that failed or was refused is tried again on the next tick.**
  4. **An index someone else is already rebuilding is not "stale with no owner"**, and a
     tick that does nothing is not recorded as a job run.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_search_index_janitor
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import admission, db  # noqa: E402
from app import scheduler as sched  # noqa: E402
from app import search_index as SI  # noqa: E402
from app.accounts import _upsert_channels  # noqa: E402
from app.config import _DEFAULTS, _cfg_m008_drop_index_janitor_grace  # noqa: E402
from app.database import Channel, JobRun, M3uAccount, SearchIndexState  # noqa: E402
from tests.support import make_test_app, seed  # noqa: E402


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

    def tearDown(self):
        admission.reset_for_tests()
        self.t.cleanup()

    def _make_stale(self):
        """Rename a channel through the real upsert path, which moves the indexed text and
        therefore the watermark - the same way a provider rename does in production."""
        _upsert_channels(self.account, [_stream(1, name='Renamed HD')] + self.streams[1:])
        db.session.commit()
        db.session.expire_all()
        self.assertFalse(SI.search_index_readiness(SI.SEARCH_INDEX_CHANNELS)[0])

    def _ready(self, name=SI.SEARCH_INDEX_CHANNELS):
        return SI.search_index_readiness(name)[0]


class FirstLookTests(_JanitorCase):
    def test_a_fresh_index_is_left_alone(self):
        with mock.patch.object(SI, 'rebuild_search_indexes') as rebuild:
            outcome = SI.run_index_janitor()
        rebuild.assert_not_called()
        self.assertEqual(outcome['due'], [])

    def test_the_first_look_at_a_stale_index_repairs_it(self):
        """The headline. A tick that finds a stale index with nobody on it does the repair
        there and then, rather than noting it and waiting for a later tick."""
        self._make_stale()
        outcome = SI.run_index_janitor()
        self.assertEqual(outcome['due'], [SI.SEARCH_INDEX_CHANNELS])
        self.assertTrue(outcome['results'][SI.SEARCH_INDEX_CHANNELS])
        self.assertTrue(self._ready(), 'the degraded window must actually be closed')

    def test_the_tick_after_a_repair_finds_nothing_to_do(self):
        self._make_stale()
        SI.run_index_janitor()
        with mock.patch.object(SI, 'rebuild_search_indexes') as rebuild:
            outcome = SI.run_index_janitor()
        rebuild.assert_not_called()
        self.assertEqual(outcome['due'], [])

    def test_an_index_repaired_by_somebody_else_is_left_alone(self):
        self._make_stale()
        SI.rebuild_search_indexes('a sync close-out')
        with mock.patch.object(SI, 'rebuild_search_indexes') as rebuild:
            SI.run_index_janitor()
        rebuild.assert_not_called()


class KeyEditRepairTests(_JanitorCase):
    """What an EPG key edit leaves behind: a few new epg_entries rows and an index that is
    otherwise right (dev/changelog/1431)."""

    def setUp(self):
        super().setUp()
        self.channel = Channel.query.filter_by(account_id=self.account.id).first()
        seed.make_epg_entry(self.channel, title='Already indexed')
        db.session.commit()
        SI.rebuild_search_indexes('test setup')

    def _chan_prog_titles(self):
        return {row[0] for row in db.session.execute(db.text('SELECT title FROM chan_prog'))}

    def test_it_writes_only_the_rows_that_differ(self):
        seed.make_epg_entry(self.channel, title='Copied by the key edit')
        db.session.commit()
        db.session.expire_all()
        self.assertFalse(self._ready(SI.SEARCH_INDEX_PROGRAMS))
        with self.assertLogs('app.search_index', level='INFO') as logs:
            outcome = SI.run_index_janitor()
        self.assertEqual(outcome['due'], [SI.SEARCH_INDEX_PROGRAMS])
        self.assertIn('1 rows added and 0 removed', '\n'.join(logs.output))
        self.assertTrue(self._ready(SI.SEARCH_INDEX_PROGRAMS))
        self.assertEqual(self._chan_prog_titles(), {'Already indexed', 'Copied by the key edit'})

    def test_a_failed_index_is_still_rebuilt_in_full(self):
        """Comparing rows cannot see an FTS side the failed rebuild never populated, so
        `only_changes` must not turn this repair into a no-op."""
        state = SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_PROGRAMS).first()
        state.status = SI.STATUS_FAILED
        db.session.commit()
        with self.assertLogs('app.search_index', level='INFO') as logs:
            SI.run_index_janitor()
        self.assertIn('full, because', '\n'.join(logs.output))
        self.assertTrue(self._ready(SI.SEARCH_INDEX_PROGRAMS))


class AdmissionTests(_JanitorCase):
    """The janitor is the one caller of rebuild_search_indexes that asks rather than forces.
    Every assertion here holds a REAL ticket, because a stubbed "is anything running?" check
    would pass just as happily against the check-then-act shape dev/changelog/679 removed."""

    def test_a_running_sync_refuses_the_janitor(self):
        self._make_stale()
        with _Held(admission.KIND_SYNC, 'account 2'):
            outcome = SI.run_index_janitor()
        self.assertTrue(outcome['refused'])
        self.assertEqual(outcome['results'], {})
        self.assertFalse(self._ready(), 'a refused janitor must not look like a rebuild')

    def test_a_running_rebuild_refuses_the_janitor(self):
        self._make_stale()
        with _Held(admission.KIND_REBUILD, 'manual rebuild from Maintenance'):
            outcome = SI.run_index_janitor()
        self.assertTrue(outcome['refused'])
        self.assertEqual(outcome['results'], {})

    def test_maintenance_does_not_refuse_the_janitor(self):
        """Maintenance yields to a rebuild, not the other way round - a janitor that stood
        down for the nightly prune would be blocked at exactly the wrong time."""
        self._make_stale()
        with _Held(admission.KIND_MAINTENANCE, 'database maintenance'):
            outcome = SI.run_index_janitor()
        self.assertFalse(outcome['refused'])
        self.assertTrue(self._ready())

    def test_it_asks_refusably_rather_than_forcing(self):
        self._make_stale()
        with mock.patch.object(SI, 'rebuild_search_indexes', return_value={}) as rebuild:
            SI.run_index_janitor()
        self.assertTrue(rebuild.call_args.kwargs['refusable'])

    def test_a_refusal_leaves_no_ticket_behind(self):
        """A leaked ticket blocks its dependents for the life of the process."""
        self._make_stale()
        with _Held(admission.KIND_SYNC, 'account 2'):
            SI.run_index_janitor()
        self.assertEqual(admission.active_kinds(), set())

    def test_a_completed_rebuild_leaves_no_ticket_behind(self):
        self._make_stale()
        SI.run_index_janitor()
        self.assertEqual(admission.active_kinds(), set())


class RetryTests(_JanitorCase):
    def test_a_failing_index_is_retried_on_the_next_tick(self):
        self._make_stale()
        failed = {SI.SEARCH_INDEX_CHANNELS: False}
        with mock.patch.object(SI, 'rebuild_search_indexes', return_value=failed) as rebuild:
            SI.run_index_janitor()
            SI.run_index_janitor()
        self.assertEqual(rebuild.call_count, 2)

    def test_a_refused_attempt_is_retried_on_the_next_tick(self):
        self._make_stale()
        with _Held(admission.KIND_SYNC, 'account 2'):
            SI.run_index_janitor()
        outcome = SI.run_index_janitor()
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
            SI.run_index_janitor()
        rebuild.assert_not_called()

    def test_only_the_ownerless_index_is_rebuilt(self):
        """The programs index is fresh here, and rebuilding it anyway would spend ~76s
        re-deriving an index that was already correct."""
        self._make_stale()
        with mock.patch.object(SI, 'rebuild_search_indexes', return_value={}) as rebuild:
            SI.run_index_janitor()
        self.assertEqual(tuple(rebuild.call_args.kwargs['names']), (SI.SEARCH_INDEX_CHANNELS,))

    def test_a_never_built_index_is_repaired_like_any_other(self):
        """A fresh install that has synced but never rebuilt is degraded for the same
        reason and by the same amount as a stale one, so it gets the same repair."""
        SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_CHANNELS).delete()
        db.session.commit()
        self.assertIn('never been built',
                      SI.search_index_readiness(SI.SEARCH_INDEX_CHANNELS)[1])
        SI.run_index_janitor()
        self.assertTrue(self._ready())

    def test_the_reason_reaches_the_log(self):
        """Product principle 1: a rebuild nobody asked for has to say what it was for."""
        self._make_stale()
        with self.assertLogs('app.search_index', level='INFO') as logs:
            SI.run_index_janitor()
        line = '\n'.join(logs.output)
        self.assertIn('janitor', line)
        self.assertIn(SI.SEARCH_INDEX_CHANNELS, line)
        self.assertIn('stale', line)


class SchedulerWiringTests(_JanitorCase):
    """The job exists, fires on an interval, and records only the ticks that repaired."""

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
        runs = JobRun.query.filter_by(job_id='search_index_janitor').all()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].outcome, 'SUCCESS')
        self.assertTrue(self._ready())


class RetiredSettingTests(unittest.TestCase):
    """search.index_janitor_grace_minutes went with the wait it set (dev/changelog/1431)."""

    def test_the_migration_drops_it_and_says_what_it_was(self):
        cfg = {'search': {'page_size': 50, 'index_janitor_grace_minutes': 0}}
        with self.assertLogs('app.config', level='WARNING') as logs:
            out = _cfg_m008_drop_index_janitor_grace(cfg)
        self.assertEqual(out, {'search': {'page_size': 50}})
        self.assertIn('was 0', '\n'.join(logs.output))

    def test_a_config_that_never_set_it_is_untouched(self):
        cfg = {'search': {'page_size': 50}}
        self.assertEqual(_cfg_m008_drop_index_janitor_grace(cfg), {'search': {'page_size': 50}})

    def test_it_is_no_longer_a_default(self):
        self.assertNotIn('index_janitor_grace_minutes', _DEFAULTS['search'])


if __name__ == '__main__':
    unittest.main()
