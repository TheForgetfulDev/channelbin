"""Tier 2 - source priority and the per-channel source override (DESIGN-epg-sources.md §5,
§6.2, §9.3; dev/changelog/1107).

  - MoveTests: Move up / Move down renumbers the account's priority order and re-decides
    every channel's guide from rows already held - a move between the two tables, never a
    fetch. A real schedule still beats one program all day whatever the order (§5.3), and a
    channel whose new winner holds none of its rows yet is left where it is.
  - OverrideTests: Use this source / Clear override through the one writer, its event, the
    row moves, an override with no listings kept but not in force (§5.1 step 1), and an
    override on a source the account stopped reading never winning.
  - PageTests: both pages render the controls and the fall-through line.

No network: every feed is a local byte string (CLAUDE.md §Testing).
"""
import json
import os
import sys
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.database import (Channel, ChannelEvent, EpgAlternateEntry, EpgSource,  # noqa: E402
                          EpgSourceChannel,
                          EpgSourceSubscription, CHANNEL_EPG_SOURCE_CHANGED,
                          CHANNEL_EPG_SOURCE_OVERRIDE_CHANGED, EPG_STATUS_FAILED)
from app.epg_sources import (OVERRIDE_NO_LISTINGS, OVERRIDE_NOT_READ, OVERRIDE_WAITING,  # noqa: E402
                             channel_guide_view, stop_using_source)
from tests.test_epg_sources import BANNER, CFG, REAL, _Fixture, _schedule, _xmltv  # noqa: E402

OTHER = ['Other News', 'Other Film', 'Other Sport']


class _TwoSources(_Fixture):
    """Alpha reads self.src first and self.ext second. `both` has a real schedule in each;
    `ext_only` is in ext alone; `banner` lists one program in src and a schedule in ext;
    `src_only` is in src alone."""

    def setUp(self):
        super().setUp()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.ext = self._second_source()
        self.both = self._channel('Both', 'both.test')
        self.ext_only = self._channel('Ext Only', 'ext.test')
        self.banner = self._channel('Banner', 'banner.test')
        self.src_only = self._channel('Src Only', 'src.test')
        import_source(self.src, _xmltv(_schedule('both.test', REAL)
                                       + _schedule('banner.test', BANNER)
                                       + _schedule('src.test', REAL)), epg_days=3, cfg=CFG)
        import_source(self.ext, _xmltv(_schedule('both.test', OTHER)
                                       + _schedule('ext.test', OTHER)
                                       + _schedule('banner.test', OTHER)), epg_days=3, cfg=CFG)

    def _order(self):
        db.session.expire_all()
        return [(s.source_id, s.priority) for s in EpgSourceSubscription.query.filter_by(
            account_id=self.acct.id).order_by(EpgSourceSubscription.priority)]

    def _move(self, source_id, direction):
        with mock.patch('app.accounts.requests.get') as fetch:
            r = self.client.post(f'/api/accounts/{self.acct.id}/epg-sources/{source_id}/move',
                                 json={'direction': direction})
        fetch.assert_not_called()
        return r

    def _override(self, ch, source_id):
        with mock.patch('app.accounts.requests.get') as fetch:
            r = self.client.post(f'/api/channels/{ch.id}/epg-source-override',
                                 json={'source_id': source_id})
        fetch.assert_not_called()
        return r


