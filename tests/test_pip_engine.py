"""A picture-in-picture play on its two pictures combined (app/pip_engine.py,
dev/docs/DESIGN-pip-channels.md 6, 7, 9 and 13): one play on a TV, two pictures behind it that
belong to the channel's row, each on its own connection; a small picture that stops never
stops the big one; a picture whose GPU decode fails is decoded on the CPU once and the play
says so; a combiner that dies twice drops the play to its big picture alone; two plays of one
row share its pictures; and every end leaves no ffmpeg, seat or thread behind.

The combiner and the GPU are stood in for (tests/support/pip_engine.py): the suite has no GPU.
No dev/docs/BUGS.md entry: a feature.

  python3 -m unittest tests.test_pip_engine
"""
import json
import os
import signal
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.feed_plays import wait_for  # noqa: E402
from tests.support.pip_engine import PipPlayCase as _Engine, engine_threads, src  # noqa: E402

import app.connection_limits as connlim  # noqa: E402
import app.output_feed as feed  # noqa: E402
import app.pip_channels as pip  # noqa: E402
import app.pip_engine as engine  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import PIP_SOURCE_CHANNEL, PIP_SOURCE_SEARCH, Alert, OutputFeedPlay  # noqa: E402
from app.output_feed import feed_id_for_pip  # noqa: E402


def _cont(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGCONT)
    except OSError:
        pass   # already gone


