"""What a tune-in to a picture-in-picture channel plays (app/output_feed.py's card section,
dev/docs/DESIGN-pip-channels.md 6.4, 7.3, 8): its big picture alone, as a plain stream copy on
one connection, or the channel's card, ChannelBin's own looped clip saying why nothing is on,
with no seat and not counted. A card asks again and puts the picture on in its place; a
picture whose every channel dropped goes to the card instead of ending; every end kills the
ffmpeg and frees the seat.

The card's clip is not drawn here (tests/support/feed_plays.py). No dev/docs/BUGS.md entry: a
feature.

  python3 -m unittest tests.test_pip_play
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.feed_plays import ClipPlayCase, feed_cfg, wait_for  # noqa: E402
from tests.support.seed import make_account  # noqa: E402

import app.connection_limits as connlim  # noqa: E402
import app.output_feed as feed  # noqa: E402
import app.pip_channels as pip  # noqa: E402
import app.sports_screen as ss  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import PIP_SOURCE_CHANNEL, PIP_SOURCE_SEARCH, OutputFeedPlay  # noqa: E402


def src(kind, ref=None, query=''):
    return {'kind': kind, 'ref': ref, 'query': query}


class _PipPlay(ClipPlayCase):
    def setUp(self):
        super().setUp()
        self.big = self.plain_channel('Big one', self.account)
        self.small = self.plain_channel('Small one')
        self.row = self.make()
        self.feed_id = feed.feed_id_for_pip(self.row.id)

    def make(self, **over):
        data = {'name': 'PiP 1', 'main': src(PIP_SOURCE_CHANNEL, self.big.id),
                'inset': src(PIP_SOURCE_CHANNEL, self.small.id), 'layout': 'corner', 'corner': 'br',
                'size': 'm', 'swapped_copy': True}
        data.update(over)
        spec = pip.clean(data)
        pip.check_sources(spec, load_config())
        return pip.create(spec)

    def seats(self, account=None):
        return connlim._holders.get((account or self.account).id, [])

    def on_card(self, feed_id=None):
        resp = self.play(feed_id)
        self.read(resp)
        s = feed.live_sessions()[-1]
        self.assertEqual(s.state, feed.STATE_CARD)
        return resp, s, s.proc

    def assert_gone(self, s, proc, reason):
        self.assertTrue(wait_for(lambda: not s.live, 10), s)
        self.assertEqual(s.reason, reason)
        self.assertIsNotNone(proc.poll(), 'its ffmpeg is dead')
        self.assertFalse(os.path.exists(s.stderr_path))
        self.assertNotIn(('viewer', s.id), self.seats() + self.seats(self.small.account))


class BigPictureTests(_PipPlay):
    def test_a_tune_in_plays_the_big_picture_alone_on_one_connection(self):
        resp = self.play()
        self.assertEqual(self.read(resp)[0][0], 0x47)
        s = self.only_session()
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_STREAMING))
        self.assertEqual((s.channel_id, s.account_id, s.pip_row_id), (self.big.id, self.account.id, self.row.id))
        self.assertEqual(self.seats(), [('viewer', s.id)])
        self.assertEqual(self.seats(self.small.account), [], 'the small picture takes no connection yet')
        self.assertTrue(s.counted)
        self.assertTrue(s.fails_over)

    def test_the_swapped_copy_plays_the_other_picture(self):
        self.read(self.play(feed.feed_id_for_pip(self.row.id, True)))
        s = self.only_session()
        self.assertEqual(s.channel_id, self.small.id)
        self.assertIn('PiP 1 (swapped)', s.label)

    def test_a_swapped_copy_that_is_not_offered_is_a_404(self):
        pip.set_swapped_copy(self.row.id, False)
        self.play(feed.feed_id_for_pip(self.row.id, True), expect=404)

    def test_taken_off_the_output_it_is_refused_by_name_with_no_alert(self):
        feed.set_on_output(feed.KIND_PIP, self.row.id, False)
        db.session.commit()
        resp = self.play(expect=409)
        self.assertIn('taken off', resp.get_data(as_text=True))

    def test_every_end_frees_the_seat_and_kills_ffmpeg(self):
        for end, reason in ((lambda s, r: r.close(), feed.REASON_CLIENT_GONE),
                            (lambda s, r: feed.stop_session(s.id), feed.REASON_STOPPED),
                            (lambda s, r: feed.stop_pip(self.row.id), feed.REASON_TAKEN_OFF),
                            (lambda s, r: feed.kill_all_viewers(), feed.REASON_SHUTDOWN)):
            resp = self.play()
            self.read(resp)
            s = feed.live_sessions()[-1]
            proc = s.proc
            end(s, resp)
            self.assert_gone(s, proc, reason)


class CardTests(_PipPlay):
    def test_nothing_set_up_plays_its_card_with_no_seat_and_is_not_counted(self):
        pip.clear(self.row.id)
        resp, s, _proc = self.on_card()
        self.assertEqual(resp.mimetype, 'video/mp2t')
        self.assertEqual((s.card_state, s.card_text), (pip.PANE_EMPTY, 'nothing is set up'))
        self.assertEqual((s.account_id, s.channel_id), (None, None))
        self.assertFalse(s.counted)
        self.assertEqual(self.seats(), [])
        screen = self.renders[0][2]
        self.assertEqual((screen.state, screen.words.headline), (ss.STATE_CARD, 'PiP 1 has nothing on it'))
        self.assertIn(f'/p{self.row.id}-', self.renders[0][0], 'its clip is the row\'s own')
        self.assertEqual(feed.recent_refused(), [])

    def test_a_search_with_no_match_says_so_and_comes_on_when_one_appears(self):
        row = self.make(name='Cams', main=src(PIP_SOURCE_SEARCH, query='in-car'), swapped_copy=False)
        _resp, s, clip = self.on_card(feed.feed_id_for_pip(row.id))
        self.assertEqual(s.card_state, pip.PANE_NO_MATCH)
        cam = self.plain_channel('Drive: in-car 24', self.account)
        feed.recheck_waiting()
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_STREAMING and s.channel_id == cam.id), s)
        self.assertEqual(self.seats(), [('viewer', s.id)])
        self.assertIsNotNone(clip.poll(), "the card's ffmpeg is dead")
        self.assertEqual((s.card_state, s.card_text), ('', ''))
        self.assertIsNotNone(s.left_screen_at)

    def test_every_account_busy_at_the_tune_in_waits_on_the_card_and_takes_the_seat_when_free(self):
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 77))
        _resp, s, _clip = self.on_card()
        self.assertEqual((s.card_state, s.screen_held), (pip.PANE_BUSY, ss.HELD_NO_CONNECTION))
        self.assertEqual(feed.recent_refused(), [], 'a busy account is a card, never a refusal')
        connlim.release(self.account.id, 'recording', 77)
        feed.recheck_waiting(held_only=True)
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_STREAMING and s.channel_id == self.big.id), s)

    def test_a_card_whose_words_change_is_redrawn_in_place(self):
        pip.clear(self.row.id)
        _resp, s, _clip = self.on_card()
        self.assertTrue(wait_for(lambda: not s.screen_still, 10))
        first = s.screen_key
        pip.change(self.row.id, pip.clean({
            'name': 'PiP 1', 'main': src(PIP_SOURCE_SEARCH, query='zebra'),
            'inset': src(PIP_SOURCE_CHANNEL, self.small.id)}))
        self.assertEqual(feed.refresh_pip(self.row.id), (1, 0, 0))
        self.assertTrue(wait_for(lambda: s.card_state == pip.PANE_NO_MATCH and s.screen_key != first, 10), s)
        self.assertTrue(s.live)

    def test_a_picture_whose_only_channel_drops_goes_to_its_card_and_gives_the_seat_back(self):
        resp = self.play()
        self.read(resp)
        s = self.only_session()
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_STREAMING))
        s.proc.kill()
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_CARD, 15), s)
        self.assertTrue(s.live, 'zero channels left is a card, never the end of the play')
        self.assertEqual(s.card_state, pip.PANE_RECONNECTING)
        self.assertEqual(self.seats(), [])
        self.assertIsNone(s.account_id)
        self.assertIn('showed its card', feed.play_notes(s))
        # It tries the channel again by itself at its next turn.
        feed.recheck_waiting()
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_STREAMING and s.channel_id == self.big.id, 15), s)
        self.assertEqual(self.seats(), [('viewer', s.id)])

    def test_a_play_that_left_a_picture_for_its_card_says_so_in_recent_plays(self):
        resp = self.play()
        self.read(resp)
        s = self.only_session()
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_STREAMING))
        s.proc.kill()
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_CARD, 15), s)
        resp.close()
        self.assertTrue(wait_for(lambda: not s.live, 5))
        feed.flush_play_log()
        message = OutputFeedPlay.query.one().message
        self.assertIn('Was on its card for', message)
        self.assertNotIn('Showed only its card', message)

    def test_a_card_that_cannot_be_made_is_refused_by_name(self):
        pip.clear(self.row.id)
        with mock.patch.object(ss, 'render_clip', side_effect=ss.ClipFailed('the folder cannot be written')):
            resp = self.play(expect=409)
        self.assertIn('Its card could not be shown', resp.get_data(as_text=True))
        self.assertEqual(feed.live_sessions(), [])

    def test_recent_plays_says_it_showed_only_its_card(self):
        pip.clear(self.row.id)
        resp, s, _clip = self.on_card()
        resp.close()
        self.assertTrue(wait_for(lambda: not s.live, 5))
        feed.flush_play_log()
        line = OutputFeedPlay.query.one()
        self.assertEqual(line.channel_name, feed.CARD_NAME)
        self.assertIn('Showed only its card', line.message)


class CardTeardownTests(_PipPlay):
    """Every end of a play on its card kills the clip's ffmpeg (CLAUDE.md teardown rule)."""

    def setUp(self):
        super().setUp()
        pip.clear(self.row.id)

    def test_the_player_leaving_stop_and_shutdown(self):
        for end, reason in ((lambda s, r: r.close(), feed.REASON_CLIENT_GONE),
                            (lambda s, r: feed.stop_session(s.id), feed.REASON_STOPPED),
                            (lambda s, r: feed.stop_pip(self.row.id), feed.REASON_TAKEN_OFF),
                            (lambda s, r: feed.kill_all_viewers(), feed.REASON_SHUTDOWN)):
            resp, s, proc = self.on_card()
            end(s, resp)
            self.assert_gone(s, proc, reason)

    def test_taken_off_or_removed_it_ends_at_its_recheck(self):
        def take_off():
            feed.set_on_output(feed.KIND_PIP, self.row.id, False)
            db.session.commit()
        for act in (take_off, lambda: pip.remove(self.row.id)):
            _resp, s, proc = self.on_card()
            act()
            feed.recheck_waiting()
            self.assert_gone(s, proc, feed.REASON_TAKEN_OFF)
            feed.set_on_output(feed.KIND_PIP, self.row.id, True)
            db.session.commit()

    def test_a_clip_ffmpeg_that_dies_ends_the_play_by_name(self):
        _resp, s, proc = self.on_card()
        proc.kill()
        self.assert_gone(s, proc, feed.REASON_CARD_FAILED)


