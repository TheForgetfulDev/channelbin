"""Tier 2 - a sync updates the programs index in place (dev/changelog/1416).

A full rebuild's cost grows with everything the database holds, so a sync close-out writes only
the rows that differ. That is safe only if the result cannot be told apart from a full rebuild,
so that is what these assert, and they assert the refusals: an index a comparison cannot trust
gets the full rebuild and the log line says why.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel  # noqa: E402
from app import db  # noqa: E402
from app import search_index as SI  # noqa: E402
from app.database import EPGEntry, SearchIndexState  # noqa: E402


def _rows():
    return sorted(
        (tuple(r) for r in db.session.execute(text(
            'SELECT channel_id, title, sub_title, description FROM chan_prog')).fetchall()),
        key=repr)


def _ids_by_title():
    return {r[1]: r[0] for r in db.session.execute(text('SELECT id, title FROM chan_prog'))}


def _matches(term):
    return sorted(r[0] for r in db.session.execute(text(
        'SELECT p.title FROM chan_prog_fts JOIN chan_prog p ON p.id = chan_prog_fts.rowid '
        'WHERE chan_prog_fts MATCH :m'), {'m': SI.fts_match_term(term)}))


def _fts_hits(term):
    """How many rows the FTS side alone holds for a term, without joining back to chan_prog: a
    join would drop an FTS row that outlived its content, which is the failure being looked for."""
    return db.session.execute(text(
        'SELECT COUNT(*) FROM chan_prog_fts WHERE chan_prog_fts MATCH :m'),
        {'m': SI.fts_match_term(term)}).scalar()


def _assert_fts_intact():
    """rank=1 is what compares an external-content index against its content table. Without it
    'integrity-check' passes with a content row gone and no FTS delete."""
    db.session.execute(text(
        "INSERT INTO chan_prog_fts(chan_prog_fts, rank) VALUES('integrity-check', 1)"))
    db.session.commit()


class _Rig(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = make_account()
        self.one = make_channel(self.acc, name='One')
        self.two = make_channel(self.acc, name='Two')
        self.three = make_channel(self.acc, name='Three')
        self.now = datetime.utcnow()

    def tearDown(self):
        self.t.cleanup()

    def _air(self, channel, title, sub=None, desc=None, hours=1):
        db.session.add(EPGEntry(channel_id=channel.id, title=title, sub_title=sub,
                                description=desc,
                                start_time=self.now + timedelta(hours=hours),
                                stop_time=self.now + timedelta(hours=hours + 1)))
        db.session.commit()

    def _drop(self, title):
        EPGEntry.query.filter_by(title=title).delete()
        db.session.commit()


class SameResultAsAFullRebuildTests(_Rig):
    def _seed_and_build(self):
        self._air(self.one, 'Keeps Its Row', sub='Week 3', desc='Stays the same')
        self._air(self.one, 'Keeps Its Row', sub='Week 3', desc='Stays the same', hours=26)
        self._air(self.one, 'Null Subtitle Stays')
        self._air(self.one, 'Goes Away Soon')
        self._air(self.two, 'Whole Channel Goes')
        self._air(self.three, 'Description Changes', desc='the old words')
        SI.rebuild_search_indexes('seed')

    def _change_everything(self):
        self._drop('Goes Away Soon')
        self._drop('Whole Channel Goes')
        self._drop('Description Changes')
        self._air(self.three, 'Description Changes', desc='the new words')
        self._air(self.one, 'Brand New Program', sub='Pilot')
        self._air(self.two, 'Channel Comes Back')
        # Two showings of one new program must land as one row, as SELECT DISTINCT makes them.
        self._air(self.two, 'Twice Today', hours=2)
        self._air(self.two, 'Twice Today', hours=5)

    def test_rows_equal_a_full_rebuild(self):
        self._seed_and_build()
        self._change_everything()
        self.assertEqual(SI.rebuild_search_indexes(
            'sync', names=(SI.SEARCH_INDEX_PROGRAMS,), only_changes=True),
            {SI.SEARCH_INDEX_PROGRAMS: True})
        in_place = _rows()
        SI.rebuild_search_indexes('full', names=(SI.SEARCH_INDEX_PROGRAMS,))
        self.assertEqual(in_place, _rows())

    def test_matches_equal_a_full_rebuild(self):
        self._seed_and_build()
        self._change_everything()
        SI.rebuild_search_indexes('sync', names=(SI.SEARCH_INDEX_PROGRAMS,), only_changes=True)
        terms = ('Keeps', 'old words', 'new words', 'Goes Away', 'Whole Channel', 'Pilot',
                 'Twice', 'Null Subtitle', 'Comes Back')
        in_place = {term: _matches(term) for term in terms}
        self.assertEqual(_fts_hits('old words'), 0, 'an FTS row outlived the row it described')
        _assert_fts_intact()
        self.assertEqual(in_place['Twice'], ['Twice Today'])
        SI.rebuild_search_indexes('full', names=(SI.SEARCH_INDEX_PROGRAMS,))
        self.assertEqual(in_place, {term: _matches(term) for term in terms})

    def test_fts_integrity_check_passes_after_repeated_passes(self):
        self._seed_and_build()
        self._change_everything()
        SI.rebuild_search_indexes('sync', names=(SI.SEARCH_INDEX_PROGRAMS,), only_changes=True)
        self._air(self.three, 'Third Pass')
        self._drop('Channel Comes Back')
        SI.rebuild_search_indexes('sync', names=(SI.SEARCH_INDEX_PROGRAMS,), only_changes=True)
        _assert_fts_intact()
        self.assertEqual(_matches('Third Pass'), ['Third Pass'])
        self.assertEqual(_matches('Comes Back'), [])

    def test_an_ended_showing_leaves_like_a_full_rebuild_would_drop_it(self):
        self._air(self.one, 'Ending')
        SI.rebuild_search_indexes('seed')
        EPGEntry.query.filter_by(title='Ending').update(
            {'stop_time': self.now - timedelta(minutes=5)})
        db.session.commit()
        SI.rebuild_search_indexes('sync', names=(SI.SEARCH_INDEX_PROGRAMS,), only_changes=True)
        self.assertEqual(_matches('Ending'), [])

    def test_state_reads_ready_afterwards(self):
        self._seed_and_build()
        self._change_everything()
        SI.rebuild_search_indexes('sync', names=(SI.SEARCH_INDEX_PROGRAMS,), only_changes=True)
        state = SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_PROGRAMS).one()
        self.assertEqual(state.status, SI.STATUS_OK)
        self.assertEqual(state.row_count, len(_rows()))
        self.assertTrue(SI.search_index_ready(SI.SEARCH_INDEX_PROGRAMS))


class WritesOnlyWhatChangedTests(_Rig):
    def test_a_row_that_did_not_change_keeps_its_id(self):
        self._air(self.one, 'Stays')
        self._air(self.two, 'Leaves')
        SI.rebuild_search_indexes('seed')
        before = _ids_by_title()
        self._drop('Leaves')
        self._air(self.three, 'Arrives')
        SI.rebuild_search_indexes('sync', names=(SI.SEARCH_INDEX_PROGRAMS,), only_changes=True)
        self.assertEqual(_ids_by_title()['Stays'], before['Stays'])

    def test_no_change_writes_nothing_to_the_index(self):
        self._air(self.one, 'Stays')
        SI.rebuild_search_indexes('seed')
        with mock.patch.object(SI, 'rebuild_units') as full, \
                mock.patch.object(SI, '_apply_program_changes') as write:
            SI.rebuild_search_indexes('sync', names=(SI.SEARCH_INDEX_PROGRAMS,),
                                      only_changes=True)
        full.assert_not_called()
        write.assert_called_once_with([], [])
        self.assertEqual(_matches('Stays'), ['Stays'])

    def test_a_chunk_boundary_loses_nothing(self):
        for n in range(7):
            self._air(self.one, f'Old {n}')
        SI.rebuild_search_indexes('seed')
        for n in range(7):
            self._drop(f'Old {n}')
            self._air(self.two, f'New {n}')
        with mock.patch.object(SI, 'DIFF_CHUNK_ROWS', 3):
            SI.rebuild_search_indexes('sync', names=(SI.SEARCH_INDEX_PROGRAMS,),
                                      only_changes=True)
        _assert_fts_intact()
        self.assertEqual(_matches('Old'), [])
        self.assertEqual(_matches('New'), [f'New {n}' for n in range(7)])

    def test_the_index_declares_itself_building_while_it_writes(self):
        self._air(self.one, 'Stays')
        SI.rebuild_search_indexes('seed')
        self._air(self.two, 'Arrives')
        seen = []
        real = SI._apply_program_changes

        def spy(*args):
            db.session.expire_all()
            seen.append(SearchIndexState.query.filter_by(
                name=SI.SEARCH_INDEX_PROGRAMS).one().status)
            return real(*args)

        with mock.patch.object(SI, '_apply_program_changes', spy):
            SI.rebuild_search_indexes('sync', names=(SI.SEARCH_INDEX_PROGRAMS,),
                                      only_changes=True)
        self.assertEqual(seen, [SI.STATUS_BUILDING])

    def test_a_lock_on_one_chunk_is_retried_without_losing_the_row(self):
        from sqlalchemy.exc import OperationalError
        self._air(self.one, 'Stays')
        SI.rebuild_search_indexes('seed')
        self._air(self.two, 'Arrives')
        real_commit = db.session.commit
        raised = []

        def lock_once():
            pending = db.session.execute(text(
                "SELECT COUNT(*) FROM chan_prog WHERE title = 'Arrives'")).scalar()
            if pending and not raised:
                raised.append(True)
                raise OperationalError('stmt', {}, Exception('database is locked'))
            return real_commit()

        with mock.patch.object(db.session, 'commit', lock_once), \
                mock.patch('app.db_utils.time.sleep'):
            SI.rebuild_search_indexes('sync', names=(SI.SEARCH_INDEX_PROGRAMS,),
                                      only_changes=True)
        self.assertEqual(raised, [True])
        _assert_fts_intact()
        self.assertEqual(_matches('Arrives'), ['Arrives'])


class FullRebuildWhenItCannotBeTrustedTests(_Rig):
    def _build_then_add(self):
        self._air(self.one, 'Stays')
        SI.rebuild_search_indexes('seed')
        self._air(self.two, 'Arrives')

    def _rebuild_capturing_log(self):
        with self.assertLogs('app.search_index', level='INFO') as logs:
            SI.rebuild_search_indexes('sync', names=(SI.SEARCH_INDEX_PROGRAMS,),
                                      only_changes=True)
        return '\n'.join(logs.output)

    def _set_status(self, status):
        state = SearchIndexState.query.filter_by(name=SI.SEARCH_INDEX_PROGRAMS).one()
        state.status = status
        db.session.commit()

    def test_an_interrupted_full_rebuild_is_never_compared_against(self):
        """The bad state: chan_prog complete, the FTS side empty. A comparison finds nothing to
        do and the index reports OK while matching nothing."""
        self._air(self.one, 'Stays')
        SI.rebuild_search_indexes('seed')
        db.session.execute(text("INSERT INTO chan_prog_fts(chan_prog_fts) VALUES('delete-all')"))
        db.session.commit()
        self._set_status(SI.STATUS_FAILED)
        output = self._rebuild_capturing_log()
        self.assertIn('full, because the last rebuild left it FAILED', output)
        self.assertEqual(_matches('Stays'), ['Stays'])

    def test_status_ok_with_the_two_tables_apart_is_caught_too(self):
        self._air(self.one, 'Stays')
        SI.rebuild_search_indexes('seed')
        db.session.execute(text("INSERT INTO chan_prog_fts(chan_prog_fts) VALUES('delete-all')"))
        db.session.commit()
        output = self._rebuild_capturing_log()
        self.assertIn('full, because chan_prog holds 1 rows and its FTS index 0', output)
        self.assertEqual(_matches('Stays'), ['Stays'])

    def test_a_building_status_means_full(self):
        self._build_then_add()
        self._set_status(SI.STATUS_BUILDING)
        self.assertIn('the last rebuild left it BUILDING', self._rebuild_capturing_log())

    def test_a_never_built_index_means_full(self):
        self._air(self.one, 'Arrives')
        self.assertIn('full, because the last rebuild left it never built',
                      self._rebuild_capturing_log())
        self.assertEqual(_matches('Arrives'), ['Arrives'])

    def test_an_empty_index_means_full(self):
        SI.rebuild_search_indexes('seed')
        self._air(self.one, 'Arrives')
        self.assertIn('full, because it is empty', self._rebuild_capturing_log())

    def test_too_many_differences_means_full(self):
        self._build_then_add()
        self._air(self.three, 'Also Arrives')
        with mock.patch.object(SI, 'DIFF_MAX_CHANGED_ROWS', 1):
            output = self._rebuild_capturing_log()
        self.assertIn('full, because more than 1 rows differ', output)
        self.assertEqual(_matches('Also Arrives'), ['Also Arrives'])

    def test_a_comparison_that_errors_means_full(self):
        import sqlite3
        self._build_then_add()
        with mock.patch.object(SI, '_program_changes', side_effect=sqlite3.OperationalError('x')):
            output = self._rebuild_capturing_log()
        self.assertIn('full, because comparing it failed: x', output)
        self.assertEqual(_matches('Arrives'), ['Arrives'])

    def test_the_log_line_names_what_an_in_place_update_did(self):
        self._build_then_add()
        output = self._rebuild_capturing_log()
        self.assertIn('1 rows added and 0 removed', output)

    def test_only_changes_never_touches_the_channel_index_path(self):
        self._build_then_add()
        with mock.patch.object(SI, '_program_changes') as compare:
            SI.rebuild_search_indexes('sync', names=(SI.SEARCH_INDEX_CHANNELS,),
                                      only_changes=True)
        compare.assert_not_called()


class WhoAsksForWhatTests(_Rig):
    def test_a_plain_rebuild_stays_full(self):
        self._air(self.one, 'Stays')
        SI.rebuild_search_indexes('seed')
        with mock.patch.object(SI, '_program_changes') as compare:
            SI.rebuild_search_indexes('manual', names=(SI.SEARCH_INDEX_PROGRAMS,))
        compare.assert_not_called()

    def _cleanup_rebuilds(self, keep_days=1):
        from app.accounts import cleanup_old_epg_entries
        with mock.patch('app.accounts.load_config',
                        return_value={'sync': {'epg_keep_days': keep_days}}), \
                mock.patch.object(SI, 'rebuild_search_indexes') as rebuild:
            cleanup_old_epg_entries(self.t.app)
        return rebuild

    def test_the_daily_cleanup_stays_full_because_it_is_what_compacts(self):
        self._air(self.one, 'Old', hours=-1000)
        rebuild = self._cleanup_rebuilds()
        self.assertEqual(rebuild.call_count, 1)
        self.assertNotIn('only_changes', rebuild.call_args.kwargs)

    def test_the_daily_rebuild_runs_on_a_day_nothing_aged_out(self):
        self._air(self.one, 'Upcoming')
        self.assertEqual(self._cleanup_rebuilds().call_count, 1)

    def test_the_daily_rebuild_runs_when_listings_are_kept_forever(self):
        self._air(self.one, 'Old', hours=-1000)
        self.assertEqual(self._cleanup_rebuilds(keep_days=0).call_count, 1)
        self.assertEqual(EPGEntry.query.count(), 1, 'keep forever must still delete nothing')


class DiffTests(unittest.TestCase):
    def test_a_row_repeated_on_the_index_side_keeps_one_copy(self):
        have = [(1, 5, 'A', None, None), (2, 5, 'A', None, None)]
        remove, add = SI._diff_program_rows(iter([(5, 'A', None, None)]), iter(have), 100)
        self.assertEqual(remove, [2])
        self.assertEqual(add, [])

    def test_channels_only_on_one_side(self):
        want = [(2, 'B', None, None), (2, 'B', None, None)]
        have = [(9, 1, 'A', None, None), (8, 3, 'C', None, None)]
        remove, add = SI._diff_program_rows(iter(want), iter(have), 100)
        self.assertEqual(sorted(remove), [8, 9])
        self.assertEqual(add, [(2, 'B', None, None)])

    def test_none_and_empty_text_are_different_rows(self):
        remove, add = SI._diff_program_rows(iter([(1, 'A', '', None)]),
                                            iter([(7, 1, 'A', None, None)]), 100)
        self.assertEqual(remove, [7])
        self.assertEqual(add, [(1, 'A', '', None)])

    def test_stops_past_the_cap(self):
        want = [(n, 'A', None, None) for n in range(10)]
        self.assertIsNone(SI._diff_program_rows(iter(want), iter([]), 3))


if __name__ == '__main__':
    unittest.main()
