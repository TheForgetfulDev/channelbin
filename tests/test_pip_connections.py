"""How a picture-in-picture play uses provider connections beside a recording
(dev/docs/DESIGN-pip-channels.md 8.2, 9, 12): the small picture gives way first in the three
places a recording takes a viewer's connection, a recording that needs one connection cuts one
viewer, the big picture takes its own small picture's connection when it has no other, a Swap
on a watched channel keeps both connections, and Recent plays says when a small picture
stopped and came back.

The combiner and the GPU are stood in for (tests/support/pip_engine.py): the suite has no GPU.
One defect is guarded here, dev/docs/BUGS.md 2026-11-23: a swapped copy whose big picture was
"the same channel as the other" ended instead of showing a card.

  python3 -m unittest tests.test_pip_connections
"""
import os
import sys
import threading
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.feed_plays import wait_for  # noqa: E402
from tests.support.seed import make_account  # noqa: E402
from tests.support.pip_engine import PipPlayCase as _Engine, src  # noqa: E402

import app.connection_limits as connlim  # noqa: E402
import app.output_feed as feed  # noqa: E402
import app.pip_channels as pip  # noqa: E402
import app.pip_engine as engine  # noqa: E402
from app import db, recorder  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (FEED_PICTURE_BACK, FEED_PICTURE_STOPPED, FEED_PLAY_PICTURE, PIP_SOURCE_CHANNEL, PIP_SOURCE_SEARCH, Alert,  # noqa: E402
                          OutputFeedPlay)
from app.output_feed import feed_id_for_pip  # noqa: E402

TWO_TVS = {'integrations': {'output_feed': {'enabled': True, 'username': 'tv', 'password': 'tv-secret',
                                            'reserve_seconds': 0, 'max_viewers': 3}}}


class _Connections(_Engine):
    def record(self, account, recording_id=77) -> bool:
        """A recording asks for a connection on `account` as the recorder does: its own
        acquire, the viewers cut one at a time, the plays it cut moved or ended."""
        got = recorder._try_acquire_slot_with_preemption(self.t.app, recording_id, account.id)
        if got:
            self.addCleanup(connlim.release, account.id, 'recording', recording_id)
        return got


class GiveWayOrderTests(_Connections):
    FEED = TWO_TVS

    def test_the_small_picture_is_the_one_that_gives_way(self):
        _resp, _s = self.combined()
        big, small = self.pictures()
        self.assertEqual(engine.give_way(), {big.session.id: connlim.VIEWER_WATCHED,
                                             small.session.id: connlim.VIEWER_SMALL_PICTURE})
        self.assertEqual(
            connlim.seat_standing({self.account.id, self.small.account.id}, 'recording'),
            {self.account.id: connlim.SEAT_BY_CUTTING_VIEWER,
             self.small.account.id: connlim.SEAT_BY_PAUSING_PICTURE})

    def test_a_picture_that_is_big_for_any_tv_is_not_small(self):
        self.combined()
        self.combined(feed_id_for_pip(self.row.id, True))
        big, small = self.pictures()
        self.assertEqual(set(engine.give_way().values()), {connlim.VIEWER_WATCHED})
        self.assertEqual(
            set(connlim.seat_standing({self.account.id, self.small.account.id}, 'recording').values()),
            {connlim.SEAT_BY_CUTTING_VIEWER})

    def test_pictures_no_tv_is_on_go_before_either(self):
        resp, s = self.combined()
        big, small = self.pictures()
        resp.close()
        self.assertTrue(wait_for(lambda: not s.live, 10))
        self.assertEqual(engine.give_way(), {big.session.id: connlim.VIEWER_UNWATCHED,
                                             small.session.id: connlim.VIEWER_UNWATCHED})

    def test_a_recording_that_can_start_on_either_account_starts_on_the_small_pictures(self):
        self.combined()
        members = [self.big, self.small]
        full, viewer = recorder._busy_account_channel_ids(members, 77)
        self.assertEqual((full, set(viewer), viewer.watched), (set(), {self.big.id, self.small.id}, {self.big.id}))
        # The big picture's channel scores higher, and is still passed over.
        self.big.health_score, self.small.health_score = 95, 60
        db.session.commit()
        best = recorder._pick_by_tiers(members, {}, frozenset(), set(), full, frozenset(), 3,
                                       viewer_account=viewer)
        self.assertEqual(best.id, self.small.id)
        note = recorder._viewer_account_note(best, True, [], viewer)
        self.assertIn('picture-in-picture', note)
        self.assertIn('keeps its big picture', note)

    def test_a_plain_set_of_viewer_accounts_counts_as_all_watched(self):
        self.assertEqual(recorder._watched({1, 2}), frozenset({1, 2}))
        self.assertEqual(recorder._watched(recorder.ViewerAccounts({1, 2}, {2})), frozenset({2}))


