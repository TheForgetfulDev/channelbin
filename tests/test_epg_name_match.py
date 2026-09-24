"""Tier 2 - matching a channel to guide data by its name, through review
(DESIGN-epg-sources.md §7.3 - §7.5, dev/changelog/1105).

A channel whose provider gives it no EPG id, or the wrong one, can often be found in an
XMLTV file by its display name. A wrong name match is a plausible-looking wrong guide, so a
name is only ever PROPOSED, from the stored source directory, and a person accepts it into
the channel's key for that one source. These tests pin:

  - ProposalTests: which names are proposed (one file channel, real schedule first, the
    separator-tolerant normalization) and which are not (ambiguous, already matched,
    already decided), and what a disagreement is.
  - DirectoryTests: the import keeps each file channel's next programs for the page, and
    writes the directory even when no channel has an id to match by.
  - DecisionTests: accept goes through set_channel_key() and the listings follow at the
    next refresh; reject is remembered and undoable; a decision the server does not
    currently propose is refused; nothing name-based happens inside an import.
  - PageTests: the review page, its three views and the Sources card link.

No network - every feed is a local byte string (CLAUDE.md §Testing).
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.database import (Channel, ChannelEvent, EpgChannelKey, EpgSourceChannel,  # noqa: E402
                          CHANNEL_EPG_KEY_CHANGED, EPG_KEY_ACCEPTED, EPG_KEY_ORIGIN_NAME_MATCH,
                          EPG_KEY_REJECTED)
from app.epg_sources import (MATCH_DISAGREE, MATCH_NEW, ChannelForMatch,  # noqa: E402
                             DirectoryEntry, accept_name_matches, name_match_proposals,
                             name_match_review, propose_again, reject_name_matches)
from tests.test_epg_sources import CFG, REAL, _Fixture, _schedule, _xmltv  # noqa: E402

OTHER = ['Other News', 'Other Film', 'Other Sport']


def _entry(xml_id, names, titles=5, count=10):
    return DirectoryEntry(xml_id, names, count, titles, 'Banner' if titles == 1 else None,
                          None, [])


def _ch(cid, name, epg_id=None):
    return ChannelForMatch(cid, name, 1, epg_id)


def _propose(directory, channels, decided=(), include_single=False):
    return name_match_proposals(directory, channels, set(decided), False, include_single)


class ProposalTests(unittest.TestCase):

    def test_an_unambiguous_name_is_proposed_with_the_files_spelling(self):
        r = _propose([_entry('ae.us', ['US: A&E HD'])], [_ch(1, 'US| A&E HD')])
        self.assertEqual(len(r.proposals), 1)
        p = r.proposals[0]
        self.assertEqual((p.kind, p.entry.xml_id, p.matched_on), (MATCH_NEW, 'ae.us', 'US: A&E HD'))

    def test_a_name_that_fits_several_file_channels_is_not_proposed(self):
        r = _propose([_entry('es1.de', ['Eurosport 1']), _entry('es1.fr', ['Eurosport 1'])],
                     [_ch(1, 'EUROSPORT 1')])
        self.assertEqual(r.proposals, [])
        self.assertEqual(r.ambiguous, 1)

    def test_letters_of_every_script_survive_normalization(self):
        # The loose rule turned this station into "FM" and matched it to a channel called FM.
        r = _propose([_entry('ert.gr', ['ΕΛΛΗΝΙΚΟΣ FM'])], [_ch(1, 'FM')])
        self.assertEqual(r.proposals, [])

    def test_one_title_all_day_is_offered_only_behind_the_toggle(self):
        d = [_entry('nfl01.us', ['NFL 01'], titles=1)]
        hidden = _propose(d, [_ch(1, 'NFL 01')])
        self.assertEqual((hidden.proposals, hidden.single_hidden), ([], 1))
        shown = _propose(d, [_ch(1, 'NFL 01')], include_single=True)
        self.assertEqual([p.entry.xml_id for p in shown.proposals], ['nfl01.us'])

    def test_a_real_schedule_is_proposed_over_a_single_title_row_of_the_same_name(self):
        d = [_entry('real.us', ['Sky One']), _entry('banner.us', ['Sky One'], titles=1)]
        r = _propose(d, [_ch(1, 'Sky One')], include_single=True)
        self.assertEqual([p.entry.xml_id for p in r.proposals], ['real.us'])
        self.assertEqual(r.ambiguous, 0)

    def test_a_channel_whose_id_already_matches_that_row_is_not_proposed(self):
        r = _propose([_entry('Sky.UK', ['Sky One'])], [_ch(1, 'Sky One', 'sky.uk')])
        self.assertEqual(r.proposals, [])

    def test_an_id_matching_another_row_is_a_disagreement_with_both_sides(self):
        d = [_entry('itv2.uk', ['ITV 2']), _entry('itv2plus1.uk', ['UK: ITV 2+1'])]
        r = _propose(d, [_ch(1, 'UK: ITV 2+1', 'itv2.uk')])
        p = r.proposals[0]
        self.assertEqual((p.kind, p.entry.xml_id, p.current.xml_id),
                         (MATCH_DISAGREE, 'itv2plus1.uk', 'itv2.uk'))

    def test_an_id_the_file_does_not_carry_is_a_new_proposal(self):
        r = _propose([_entry('bbc.uk', ['BBC One'])], [_ch(1, 'BBC One', 'dummy-123')])
        self.assertEqual([p.kind for p in r.proposals], [MATCH_NEW])

    def test_a_channel_a_person_already_answered_for_is_not_proposed(self):
        r = _propose([_entry('bbc.uk', ['BBC One'])], [_ch(1, 'BBC One')], decided={1})
        self.assertEqual(r.proposals, [])

    def test_a_row_with_no_listings_is_never_a_candidate(self):
        r = _propose([_entry('bbc.uk', ['BBC One'], count=0)], [_ch(1, 'BBC One')])
        self.assertEqual(r.proposals, [])


class DirectoryTests(_Fixture):

    def test_the_directory_keeps_the_next_programs_not_yet_over_soonest_first(self):
        self._channel('Alpha One', 'a1.test')
        listings = [('a1.test', -50, 30, 'Over'), ('a1.test', 240, 30, 'Fourth'),
                    ('a1.test', -10, 30, 'On now'), ('a1.test', 60, 30, 'Second'),
                    ('a1.test', 120, 30, 'Third')]
        import_source(self.src, _xmltv(listings), epg_days=3, cfg=CFG)
        row = EpgSourceChannel.query.filter_by(source_id=self.src.id, xml_id='a1.test').one()
        self.assertEqual([t for _at, t in json.loads(row.upcoming_titles)],
                         ['On now', 'Second', 'Third'])

    def test_the_directory_is_written_when_no_channel_has_an_id_to_match(self):
        # The account that most needs name matching - no ids at all - still gets proposals.
        self._channel('Beta Two', None)
        import_source(self.src, _xmltv(_schedule('b2.test', REAL), [('b2.test', ['Beta Two'])]),
                      epg_days=3, cfg=CFG)
        self.assertEqual([x.xml_id for x in EpgSourceChannel.query.filter_by(
            source_id=self.src.id)], ['b2.test'])
        review = name_match_review(self.src, False)
        self.assertEqual([p.entry.xml_id for p in review.proposals], ['b2.test'])


class DecisionTests(_Fixture):

    def setUp(self):
        super().setUp()
        self.carrier = self._channel('Alpha One', 'a1.test')   # keeps the import armed
        self.ch = self._channel('Beta Two', None)
        self.xml = _xmltv(_schedule('a1.test', OTHER) + _schedule('b2.test', REAL),
                          [('a1.test', ['Alpha One']), ('b2.test', ['Beta Two'])])
        import_source(self.src, self.xml, epg_days=3, cfg=CFG)

    def test_name_matching_never_runs_inside_an_import(self):
        self.assertEqual(self._active(self.ch), [])

    def test_accept_writes_the_key_and_the_listings_follow_at_the_next_refresh(self):
        done = accept_name_matches(self.src, [(self.ch.id, 'b2.test')], False)
        self.assertEqual((done.applied, done.stale, done.waiting), (1, 0, 1))
        key = EpgChannelKey.query.filter_by(channel_id=self.ch.id).one()
        self.assertEqual((key.key, key.origin, key.status, key.matched_on),
                         ('b2.test', EPG_KEY_ORIGIN_NAME_MATCH, EPG_KEY_ACCEPTED, 'Beta Two'))
        self.assertEqual(ChannelEvent.query.filter_by(
            channel_id=self.ch.id, event_type=CHANNEL_EPG_KEY_CHANGED).count(), 1)
        import_source(self.src, self.xml, epg_days=3, cfg=CFG)
        self.assertEqual({t for _s, t in self._active(self.ch)}, set(REAL))

    def test_a_pick_the_server_does_not_propose_is_refused(self):
        done = accept_name_matches(self.src, [(self.ch.id, 'a1.test')], False)
        self.assertEqual((done.applied, done.stale), (0, 1))
        done = reject_name_matches(self.src, [(self.carrier.id, 'a1.test')], False)
        self.assertEqual((done.applied, done.stale), (0, 1))
        self.assertEqual(EpgChannelKey.query.count(), 0)

    def test_a_rejection_is_remembered_changes_nothing_and_can_be_undone(self):
        done = reject_name_matches(self.src, [(self.ch.id, 'b2.test')], False)
        self.assertEqual(done.applied, 1)
        row = EpgChannelKey.query.filter_by(channel_id=self.ch.id).one()
        self.assertEqual((row.status, row.key, row.matched_on),
                         (EPG_KEY_REJECTED, None, 'Beta Two'))
        self.assertEqual(name_match_review(self.src, False).proposals, [])
        import_source(self.src, self.xml, epg_days=3, cfg=CFG)
        self.assertEqual(self._active(self.ch), [])

        self.assertEqual(propose_again(self.src.id, [self.ch.id]), 1)
        self.assertEqual([p.channel.id for p in name_match_review(self.src, False).proposals],
                         [self.ch.id])

    def test_hidden_channels_are_not_proposed(self):
        db.session.get(Channel, self.ch.id).hidden = True
        db.session.commit()
        self.assertEqual(name_match_review(self.src, False).proposals, [])


class PageTests(_Fixture):

    def setUp(self):
        super().setUp()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self._channel('Alpha One', 'a1.test')
        self.ch = self._channel('Beta Two', None)
        self.dis = self._channel('Gamma Three', 'a1.test')
        self.xml = _xmltv(_schedule('a1.test', OTHER) + _schedule('b2.test', REAL)
                          + _schedule('g3.test', REAL),
                          [('a1.test', ['Alpha One']), ('b2.test', ['Beta Two']),
                           ('g3.test', ['Gamma Three'])])

    def _page(self, query=''):
        res = self.client.get(f'/epg-sources/{self.src.id}/review{query}')
        self.assertEqual(res.status_code, 200)
        return res.get_data(as_text=True)

    def test_an_unrefreshed_source_says_so(self):
        self.assertIn('has no successful refresh yet', self._page())

    def test_the_three_views(self):
        import_source(self.src, self.xml, epg_days=3, cfg=CFG)
        page = self._page()
        self.assertIn('Beta Two', page)
        self.assertIn('b2.test', page)
        self.assertIn('Matched on', page)
        self.assertNotIn('Gamma Three', page)
        dis = self._page('?view=disagreements')
        self.assertIn('Gamma Three', dis)
        self.assertIn('g3.test', dis)
        reject_name_matches(self.src, [(self.ch.id, 'b2.test')], False)
        self.assertIn('Beta Two', self._page('?view=rejected'))
        self.assertNotIn('Beta Two', self._page())

    def test_accept_endpoint_says_when_the_listings_arrive(self):
        import_source(self.src, self.xml, epg_days=3, cfg=CFG)
        res = self.client.post(f'/api/epg-sources/{self.src.id}/name-matches/accept',
                               json={'picks': [{'channel_id': self.ch.id, 'xml_id': 'b2.test'}]})
        data = res.get_json()
        self.assertEqual(res.status_code, 200, data)
        self.assertEqual(data['accepted'], 1)
        self.assertIn('next refresh', data['message'])
        res = self.client.post(f'/api/epg-sources/{self.src.id}/name-matches/accept',
                               json={'picks': []})
        self.assertEqual(res.status_code, 400)

    def test_the_sources_card_links_to_the_review(self):
        res = self.client.get(f'/accounts/{self.acct.id}')
        self.assertIn(f'/epg-sources/{self.src.id}/review', res.get_data(as_text=True))

    def test_an_unknown_source_is_404(self):
        self.assertEqual(self.client.get('/epg-sources/9999/review').status_code, 404)


if __name__ == '__main__':
    unittest.main()