class CombinedPlayTests(_Engine):
    def test_a_tune_in_plays_two_pictures_on_two_connections_as_one_play(self):
        resp, s = self.combined()
        self.assertEqual(self.read(resp)[0][0], 0x47, 'MPEG-TS reaches the TV')
        big, small = self.pictures()
        self.assertEqual((big.session.channel_id, small.session.channel_id), (self.big.id, self.small.id))
        self.assertEqual(self.seats(self.account), [('viewer', big.session.id)])
        self.assertEqual(self.seats(self.small.account), [('viewer', small.session.id)])
        # One play: the pictures are listed nowhere and counted nowhere.
        self.assertEqual(feed.live_sessions(), [s])
        self.assertTrue(s.counted)
        self.assertFalse(big.session.counted or small.session.counted)
        self.assertEqual((s.account_id, s.channel_id), (None, None), 'the play itself holds no seat')
        self.assertTrue(wait_for(lambda: s.combined.had_frame, 15), 'the small picture is decoded')
        view = engine.views(s)
        self.assertEqual((view['big']['channel_id'], view['small']['channel_id']), (self.big.id, self.small.id))
        # The big picture was read from its buffer before its combiner started.
        self.assertEqual([kw['big_input'] for kw in self.asked('combiner')], [''])

    def test_a_move_by_hand_is_refused_by_name_and_leaves_the_play_as_it_was(self):
        _resp, s = self.combined()
        combiner = s.proc
        self.assertEqual(feed.move_offer(s), feed.MOVE_TWO_PICTURES)
        self.assertEqual(s.to_dict()['move_why'], feed.MOVE_TWO_PICTURES_TEXT)
        self.assertFalse(s.to_dict()['move_offered'])
        with self.assertRaises(feed.FeedRefused) as refused:
            feed.move_by_hand(s.id)
        self.assertIn('plays two pictures', refused.exception.message)
        self.assertIs(s.proc, combiner)
        self.assertIsNone(combiner.poll())

    def test_the_swapped_copy_has_the_pictures_the_other_way_round(self):
        _resp, s = self.combined(feed_id_for_pip(self.row.id, True))
        self.assertIs(s.combined.big, self.pictures()[1])
        self.assertIs(s.combined.small, self.pictures()[0])

    def test_a_small_picture_cut_for_good_leaves_the_big_one_playing_and_its_window_says_why(self):
        resp, s = self.combined()
        _big, small = self.pictures()
        self.assertTrue(wait_for(lambda: s.combined.had_frame, 15))
        combiner = s.proc
        sent = s.ring.total_in
        self.record_on(self.small.account)
        self.assertTrue(wait_for(lambda: not small.live and small.pane.state == pip.PANE_TAKEN, 10), small.pane)
        self.assertEqual(small.words[0], 'Paused for a recording')
        # The TV kept a picture, so no alert: the window says why.
        self.assertEqual(Alert.query.filter_by(alert_type='VIEWER_PREEMPTED').count(), 0)
        self.assertTrue(wait_for(lambda: s.combined.cards > 0, 10), 'the pacer writes the card')
        self.assertTrue(wait_for(lambda: s.ring.total_in > sent + 20_000, 10), 'the big picture kept going')
        self.assertIs(s.proc, combiner, 'nothing about the small picture restarts the combiner')
        self.assertIsNone(combiner.poll())
        self.assertEqual(self.seats(self.small.account), [('recording', 77)])
        self.assertTrue(s.live)
        self.assertFalse(engine.views(s)['small']['live'])
        self.assertIn('paused for a recording', engine.views(s)['small']['text'])
        # It comes back by itself the moment the connection is free.
        connlim.release(self.small.account.id, 'recording', 77)
        feed.recheck_waiting()
        self.assertTrue(wait_for(lambda: small.live, 10))
        self.assertIs(s.proc, combiner)

    def test_a_small_picture_that_stalls_gets_its_card_and_then_its_picture_back(self):
        _resp, s = self.combined()
        c = s.combined
        self.assertTrue(wait_for(lambda: c.had_frame, 15))
        combiner, sent = s.proc, s.ring.total_in
        # The small picture's frames stop arriving: the pacer holds the last one, then the card.
        os.kill(c.decoder.pid, signal.SIGSTOP)
        self.addCleanup(lambda pid=c.decoder.pid: _cont(pid))
        cards = c.cards
        self.assertTrue(wait_for(lambda: c.cards > cards, engine.CARD_AFTER_SECONDS + 5))
        self.assertEqual(c._small_words(), 'Reconnecting')
        self.assertTrue(wait_for(lambda: s.ring.total_in > sent + 20_000, 10), 'the big picture kept going')
        # Frames arriving again are shown again.
        stalled_at = c.frames.at
        os.kill(c.decoder.pid, signal.SIGCONT)
        self.assertTrue(wait_for(lambda: c.frames.at > stalled_at, 10), 'the picture came back')
        self.assertIs(s.proc, combiner)

    def test_a_big_picture_that_drops_restarts_the_combiner_on_its_card_and_the_small_one_plays_on(self):
        resp, s = self.combined()
        big, small = self.pictures()
        first = s.proc
        self.record_on(self.account)
        self.assertTrue(wait_for(lambda: s.proc is not None and s.proc is not first
                                 and s.state == feed.STATE_STREAMING, 20), s)
        self.assertIsNotNone(first.poll(), 'the old combiner is dead')
        self.assertTrue(self.asked('combiner')[-1]['big_input'], 'the new one loops the card')
        self.assertEqual(big.pane.state, pip.PANE_TAKEN)
        self.assertTrue(s.live and small.live)
        self.assertEqual(self.seats(self.account), [('recording', 77)])
        self.assertEqual(self.seats(self.small.account), [('viewer', small.session.id)])

    def test_neither_picture_able_to_play_is_the_channels_card_with_nothing_held(self):
        for account in (self.account, self.small.account):
            self.record_on(account, 70 + account.id)
        resp = self.play()
        self.read(resp)
        s = self.only_session()
        self.assertEqual(s.state, feed.STATE_CARD)
        self.assertIsNone(s.combined)
        self.assertEqual(s.card_state, pip.PANE_BUSY)
        self.assertFalse(feed._internal)
        self.assertTrue(wait_for(lambda: not engine._pairs or not engine._pairs[self.row.id].plays))

    def test_a_card_moves_to_the_combined_picture_when_a_connection_frees(self):
        for account in (self.account, self.small.account):
            self.record_on(account, 70 + account.id)
        resp = self.play()
        self.read(resp)
        s = self.only_session()
        clip = s.proc
        connlim.release(self.small.account.id, 'recording', 70 + self.small.account.id)
        feed.recheck_waiting()
        self.assertTrue(wait_for(lambda: s.combined is not None and s.state == feed.STATE_STREAMING, 20), s)
        self.assertIsNotNone(clip.poll(), "the card's ffmpeg is dead")
        big, small = self.pictures()
        self.assertTrue(small.live)
        self.assertFalse(big.live)
        self.assertEqual(big.pane.state, pip.PANE_BUSY)
        self.assertEqual((s.card_state, s.card_text), ('', ''))


