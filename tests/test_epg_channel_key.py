"""Tier 2 - the user's per-channel EPG key (DESIGN-epg-sources.md §6.1, dev/changelog/1102).

`Channel.epg_channel_id` is what the provider said and every sync rewrites it, so a channel
carrying the wrong id could not be fixed in a way that lasted. The key is the user's answer
to "which id is this channel in that source's file": per channel AND per source, written by
`epg_sources.set_channel_key()` alone, never by a sync. These tests pin:

  - SurvivesSyncTests: the key, not the provider's id, decides the channel's listings on
    every import, and a key on one source does not leak into another.
  - ApplyNowTests: saving a key copies listings the database already holds (another visible
    channel carries that key), moving the winner between sources when it changes; a key no
    channel carries waits for the source's next refresh and the page says so until then.
  - RouteTests: the Set key / Clear key endpoint and the lookup, and what they refuse.

No network - every feed is a local byte string (CLAUDE.md §Testing).
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app import accounts as accounts_mod  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.database import (Channel, ChannelEvent, EpgChannelKey, EpgSource,  # noqa: E402
                          EpgSourceSubscription, CHANNEL_EPG_KEY_CHANGED,
                          CHANNEL_EPG_SOURCE_CHANGED)
from app.epg_sources import (KEY_COPIED, KEY_HIDDEN, KEY_NOT_IN_FILE,  # noqa: E402
                             KEY_SAME_LISTINGS, KEY_UNREFRESHED, KEY_WAITS, set_channel_key)
from tests.test_epg_sources import BANNER, CFG, REAL, _Fixture, _schedule, _xmltv  # noqa: E402

RIGHT = ['Right News', 'Right Film', 'Right Sport']
WRONG = ['Tulsa News', 'Tulsa Film', 'Tulsa Sport']


class _KeyFixture(_Fixture):

    def _set(self, ch, source, key, case_sensitive=False):
        change = set_channel_key(db.session.get(Channel, ch.id),
                                 db.session.get(EpgSource, source.id), key,
                                 case_sensitive=case_sensitive)
        db.session.commit()
        db.session.expire_all()
        return change

    def _titles(self, rows):
        return {t for _s, t in rows}

    def _key_events(self, ch):
        return ChannelEvent.query.filter_by(channel_id=ch.id,
                                            event_type=CHANNEL_EPG_KEY_CHANGED).all()


class SurvivesSyncTests(_KeyFixture):

    def test_the_key_decides_the_listings_after_the_provider_rewrites_its_id(self):
        ch = self._channel('US: CW TAMPA BAY HD', 'kqcw.test')
        xml = _xmltv(_schedule('kqcw.test', WRONG) + _schedule('wtog.test', RIGHT))
        import_source(self.src, xml, epg_days=3, cfg=CFG)
        self._set(ch, self.src, 'wtog.test')

        # A sync rewrites what the provider says; it never touches the key.
        db.session.get(Channel, ch.id).epg_channel_id = 'kqcw-renamed.test'
        db.session.commit()
        import_source(self.src, xml, epg_days=3, cfg=CFG)

        self.assertEqual(self._titles(self._active(ch)), set(RIGHT))
        self.assertEqual(EpgChannelKey.query.filter_by(channel_id=ch.id).one().key, 'wtog.test')

    def test_a_key_on_one_source_does_not_leak_into_another(self):
        ch = self._channel('Alpha One', 'a1.test')
        ext = self._second_source()
        self._set(ch, ext, 'ext-a1.test')
        import_source(self.src, _xmltv(_schedule('a1.test', REAL)), epg_days=3, cfg=CFG)
        import_source(ext, _xmltv(_schedule('ext-a1.test', RIGHT) + _schedule('a1.test', WRONG)),
                      epg_days=3, cfg=CFG)
        self.assertEqual(self._titles(self._active(ch)), set(REAL))
        self.assertEqual(self._titles(self._alternates(ch)), set(RIGHT),
                         'the external source must match on the key, not the provider id')

    def test_clearing_goes_back_to_the_provider_id(self):
        ch = self._channel('Alpha One', 'a1.test')
        xml = _xmltv(_schedule('a1.test', REAL) + _schedule('other.test', RIGHT))
        import_source(self.src, xml, epg_days=3, cfg=CFG)
        self._set(ch, self.src, 'other.test')
        self._set(ch, self.src, '')
        self.assertIsNone(EpgChannelKey.query.filter_by(channel_id=ch.id).first())
        self.assertIn('cleared', self._key_events(ch)[-1].detail)
        import_source(self.src, xml, epg_days=3, cfg=CFG)
        self.assertEqual(self._titles(self._active(ch)), set(REAL))


class ApplyNowTests(_KeyFixture):

    def test_listings_another_channel_holds_are_copied_at_once(self):
        ch = self._channel('US: CW TAMPA BAY HD', 'kqcw.test')
        donor = self._channel('CW Tampa (backup feed)', 'wtog.test')
        import_source(self.src, _xmltv(_schedule('kqcw.test', WRONG) + _schedule('wtog.test', RIGHT)),
                      epg_days=3, cfg=CFG)

        change = self._set(ch, self.src, 'wtog.test')

        self.assertEqual(change.outcome, KEY_COPIED)
        self.assertEqual(self._titles(self._active(ch)), set(RIGHT),
                         'no refresh ran: the rows must come from the channel already holding them')
        self.assertEqual(len(self._active(ch)), 3)
        self.assertIn(f'#{donor.id}', self._key_events(ch)[0].detail)
        self.assertEqual(self._titles(self._active(donor)), set(RIGHT), 'the donor keeps its own')

    def test_a_key_no_channel_carries_waits_for_the_next_refresh_and_the_page_says_so(self):
        ch = self._channel('US: CW TAMPA BAY HD', 'kqcw.test')
        xml = _xmltv(_schedule('kqcw.test', WRONG) + _schedule('wtog.test', RIGHT))
        import_source(self.src, xml, epg_days=3, cfg=CFG)

        change = self._set(ch, self.src, 'wtog.test')

        self.assertEqual(change.outcome, KEY_WAITS)
        self.assertEqual(self._titles(self._active(ch)), set(WRONG),
                         'the old listings stay until the refresh brings the new ones')
        page = self.t.app.test_client().get(f'/channels/{ch.id}').get_data(as_text=True)
        self.assertIn("Waiting for this source&#39;s next refresh", page)

        import_source(self.src, xml, epg_days=3, cfg=CFG)
        self.assertEqual(self._titles(self._active(ch)), set(RIGHT))
        page = self.t.app.test_client().get(f'/channels/{ch.id}').get_data(as_text=True)
        self.assertNotIn('Waiting for this source', page)

    def test_a_key_not_in_the_file_says_so(self):
        ch = self._channel('Alpha One', 'a1.test')
        import_source(self.src, _xmltv(_schedule('a1.test', REAL)), epg_days=3, cfg=CFG)
        self.assertEqual(self._set(ch, self.src, 'nowhere.test').outcome, KEY_NOT_IN_FILE)

    def test_a_source_never_refreshed_applies_the_key_at_its_first_refresh(self):
        ch = self._channel('Alpha One', 'a1.test')
        self.assertEqual(self._set(ch, self.src, 'b1.test').outcome, KEY_UNREFRESHED)

    def test_a_hidden_channel_imports_nothing_to_wait_for(self):
        ch = self._channel('Alpha One', 'a1.test', hidden=True)
        self.assertEqual(self._set(ch, self.src, 'b1.test').outcome, KEY_HIDDEN)

    def test_a_key_that_matches_the_same_listings_changes_nothing_else(self):
        ch = self._channel('Alpha One', 'a1.test')
        import_source(self.src, _xmltv(_schedule('a1.test', REAL)), epg_days=3, cfg=CFG)
        self.assertEqual(self._set(ch, self.src, 'A1.TEST').outcome, KEY_SAME_LISTINGS)
        self.assertEqual(self._titles(self._active(ch)), set(REAL))

    def test_saving_the_same_key_twice_writes_one_event(self):
        ch = self._channel('Alpha One', 'a1.test')
        self._set(ch, self.src, 'b1.test')
        self.assertIsNone(self._set(ch, self.src, 'b1.test'))
        self.assertEqual(len(self._key_events(ch)), 1)

    def test_a_copy_that_changes_the_winner_moves_rows_between_the_tables(self):
        # The provider lists only a banner under the channel's own id; the external file
        # has a real schedule under the id a second channel already carries there.
        ch = self._channel('Alpha One', 'a1.test')
        donor = self._channel('Alpha One (feed 2)', 'b1.test')
        ext = self._second_source()
        import_source(self.src, _xmltv(_schedule('a1.test', BANNER)), epg_days=3, cfg=CFG)
        import_source(ext, _xmltv(_schedule('b1.test', RIGHT)), epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(ch), self.src.id)

        self.assertEqual(self._set(ch, ext, 'b1.test').outcome, KEY_COPIED)

        self.assertEqual(self._winner(ch), ext.id)
        self.assertEqual({s for s, _t in self._active(ch)}, {ext.id},
                         'the old winner\'s rows must leave epg_entries')
        self.assertEqual({s for s, _t in self._alternates(ch)}, {self.src.id})
        self.assertEqual(ChannelEvent.query.filter_by(
            channel_id=ch.id, event_type=CHANNEL_EPG_SOURCE_CHANGED).count(), 1)
        self.assertEqual(self._winner(donor), ext.id)

    def test_case_folding_follows_the_matching_setting(self):
        exact = self._channel('Alpha One', 'a1.test')
        folded = self._channel('Alpha Two', 'a2.test')
        self._channel('Beta', 'b1.test')
        import_source(self.src, _xmltv(_schedule('a1.test', REAL) + _schedule('a2.test', REAL)
                                       + _schedule('b1.test', RIGHT)), epg_days=3, cfg=CFG)
        self.assertEqual(self._set(exact, self.src, 'B1.TEST', case_sensitive=True).outcome,
                         KEY_NOT_IN_FILE)
        self.assertEqual(self._set(folded, self.src, 'B1.TEST', case_sensitive=False).outcome,
                         KEY_COPIED)

    def test_last_success_is_when_the_import_read_the_keys(self):
        # A key saved while an import runs was not in its channel map; "waiting" compares
        # against last_success_at, so that must be no later than the keys were read.
        self._channel('Alpha One', 'a1.test')
        read_at = []
        real = accounts_mod.accepted_keys

        def _spy(ids):
            read_at.append(datetime.utcnow())
            return real(ids)
        with mock.patch.object(accounts_mod, 'accepted_keys', _spy):
            import_source(self.src, _xmltv(_schedule('a1.test', REAL)), epg_days=3, cfg=CFG)
        db.session.expire_all()
        self.assertLessEqual(db.session.get(EpgSource, self.src.id).last_success_at, read_at[0])


class RouteTests(_KeyFixture):

    def setUp(self):
        super().setUp()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.ch = self._channel('US: CW TAMPA BAY HD', 'kqcw.test')
        import_source(self.src, _xmltv(
            _schedule('kqcw.test', WRONG) + _schedule('wtog.test', RIGHT),
            channels=[('wtog.test', ['US: CW Tampa Bay', 'WTOG']), ('kqcw.test', ['KQCW Tulsa'])]),
            epg_days=3, cfg=CFG)

    def _post(self, body):
        return self.client.post(f'/api/channels/{self.ch.id}/epg-key', json=body)

    def test_set_and_clear(self):
        r = self._post({'source_id': self.src.id, 'key': ' wtog.test '})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()['changed'])
        self.assertEqual(EpgChannelKey.query.filter_by(channel_id=self.ch.id).one().key,
                         'wtog.test')
        r = self._post({'source_id': self.src.id, 'key': ''})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(EpgChannelKey.query.filter_by(channel_id=self.ch.id).first())

    def test_a_source_the_account_does_not_read_is_refused(self):
        other = self._second_source(name='Elsewhere')
        EpgSourceSubscription.query.filter_by(source_id=other.id).delete()
        db.session.commit()
        self.assertEqual(self._post({'source_id': other.id, 'key': 'x'}).status_code, 400)
        self.assertEqual(self._post({'source_id': 'nope', 'key': 'x'}).status_code, 400)
        self.assertIsNone(EpgChannelKey.query.first())

    def test_an_overlong_or_non_text_key_is_refused(self):
        self.assertEqual(self._post({'source_id': self.src.id, 'key': 'x' * 256}).status_code, 400)
        self.assertEqual(self._post({'source_id': self.src.id, 'key': 5}).status_code, 400)

    def test_the_lookup_finds_an_id_by_display_name(self):
        r = self.client.get(f'/api/channels/{self.ch.id}/epg-key/lookup',
                            query_string={'source_id': self.src.id, 'q': 'tampa'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x['xml_id'] for x in r.get_json()['results']], ['wtog.test'])
        r = self.client.get(f'/api/channels/{self.ch.id}/epg-key/lookup',
                            query_string={'source_id': self.src.id, 'q': '100%'})
        self.assertEqual(r.get_json()['results'], [], 'a % in the query is a literal')

    def test_the_page_offers_set_key_on_each_source_the_account_reads(self):
        self._post({'source_id': self.src.id, 'key': 'wtog.test'})
        html = self.client.get(f'/channels/{self.ch.id}').get_data(as_text=True)
        self.assertIn('data-act="epg-key-set"', html)
        self.assertIn('data-act="epg-key-clear"', html)
        self.assertIn('your key', html)


if __name__ == '__main__':
    unittest.main()