class OneViewerAtATimeTests(_Connections):
    FEED = TWO_TVS

    def setUp(self):
        super().setUp()
        self.account.max_connections = 2
        db.session.commit()

    def make(self, **over):
        # Both pictures on the one account, which has two connections.
        self.small = self.plain_channel('Small one', self.account)
        return super().make(**over)

    def test_a_recording_takes_the_small_pictures_connection_and_leaves_the_big_one(self):
        _resp, s = self.combined()
        big, small = self.pictures()
        kept = big.session
        self.assertEqual(len(self.seats(self.account)), 2)
        self.assertTrue(self.record(self.account))
        self.assertTrue(wait_for(lambda: small.pane.state == pip.PANE_TAKEN, 10), small.pane)
        self.assertIs(big.session, kept)
        self.assertTrue(kept.live and s.live, 'the big picture was not touched')
        self.assertEqual(sorted(self.seats(self.account)), [('recording', 77), ('viewer', kept.id)])
        self.assertEqual(Alert.query.filter_by(alert_type='VIEWER_PREEMPTED').count(), 0)

    def test_two_plain_plays_lose_one_of_them_not_both(self):
        other = self.plain_channel('Other one', self.account)
        for ch in (self.big, other):
            self.read(self.play(feed.feed_id_for_channel(ch.id)))
        self.assertEqual(len(feed.live_sessions()), 2)
        self.assertTrue(self.record(self.account))
        self.assertTrue(wait_for(lambda: len(feed.live_sessions()) == 1, 10), feed.live_sessions())
        left = self.only_session()
        self.assertEqual(sorted(self.seats(self.account)), [('recording', 77), ('viewer', left.id)])

    def test_a_second_recording_takes_the_other_one(self):
        _resp, s = self.combined()
        big, small = self.pictures()
        self.assertTrue(self.record(self.account, 77))
        self.assertTrue(self.record(self.account, 78))
        self.assertEqual(sorted(self.seats(self.account)), [('recording', 77), ('recording', 78)])
        self.assertTrue(wait_for(lambda: big.pane.state == pip.PANE_TAKEN, 10), big.pane)


class StripOrderTests(_Connections):
    """connection_limits._strip_kind's order, with no play at all."""

    def setUp(self):
        super().setUp()
        self.account.max_connections = 3
        db.session.commit()
        for holder in ('small', 'watched', 'unwatched'):
            self.assertTrue(connlim.try_acquire(self.account.id, 'viewer', holder))
            self.addCleanup(connlim.release, self.account.id, 'viewer', holder)
        self.ranks = {'small': connlim.VIEWER_SMALL_PICTURE, 'unwatched': connlim.VIEWER_UNWATCHED}

    def strip(self, **kw):
        return [h for h, _ in connlim._strip_kind(self.account.id, 'viewer', give_way=self.ranks, **kw)]

    def test_the_readiest_to_give_way_goes_first(self):
        self.assertEqual(self.strip(at_most=1), ['unwatched'])
        self.assertEqual(self.strip(at_most=1), ['small'])
        self.assertEqual(self.strip(at_most=1), ['watched'])
        self.assertEqual(self.strip(at_most=1), [])

    def test_a_held_seat_is_kept_for_the_watched_play(self):
        self.assertEqual(self.strip(keep={self.account.id: 1}), ['unwatched', 'small'])

    def test_with_no_bound_every_one_in_the_way_goes(self):
        self.assertEqual(self.strip(), ['unwatched', 'small', 'watched'])