class ClearedWhileWatchedTests(_PipPlay):
    def test_a_play_on_a_picture_goes_to_the_card_and_its_swapped_copy_ends(self):
        self.read(self.play())
        other = make_account('Third', max_connections=1)
        self.small.account_id = other.id
        db.session.commit()
        self.read(self.play(feed.feed_id_for_pip(self.row.id, True)))
        plain, twin = sorted(feed.live_sessions(), key=lambda x: x.feed_id)
        old = plain.proc
        resp = self.client.post(f'/api/iptv-output/pip/{self.row.id}/clear')
        self.assertEqual((resp.get_json()['cut'], resp.get_json()['moved']), (1, 1))
        self.assertEqual(twin.reason, feed.REASON_TAKEN_OFF)
        self.assertTrue(wait_for(lambda: plain.state == feed.STATE_CARD and plain.card_state == pip.PANE_EMPTY, 15),
                        plain)
        self.assertTrue(plain.live)
        self.assertIsNotNone(old.poll(), "the picture's ffmpeg is dead")
        self.assertEqual(self.seats(), [], 'its connection is given back')


class PruneTests(_PipPlay):
    def test_a_removed_channels_clips_are_pruned_and_a_live_ones_kept(self):
        cfg = load_config()
        folder = ss.clip_dir(cfg)
        os.makedirs(folder, exist_ok=True)
        mine = ss.clip_path(cfg, ss.pip_owner(self.row.id), 'a' * 20)
        gone = ss.clip_path(cfg, ss.pip_owner(4242), 'b' * 20)
        for path in (mine, gone):
            open(path, 'wb').close()
        self.assertEqual(ss.prune_clips(cfg), 1)
        self.assertTrue(os.path.exists(mine))
        self.assertFalse(os.path.exists(gone))


