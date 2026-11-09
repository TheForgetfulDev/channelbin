"""Duplicates show by default in the channel search (dev/changelog/1458).

"Show duplicates" was off by default, so every copy of a duplicated stream but the kept one
was left out of a bare search - and the person looking for one of those channels was told,
in effect, that it did not exist. The default is now on, on both tabs.

  - DefaultTests: a bare search lists every copy and reports nothing hidden as a duplicate;
    unticking the option folds them again and says how many.
  - StoredSearchTests: a search that names its own standing set is not moved by the new
    default - an old link, and a rule stored with the old defaults written out
    (dev/changelog/1394).

No network, no ffmpeg.
"""
import os
import sys
import unittest
from datetime import datetime
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.datastructures import MultiDict  # noqa: E402

from app import db, duplicate_streams  # noqa: E402
from app.auto_rules import clean_search  # noqa: E402
from app.channel_search import (DEFAULT_STANDING, GRAIN_AIRINGS, GRAIN_CHANNELS,  # noqa: E402
                                SearchContext, SearchState, default_standing_for, search,
                                standing_applied)
from tests.support import make_test_app, seed  # noqa: E402
from tests.support.search import only_hiding  # noqa: E402

OLD_DEFAULT = only_hiding('showhidden', 'showdup', 'shownotnorm')


class _Case(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        acct = seed.make_account(name='Alpha', last_sync_at=datetime.utcnow())
        self.kept = seed.make_channel(acct, name='FS1 HD', in_guide=True,
                                      last_seen_at=datetime.utcnow())
        self.copy = seed.make_channel(acct, name='Fox Sports 1', last_seen_at=datetime.utcnow())
        self.copy.stream_url = self.copy.raw_stream_url = self.kept.stream_url
        self.other = seed.make_channel(acct, name='Elsewhere', last_seen_at=datetime.utcnow())
        for ch in (self.kept, self.copy, self.other):
            seed.make_epg_entry(ch, title=f'Show on {ch.name}', offset_minutes=30)
        db.session.commit()
        duplicate_streams.recompute()
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _names(self, **kw):
        kw.setdefault('facets', ())
        result = search(SearchState(**kw), SearchContext.build())
        if kw.get('grain') == GRAIN_AIRINGS:
            return sorted(r.title for r in result.rows), result
        return sorted(r.name for r in result.rows), result


class DefaultTests(_Case):

    def test_the_option_is_on_by_default_on_both_tabs(self):
        self.assertIn('showdup', DEFAULT_STANDING)
        for grain in (GRAIN_CHANNELS, GRAIN_AIRINGS):
            self.assertFalse(standing_applied(default_standing_for(grain), 'showdup'))

    def test_a_bare_channel_search_lists_every_copy(self):
        names, result = self._names()
        self.assertEqual(names, ['Elsewhere', 'FS1 HD', 'Fox Sports 1'])
        self.assertNotIn('showdup', result.standing_hidden)

    def test_a_bare_program_search_lists_every_copys_showings(self):
        titles, _result = self._names(grain=GRAIN_AIRINGS,
                                      standing=default_standing_for(GRAIN_AIRINGS))
        self.assertEqual(titles, ['Show on Elsewhere', 'Show on FS1 HD', 'Show on Fox Sports 1'])

    def test_the_api_with_no_standing_parameter_lists_every_copy(self):
        payload = self.t.client.get('/api/channels/search?q=fox').get_json()
        self.assertEqual([r['name'] for r in payload['rows']], ['Fox Sports 1'],
                         'the copy is findable by its own name')
        # A typed search's counts come from their own endpoint, so the key may be absent.
        self.assertFalse((payload.get('standing_hidden') or {}).get('showdup'))

    def test_unticking_it_folds_them_again_and_says_how_many(self):
        names, result = self._names(standing=DEFAULT_STANDING - {'showdup'})
        self.assertEqual(names, ['Elsewhere', 'FS1 HD'])
        self.assertEqual(result.standing_hidden['showdup'], 1)


class StoredSearchTests(_Case):

    def test_a_link_that_names_its_standing_set_keeps_its_fold(self):
        query = urlencode([('standing', k) for k in sorted(OLD_DEFAULT)])
        payload = self.t.client.get(f'/api/channels/search?{query}').get_json()
        self.assertEqual(sorted(r['name'] for r in payload['rows']), ['Elsewhere', 'FS1 HD'])
        self.assertEqual(payload['standing_hidden']['showdup'], 1)

    def test_a_rule_stored_with_the_old_defaults_keeps_hiding_duplicates(self):
        """A rule's spelling always writes its standing set, so the day the default moved
        is not the day the rule starts matching both copies."""
        stored = urlencode(SearchState(
            grain=GRAIN_AIRINGS, q='show',
            standing=default_standing_for(GRAIN_AIRINGS) - {'showdup'}).to_params(explicit=True))
        state, params = clean_search(stored)
        self.assertTrue(standing_applied(state.standing, 'showdup'))
        again = SearchState.from_params(MultiDict(
            [tuple(pair.split('=', 1)) for pair in params.split('&')]))
        self.assertTrue(standing_applied(again.standing, 'showdup'),
                        'and its stored spelling still says so')
        titles = sorted(r.title for r in search(state, SearchContext.build()).rows)
        self.assertEqual(titles, ['Show on Elsewhere', 'Show on FS1 HD'])


if __name__ == '__main__':
    unittest.main()