class BigTakesSmallSeatTests(_Connections):
    """The big picture can play from the account its small picture is on, and from one more."""

    def make(self, **over):
        self.shared = make_account('Shared acct', max_connections=1)
        db.session.commit()
        self.big = self.plain_channel('Game one', self.account)
        self.second = self.plain_channel('Game two', self.shared)
        self.small = self.plain_channel('Small one', self.shared)
        over.setdefault('main', src(PIP_SOURCE_SEARCH, query='Game'))
        return super().make(**over)

    def tuned(self, feed_id=None):
        resp, s = self.combined(feed_id)
        big, small = self.pictures()
        self.assertEqual((big.session.channel_id, small.session.channel_id), (self.big.id, self.small.id))
        return s, big, small

    def test_with_nothing_else_free_the_big_picture_takes_its_small_pictures_connection(self):
        s, big, small = self.tuned()
        self.assertTrue(self.record(self.account))
        self.assertTrue(wait_for(lambda: big.live and big.session.channel_id == self.second.id, 15),
                        (big.pane, small.pane))
        self.assertTrue(wait_for(lambda: small.pane.state == pip.PANE_YIELDED, 10), small.pane)
        self.assertEqual(small.words[0], 'Paused: the big picture needed its connection')
        self.assertEqual(self.seats(self.shared), [('viewer', big.session.id)])
        self.assertTrue(s.live)
        self.assertEqual(Alert.query.filter_by(alert_type='VIEWER_PREEMPTED').count(), 0)
        # It stays said while the big picture is on that connection, however often it asks.
        feed.recheck_waiting()
        engine.ask_all()
        self.assertFalse(wait_for(lambda: small.pane.state != pip.PANE_YIELDED, 2), small.pane)
        self.assertIn('the big picture needed its connection', engine.views(s)['small']['text'])

    def test_with_a_connection_free_again_the_big_picture_moves_to_it_and_the_small_one_is_back(self):
        s, big, small = self.tuned()
        self.assertTrue(self.record(self.account))
        self.assertTrue(wait_for(lambda: small.pane.state == pip.PANE_YIELDED and big.live, 15), small.pane)
        connlim.release(self.account.id, 'recording', 77)
        feed.recheck_waiting()
        self.assertTrue(wait_for(lambda: small.live and big.live and big.session.channel_id == self.big.id, 20),
                        (big.pane, small.pane))
        self.assertEqual(small.session.channel_id, self.small.id)
        self.assertEqual(self.seats(self.account), [('viewer', big.session.id)])
        self.assertEqual(self.seats(self.shared), [('viewer', small.session.id)])
        self.assertTrue(s.live)
        # Not again for a while, so the two cannot pass one connection back and forth.
        self.assertIsNotNone(small.last_back_mono)
        with feed._lock:
            small.yielded = True
        self.assertFalse(small._take_seat_back(load_config()))
        with feed._lock:
            small.yielded = False

    def test_a_small_picture_that_did_not_give_its_connection_up_moves_nobody(self):
        s, big, small = self.tuned()
        self.assertFalse(small._take_seat_back(load_config()))
        self.assertFalse(big._take_seat_back(load_config()))
        self.assertTrue(big.live and small.live)

    def test_it_does_not_take_it_twice_in_a_row(self):
        s, big, small = self.tuned()
        big.last_yield_mono = engine.time.monotonic()
        self.assertTrue(self.record(self.account))
        self.assertTrue(wait_for(lambda: big.pane.state == pip.PANE_TAKEN, 10), big.pane)
        self.assertFalse(wait_for(lambda: not small.live, 2), 'the small picture kept playing')
        self.assertEqual(big.words[0], 'Paused for a recording')

    def test_a_small_picture_another_tv_has_as_its_big_one_keeps_its_connection(self):
        s, big, small = self.tuned()
        with feed._lock:
            big.pair.plays['another tv'] = True      # a play of the swapped copy
        try:
            self.assertFalse(big._take_small_seat(load_config()))
        finally:
            with feed._lock:
                big.pair.plays.pop('another tv', None)
        self.assertTrue(small.live)

    def test_a_small_picture_never_takes_the_big_ones(self):
        s, big, small = self.tuned()
        self.assertFalse(small._take_small_seat(load_config()))
        self.assertTrue(big.live)