class RaceGuardTests(_PipPlay):
    def test_a_dead_ffmpeg_the_reaper_read_earlier_never_fails_over_a_play_on_its_card(self):
        """The reaper reads a play's ffmpeg and judges it a moment later. By then the play may be
        on its card, or on its way there, and a failover beside that would start a second clip."""
        pip.clear(self.row.id)
        _resp, s, proc = self.on_card()
        with mock.patch.object(feed, '_failover') as failover:
            feed._member_died(s, feed.REASON_FFMPEG_EXITED, 'stale')
            s._carding = True
            s.state = feed.STATE_STREAMING
            feed._member_died(s, feed.REASON_FFMPEG_EXITED, 'stale')
            s._carding = False
            s.state = feed.STATE_CARD
        failover.assert_not_called()
        self.assertIs(s.proc, proc)
        self.assertTrue(s.live)

    def test_the_names_of_a_clip_come_from_the_kind_of_play_not_its_state(self):
        pip.clear(self.row.id)
        _resp, s, _proc = self.on_card()
        for state in feed.STATES:
            with mock.patch.object(s, 'state', state):
                self.assertEqual(s.own_picture_name, feed.CARD_NAME, state)
                self.assertEqual(feed._clip_failed_reason(s), feed.REASON_CARD_FAILED, state)

    def test_a_cleared_play_still_launching_is_moved_once_it_has_an_ffmpeg(self):
        self.read(self.play())
        s = self.only_session()
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_STREAMING))
        with feed._lock:
            held, s.proc = s.proc, None      # as inside its launch window
        pip.clear(self.row.id)
        self.assertEqual(feed.clear_pip(self.row.id), 1)
        self.assertFalse(wait_for(lambda: s.state == feed.STATE_CARD, 0.5), 'nothing to take yet')
        with feed._lock:
            s.proc = held
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_CARD and s.card_state == pip.PANE_EMPTY, 15), s)
        self.assertIsNotNone(held.poll())


