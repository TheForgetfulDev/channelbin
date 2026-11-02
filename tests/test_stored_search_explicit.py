"""A stored Programs search names its own search fields and standing options
(dev/changelog/1394, dev/docs/BUGS.md 2026-11-01 12:42 PM).

An auto-record rule used to be stored through `to_params()`, which leaves out whatever sits at
the grain's default. A rule saved with the default Search in therefore carried no `in=` at all
and meant "whatever the default is on the day this rule runs" - so moving the airing grain's
default fields to include the channel name would have widened every such rule with nobody
looking. Rules are now stored with `to_params(explicit=True)`, and migration 108 wrote the old
defaults into every rule (and saved search) that was missing them.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import auto_rules, channel_search  # noqa: E402
from app import migrations as M  # noqa: E402
from app.channel_search import GRAIN_AIRINGS  # noqa: E402

OLD_FIELDS = ('epg-title', 'epg-sub', 'epg-desc')
OLD_STANDING = frozenset({'grpdedup', 'shownoepg', 'showuntested'})


def _keys(params):
    return {k for k, _v in parse_qsl(params, keep_blank_values=True)}


class RuleStoredExplicitlyTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def stored(self, raw):
        return auto_rules.read_rule_values({'name': 'r', 'search': raw}).search_params

    def test_a_rule_at_the_defaults_still_spells_them_out(self):
        params = self.stored('grain=airings&q=nascar')
        self.assertIn('in', _keys(params))
        self.assertIn('standing', _keys(params))

    def test_moving_the_default_does_not_move_a_stored_rule(self):
        params = self.stored('grain=airings&q=nascar')
        before = auto_rules.parse_search(params)
        moved = {**channel_search.DEFAULT_FIELDS_BY_GRAIN, GRAIN_AIRINGS: ('epg-title',)}
        with mock.patch.dict(channel_search.DEFAULT_FIELDS_BY_GRAIN, moved):
            after = auto_rules.parse_search(params)
        self.assertEqual(after.fields, before.fields)
        self.assertEqual(after.standing, before.standing)

    def test_the_edit_search_link_carries_the_rules_own_fields(self):
        from app.auto_rules_view import search_urls
        with self.t.app.test_request_context():
            _open, edit = search_urls(7, 'q=cup&in=epg-title&in=epg-sub&in=epg-desc'
                                         '&standing=grpdedup&grain=airings')
        self.assertEqual(parse_qsl(edit.split('?', 1)[1]).count(('in', 'epg-title')), 1)
        self.assertIn(('standing', 'grpdedup'), parse_qsl(edit.split('?', 1)[1]))


class Migration108Tests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(os.path.join(self.td.name, 'scratch.db'))
        self.cur = self.conn.cursor()
        self.cur.execute('CREATE TABLE auto_rules (id INTEGER PRIMARY KEY, search_params TEXT)')
        self.cur.execute('CREATE TABLE user_prefs (key TEXT PRIMARY KEY, value TEXT)')

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def rule(self, params):
        self.cur.execute('INSERT INTO auto_rules (search_params) VALUES (?)', (params,))
        return self.cur.lastrowid

    def params_of(self, rule_id):
        return self.cur.execute('SELECT search_params FROM auto_rules WHERE id = ?',
                                (rule_id,)).fetchone()[0]

    def run_twice(self):
        M._m108_stored_searches_explicit(self.conn, self.cur)
        M._m108_stored_searches_explicit(self.conn, self.cur)

    def test_a_rule_naming_neither_keeps_matching_what_it_matched(self):
        rid = self.rule('q=NASCAR+Cup&f.when=next%3A7%3Adays&grain=airings')
        self.run_twice()
        state = auto_rules.parse_search(self.params_of(rid))
        self.assertEqual(state.fields, OLD_FIELDS)
        self.assertEqual(state.standing, OLD_STANDING)
        self.assertEqual(state.q, 'NASCAR Cup')
        self.assertEqual(state.filter_for('when').values, ('next:7:days',))
        # Written in the engine's own order, so saving the rule unchanged is not an edit.
        self.assertEqual(urlencode(state.to_params(explicit=True)), self.params_of(rid))

    def test_only_the_missing_half_is_written(self):
        rid = self.rule('q=cup&in=epg-title&grain=airings')
        self.run_twice()
        state = auto_rules.parse_search(self.params_of(rid))
        self.assertEqual(state.fields, ('epg-title',))
        self.assertEqual(state.standing, OLD_STANDING)

    def test_an_empty_standing_set_is_kept_empty(self):
        rid = self.rule('q=cup&standing=&grain=airings')
        self.run_twice()
        self.assertEqual(auto_rules.parse_search(self.params_of(rid)).standing, frozenset())

    def test_a_rule_naming_both_is_untouched(self):
        text = 'q=cup&in=epg-title&standing=grpdedup&grain=airings'
        rid = self.rule(text)
        self.run_twice()
        self.assertEqual(self.params_of(rid), text)

    def test_saved_searches_get_the_same_and_a_channel_search_is_left_alone(self):
        saved = [{'name': 'a', 'params': 'q=cup&grain=airings', 'is_default': False},
                 {'name': 'c', 'params': 'q=cup', 'is_default': True},
                 'not a record']
        self.cur.execute("INSERT INTO user_prefs VALUES ('channel_search_saved', ?)",
                         (json.dumps(saved),))
        self.run_twice()
        rows = json.loads(self.cur.execute(
            "SELECT value FROM user_prefs WHERE key = 'channel_search_saved'").fetchone()[0])
        self.assertEqual(auto_rules.parse_search(rows[0]['params']).fields, OLD_FIELDS)
        self.assertEqual(rows[1:], saved[1:])

    def test_a_saved_search_pref_that_is_not_a_list_is_left_alone(self):
        self.cur.execute("INSERT INTO user_prefs VALUES ('channel_search_saved', '{oops')")
        M._m108_stored_searches_explicit(self.conn, self.cur)
        self.assertEqual(self.cur.execute('SELECT value FROM user_prefs').fetchone()[0], '{oops')

    def test_registered_at_its_own_version(self):
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(108), M._m108_stored_searches_explicit)


if __name__ == '__main__':
    unittest.main()