class SwapKeepsConnectionsTests(_Connections):
    def swapped(self, how):
        _resp, s = self.combined()
        big, small = self.pictures()
        sessions = (big.session, small.session)
        self.assertTrue(wait_for(lambda: s.combined.had_frame, 15))
        first = s.proc
        how()
        self.assertEqual(feed.refresh_pip(self.row.id), (0, 0, 1))
        self.assertTrue(wait_for(lambda: s.proc is not None and s.proc is not first
                                 and s.state == feed.STATE_STREAMING, 20), s)
        self.assertEqual((big.session, small.session), sessions, 'neither picture reconnected')
        self.assertTrue(all(x.live for x in sessions))
        self.assertIs(s.combined.big, small)
        self.assertIs(s.combined.small, big)
        self.assertEqual(self.pictures(), (small, big), 'the pair\'s pictures changed places')
        self.assertEqual((small.role, big.role), (pip.ROLE_MAIN, pip.ROLE_INSET))
        self.assertTrue(wait_for(lambda: s.combined.had_frame, 15), 'the new small picture is decoded')
        self.assertEqual(self.seats(self.account), [('viewer', big.session.id)])
        self.assertEqual(self.seats(self.small.account), [('viewer', small.session.id)])
        # The recording now takes the other one first.
        self.assertEqual(engine.give_way()[big.session.id], connlim.VIEWER_SMALL_PICTURE)
        feed.flush_play_log()
        self.assertEqual(OutputFeedPlay.query.filter_by(kind=FEED_PLAY_PICTURE).count(), 0,
                         'a swap is not the small picture stopping')

    def test_a_swap_that_lands_inside_a_rebuild_is_built_again(self):
        _resp, s = self.combined()
        c = s.combined
        big, small = self.pictures()
        self.assertTrue(wait_for(lambda: c.had_frame and not c._busy, 15))
        gate, inside = threading.Event(), threading.Event()
        real, built = engine.probe_picture, len(self.asked('decoder'))

        def held(cfg, data):
            inside.set()
            gate.wait(10)
            return real(cfg, data)

        with mock.patch.object(engine, 'probe_picture', held):
            self.assertTrue(c._start_job(c._rebuild_decoder, ((engine.INPUT_LIVE, small.buffer.source_no),)))
            self.assertTrue(inside.wait(10), 'the rebuild is under way')
            pip.swap(self.row.id)
            feed.refresh_pip(self.row.id)
            self.assertIs(c.small, big)
            gate.set()
            # The decoder that was being built was for the picture that is now the big one: it
            # is not taken as built, and another is, on the picture that is now small.
            self.assertTrue(wait_for(lambda: len(self.asked('decoder')) >= built + 2, 20), self.asked('decoder'))
        self.assertTrue(wait_for(lambda: c.small_now == (engine.INPUT_LIVE, big.buffer.source_no)
                                 and c.big_now == (engine.INPUT_LIVE, small.buffer.source_no) and c.had_frame, 20))

    def test_swap_the_two_pictures_on_a_watched_channel(self):
        self.swapped(lambda: pip.swap(self.row.id))

    def test_a_save_that_only_exchanges_them_is_the_same_swap(self):
        self.swapped(lambda: pip.change(self.row.id, pip.clean({
            'name': 'PiP 1', 'main': src(PIP_SOURCE_CHANNEL, self.small.id),
            'inset': src(PIP_SOURCE_CHANNEL, self.big.id), 'layout': 'corner', 'corner': 'br',
            'size': 's', 'swapped_copy': True})))

    def test_swapping_back_is_a_swap_too(self):
        _resp, s = self.combined()
        big, small = self.pictures()
        sessions = (big.session, small.session)
        for _ in range(2):
            proc = s.proc
            pip.swap(self.row.id)
            feed.refresh_pip(self.row.id)
            self.assertTrue(wait_for(lambda: s.proc is not None and s.proc is not proc
                                     and s.state == feed.STATE_STREAMING, 20), s)
        self.assertEqual(self.pictures(), (big, small))
        self.assertEqual((big.session, small.session), sessions)
        self.assertIs(s.combined.big, big)