class MoveByHandTests(_PipPlay):
    def test_a_move_that_finds_nothing_startable_says_it_shows_its_card_not_that_it_ended(self):
        self.plain_channel('Cam two', make_account('Cam acct', max_connections=1))
        row = self.make(name='Cams', main=src(PIP_SOURCE_SEARCH, query='cam'), swapped_copy=False)
        cam = self.plain_channel('Cam one', self.account)
        cam.guide_sort_order = -5
        db.session.commit()
        self.read(self.play(feed.feed_id_for_pip(row.id)))
        s = self.only_session()
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_STREAMING))
        with mock.patch.object(feed, '_launch', side_effect=feed.FeedRefused('will not start', 500)):
            resp = self.client.post(f'/api/iptv-output/plays/{s.id}/move')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('showing its card', resp.get_json()['error'])
        self.assertNotIn('ended', resp.get_json()['error'])
        self.assertTrue(s.live)
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_CARD, 5), s)


class CfgTests(_PipPlay):
    FEED = feed_cfg(max_viewers=1)

    def test_a_tune_in_at_the_cap_waits_on_its_card_and_comes_on_when_a_slot_frees(self):
        first = self.play(feed.feed_id_for_channel(self.small.id))
        self.read(first)
        _resp, s, _clip = self.on_card()
        self.assertEqual((s.card_state, s.screen_held), (pip.PANE_BUSY, ss.HELD_NO_SLOT))
        self.assertEqual(feed.recent_refused(), [])
        first.close()
        self.assertTrue(wait_for(lambda: s.state == feed.STATE_STREAMING and s.channel_id == self.big.id, 15), s)

    def test_a_card_is_not_counted_so_a_plain_channel_still_plays_at_the_cap(self):
        pip.clear(self.row.id)
        self.on_card()
        self.read(self.play(feed.feed_id_for_channel(self.small.id)))
        self.assertEqual(len(feed.live_sessions()), 2)


if __name__ == '__main__':
    import unittest
    unittest.main()