class ClearedWhileWatchedTests(_Engine):
    def test_clearing_a_watched_channel_moves_its_play_to_the_card_and_gives_both_connections_back(self):
        _resp, s = self.combined()
        big, small = self.pictures()
        inner = (big.session, small.session)
        combiner = s.proc
        pip.clear(self.row.id)
        self.assertEqual(feed.clear_pip(self.row.id), 1)
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_CARD and s.proc is not None, 20), s)
        self.assertIsNone(s.combined)
        self.assertEqual(s.card_state, pip.PANE_EMPTY)
        self.assertIsNotNone(combiner.poll())
        self.assertTrue(s.live)
        self.assertFalse(s.counted)
        self.assert_nothing_left(*inner)
        # The provider time its pictures ran up is still this play's.
        self.assertEqual(set(s.account_seconds), {self.account.id, self.small.account.id})


class SavedWhileWatchedTests(_Engine):
    def test_a_changed_picture_is_replaced_in_place_and_the_other_keeps_its_connection(self):
        _resp, s = self.combined()
        big, small = self.pictures()
        kept, replaced = big.session, small.session
        other = self.plain_channel('Other one')
        combiner = s.proc
        pip.change(self.row.id, pip.clean({
            'name': 'PiP 1', 'main': src(PIP_SOURCE_CHANNEL, self.big.id),
            'inset': src(PIP_SOURCE_CHANNEL, other.id), 'layout': 'corner', 'corner': 'br', 'size': 's',
            'swapped_copy': True}))
        self.assertEqual(feed.refresh_pip(self.row.id), (0, 0, 1))
        self.assertTrue(wait_for(lambda: small.live and small.session.channel_id == other.id, 15), small.pane)
        self.assertFalse(replaced.live)
        self.assertIs(big.session, kept, 'the picture that did not change keeps its connection')
        self.assertIs(s.proc, combiner, 'and a new small picture never restarts the combiner')
        self.assertEqual(self.seats(self.small.account), [])
        self.assertEqual(self.seats(other.account), [('viewer', small.session.id)])

    def test_a_new_layout_is_the_next_combiners(self):
        _resp, s = self.combined()
        first = s.proc
        pip.change(self.row.id, pip.clean({
            'name': 'PiP 1', 'main': src(PIP_SOURCE_CHANNEL, self.big.id),
            'inset': src(PIP_SOURCE_CHANNEL, self.small.id), 'layout': 'side', 'swapped_copy': True}))
        self.assertEqual(feed.refresh_pip(self.row.id), (0, 0, 1))
        self.assertTrue(wait_for(lambda: s.proc is not None and s.proc is not first
                                 and s.state == feed.STATE_STREAMING, 20), s)
        side = self.asked('combiner')[-1]['geometry']
        self.assertEqual((side.small_w, side.small_fps), (960, 60))
        self.assertTrue(wait_for(lambda: s.combined.had_frame, 15), 'the decoder was rebuilt to the new size')
        self.assertEqual(s.combined.frame_bytes, 960 * 540 * 3 // 2)
        big, small = self.pictures()
        self.assertTrue(big.live and small.live, 'neither connection was touched')


class PictureEndTests(_Engine):
    """What a window says when its picture's session stops, by why it stopped."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(engine, '_ask_later', lambda picture, periodic: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_account_turned_off_is_not_called_reconnecting(self):
        self.combined()
        _big, small = self.pictures()
        feed.stop_account(self.small.account.id)
        self.assertTrue(wait_for(lambda: small.pane.state == pip.PANE_CANNOT, 10), small.pane)
        self.assertIn('turned off', small.pane.why)

    def test_a_dropped_channel_is_reconnecting(self):
        self.combined()
        _big, small = self.pictures()
        feed._finish(small.session, feed.REASON_FFMPEG_EXITED, detail='ffmpeg exited with code 1')
        self.assertTrue(wait_for(lambda: small.pane.state == pip.PANE_RECONNECTING, 10), small.pane)
        self.assertEqual(small.words[0], 'Reconnecting')

    def test_a_recording_taking_the_seat_inside_the_launch_is_paused_not_a_failed_channel(self):
        self.combined()
        _big, small = self.pictures()
        feed._finish(small.session, feed.REASON_STOPPED)
        self.assertTrue(wait_for(lambda: small.session is None, 10))

        def taken(session, member, cfg, **kw):
            feed._finish(session, feed.REASON_PREEMPTED, detail='recording 9 took the connection')
            raise feed.FeedRefused(feed.REASON_TEXT[feed.REASON_PREEMPTED], 409)

        with mock.patch.object(feed, '_launch', taken):
            small.ask(load_config())
        self.assertEqual(small.pane.state, pip.PANE_TAKEN)
        self.assertEqual(small.failed_ids, set(), 'the channel did not fail')
        self.assertFalse(feed._internal.get(small.session.id if small.session else '', None))

    def test_a_stopped_sessions_pane_never_overwrites_a_newer_one(self):
        """dev/docs/BUGS.md 2026-11-24 @ 10:11:43 AM: ended() names a stopped session's pane
        after letting the lock go. An ask that lands in that gap names what is next ("taken"),
        and the reaper's older "reconnecting" must not replace it. Forced here by running the
        ask from inside ended()'s pane_words call, which is in that gap."""
        self.combined()
        _big, small = self.pictures()
        real, real_ended = engine.pip_channels.pane_words, engine.Picture.ended
        ran, done = [], []

        def words(pane, *a, **kw):
            if pane.state == pip.PANE_RECONNECTING and not ran:
                ran.append(True)
                small._set_pane(pip.Pane(pip.PANE_TAKEN, pane.source, pane.name,
                                         why='recording 9 took the connection'))
            return real(pane, *a, **kw)

        def ended(picture):
            collected = real_ended(picture)
            if collected and picture is small:
                done.append(True)
            return collected

        with mock.patch.object(engine.pip_channels, 'pane_words', words), \
                mock.patch.object(engine.Picture, 'ended', ended):
            feed._finish(small.session, feed.REASON_FFMPEG_EXITED, detail='ffmpeg exited with code 1')
            self.assertTrue(wait_for(lambda: done, 10), 'the reaper collected the session')
        self.assertTrue(ran)
        self.assertEqual(small.pane.state, pip.PANE_TAKEN)
        self.assertEqual(small.words[0], real(small.pane)[0])


class BuildFailureTests(_Engine):
    def test_a_combiner_that_cannot_be_spawned_drops_the_play_to_the_big_picture_alone(self):
        with mock.patch.object(engine, 'build_combiner_cmd', lambda *a, **kw: ['/nowhere/ffmpeg']):
            resp = self.play()
            s = self.only_session()
            self.assertTrue(wait_for(lambda: s.combined is None and s.state == feed.STATE_STREAMING, 30), s)
        self.assertIn('could not start', s.combine_off)
        self.assertEqual((s.channel_id, s.account_id), (self.big.id, self.account.id))
        self.assertEqual(self.seats(self.small.account), [])
        self.assertTrue(wait_for(lambda: not engine_threads(), 10), engine_threads())
        resp.close()

    def test_a_rebuild_that_keeps_raising_ends_the_play_by_name_and_leaks_no_pipe(self):
        def raises(*a, **kw):
            raise RuntimeError('no such layout')

        before = len(os.listdir('/proc/self/fd'))
        with mock.patch.object(engine, 'build_combiner_cmd', raises):
            # The test client hands the response back at its first byte or its end, and this
            # play never has a first byte.
            resp = self.play()
            s = (feed.live_sessions() or feed.recent_ended())[0]
            self.assertTrue(wait_for(lambda: not s.live, 20), s)
        self.assertEqual(s.reason, feed.REASON_LAUNCH_FAILED)
        self.assertIn('no such layout', s.detail)
        resp.close()
        with mock_linger(0.0):
            self.assertTrue(wait_for(lambda: not engine._pairs and not feed._internal, 15))
        self.assertTrue(wait_for(lambda: len(os.listdir('/proc/self/fd')) <= before + 2, 10),
                        (before, len(os.listdir('/proc/self/fd'))))


class NoGpuTests(_Engine):
    def test_with_no_gpu_a_tune_in_plays_the_big_picture_alone_and_says_why(self):
        self.gpu = ('', 'Two pictures are combined only on a GPU, and there is no GPU device.')
        self.read(self.play())
        s = self.only_session()
        self.assertIsNone(s.combined)
        self.assertEqual((s.channel_id, s.account_id), (self.big.id, self.account.id))
        self.assertIn('no GPU device', s.combine_off)
        self.assertEqual(self.seats(self.small.account), [], 'the small picture takes no connection')
        self.assertFalse(engine._pairs)
        self.assertEqual(self.built, [])


class GpuDecodeFallbackTests(_Engine):
    def test_each_picture_whose_gpu_decode_fails_restarts_once_on_the_cpu_and_the_play_says_so(self):
        self.failing.add('gpu')
        _resp, s = self.combined()
        self.assertTrue(wait_for(lambda: s.combined.had_frame, 20))
        self.assertEqual([kw['gpu_decode'] for kw in self.asked('combiner')], [True, False])
        self.assertEqual([kw['gpu_decode'] for kw in self.asked('decoder')], [True, False])
        notes = feed.play_notes(s)
        self.assertIn('The GPU could not decode Big one, so it is decoded on the CPU for this play.', notes)
        self.assertIn('The GPU could not decode Small one, so it is decoded on the CPU for this play.', notes)
        self.assertIsNotNone(s.combined, 'a GPU decode failure is not a combiner failure')


class CombinerDeathTests(_Engine):
    def test_a_combiner_that_dies_is_restarted_once(self):
        _resp, s = self.combined()
        first = s.proc
        first.kill()
        self.assertTrue(wait_for(lambda: s.proc is not None and s.proc is not first
                                 and s.state == feed.STATE_STREAMING, 20), s)
        self.assertIsNotNone(s.combined)
        self.assertEqual(len(self.asked('combiner')), 2)

    def test_dying_twice_drops_the_play_to_the_big_picture_alone_and_gives_the_small_seat_back(self):
        self.failing.add('combiner')
        resp = self.play()
        s = self.only_session()
        self.assertTrue(wait_for(lambda: s.combined is None and s.state == feed.STATE_STREAMING, 30), s)
        self.assertEqual(len(self.asked('combiner')), 2, 'one restart, then the big picture alone')
        self.assertEqual((s.channel_id, s.account_id), (self.big.id, self.account.id))
        self.assertEqual(self.seats(self.account), [('viewer', s.id)])
        self.assertEqual(self.seats(self.small.account), [])
        self.assertIn('could not be combined', feed.play_notes(s))
        self.assertTrue(s.combine_off)
        self.assertFalse(engine._pairs)
        self.assertEqual(self.read(resp)[0][0], 0x47)
        self.assertTrue(wait_for(lambda: not engine_threads(), 10), engine_threads())


class SharedPicturesTests(_Engine):
    FEED = {'integrations': {'output_feed': {'enabled': True, 'username': 'tv', 'password': 'tv-secret',
                                             'reserve_seconds': 0, 'max_viewers': 3}}}

    def test_the_channel_and_its_swapped_copy_share_two_pictures_and_two_connections(self):
        _r1, a = self.combined()
        big, small = self.pictures()
        sessions = (big.session, small.session)
        _r2, b = self.combined(feed_id_for_pip(self.row.id, True))
        self.assertEqual((big.session, small.session), sessions, 'the same two pictures, not two more')
        self.assertEqual(len(feed._internal), 2)
        self.assertEqual(len(self.seats(self.account)) + len(self.seats(self.small.account)), 2)
        self.assertIsNot(a.proc, b.proc, 'each play has its own combiner')
        self.assertIs(b.combined.big, a.combined.small)

    def test_the_last_play_leaving_lets_the_pictures_linger_then_closes_them(self):
        resp, s = self.combined()
        big, small = self.pictures()
        resp.close()
        self.assertTrue(wait_for(lambda: not s.live, 10))
        self.assertTrue(big.live and small.live, 'a player ends the old channel before it asks for the new one')
        # A change to the swapped copy inside the linger finds both already playing.
        r2, b = self.combined(feed_id_for_pip(self.row.id, True))
        self.assertIs(b.combined.big.session, small.session)
        # And with nobody back inside the linger, they close.
        inner = (big.session, small.session)
        with mock_linger(0.5):
            r2.close()
            self.assert_nothing_left(b, *inner)

    def test_a_pair_a_play_just_joined_is_never_closed_under_it(self):
        _resp, s = self.combined()
        pair = engine._pairs[self.row.id]
        big, small = self.pictures()
        # What the linger's end, a refused seat and a channel taken off all ask for.
        self.assertFalse(engine._close_pair(pair, feed.REASON_CLIENT_GONE))
        self.assertEqual(engine.drop_lingering(), 0)
        engine.close_row(self.row.id)
        self.assertFalse(pair.closed)
        self.assertTrue(big.live and small.live and s.live)

    def test_a_play_whose_pictures_are_gone_ends_by_name_instead_of_sitting_with_no_ffmpeg(self):
        resp = self.play()
        s = self.only_session()
        pair = engine._pairs[self.row.id]
        self.assertTrue(engine._close_pair(pair, feed.REASON_STOPPED, even_if_played=True))
        self.assertTrue(wait_for(lambda: not s.live, 10), s)
        self.assertEqual(s.reason, feed.REASON_LAUNCH_FAILED)
        self.assertIn('closed under it', s.detail)
        resp.close()

    def test_a_lingering_picture_gives_its_seat_to_whatever_asks(self):
        resp, s = self.combined()
        big, _small = self.pictures()
        held = big.session
        resp.close()
        self.assertTrue(wait_for(lambda: not s.live, 10))
        self.assertTrue(held.live)
        # A plain play of the big picture's own channel needs the one connection its account has.
        self.read(self.play(feed.feed_id_for_channel(self.big.id)))
        plain = self.only_session()
        self.assertEqual(self.seats(self.account), [('viewer', plain.id)])
        self.assertFalse(held.live)
        self.assertFalse(engine._pairs)


class TeardownTests(_Engine):
    def test_every_end_leaves_no_ffmpeg_seat_or_thread(self):
        for end, reason in ((lambda s, r: r.close(), feed.REASON_CLIENT_GONE),
                            (lambda s, r: feed.stop_session(s.id), feed.REASON_STOPPED),
                            (lambda s, r: feed.stop_pip(self.row.id), feed.REASON_TAKEN_OFF),
                            (lambda s, r: feed.stop_all(feed.REASON_DISABLED), feed.REASON_DISABLED),
                            (lambda s, r: feed.kill_all_viewers(), feed.REASON_SHUTDOWN)):
            with self.subTest(reason=reason):
                resp, s = self.combined()
                self.assertTrue(wait_for(lambda: s.combined.decoder is not None, 15))
                spool = s.combined.decoder_stderr
                self.assertTrue(os.path.exists(spool))
                big, small = self.pictures()
                procs = [s.proc, s.combined.decoder, big.session.proc, small.session.proc]
                inner = (big.session, small.session)
                end(s, resp)
                self.assertTrue(wait_for(lambda: not s.live, 10), s)
                self.assertEqual(s.reason, reason)
                with mock_linger():
                    self.assert_nothing_left(s, *inner)
                for proc in procs:
                    self.assertIsNotNone(proc.poll(), 'every ffmpeg is dead')
                self.assertFalse(os.path.exists(spool), "the decoder's stderr spool is removed")

    def test_a_play_that_ends_is_credited_both_pictures_provider_time(self):
        resp, s = self.combined()
        time.sleep(1.0)
        resp.close()
        self.assertTrue(wait_for(lambda: not s.live, 10))
        self.assertEqual(set(s.account_seconds), {self.account.id, self.small.account.id})
        self.assertTrue(all(v > 0.5 for v in s.account_seconds.values()), s.account_seconds)
        # One line in Recent plays for the play, naming both pictures; none for a picture.
        self.assert_nothing_left(s)
        feed.flush_play_log()
        line = OutputFeedPlay.query.one()
        self.assertEqual((line.channel_name, line.feed_id), ('Big one + Small one', self.feed_id))
        self.assertEqual(set(json.loads(line.account_seconds)),
                         {str(self.account.id), str(self.small.account.id)})


class SamePictureTests(_Engine):
    def test_the_small_picture_never_plays_the_channel_the_big_one_is_on(self):
        row = self.make(name='Two searches', main=src(PIP_SOURCE_CHANNEL, self.big.id),
                        inset=src(PIP_SOURCE_SEARCH, query='Big'), swapped_copy=False)
        resp = self.play(feed_id_for_pip(row.id))
        s = self.only_session()
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_STREAMING, 20), s)
        big, small = self.pictures(row)
        self.assertTrue(big.live)
        self.assertFalse(small.live)
        self.assertEqual(small.pane.state, pip.PANE_SAME)
        self.assertEqual(small.words[0], 'Same channel as the big picture')
        resp.close()


