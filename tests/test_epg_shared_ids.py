"""Tier 2 - one EPG id shared by unrelated channels (dev/changelog/1716).

Guards dev/docs/BUGS.md 2026-11-24 @ 11:15:12 AM: a provider gave one guide id, `TS`, to
180 of an account's channels - BBC 3, ITV London, HGTV +1 - and all of them showed a Greek
radio station's schedule, with nothing in the app saying so and no way to stop it. A blank
or rejected key falls back to the provider's id, so neither could. These tests pin:

  - CheckTests: find_shared_ids() flags an id only when the channels' names disagree with
    the id's own names AND with each other, on a real schedule - never by count.
  - IgnoreTests: stopping the id removes its listings from every source the account reads
    now, keeps the channel named for the id, survives the next import, hands the guide to a
    source the user keyed, ends when the provider changes the id, and is undone by a clear.
  - NameMatchTests: a channel whose id is ignored is still proposed name matches.
  - RouteTests: the review tab, the action endpoint, the account page line, the channel page.

No network - every feed is a local byte string (CLAUDE.md §Testing).
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.database import (Channel, ChannelEvent, EpgAlternateEntry, EpgChannelKey,  # noqa: E402
                          EpgSource, EpgSourceSubscription, EPGEntry,
                          CHANNEL_EPG_KEY_CHANGED, CHANNEL_EPG_SOURCE_CHANGED,
                          EPG_KEY_IGNORED, EPG_KEY_REJECTED)
from app.epg_sources import (ChannelForMatch, DirectoryEntry, KEY_DROPPED,  # noqa: E402
                             MATCH_NEW, accept_name_matches, find_shared_ids,
                             ignore_shared_id, name_core, name_match_review, propose_again,
                             reject_name_matches, rejected_name_matches, set_channel_key,
                             shared_ids)
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.seed import make_epg_source  # noqa: E402

CFG = {'sync': {'epg_collapse_threshold_percent': 20}}
RADIO = ['Morning Sport', 'Talk at Noon', 'Evening Results']
NEWS = ['Noticias', 'Debate', 'Ultima Hora']
BBC3 = ['Three News', 'Three Film', 'Three Comedy']
UNRELATED = ['UK: BBC 3 HD', 'UK: ITV LONDON 4K', 'UK: HGTV +1', 'UK: FOOD NETWORK']


def _xmltv(listings, channels=()):
    """listings: (xml_id, titles) one 30-minute program per title from an hour ahead;
    channels: (xml_id, [display names])."""
    now = datetime.utcnow()
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for xml_id, names in channels:
        parts.append(f'<channel id="{xml_id}">'
                     + ''.join(f'<display-name>{n}</display-name>' for n in names)
                     + '</channel>')
    for xml_id, titles in listings:
        for i, title in enumerate(titles):
            start = now + timedelta(minutes=60 + 30 * i)
            stop = start + timedelta(minutes=30)
            parts.append(f'<programme start="{start:%Y%m%d%H%M%S} +0000" '
                         f'stop="{stop:%Y%m%d%H%M%S} +0000" channel="{xml_id}">'
                         f'<title>{title}</title></programme>')
    parts.append('</tv>')
    return ''.join(parts).encode()


def _entry(xml_id, names, titles=20):
    return DirectoryEntry(xml_id, names, 40, titles, None, None, [])


def _chans(names, xml_id, account_id=1, start=1):
    return [ChannelForMatch(start + i, n, account_id, xml_id) for i, n in enumerate(names)]


def _find(channels, directory):
    return find_shared_ids(9, channels, {c.id: c.epg_channel_id for c in channels},
                           directory, False)


class CheckTests(unittest.TestCase):

    def test_name_core_keeps_which_channel_and_drops_how_it_is_delivered(self):
        self.assertEqual(name_core('UK: SKY SPORTS F1 ᴴᴰ ◉'), 'skysportsf1')
        self.assertEqual(name_core('VIP: SKY SPORTS F1 ⁴ᵏ'), 'skysportsf1')
        self.assertEqual(name_core('US| NFL NETWORK HD'), 'nflnetwork')
        self.assertEqual(name_core('Telefórmula'), 'teleformula')
        self.assertTrue(name_core('GR: ΣΚΑΪ HD'), 'a Greek name must not be emptied')

    def test_unrelated_channels_on_one_id_are_flagged_with_every_one_of_them(self):
        chans = _chans(UNRELATED + ['UK: BBC EARTH', 'UK: FILM 4'], 'TS')
        found = _find(chans, [_entry('TS', ['GR: SPORT FM TV 1 ᴿᴬᵂ ◉'])])
        self.assertEqual(len(found), 1)
        self.assertEqual({c.id for c in found[0].wrong}, {c.id for c in chans})

    def test_the_channel_the_id_is_named_for_is_left_out(self):
        chans = _chans(UNRELATED + ['GR: SPORT FM TV 1 HD'], 'TS')
        found = _find(chans, [_entry('TS', ['GR: SPORT FM TV 1'])])
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0].channels), 5)
        self.assertNotIn('GR: SPORT FM TV 1 HD', {c.name for c in found[0].wrong})

    def test_copies_of_one_channel_are_never_flagged_however_many(self):
        names = ['UK: SKY SPORTS F1 HD', 'VIP: SKY SPORTS F1 ⁴ᵏ', 'NOW: SKY SPORT F1 ᴿᴬᵂ',
                 'UK: SKY SPORTS F1 HEVC HD'] * 6
        self.assertEqual(_find(_chans(names, 'skysportsf1.uk'),
                               [_entry('skysportsf1.uk', ['UK: SKY SPORT F1'])]), [])

    def test_a_local_station_under_its_city_names_is_not_flagged(self):
        names = ['NBC 5 (KXAS) DALLAS (A)', 'NBC 5 (KXAS) FORT WORTH (S)',
                 'NBC (KXAS) THE COLONY (H)', 'DALLAS, TX KXAS NBC 5']
        self.assertEqual(_find(_chans(names, 'kxas.us'), [_entry('kxas.us', ['NBC 5'])]), [])

    def test_one_spelling_the_guide_does_not_use_is_not_flagged(self):
        # Every channel agrees with every other; the names differ only from the file's.
        names = ['Nat Geo Wild'] * 5 + ['National Geographic Wild']
        self.assertEqual(_find(_chans(names, 'natgeowild.us'),
                               [_entry('natgeowild.us', ['National Geographic Wild'])]), [])

    def test_an_id_listing_one_program_all_day_is_not_flagged(self):
        names = ['Arizona Cardinals', 'Atlanta Falcons', 'Baltimore Ravens', 'Buffalo Bills']
        self.assertEqual(_find(_chans(names, 'local.gr'), [_entry('local.gr', ['Local'], 1)]),
                         [])

    def test_each_account_is_judged_on_its_own_channels(self):
        chans = (_chans(UNRELATED, 'TS', account_id=1)
                 + _chans(['UK: BBC 3 HD'], 'TS', account_id=2, start=50))
        found = _find(chans, [_entry('TS', ['GR: SPORT FM TV 1'])])
        self.assertEqual([f.account_id for f in found], [1])

    def test_ids_differing_only_in_case_are_one_id(self):
        chans = _chans(UNRELATED[:2], 'TS') + _chans(UNRELATED[2:], 'ts', start=10)
        found = _find(chans, [_entry('TS', ['GR: SPORT FM TV 1'])])
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0].wrong), 4)


class _Fixture(unittest.TestCase):
    """One account reading two sources: its own (`src`, first) and an external one (`ext`).
    Both list the id `ts.test`, each as a different channel."""

    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account(name='Alpha')
        self.src = make_epg_source(self.acct, name='Provider')
        self.ext = EpgSource(kind='url', owner_account_id=self.acct.id, name='External',
                             url='http://external.test/epg.xml')
        db.session.add(self.ext)
        db.session.flush()
        db.session.add(EpgSourceSubscription(source_id=self.ext.id, account_id=self.acct.id,
                                             priority=2))
        db.session.commit()
        self.wrong = [self._channel(n, 'ts.test') for n in UNRELATED]
        self.owner = self._channel('GR: SPORT FM TV 1 HD', 'ts.test')
        self._import_both()

    def tearDown(self):
        self.t.cleanup()

    def _channel(self, name, epg_id):
        ch = seed.make_channel(self.acct, name=name, epg_channel_id=epg_id)
        db.session.commit()
        return ch

    def _import_both(self):
        import_source(db.session.get(EpgSource, self.src.id),
                      _xmltv([('ts.test', RADIO)],
                             channels=[('ts.test', ['GR: SPORT FM TV 1'])]),
                      epg_days=3, cfg=CFG)
        import_source(db.session.get(EpgSource, self.ext.id),
                      _xmltv([('ts.test', NEWS), ('bbc3.test', BBC3)],
                             channels=[('ts.test', ['PT - NewsNOW']),
                                       ('bbc3.test', ['UK: BBC 3 HD'])]),
                      epg_days=3, cfg=CFG)
        db.session.expire_all()

    def _titles(self, ch, model=EPGEntry):
        return {e.title for e in model.query.filter_by(channel_id=ch.id)}

    def _ids(self, chans):
        return [c.id for c in chans]

    def _ignore(self, ids=None):
        done = ignore_shared_id(self.src.id, self.acct.id, 'ts.test',
                                self._ids(self.wrong) if ids is None else ids, False)
        db.session.expire_all()
        return done


class IgnoreTests(_Fixture):

    def test_the_check_reads_it_from_the_database(self):
        found = shared_ids([self.src.id], [self.acct.id], False)
        self.assertEqual(len(found), 1)
        self.assertEqual(set(self._ids(found[0].wrong)), set(self._ids(self.wrong)))
        self.assertEqual(found[0].entry.display_names, ['GR: SPORT FM TV 1'])

    def test_stopping_it_removes_the_wrong_guide_from_every_source_now(self):
        self.assertEqual(self._titles(self.wrong[0]), set(RADIO))
        self.assertEqual(self._titles(self.wrong[0], EpgAlternateEntry), set(NEWS))

        done = self._ignore()

        self.assertEqual((done.applied, done.skipped), (4, 0))
        for ch in self.wrong:
            self.assertEqual(self._titles(ch), set(), ch.name)
            self.assertEqual(self._titles(ch, EpgAlternateEntry), set(), ch.name)
            self.assertIsNone(db.session.get(Channel, ch.id).epg_source_id)
            rows = EpgChannelKey.query.filter_by(channel_id=ch.id).all()
            self.assertEqual({(r.source_id, r.status, r.key) for r in rows},
                             {(self.src.id, EPG_KEY_IGNORED, 'ts.test'),
                              (self.ext.id, EPG_KEY_IGNORED, 'ts.test')})
            events = ChannelEvent.query.filter_by(
                channel_id=ch.id, event_type=CHANNEL_EPG_KEY_CHANGED).all()
            self.assertEqual(len(events), 2)
            self.assertEqual(json.loads(events[0].extra_data)['outcome'], KEY_DROPPED)
        self.assertEqual(self._titles(self.owner), set(RADIO), 'the id is its own')
        self.assertEqual(shared_ids([self.src.id], [self.acct.id], False), [])

    def test_no_channel_is_logged_moving_to_a_source_it_then_loses(self):
        # Ignored on its winner first, a channel was handed to the next source, and that
        # source was taken away a step later: the timeline named a move that never stood.
        self._ignore()
        self.assertEqual(ChannelEvent.query.filter(
            ChannelEvent.channel_id.in_(self._ids(self.wrong)),
            ChannelEvent.event_type == CHANNEL_EPG_SOURCE_CHANGED).count(), 0)

    def test_a_channel_on_the_id_through_its_own_key_is_skipped_and_counted(self):
        ch = self.wrong[0]
        set_channel_key(db.session.get(Channel, ch.id), db.session.get(EpgSource, self.src.id),
                        'ts.test', case_sensitive=False)
        db.session.commit()
        done = self._ignore()
        self.assertEqual((done.applied, done.skipped), (3, 1))
        self.assertEqual(EpgChannelKey.query.filter_by(channel_id=ch.id,
                                                       status=EPG_KEY_IGNORED).count(), 0)

    def test_the_next_import_does_not_bring_it_back(self):
        self._ignore()
        self._import_both()
        for ch in self.wrong:
            self.assertEqual(self._titles(ch) | self._titles(ch, EpgAlternateEntry), set())
        self.assertEqual(self._titles(self.owner), set(RADIO))

    def test_only_shown_channels_still_misled_are_touched(self):
        db.session.get(Channel, self.wrong[1].id).epg_channel_id = 'itv.test'
        db.session.commit()
        done = self._ignore([self.wrong[0].id, self.wrong[1].id, self.owner.id])
        self.assertEqual((done.applied, done.skipped), (1, 2))
        self.assertEqual(EpgChannelKey.query.filter(
            EpgChannelKey.channel_id.in_([self.wrong[1].id, self.owner.id,
                                          self.wrong[2].id])).count(), 0)

    def test_a_key_the_user_set_is_kept_and_takes_the_guide(self):
        bbc3 = self.wrong[0]
        set_channel_key(db.session.get(Channel, bbc3.id), db.session.get(EpgSource, self.ext.id),
                        'bbc3.test', case_sensitive=False)
        db.session.commit()
        self._import_both()   # the key waits for the source's next refresh
        self.assertEqual(self._titles(bbc3), set(RADIO))
        self.assertEqual(self._titles(bbc3, EpgAlternateEntry), set(BBC3))

        self._ignore()

        row = EpgChannelKey.query.filter_by(channel_id=bbc3.id, source_id=self.ext.id).one()
        self.assertEqual(row.key, 'bbc3.test')
        self.assertEqual(self._titles(bbc3), set(BBC3), 'the keyed source takes the guide now')
        self.assertEqual(db.session.get(Channel, bbc3.id).epg_source_id, self.ext.id)

    def test_it_ends_when_the_provider_changes_the_id(self):
        self._ignore()
        db.session.get(Channel, self.wrong[0].id).epg_channel_id = 'bbc3.test'
        db.session.commit()
        self._import_both()
        self.assertEqual(self._titles(self.wrong[0]), set(BBC3))

    def test_using_the_provider_id_again_brings_its_listings_back(self):
        self._ignore()
        ch = db.session.get(Channel, self.wrong[0].id)
        set_channel_key(ch, db.session.get(EpgSource, self.src.id), '', case_sensitive=False)
        db.session.commit()
        self.assertIsNone(EpgChannelKey.query.filter_by(channel_id=ch.id,
                                                        source_id=self.src.id).first())
        self._import_both()
        self.assertEqual(self._titles(self.wrong[0]), set(RADIO))

    def test_a_hidden_channel_is_skipped_by_the_check(self):
        for ch in self.wrong[1:]:
            db.session.get(Channel, ch.id).hidden = True
        db.session.commit()
        self.assertEqual(shared_ids([self.src.id], [self.acct.id], False), [])


class NameMatchTests(_Fixture):

    def _ext(self):
        return db.session.get(EpgSource, self.ext.id)

    def test_an_ignored_channel_is_still_proposed_its_right_id(self):
        self._ignore()
        props = {p.channel.id: p for p in name_match_review(self._ext(), False).proposals}
        self.assertIn(self.wrong[0].id, props)
        self.assertEqual(props[self.wrong[0].id].kind, MATCH_NEW)
        self.assertEqual(props[self.wrong[0].id].entry.xml_id, 'bbc3.test')

        accept_name_matches(self._ext(), [(self.wrong[0].id, 'bbc3.test')], False)
        db.session.expire_all()
        row = EpgChannelKey.query.filter_by(channel_id=self.wrong[0].id,
                                            source_id=self.ext.id).one()
        self.assertEqual(row.key, 'bbc3.test')

    def test_rejecting_keeps_the_ignore_and_propose_again_keeps_it_too(self):
        self._ignore()
        reject_name_matches(self._ext(), [(self.wrong[0].id, 'bbc3.test')], False)
        db.session.expire_all()
        row = EpgChannelKey.query.filter_by(channel_id=self.wrong[0].id,
                                            source_id=self.ext.id).one()
        self.assertEqual((row.status, row.key), (EPG_KEY_IGNORED, 'ts.test'))
        self.assertEqual([r['channel_id'] for r in rejected_name_matches(self.ext.id)],
                         [self.wrong[0].id])
        self.assertNotIn(self.wrong[0].id,
                         {p.channel.id for p in name_match_review(self._ext(), False).proposals})

        self.assertEqual(propose_again(self.ext.id, [self.wrong[0].id]), 1)
        db.session.expire_all()
        row = EpgChannelKey.query.filter_by(channel_id=self.wrong[0].id,
                                            source_id=self.ext.id).one()
        self.assertEqual((row.status, row.matched_on), (EPG_KEY_IGNORED, None))

    def test_clearing_an_ignore_that_carries_a_rejection_keeps_the_rejection(self):
        self._ignore()
        reject_name_matches(self._ext(), [(self.wrong[0].id, 'bbc3.test')], False)
        set_channel_key(db.session.get(Channel, self.wrong[0].id), self._ext(), '',
                        case_sensitive=False)
        db.session.commit()
        row = EpgChannelKey.query.filter_by(channel_id=self.wrong[0].id,
                                            source_id=self.ext.id).one()
        self.assertEqual((row.status, row.key), (EPG_KEY_REJECTED, None))


class RouteTests(_Fixture):

    def setUp(self):
        super().setUp()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()

    def _post(self, body):
        return self.client.post(f'/api/epg-sources/{self.src.id}/shared-ids/ignore',
                                json=body)

    def test_the_review_tab_lists_the_id_and_its_channels(self):
        r = self.client.get(f'/epg-sources/{self.src.id}/review?view=shared')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('data-act="ignore-shared"', html)
        self.assertIn('GR: SPORT FM TV 1', html)
        for name in UNRELATED:
            self.assertIn(name, html)
        self.assertIn('Left alone, named for this id: GR: SPORT FM TV 1 HD</div>', html)

    def test_a_channel_whose_name_says_nothing_is_not_called_named_for_the_id(self):
        self._channel('◉ ◉', 'ts.test')
        html = self.client.get(f'/epg-sources/{self.src.id}/review?view=shared').get_data(
            as_text=True)
        self.assertIn('Left alone, named for this id: GR: SPORT FM TV 1 HD</div>', html)

    def test_the_account_page_says_so_under_the_source(self):
        html = self.client.get(f'/accounts/{self.acct.id}').get_data(as_text=True)
        self.assertIn('4 channels with different names share the id', html)
        self.assertIn(f'/epg-sources/{self.src.id}/review?view=shared', html)

    def test_the_endpoint_stops_it_for_the_shown_channels(self):
        r = self._post({'account_id': self.acct.id, 'xml_id': 'ts.test',
                        'channel_ids': self._ids(self.wrong)})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()['applied'], 4)
        self.assertEqual(EpgChannelKey.query.filter_by(status=EPG_KEY_IGNORED).count(), 8)

    def test_the_endpoint_refuses_an_account_that_does_not_read_the_source(self):
        other = seed.make_account(name='Other')
        db.session.commit()
        r = self._post({'account_id': other.id, 'xml_id': 'ts.test',
                        'channel_ids': self._ids(self.wrong)})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(EpgChannelKey.query.count(), 0)

    def test_the_endpoint_refuses_nothing_selected(self):
        r = self._post({'account_id': self.acct.id, 'xml_id': 'ts.test', 'channel_ids': []})
        self.assertEqual(r.status_code, 400)

    def test_the_channel_page_offers_the_provider_id_back(self):
        self._post({'account_id': self.acct.id, 'xml_id': 'ts.test',
                    'channel_ids': self._ids(self.wrong)})
        html = self.client.get(f'/channels/{self.wrong[0].id}').get_data(as_text=True)
        self.assertIn('you stopped using the provider id <code>ts.test</code>', html)
        self.assertIn('data-ignored-id="ts.test"', html)
        self.assertNotIn('hidden channels do not have EPG imported', html,
                         'no sync fills in a guide whose id the user stopped using')


if __name__ == '__main__':
    unittest.main()