class SmallPictureLinesTests(_Connections):
    def lines(self):
        feed.flush_play_log()
        return OutputFeedPlay.query.filter_by(kind=FEED_PLAY_PICTURE).order_by(OutputFeedPlay.id).all()

    def test_recent_plays_says_when_the_small_picture_stopped_and_when_it_came_back(self):
        _resp, s = self.combined()
        _big, small = self.pictures()
        self.assertTrue(wait_for(lambda: s.combined._small_live is True, 10))
        self.assertEqual(self.lines(), [], 'nothing for how it was at the tune-in')
        self.assertTrue(self.record(self.small.account))
        self.assertTrue(wait_for(lambda: len(self.lines()) == 1, 10))
        stopped = self.lines()[0]
        self.assertEqual((stopped.feed_id, stopped.channel_id, stopped.account_id, stopped.reason),
                         (self.feed_id, None, None, FEED_PICTURE_STOPPED))
        self.assertIn('The small picture (Small one) stopped: paused for a recording', stopped.message)
        connlim.release(self.small.account.id, 'recording', 77)
        feed.recheck_waiting()
        self.assertTrue(wait_for(lambda: len(self.lines()) == 2, 15))
        back = self.lines()[1]
        self.assertEqual((back.channel_id, back.channel_name, back.account_id, back.reason),
                         (self.small.id, 'Small one', self.small.account.id, FEED_PICTURE_BACK))
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('Small picture stopped', html)
        self.assertIn('Small picture back', html)
        self.assertIn(f'data-hist="{FEED_PLAY_PICTURE}">Small picture<', html, 'and a chip to show only those')
        only = self.client.get(f'/iptv-output?plays={FEED_PLAY_PICTURE}').get_data(as_text=True)
        self.assertIn('Small picture stopped', only)


class SwappedCopySameChannelTests(_Connections):
    """dev/docs/BUGS.md 2026-11-23: the swapped copy's big picture is the row's second
    picture, which can be "the same channel as the other"; the card had no words for it, so
    the play's combiner could never be built and the play ended."""
    FEED = TWO_TVS

    def test_a_swapped_copy_whose_big_picture_is_the_same_channel_shows_a_card_saying_so(self):
        row = self.make(name='Same', main=src(PIP_SOURCE_CHANNEL, self.big.id),
                        inset=src(PIP_SOURCE_SEARCH, query='Big'))
        self.combined(feed_id_for_pip(row.id))
        _big, small = self.pictures(row)
        self.assertEqual(small.pane.state, pip.PANE_SAME)
        before = {x.id for x in feed.live_sessions()}
        self.play(feed_id_for_pip(row.id, True))
        b = next(x for x in feed.live_sessions() if x.id not in before)
        self.assertTrue(wait_for(lambda: b.state == feed.STATE_STREAMING, 20), (b.state, b.reason, b.detail))
        self.assertTrue(self.asked('combiner')[-1]['big_input'], 'its big window loops a card')
        self.assertIn('same channel as the other picture', repr(self.renders[-1][2]), 'the card says why')
        self.assertTrue(b.live)