class PageTests(_Engine):
    """Watching now and the Picture-in-picture card (templates/iptv_output.html)."""

    def page(self, notice=''):
        with mock.patch.object(engine, 'gpu_notice', return_value=notice):
            return self.client.get('/iptv-output').get_data(as_text=True)

    def test_watching_now_names_both_pictures_with_their_channels_and_accounts(self):
        self.combined()
        html = self.page()
        for label, ch in (('Big', self.big), ('Small', self.small)):
            self.assertIn(f'<span class="pip-k">{label}</span><a class="name-link" href="/channels/{ch.id}">'
                          f'{ch.name}</a>', html)
        self.assertNotIn('not shown yet', html)
        self.assertNotIn('play their big picture alone', html)

    def test_a_small_picture_that_is_not_playing_says_why_in_its_place(self):
        _resp, s = self.combined()
        _big, small = self.pictures()
        self.record_on(self.small.account)
        self.assertTrue(wait_for(lambda: small.pane.state == pip.PANE_TAKEN, 10))
        html = self.page()
        self.assertIn('<span class="pip-k">Small</span>Paused for a recording', html)

    def test_the_card_says_when_the_gpu_is_not_working_and_only_then(self):
        why = 'A test of combining two pictures through /dev/dri/renderD128 failed: No such filter.'
        html = self.page(notice=why)
        self.assertIn('play their big picture alone right now', html)
        self.assertIn(why, html)
        self.assertIn('The GPU can combine two pictures', html)
        self.assertNotIn('play their big picture alone right now', self.page())

    def test_a_play_with_no_gpu_says_its_small_picture_is_off_and_why(self):
        self.gpu = ('', 'There is no GPU device.')
        self.read(self.play())
        html = self.page(notice='There is no GPU device.')
        self.assertIn('<span class="pip-k">Small</span>Off, so the big picture plays alone. '
                      'There is no GPU device.', html)


class mock_linger:
    """The linger cut short, so a teardown test does not wait it out."""

    def __init__(self, seconds=0.0):
        self.seconds = seconds

    def __enter__(self):
        self._was = feed.SWITCH_GRACE_SECONDS
        feed.SWITCH_GRACE_SECONDS = self.seconds

    def __exit__(self, *exc):
        feed.SWITCH_GRACE_SECONDS = self._was


if __name__ == '__main__':
    import unittest
    unittest.main()
