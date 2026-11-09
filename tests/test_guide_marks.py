"""The XMLTV <live/> and <new/> elements as a real attribute of a showing
(dev/changelog/1454).

Characterization tests for new behavior, except where a docstring says otherwise:

  - ImportTests: the import reads both elements into both listing tables, and a change of
    winner carries them across.
  - SearchTests: the Guide mark dimension on the Programs search - the filter, its exclusion,
    the facet, the 400 for a value outside the vocabulary, the row payload, and a rule's
    validation. The facet-equals-filter guarantee under the duplicate fold and the group
    collapse is held by tests/test_duplicate_fold.py and tests/test_collapse_facet_counts.py,
    which count this dimension with the rest.
  - MigrationTests: step 113 adds the four columns to a database that lacks them and is a
    no-op on one that has them.

No network: every feed is a local byte string.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app import migrations as M  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.auto_rules import RuleError, clean_search  # noqa: E402
from app.channel_search import (GRAIN_AIRINGS, GRAIN_CHANNELS, MARK_VALUES,  # noqa: E402
                                DimensionFilter, SearchContext, SearchState, SearchStateError,
                                describe_state, dimensions_for, search, search_facets)
from app.channel_search_rows import build_rows  # noqa: E402
from app.database import EPGEntry, EpgAlternateEntry  # noqa: E402
from app.epg_sources import move_source  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import only_hiding  # noqa: E402
from tests.test_epg_sources import CFG, _Fixture  # noqa: E402


def _xmltv(programs):
    """programs: (xml_id, offset_minutes, title, inner xml)."""
    now = datetime.utcnow()
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for xml_id, offset, title, inner in programs:
        start = now + timedelta(minutes=offset)
        parts.append(f'<programme start="{start:%Y%m%d%H%M%S} +0000" '
                     f'stop="{start + timedelta(minutes=30):%Y%m%d%H%M%S} +0000" '
                     f'channel="{xml_id}"><title>{title}</title>{inner}</programme>')
    parts.append('</tv>')
    return ''.join(parts).encode()


FEED = [('a.test', 60, 'The Match', '<rating><value>M</value></rating><live/>'),
        ('a.test', 90, 'The Premiere', '<new />'),
        ('a.test', 120, 'Both', '<live></live><new/>'),
        ('a.test', 150, 'Neither', '<previously-shown/>')]


class ImportTests(_Fixture):

    def _marks(self, model, ch):
        return {e.title: (e.is_live, e.is_new)
                for e in model.query.filter_by(channel_id=ch.id)}

    def test_the_import_reads_both_elements(self):
        ch = self._channel('Alpha One', 'a.test')
        import_source(self.src, _xmltv(FEED), epg_days=3, cfg=CFG)
        self.assertEqual(self._marks(EPGEntry, ch), {
            'The Match': (True, False), 'The Premiere': (False, True),
            'Both': (True, True), 'Neither': (False, False)})

    def test_a_change_of_winner_carries_the_marks_both_ways(self):
        ch = self._channel('Alpha One', 'a.test')
        ext = self._second_source()
        import_source(self.src, _xmltv(FEED), epg_days=3, cfg=CFG)
        import_source(ext, _xmltv([('a.test', 60, 'Ext one', '<new/>'),
                                   ('a.test', 90, 'Ext two', ''),
                                   ('a.test', 120, 'Ext three', '<live/>')]),
                      epg_days=3, cfg=CFG)
        self.assertEqual(self._marks(EpgAlternateEntry, ch), {
            'Ext one': (False, True), 'Ext two': (False, False), 'Ext three': (True, False)},
            'the losing source\'s rows carry their marks in the alternates')
        move_source(self.acct.id, self.src.id, 1, False)
        db.session.expire_all()
        self.assertEqual(self._winner(ch), ext.id)
        self.assertEqual(self._marks(EPGEntry, ch), {
            'Ext one': (False, True), 'Ext two': (False, False), 'Ext three': (True, False)})
        self.assertEqual(self._marks(EpgAlternateEntry, ch)['Both'], (True, True))


class SearchTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        acct = seed.make_account(name='Alpha', last_sync_at=datetime.utcnow())
        self.ch = seed.make_channel(acct, name='Sports One', last_seen_at=datetime.utcnow())
        seed.make_epg_entry(self.ch, title='The Match', offset_minutes=60, is_live=True,
                            is_new=False)
        seed.make_epg_entry(self.ch, title='The Premiere', offset_minutes=120, is_live=False,
                            is_new=True)
        seed.make_epg_entry(self.ch, title='Both', offset_minutes=180, is_live=True, is_new=True)
        seed.make_epg_entry(self.ch, title='Neither', offset_minutes=240, is_live=False,
                            is_new=False)
        # Imported before the columns existed: no mark either way.
        seed.make_epg_entry(self.ch, title='Old import', offset_minutes=300)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _state(self, *filters, **kw):
        kw.setdefault('standing', only_hiding())
        return SearchState(grain=GRAIN_AIRINGS, filters=tuple(filters), **kw)

    def _titles(self, *filters):
        return {r.title for r in search(self._state(*filters, facets=()),
                                        SearchContext.build()).rows}

    def test_the_dimension_is_on_programs_only(self):
        self.assertIn('mark', [d.key for d in dimensions_for(GRAIN_AIRINGS)])
        self.assertNotIn('mark', [d.key for d in dimensions_for(GRAIN_CHANNELS)])

    def test_picking_a_mark_keeps_the_showings_carrying_it(self):
        self.assertEqual(self._titles(DimensionFilter('mark', ('live',))),
                         {'The Match', 'Both'})
        self.assertEqual(self._titles(DimensionFilter('mark', ('new',))),
                         {'The Premiere', 'Both'})

    def test_two_marks_mean_either(self):
        self.assertEqual(self._titles(DimensionFilter('mark', ('live', 'new'))),
                         {'The Match', 'The Premiere', 'Both'})

    def test_excluding_a_mark_keeps_a_showing_with_no_mark_stored(self):
        self.assertEqual(self._titles(DimensionFilter('mark', (), ('live',))),
                         {'The Premiere', 'Neither', 'Old import'})

    def test_the_facet_counts_each_mark(self):
        facets = search_facets(self._state(), SearchContext.build())
        self.assertEqual(facets['mark'], {'live': 2, 'new': 2})
        picked = search_facets(self._state(DimensionFilter('mark', ('live',))),
                               SearchContext.build())
        self.assertEqual(picked['mark'], {'live': 2, 'new': 2},
                         'its own filter comes off when it is counted')

    def test_a_value_outside_the_vocabulary_is_refused(self):
        with self.assertRaises(SearchStateError):
            search(self._state(DimensionFilter('mark', ('premiere',)), facets=()),
                   SearchContext.build())

    def test_a_row_says_its_marks_in_words(self):
        state = self._state(facets=())
        ctx = SearchContext.build()
        rows = {r['title']: r['guide_marks'] for r in build_rows(search(state, ctx), state, ctx)}
        self.assertEqual(rows, {'The Match': ['Live'], 'The Premiere': ['New'],
                                'Both': ['Live', 'New'], 'Neither': [], 'Old import': []})

    def test_a_search_is_described_with_the_marks_words(self):
        said = describe_state(self._state(DimensionFilter('mark', ('live',))))
        self.assertIn('guide mark Live', said['summary'])

    def test_a_rule_refuses_a_mark_outside_the_vocabulary(self):
        for value, ok in (('live', True), ('premiere', False)):
            params = urlencode(SearchState(grain=GRAIN_AIRINGS, q='match', filters=(
                DimensionFilter('mark', (value,)),)).to_params(explicit=True))
            with self.subTest(value=value):
                if ok:
                    clean_search(params)
                else:
                    with self.assertRaises(RuleError):
                        clean_search(params)

    def test_the_vocabulary_is_live_and_new(self):
        self.assertEqual(MARK_VALUES, ('live', 'new'))


class MigrationTests(unittest.TestCase):

    def _scratch(self, columns):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        conn = sqlite3.connect(os.path.join(td.name, 'scratch.db'))
        self.addCleanup(conn.close)
        cur = conn.cursor()
        for table in ('epg_entries', 'epg_alternate_entries'):
            cur.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY, title VARCHAR(512)'
                        f'{columns})')
            cur.execute(f"INSERT INTO {table} (title) VALUES ('kept')")
        conn.commit()
        return conn, cur

    def _columns(self, cur, table):
        return [r[1] for r in cur.execute(f'PRAGMA table_info({table})')]

    def test_the_columns_are_added_and_no_row_is_touched(self):
        conn, cur = self._scratch('')
        M._m113_epg_live_new_marks(conn, cur)
        for table in ('epg_entries', 'epg_alternate_entries'):
            self.assertEqual(self._columns(cur, table)[-2:], ['is_live', 'is_new'])
            self.assertEqual(cur.execute(f'SELECT title, is_live, is_new FROM {table}')
                             .fetchall(), [('kept', None, None)])

    def test_a_database_that_has_them_is_left_alone(self):
        conn, cur = self._scratch(', is_live BOOLEAN, is_new BOOLEAN')
        before = cur.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall()
        M._m113_epg_live_new_marks(conn, cur)
        M._m113_epg_live_new_marks(conn, cur)
        self.assertEqual(cur.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall(),
                         before)


if __name__ == '__main__':
    unittest.main()