class AccountsCardAndPreviewTests(_Connections):
    FEED = TWO_TVS

    def streams(self) -> dict:
        from app.output_feed_view import page_context
        with self.t.app.test_request_context('/iptv-output'):
            ctx = page_context(load_config(), 'http://localhost/', {})
        return {a['account'].id: a['streams'] for a in ctx['accounts']}

    def test_the_accounts_card_counts_each_pictures_connection_once_however_many_tvs_watch(self):
        self.combined()
        want = {self.account.id: 1, self.small.account.id: 1}
        self.assertEqual(self.streams(), want)
        self.combined(feed_id_for_pip(self.row.id, True))
        self.assertEqual(self.streams(), want, 'two plays of one row share two connections')

    def test_a_rows_preview_is_a_combined_play_on_two_connections(self):
        import app.preview as preview
        session = preview.start_feed_preview(self.feed_id)
        self.addCleanup(preview.stop_all)
        self.assertTrue(wait_for(lambda: feed.live_sessions() and feed.live_sessions()[0].combined is not None, 15))
        play = self.only_session()
        self.assertTrue(play.preview)
        big, small = self.pictures()
        self.assertTrue(wait_for(lambda: big.live and small.live, 15), (big.pane, small.pane))
        self.assertEqual(self.seats(self.account), [('viewer', big.session.id)])
        self.assertEqual(self.seats(self.small.account), [('viewer', small.session.id)])
        self.assertEqual(session.feed_id, self.feed_id)


class WouldPlayTests(_Connections):
    """The setup dialog's "Would play from" lines and its connection count
    (pip_channels.would_play, POST /api/iptv-output/pip/would-play)."""

    def ask(self, main, inset):
        got = self.client.post('/api/iptv-output/pip/would-play', json={'main': main, 'inset': inset})
        self.assertEqual(got.status_code, 200)
        return got.get_json()

    def test_each_picture_names_its_channel_and_account_and_two_connections_are_counted(self):
        got = self.ask(src(PIP_SOURCE_CHANNEL, self.big.id), src(PIP_SOURCE_CHANNEL, self.small.id))
        self.assertEqual(got['main'], {'channel': 'Big one', 'account': self.account.name})
        self.assertEqual(got['inset'], {'channel': 'Small one', 'account': self.small.account.name})
        self.assertEqual(got['seats'], f'While a TV watches it, it uses two connections: Big one on '
                                       f'{self.account.name} and Small one on {self.small.account.name}. '
                                       'Right now 2 of your 2 connections are free.')
        self.assertEqual(self.seats(self.account) + self.seats(self.small.account), [], 'a look takes no seat')

    def test_a_small_picture_that_would_share_the_big_ones_only_connection_is_not_promised(self):
        twin = self.plain_channel('Big two', self.account)
        got = self.ask(src(PIP_SOURCE_CHANNEL, self.big.id), src(PIP_SOURCE_CHANNEL, twin.id))
        self.assertEqual(got['main']['channel'], 'Big one')
        self.assertIsNone(got['inset'])
        self.assertIn('it uses one connection now (Big one on', got['seats'])
        self.account.max_connections = 2
        db.session.commit()
        self.assertEqual(self.ask(src(PIP_SOURCE_CHANNEL, self.big.id),
                                  src(PIP_SOURCE_CHANNEL, twin.id))['inset']['channel'], 'Big two')

    def test_a_search_for_the_small_picture_goes_to_its_next_channel_down(self):
        self.plain_channel('Big two', self.account)
        elsewhere = self.plain_channel('Big three')
        got = self.ask(src(PIP_SOURCE_CHANNEL, self.big.id), src(PIP_SOURCE_SEARCH, query='Big t'))
        self.assertEqual(got['inset'], {'channel': 'Big three', 'account': elsewhere.account.name})

    def test_busy_accounts_and_half_a_draft(self):
        self.record_on(self.account)
        self.record_on(self.small.account, 78)
        got = self.ask(src(PIP_SOURCE_CHANNEL, self.big.id), src(PIP_SOURCE_CHANNEL, self.small.id))
        self.assertEqual(got['seats'], 'Neither picture is on yet, so it uses no connection until one comes on. '
                                       'Right now 0 of your 2 connections are free.')
        self.assertEqual((got['main'], got['inset']), (None, None))
        half = self.ask(src(PIP_SOURCE_CHANNEL, self.big.id), None)
        self.assertEqual((half['inset'], half['seats']), (None, ''))
        self.assertEqual(self.ask({'kind': 'nonsense'}, None)['main'], None)


if __name__ == '__main__':
    import unittest
    unittest.main()