class MoveTests(_TwoSources):

    def test_the_setup_starts_where_the_order_says(self):
        self.assertEqual(self._winner(self.both), self.src.id)
        self.assertEqual(self._winner(self.banner), self.ext.id)

    def test_moving_a_source_down_hands_its_channels_to_the_new_first(self):
        r = self._move(self.src.id, 'down')
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self._order(), [(self.ext.id, 1), (self.src.id, 2)])
        self.assertEqual(self._winner(self.both), self.ext.id)
        self.assertEqual({t for _s, t in self._active(self.both)}, set(OTHER))
        self.assertEqual({t for s, t in self._alternates(self.both) if s == self.src.id},
                         set(REAL), 'the loser is kept aside, not deleted')
        ev = ChannelEvent.query.filter_by(channel_id=self.both.id,
                                          event_type=CHANNEL_EPG_SOURCE_CHANGED).one()
        self.assertIn(self.ext.name, ev.detail)
        self.assertEqual(r.get_json()['switched'], 1)
        self.assertIn('1 channel now takes its guide from a different source', r.get_json()['message'])

    def test_a_real_schedule_still_beats_one_program_whatever_the_order(self):
        self._move(self.ext.id, 'down')     # already last: nothing changes
        self._move(self.src.id, 'down')
        self._move(self.src.id, 'up')
        self.assertEqual(self._winner(self.banner), self.ext.id)
        self.assertEqual(self._winner(self.src_only), self.src.id)
        self.assertEqual(self._winner(self.both), self.src.id, 'moving back undoes it')
        self.assertEqual({t for _s, t in self._active(self.both)}, set(REAL))

    def test_an_edge_move_changes_nothing_and_says_so(self):
        r = self._move(self.src.id, 'up')
        self.assertEqual(r.status_code, 200)
        self.assertIn('already first', r.get_json()['message'])
        self.assertEqual(self._order(), [(self.src.id, 1), (self.ext.id, 2)])

    def test_gaps_in_the_numbering_are_closed(self):
        for sid, p in ((self.ext.id, 9), (self.src.id, 4)):
            EpgSourceSubscription.query.filter_by(account_id=self.acct.id,
                                                  source_id=sid).one().priority = p
        db.session.commit()
        self._move(self.ext.id, 'up')
        self.assertEqual(self._order(), [(self.ext.id, 1), (self.src.id, 2)])

    def test_bad_requests_are_refused(self):
        other = EpgSource(kind='url', owner_account_id=self.acct.id, name='Unread',
                          url='http://unread.test/x.xml')
        db.session.add(other)
        db.session.commit()
        self.assertEqual(self._move(other.id, 'up').status_code, 400)
        self.assertEqual(self._move(self.src.id, 'sideways').status_code, 400)
        db.session.get(EpgSource, self.ext.id).refresh_started_at = datetime.utcnow()
        db.session.commit()
        self.assertEqual(self._move(self.src.id, 'down').status_code, 409)
        self.assertEqual(self._order(), [(self.src.id, 1), (self.ext.id, 2)])

    def test_a_channel_whose_new_winner_holds_nothing_yet_is_left_alone(self):
        """dev/docs/BUGS.md 2026-09-23 @ 01:50:53 PM ET. ext's file lists `w.test` but no
        channel carried it at ext's import, so the directory covers it and no row was kept.
        Moving ext first must not point the channel at a source holding nothing for it."""
        import_source(self.ext, _xmltv(_schedule('both.test', OTHER)
                                       + _schedule('ext.test', OTHER)
                                       + _schedule('banner.test', OTHER)
                                       + _schedule('w.test', OTHER)), epg_days=3, cfg=CFG)
        w = self._channel('Waiting', 'w.test')
        import_source(db.session.get(EpgSource, self.src.id), _xmltv(
            _schedule('both.test', REAL) + _schedule('banner.test', BANNER)
            + _schedule('src.test', REAL) + _schedule('w.test', REAL)), epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(w), self.src.id)
        self._move(self.ext.id, 'up')
        self.assertEqual(self._winner(w), self.src.id)
        self.assertEqual({t for _s, t in self._active(w)}, set(REAL), 'the guide is not emptied')
        self.assertEqual(self._winner(self.both), self.ext.id, 'the rest still moved')

    def _forget_src_directory(self, status=None):
        """The live shape after m072: a provider source whose rows are held but which has no
        directory until its first successful refresh - never, with syncing switched off or a
        feed that answers 404 (`status`)."""
        src = db.session.get(EpgSource, self.src.id)
        src.last_status = status
        EpgSourceChannel.query.filter_by(source_id=self.src.id).delete()
        db.session.commit()

    def test_a_source_with_no_directory_yet_covers_what_it_holds(self):
        """dev/docs/BUGS.md 2026-09-23 @ 01:50:52 PM ET. Its empty directory read as "covers
        nothing": a move took src_only's guide away and handed `both` to ext regardless of
        the order."""
        self._forget_src_directory()
        self._move(self.ext.id, 'down')     # an edge move: no change, but nothing lost
        self._move(self.src.id, 'down')
        self.assertEqual(self._winner(self.src_only), self.src.id)
        self.assertEqual({t for _s, t in self._active(self.src_only)}, set(REAL))
        self.assertEqual(self._winner(self.both), self.ext.id, 'the order still decides')
        self._move(self.src.id, 'up')
        self.assertEqual(self._winner(self.both), self.src.id)
        self.assertEqual({t for _s, t in self._active(self.both)}, set(REAL))

    def test_another_sources_import_leaves_a_source_with_no_directory_alone(self):
        """dev/docs/BUGS.md 2026-09-23 @ 01:50:52 PM ET - the same misreading inside
        import_source, reached by refreshing any second source an account reads."""
        self._forget_src_directory()
        import_source(db.session.get(EpgSource, self.ext.id), _xmltv(
            _schedule('both.test', OTHER) + _schedule('ext.test', OTHER)
            + _schedule('banner.test', OTHER)), epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(self.src_only), self.src.id)
        self.assertEqual({t for _s, t in self._active(self.src_only)}, set(REAL))
        self.assertEqual(self._winner(self.both), self.src.id)

    def test_a_source_whose_refreshes_all_failed_still_covers_what_it_holds(self):
        """dev/docs/BUGS.md 2026-09-23 @ 02:31:03 PM ET. A failed refresh gives a source a
        status but no directory; asked of the status, it covered nothing, and another
        source's import took its channels - 616 on the live account 4."""
        self._forget_src_directory(EPG_STATUS_FAILED)
        import_source(db.session.get(EpgSource, self.ext.id), _xmltv(
            _schedule('both.test', OTHER) + _schedule('ext.test', OTHER)
            + _schedule('banner.test', OTHER)), epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(self.both), self.src.id)
        self.assertEqual({t for _s, t in self._active(self.both)}, set(REAL))
        self._move(self.src.id, 'down')
        self._move(self.src.id, 'up')
        self.assertEqual(self._winner(self.src_only), self.src.id)
        self.assertEqual(self._winner(self.both), self.src.id)

    def test_the_channel_page_counts_what_a_failed_source_holds(self):
        """dev/docs/BUGS.md 2026-09-23 @ 02:31:03 PM ET - the channel page read the same
        status and showed the held listings as none."""
        self._forget_src_directory(EPG_STATUS_FAILED)
        row = next(r for r in channel_guide_view(db.session.get(Channel, self.both.id),
                                                 False)['rows'] if r['id'] == self.src.id)
        self.assertTrue(row['covers'])
        self.assertFalse(row['refreshed'])
        self.assertEqual(row['entry_count'], len(self._active(self.both)))
        html = self.client.get(f'/channels/{self.both.id}').get_data(as_text=True)
        self.assertIn('this source has no successful refresh yet', html)


class OverrideTests(_TwoSources):

    def _events(self, ch):
        return ChannelEvent.query.filter_by(channel_id=ch.id,
                                            event_type=CHANNEL_EPG_SOURCE_OVERRIDE_CHANGED).all()

    def test_use_this_source_moves_the_guide_and_logs_it(self):
        r = self._override(self.both, self.ext.id)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()['changed'])
        db.session.expire_all()
        self.assertEqual(db.session.get(Channel, self.both.id).epg_source_override_id, self.ext.id)
        self.assertEqual(self._winner(self.both), self.ext.id)
        self.assertEqual({t for _s, t in self._active(self.both)}, set(OTHER))
        self.assertEqual({t for s, t in self._alternates(self.both) if s == self.src.id}, set(REAL))
        ev = self._events(self.both)
        self.assertEqual(len(ev), 1)
        self.assertEqual(json.loads(ev[0].extra_data), {'source_id': self.ext.id, 'previous': None})
        self.assertIn(f'guide now from {self.ext.name}', ev[0].detail)
        moved = ChannelEvent.query.filter_by(channel_id=self.both.id,
                                             event_type=CHANNEL_EPG_SOURCE_CHANGED).one()
        self.assertIn('override', moved.detail)

    def test_clear_override_goes_back_to_the_priority_order(self):
        self._override(self.both, self.ext.id)
        r = self._override(self.both, None)
        self.assertTrue(r.get_json()['changed'])
        self.assertEqual(self._winner(self.both), self.src.id)
        self.assertEqual({t for _s, t in self._active(self.both)}, set(REAL))
        self.assertIsNone(db.session.get(Channel, self.both.id).epg_source_override_id)
        self.assertEqual(len(self._events(self.both)), 2)

    def test_the_same_answer_twice_changes_nothing(self):
        self._override(self.both, self.ext.id)
        r = self._override(self.both, self.ext.id)
        self.assertFalse(r.get_json()['changed'])
        self.assertEqual(len(self._events(self.both)), 1)

    def test_it_wins_over_a_real_schedule_and_survives_a_reorder_and_an_import(self):
        self._override(self.banner, self.src.id)
        self.assertEqual(self._winner(self.banner), self.src.id,
                         'the one event channel the §5.3 rule gets wrong')
        self._move(self.src.id, 'down')
        import_source(db.session.get(EpgSource, self.ext.id), _xmltv(
            _schedule('both.test', OTHER) + _schedule('ext.test', OTHER)
            + _schedule('banner.test', OTHER)), epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(self.banner), self.src.id)
        self.assertEqual({t for _s, t in self._active(self.banner)}, set(BANNER))

    def test_an_override_with_no_listings_is_kept_and_says_why(self):
        r = self._override(self.src_only, self.ext.id)
        self.assertEqual(r.get_json()['state'], OVERRIDE_NO_LISTINGS)
        self.assertIn('has no listings for this channel right now', r.get_json()['message'])
        self.assertEqual(self._winner(self.src_only), self.src.id)
        self.assertEqual({t for _s, t in self._active(self.src_only)}, set(REAL))
        import_source(db.session.get(EpgSource, self.ext.id), _xmltv(
            _schedule('both.test', OTHER) + _schedule('ext.test', OTHER)
            + _schedule('banner.test', OTHER) + _schedule('src.test', OTHER)),
            epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(self.src_only), self.ext.id, 'it takes over when it can')

    def test_an_override_whose_rows_have_not_arrived_waits_for_them(self):
        import_source(self.ext, _xmltv(_schedule('both.test', OTHER)
                                       + _schedule('ext.test', OTHER)
                                       + _schedule('banner.test', OTHER)
                                       + _schedule('src.test', OTHER)), epg_days=3, cfg=CFG)
        EpgAlternateEntry.query.filter_by(channel_id=self.src_only.id,
                                          source_id=self.ext.id).delete()
        db.session.commit()
        r = self._override(self.src_only, self.ext.id)
        self.assertEqual(r.get_json()['state'], OVERRIDE_WAITING)
        self.assertIn('arrive at its next refresh', r.get_json()['message'])
        self.assertEqual(self._winner(self.src_only), self.src.id, 'not pointed at nothing')
        self.assertEqual({t for _s, t in self._active(self.src_only)}, set(REAL))

    def test_an_override_on_a_source_the_account_stopped_reading_never_wins(self):
        """dev/docs/BUGS.md 2026-09-23 @ 01:50:53 PM ET. Stop using deletes the source's rows
        for the account's channels but not its directory, so resolution read it as still
        covering the overridden channel."""
        self._override(self.both, self.ext.id)
        stop_using_source(self.acct.id, self.ext.id, case_sensitive=False)
        self.assertEqual(self._winner(self.both), self.src.id)
        self.assertEqual({t for _s, t in self._active(self.both)}, set(REAL))
        ch = db.session.get(Channel, self.both.id)
        self.assertEqual(ch.epg_source_override_id, self.ext.id, 'the answer is kept')
        view = channel_guide_view(ch, case_sensitive=False)
        self.assertEqual(view['override']['state'], OVERRIDE_NOT_READ)
        self.assertEqual(view['active'].id, self.src.id)

    def test_clearing_returns_to_a_source_with_no_directory_yet(self):
        MoveTests._forget_src_directory(self)
        self._override(self.both, self.ext.id)
        self._override(self.both, None)
        self.assertEqual(self._winner(self.both), self.src.id)
        self.assertEqual({t for _s, t in self._active(self.both)}, set(REAL))

    def test_only_a_source_the_account_reads_can_be_chosen(self):
        other = EpgSource(kind='url', owner_account_id=self.acct.id, name='Unread',
                          url='http://unread.test/x.xml')
        db.session.add(other)
        db.session.commit()
        self.assertEqual(self._override(self.both, other.id).status_code, 400)
        self.assertEqual(self._override(self.both, 'x').status_code, 400)
        self.assertEqual(self._events(self.both), [])

    def test_refused_while_a_source_is_refreshing(self):
        db.session.get(EpgSource, self.src.id).refresh_started_at = datetime.utcnow()
        db.session.commit()
        self.assertEqual(self._override(self.both, self.ext.id).status_code, 409)
        self.assertIsNone(db.session.get(Channel, self.both.id).epg_source_override_id)


class PageTests(_TwoSources):

    def test_the_account_page_offers_the_moves(self):
        html = self.client.get(f'/accounts/{self.acct.id}').get_data(as_text=True)
        self.assertIn('data-act="source-move" data-direction="up"', html)
        self.assertIn('data-act="source-move" data-direction="down"', html)
        self.assertIn('gives way to one with a schedule', html)

    def test_the_channel_page_offers_the_override_and_the_fall_through(self):
        html = self.client.get(f'/channels/{self.both.id}').get_data(as_text=True)
        self.assertIn('Use this source', html)
        self.assertNotIn('Clear override', html)
        self._override(self.both, self.ext.id)
        stop_using_source(self.acct.id, self.ext.id, case_sensitive=False)
        html = self.client.get(f'/channels/{self.both.id}').get_data(as_text=True)
        self.assertIn('Clear override', html)
        self.assertIn('which this account no longer reads', html)
