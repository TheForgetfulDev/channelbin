"""A sports channel's waiting screen through the relay (app/output_feed.py's waiting-screen
section, dev/changelog/1534): a play with no game on is shown ChannelBin's own looped clip
instead of being refused, holds no provider seat and is not counted against "IPTV players at
once", still shows on Watching now, and is moved to the game in place when it comes on (at its
next re-check, or at once on Go live now). A game that is on with no slot or seat free waits on
the screen too. Every end kills the clip's ffmpeg.

The clips are not drawn here: sports_screen.render_clip is replaced by a copy of a small
synthesized TS file, so what is tested is the relay's handling of a clip, not the picture
(tests/test_sports_screen.py covers that).

  python3 -m unittest tests.test_sports_waiting
"""
import os
import shutil
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import clips  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel  # noqa: E402

import app.connection_limits as connlim  # noqa: E402
import app.output_feed as feed  # noqa: E402
import app.sports_screen as ss  # noqa: E402
from app import db, sports_feed  # noqa: E402
from app.database import (Alert, OutputFeedPlay, SportsEvent, SportsFeedChannel,  # noqa: E402
                          SportsFollow, SportsLeague, SportsMatch)

_HAVE_FFMPEG = shutil.which('ffmpeg') is not None
USER, PASS = 'tv', 'tv-secret'


def _cfg(**overrides):
    block = {'enabled': True, 'username': USER, 'password': PASS, 'reserve_seconds': 0}
    block.update(overrides)
    return {'integrations': {'output_feed': block}}


def _wait(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _build(d):
    for name, size in (('screen.ts', '192x108'), ('game.ts', '256x144')):
        subprocess.run(
            ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc=size={size}:rate=30',
             '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '20',
             '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '30', '-bf', '0', '-pix_fmt', 'yuv420p',
             '-c:a', 'aac', '-f', 'mpegts', os.path.join(d, name)],
            check=True, timeout=180)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class _Waiting(unittest.TestCase):
    FEED = _cfg()

    @classmethod
    def setUpClass(cls):
        cls.media = clips.shared('sports-waiting', _build)

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.client
        self.addCleanup(self.t.cleanup)
        self.addCleanup(feed.stop_all)
        self.t.sandbox_config(self.FEED)
        self.account = make_account('Acct', max_connections=1)
        db.session.commit()
        self.renders = []
        self.render_delay = 0.0
        patcher = mock.patch.object(ss, 'render_clip', self._fake_render)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.open = []
        self.addCleanup(self._close_all)
        self.now = datetime.utcnow()
        self.league = SportsLeague(tsdb_id=9001, name='Pretend League', match_terms='[]')
        db.session.add(self.league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=self.league.id, team_tsdb_id=0, rule_id=0))
        # The team's game is tomorrow: nothing on now.
        start = self.now + timedelta(days=1)
        self.event = SportsEvent(tsdb_id=7001, league_id=self.league.id, name='g', home_team_id=101,
                                 away_team_id=102, home_name='Ironvale Bears', away_name='Harborview Gulls',
                                 start_at=start, local_date=start.date())
        db.session.add(self.event)
        db.session.commit()
        sports_feed.set_team_channel(self.league.id, 101, True)
        self.feed_id = feed.feed_id_for_sports(SportsFeedChannel.query.one().id)

    def _fake_render(self, cfg, screen, path, *, still=False, background=False):
        self.renders.append((path, still, screen))
        if self.render_delay and not still:
            time.sleep(self.render_delay)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(os.path.join(self.media, 'screen.ts'), path)

    def _close_all(self):
        for resp in self.open:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - a response the test already closed
                pass

    def play(self, feed_id=None, expect=200):
        resp = self.client.get(f'/live/{USER}/{PASS}/{feed_id or self.feed_id}.ts')
        self.assertEqual(resp.status_code, expect, resp.get_data(as_text=True)[:300] if expect != 200 else '')
        if expect == 200:
            self.open.append(resp)
        return resp

    @staticmethod
    def read(resp, n=1):
        it = iter(resp.response)
        return [next(it) for _ in range(n)]

    def only_session(self):
        live = feed.live_sessions()
        self.assertEqual(len(live), 1, live)
        return live[0]

    def carrier(self, name='Local game', account=None, start=None):
        """A channel whose guide lists the game, from `start` (now by default)."""
        start = start or (datetime.utcnow() - timedelta(minutes=5))
        ch = make_channel(account or self.account, name=name, pace_realtime=True)
        ch.stream_url = os.path.join(self.media, 'game.ts')
        db.session.flush()
        db.session.add(SportsMatch(event_id=self.event.id, channel_id=ch.id, kind='guide', strength='strong',
                                   airing='first', program_start_time=start,
                                   program_stop_time=start + timedelta(hours=3), title_key='g', reason='r'))
        db.session.commit()
        return ch

    def game_comes_on(self, **kw):
        """The game starts now, and a channel's guide lists it."""
        start = datetime.utcnow() - timedelta(minutes=5)
        self.event.start_at = start
        self.event.local_date = start.date()
        db.session.commit()
        return self.carrier(start=start, **kw)

    def plain_channel(self, name='Plain', account=None):
        ch = make_channel(account or make_account(f'{name} acct', max_connections=1), name=name, pace_realtime=True)
        ch.stream_url = os.path.join(self.media, 'game.ts')
        ch.in_guide = True
        db.session.commit()
        return ch


class WaitingScreenTests(_Waiting):

    def test_nothing_on_plays_the_screen_with_no_seat_instead_of_a_refusal(self):
        resp = self.play()
        self.assertEqual(resp.mimetype, 'video/mp2t')
        chunk = self.read(resp)[0]
        self.assertEqual(chunk[0], 0x47, 'a TS sync byte leads the first chunk')
        s = self.only_session()
        self.assertEqual(s.state, feed.STATE_WAITING)
        self.assertIsNone(s.account_id)
        self.assertIsNone(s.channel_id)
        self.assertEqual(s.waiting_for, 'Harborview Gulls at Ironvale Bears')
        self.assertEqual(s.screen_state, ss.STATE_NEXT)
        self.assertEqual(connlim._holders.get(self.account.id, []), [])
        self.assertEqual(feed.recent_refused(), [], 'a channel between games is not refused')
        self.assertIn('-readrate', s.proc.args)
        self.assertIn('-stream_loop', s.proc.args)

    def test_the_still_plays_first_and_the_moving_clip_is_swapped_in_when_made(self):
        self.render_delay = 0.5
        resp = self.play()
        s = self.only_session()
        self.assertTrue(s.screen_still)
        still_path, still_proc = s.screen_path, s.proc
        self.assertTrue(_wait(lambda: not s.screen_still, 10), s)
        self.assertNotEqual(s.screen_path, still_path)
        self.assertIsNotNone(still_proc.poll(), "the still's ffmpeg is gone after the swap")
        # The swap flips screen_still before it launches the new clip and deletes the old one
        # (output_feed._swap_clip), so the delete is waited for, not read once
        # (dev/docs/BUGS.md 2026-11-14 12:47 AM).
        self.assertTrue(_wait(lambda: not os.path.exists(still_path), 10),
                        'a still nothing plays is deleted once replaced')
        self.assertEqual([r[1] for r in self.renders], [True, False])
        self.assertTrue(s.live)
        before = s.bytes_sent
        self.read(resp, 2)
        self.assertGreater(s.bytes_sent, before)

    def test_a_made_clip_is_reused_by_the_next_tune_in(self):
        self.play()
        s = self.only_session()
        self.assertTrue(_wait(lambda: not s.screen_still, 10))
        self.play()
        self.assertEqual(len(self.renders), 2, 'the second tune-in made nothing')
        second = [x for x in feed.live_sessions() if x is not s][0]
        self.assertFalse(second.screen_still)
        self.assertEqual(second.screen_path, s.screen_path)

    def test_not_counted_at_the_cap_and_a_plain_channel_still_plays(self):
        self.t.sandbox_config(_cfg(max_viewers=1))
        self.play()
        self.play()
        plain = self.plain_channel()
        with mock.patch.object(feed, 'SWITCH_GRACE_SECONDS', 0.2):
            self.play(feed.feed_id_for_channel(plain.id))
        states = sorted(s.state for s in feed.live_sessions())
        self.assertEqual(states.count(feed.STATE_WAITING), 2)
        self.assertEqual(len(states), 3)

    def test_watching_now_and_the_dashboard_say_it_is_on_the_waiting_screen(self):
        self.play()
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('Waiting screen', html)
        self.assertIn('Waiting for Harborview Gulls at Ironvale Bears', html)
        self.assertIn('Not counted against the limit', html)
        dash = self.client.get('/').get_data(as_text=True)
        self.assertIn('WAITING SCREEN', dash)
        self.assertIn('waiting for Harborview Gulls at Ironvale Bears', dash)
        self.assertIn('not counted against the limit', dash)

    def test_a_play_that_only_watched_the_screen_says_so_in_recent_plays(self):
        resp = self.play()
        s = self.only_session()
        resp.close()
        self.assertTrue(_wait(lambda: not s.live, 5))
        feed.flush_play_log()
        line = OutputFeedPlay.query.one()
        self.assertIsNone(line.channel_id)
        self.assertIn('Watched only the waiting screen', line.message)
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('Played the waiting screen for', html)

    def test_a_screen_that_cannot_be_made_is_refused_by_name(self):
        with mock.patch.object(ss, 'render_clip', side_effect=ss.ClipFailed('the folder is gone')):
            resp = self.play(expect=409)
        self.assertIn('Its waiting screen could not be shown: the folder is gone', resp.get_json()['error'])
        self.assertEqual(feed.live_sessions(), [])

    def test_what_went_wrong_with_the_screen_is_said_on_the_play_once(self):
        """dev/docs/BUGS.md 2026-11-16 (said only in the log): an unreadable badge and a moving clip
        that could not be made each wrote a WARNING and reached no page."""
        render = self._fake_render

        def still_only(cfg, screen, path, *, still=False, background=False):
            if not still:
                raise ss.ClipFailed('ffmpeg could not make it')
            render(cfg, screen, path, still=still, background=background)

        with mock.patch.object(ss, 'render_clip', still_only), \
                mock.patch.object(ss, 'unread_badges', return_value=[('Ironvale Bears', 'IRO')]):
            resp = self.play()
            self.read(resp)
            s = self.only_session()
            self.assertTrue(_wait(lambda: 'moving waiting screen' in feed.play_notes(s), 10), s.notes)
            # A redraw of the same screen says neither a second time.
            feed._note_badges(s, self.renders[0][2])
        self.assertEqual(len(s.notes), 2, s.notes)
        self.assertIn('The badge for Ironvale Bears could not be read as a picture, so the waiting '
                      'screen shows "IRO" in its place.', s.notes)
        self.assertIn('stayed on (ffmpeg could not make it)', feed.play_notes(s))
        self.assertTrue(s.live, 'neither costs the play its screen')
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('The badge for Ironvale Bears could not be read', html)
        self.assertIn('The moving waiting screen could not be made', html)
        resp.close()
        self.assertTrue(_wait(lambda: not s.live, 5))
        feed.flush_play_log()
        line = OutputFeedPlay.query.one()
        self.assertIn('The badge for Ironvale Bears', line.message)
        self.assertIn('The moving waiting screen could not be made', line.message)

    def test_a_clip_ffmpeg_that_dies_ends_the_play_by_name(self):
        self.play()
        s = self.only_session()
        s.proc.kill()
        self.assertTrue(_wait(lambda: not s.live, 10))
        self.assertEqual(s.reason, feed.REASON_SCREEN_FAILED)


class WaitingScreenToGameTests(_Waiting):

    def test_the_game_coming_on_swaps_in_place_and_takes_a_seat(self):
        resp = self.play()
        self.read(resp)
        s = self.only_session()
        clip_proc = s.proc
        self.assertTrue(_wait(lambda: not s.screen_still, 10))
        moving = s.screen_path
        ch = self.game_comes_on()
        feed.recheck_waiting()
        self.assertTrue(_wait(lambda: s.channel_id == ch.id and s.state == feed.STATE_STREAMING, 15), s)
        self.assertTrue(s.live)
        self.assertEqual(connlim._holders.get(self.account.id), [('viewer', s.id)])
        self.assertIsNotNone(s.left_screen_at)
        self.assertEqual(s.event_id, self.event.id)
        self.assertIn('Harborview Gulls at Ironvale Bears', s.label)
        self.assertEqual(s.ring.reserve, 0.0)
        self.assertIsNotNone(clip_proc.poll())
        self.assertFalse(os.path.exists(moving), "the screen's clip is deleted once nothing plays it")
        before = s.bytes_sent
        self.read(resp, 3)
        self.assertGreater(s.bytes_sent, before)
        resp.close()
        self.assertTrue(_wait(lambda: not s.live, 5))
        feed.flush_play_log()
        self.assertIn('before the game came on', OutputFeedPlay.query.one().message)

    def test_the_game_takes_the_configured_reserve_when_it_comes_on(self):
        self.t.sandbox_config(_cfg(reserve_seconds=2))
        self.read(self.play())
        s = self.only_session()
        self.assertEqual(s.ring.reserve, 0.0, 'the screen plays on no reserve')
        self.game_comes_on()
        feed.recheck_waiting()
        self.assertTrue(_wait(lambda: s.state != feed.STATE_WAITING, 15))
        self.assertEqual(s.ring.reserve, 2.0)

    def test_no_slot_keeps_it_on_the_screen_saying_so_until_one_frees(self):
        self.t.sandbox_config(_cfg(max_viewers=1))
        self.play()
        s = self.only_session()
        plain = self.plain_channel()
        other = self.play(feed.feed_id_for_channel(plain.id))
        self.read(other)
        self.game_comes_on()
        feed.recheck_waiting()
        self.assertTrue(_wait(lambda: s.screen_held == ss.HELD_NO_SLOT, 10), s)
        self.assertEqual(s.state, feed.STATE_WAITING)
        self.assertEqual(s.screen_state, ss.STATE_HELD)
        self.assertEqual(connlim._holders.get(self.account.id, []), [])
        from html import unescape
        self.assertIn('one of the "IPTV players at once" slots',
                      unescape(self.client.get('/iptv-output').get_data(as_text=True)))
        other.close()
        # The plain play's end asks the held screen again at once, not at its next turn.
        with mock.patch.object(feed, 'SCREEN_RECHECK_SECONDS', 3600):
            self.assertTrue(_wait(lambda: s.state != feed.STATE_WAITING, 15), s)
        self.assertEqual(connlim._holders.get(self.account.id), [('viewer', s.id)])

    def test_no_seat_keeps_it_on_the_screen_saying_so(self):
        self.read(self.play())
        s = self.only_session()
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)
        self.game_comes_on()
        feed.recheck_waiting()
        self.assertTrue(_wait(lambda: s.screen_held == ss.HELD_NO_CONNECTION, 10), s)
        self.assertEqual(s.state, feed.STATE_WAITING)
        self.assertEqual(connlim._holders.get(self.account.id), [('recording', 4242)])

    def test_a_recording_taking_the_seat_as_the_game_comes_on_leaves_the_play_on_the_screen(self):
        """dev/docs/BUGS.md 2026-11-15 01:36 PM: a play on the screen has no account, so a recording that
        took its just-acquired seat found no play to end, and the play went on to start the game on
        the connection the recording held, with no seat."""
        self.read(self.play())
        s = self.only_session()
        ch = self.game_comes_on()
        seat_for = feed._seat_for

        def seat_then_a_recording_takes_it(session, member, event_id, cfg, refused=()):
            member = seat_for(session, member, event_id, cfg, refused)
            # What recorder._try_acquire_slot_with_preemption does when its own acquire fails.
            for _id, holder_account in connlim.preempt_viewers_for_slot(self.account.id):
                feed.preempt_for_account(self.t.app, holder_account, 4242)
            self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
            return member

        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)
        with mock.patch.object(feed, '_seat_for', seat_then_a_recording_takes_it):
            feed.recheck_waiting()
            self.assertTrue(_wait(lambda: s.screen_held == ss.HELD_NO_CONNECTION or not s.waiting, 15), s)
        self.assertEqual(s.state, feed.STATE_WAITING, 'the game must not start on a seat it does not hold')
        self.assertNotEqual(s.channel_id, ch.id)
        self.assertIsNone(s.account_id)
        self.assertFalse(s.preempted, 'a play that held nothing is not cut as preempted')
        self.assertEqual(connlim._holders.get(self.account.id), [('recording', 4242)])

    def test_a_seat_lost_as_a_play_moves_to_another_account_ends_it_rather_than_relaying(self):
        """dev/docs/BUGS.md 2026-11-15 01:36 PM, the failover's half: the seat is moved to the new
        account before the session names it, and a recording that stripped it in between found
        no play to end."""
        self.game_comes_on()
        self.read(self.play())
        s = self.only_session()
        self.assertTrue(_wait(lambda: s.state == feed.STATE_STREAMING, 15), s)
        other = make_account('Other', max_connections=1)
        second = self.carrier(name='Second carrier', account=other, start=datetime.utcnow() - timedelta(minutes=5))

        def moved_then_a_recording_takes_it(session, member, first, deadline=0.0):
            self.assertTrue(connlim.move_seat('viewer', session.id, session.account_id, member.account_id))
            for _id, holder_account in connlim.preempt_viewers_for_slot(other.id):
                feed.preempt_for_account(self.t.app, holder_account, 4242)
            self.assertTrue(connlim.try_acquire(other.id, 'recording', 4242))
            return True

        self.addCleanup(connlim.release, other.id, 'recording', 4242)
        with mock.patch.object(feed, '_take_seat', moved_then_a_recording_takes_it):
            with self.assertRaises(feed.FeedRefused):
                feed._launch(s, second, None, first=False)
        self.assertFalse(s.live)
        self.assertEqual(s.reason, feed.REASON_PREEMPTED)
        self.assertEqual(connlim._holders.get(other.id), [('recording', 4242)])

    def test_a_game_put_on_from_the_screen_gets_its_channels_own_pacing(self):
        """dev/docs/BUGS.md 2026-11-15 01:36 PM: the pacing answer was resolved only at a tune-in that
        started on a channel, so a game reached from the screen always ran unpaced."""
        self.read(self.play())
        s = self.only_session()
        self.assertFalse(s.pace_realtime)
        ch = self.game_comes_on()      # the rig's channels have pace_realtime on
        feed.recheck_waiting()
        self.assertTrue(_wait(lambda: s.channel_id == ch.id, 15), s)
        self.assertTrue(s.pace_realtime)

    def test_a_tune_in_to_a_game_with_no_seat_waits_on_the_screen(self):
        self.game_comes_on()
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)
        self.play()
        s = self.only_session()
        self.assertEqual(s.state, feed.STATE_WAITING)
        self.assertEqual(s.screen_held, ss.HELD_NO_CONNECTION)
        self.assertEqual(Alert.query.filter_by(alert_type='VIEWER_REFUSED').count(), 0)

    def test_a_tune_in_tries_every_carrier_before_the_screen(self):
        """dev/docs/BUGS.md 2026-11-16 (the tune-in walk): a tune-in asked one seat, on the best-ranked
        carrier, and went to the screen for 30 seconds with another carrier's account free."""
        busy = self.game_comes_on()
        busy.health_score = 90.0
        other = make_account('Other', max_connections=1)
        free = self.carrier(name='Second carrier', account=other, start=datetime.utcnow() - timedelta(minutes=5))
        free.health_score = 10.0
        db.session.commit()
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)
        self.play()
        s = self.only_session()
        self.assertFalse(s.waiting, 'the game starts at once on the carrier with a free connection')
        self.assertEqual(s.channel_id, free.id)
        self.assertEqual(s.account_id, other.id)
        self.assertEqual(connlim._holders.get(other.id), [('viewer', s.id)])
        self.assertEqual(connlim._holders.get(self.account.id), [('recording', 4242)])
        self.assertIn('"Second carrier"', feed.play_notes(s))
        self.assertIn('no connection free', feed.play_notes(s))

    def test_a_seat_the_walk_found_and_a_recording_took_leaves_the_play_on_the_screen(self):
        """The walk's seat is on another account than the one the play was registered under, so it
        is confirmed with the account named (dev/docs/BUGS.md 2026-11-15 01:36 PM)."""
        busy = self.game_comes_on()
        busy.health_score = 90.0
        other = make_account('Other', max_connections=1)
        free = self.carrier(name='Second carrier', account=other, start=datetime.utcnow() - timedelta(minutes=5))
        free.health_score = 10.0
        db.session.commit()
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)
        seat_for = feed._seat_for

        def seat_then_a_recording_takes_it(session, member, event_id, cfg, refused=()):
            got = seat_for(session, member, event_id, cfg, refused)
            for _id, holder_account in connlim.preempt_viewers_for_slot(other.id):
                feed.preempt_for_account(self.t.app, holder_account, 4343)
            self.assertTrue(connlim.try_acquire(other.id, 'recording', 4343))
            return got

        self.addCleanup(connlim.release, other.id, 'recording', 4343)
        with mock.patch.object(feed, '_seat_for', seat_then_a_recording_takes_it):
            self.play()
        s = self.only_session()
        self.assertEqual(s.state, feed.STATE_WAITING)
        self.assertEqual(s.screen_held, ss.HELD_NO_CONNECTION)
        self.assertEqual(connlim._holders.get(other.id), [('recording', 4343)])

    def test_a_tune_in_to_a_game_at_the_cap_waits_on_the_screen(self):
        self.t.sandbox_config(_cfg(max_viewers=1))
        self.game_comes_on()
        plain = self.plain_channel()
        self.read(self.play(feed.feed_id_for_channel(plain.id)))
        with mock.patch.object(feed, 'SWITCH_GRACE_SECONDS', 0.2):
            self.play()
        s = [x for x in feed.live_sessions() if x.waiting][0]
        self.assertEqual(s.screen_held, ss.HELD_NO_SLOT)

    def test_a_seat_refused_on_one_carrier_tries_the_next(self):
        self.read(self.play())
        s = self.only_session()
        busy = self.game_comes_on()
        busy.health_score = 90.0
        other = make_account('Other', max_connections=1)
        free = self.carrier(name='Second carrier', account=other, start=datetime.utcnow() - timedelta(minutes=5))
        free.health_score = 10.0
        db.session.commit()
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)
        feed.recheck_waiting()
        self.assertTrue(_wait(lambda: s.channel_id == free.id, 15), s)

    def test_go_live_now_moves_the_play_at_once(self):
        start = datetime.utcnow() + timedelta(hours=3)
        self.event.start_at = start
        db.session.commit()
        ch = self.carrier(start=start)
        self.read(self.play())
        s = self.only_session()
        self.assertEqual(s.state, feed.STATE_WAITING)
        with mock.patch.object(feed, 'SCREEN_RECHECK_SECONDS', 3600):
            resp = self.client.put(f'/api/sports/events/{self.event.id}/live-now', json={'on': True})
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            self.assertTrue(_wait(lambda: s.channel_id == ch.id, 10), s)

    def test_a_game_listed_only_on_a_blocked_account_still_refuses_with_the_alert(self):
        from app.database import AccountBlock
        self.game_comes_on()
        db.session.add(AccountBlock(account_id=self.account.id, start_time=datetime.utcnow() - timedelta(minutes=1),
                                    stop_time=datetime.utcnow() + timedelta(hours=1)))
        db.session.commit()
        resp = self.play(expect=409)
        self.assertIn('blocked account', resp.get_json()['error'])
        self.assertEqual(Alert.query.filter_by(alert_type='FEED_NO_MEMBER').count(), 1)
        self.assertEqual(feed.live_sessions(), [])


class RedirectModeWaitingTests(_Waiting):
    """Redirect mode: the screen is ChannelBin's own stream there too, and it ends when the game's
    window opens, since ChannelBin cannot put a provider stream on in this mode."""
    FEED = _cfg(mode='redirect')

    def test_nothing_on_streams_the_screen_with_the_ticker_instead_of_a_302(self):
        resp = self.play()
        self.assertEqual(resp.mimetype, 'video/mp2t')
        self.read(resp)
        s = self.only_session()
        self.assertEqual(s.state, feed.STATE_WAITING)
        self.assertIsNone(s.account_id)
        self.assertEqual(connlim._holders.get(self.account.id, []), [])
        self.assertEqual(feed.recent_redirects(), [])
        self.assertTrue(_wait(lambda: not s.screen_still, 10))
        for _path, _still, screen in self.renders:
            self.assertEqual(screen.mode, ss.MODE_REDIRECT)
            self.assertTrue(screen.words.ticker, 'redirect mode says it cannot tune the player in')

    def test_it_ends_when_the_window_opens_and_the_next_tune_in_is_the_302(self):
        resp = self.play()
        self.read(resp)
        s = self.only_session()
        proc = s.proc
        ch = self.game_comes_on()
        feed.recheck_waiting()
        self.assertTrue(_wait(lambda: not s.live, 10), s)
        self.assertEqual(s.reason, feed.REASON_WINDOW_OPENED)
        self.assertIsNotNone(proc.poll())
        again = self.client.get(f'/live/{USER}/{PASS}/{self.feed_id}.ts')
        self.assertEqual(again.status_code, 302)
        self.assertEqual(again.headers['Location'], ch.stream_url)

    def test_go_live_now_ends_it_at_once(self):
        start = datetime.utcnow() + timedelta(hours=3)
        self.event.start_at = start
        db.session.commit()
        self.carrier(start=start)
        self.read(self.play())
        s = self.only_session()
        with mock.patch.object(feed, 'SCREEN_RECHECK_SECONDS', 3600):
            resp = self.client.put(f'/api/sports/events/{self.event.id}/live-now', json={'on': True})
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            self.assertTrue(_wait(lambda: not s.live, 10), s)
        self.assertEqual(s.reason, feed.REASON_WINDOW_OPENED)

    def test_a_game_in_its_window_still_gets_the_302(self):
        ch = self.game_comes_on()
        resp = self.client.get(f'/live/{USER}/{PASS}/{self.feed_id}.ts')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers['Location'], ch.stream_url)
        self.assertEqual(feed.live_sessions(), [])

    def test_watching_now_shows_the_one_play_it_can_see(self):
        self.play()
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('Waiting screen', html)
        self.assertIn('The one play it can see is a sports channel', html)
        self.assertIn('stops when the game comes on', html)
        dash = self.client.get('/').get_data(as_text=True)
        self.assertIn('WAITING SCREEN', dash)
        self.assertIn('it stops when the game comes on', dash)


class MixedModeWaitingTests(_Waiting):
    """Mixed mode relays a sports channel (dev/changelog/1536): its screen says the game starts by
    itself and does, in place, while a plain channel beside it is still a 302."""
    FEED = _cfg(mode='mixed')

    def test_the_screen_hands_over_in_place_and_says_it_will(self):
        resp = self.play()
        self.read(resp)
        s = self.only_session()
        self.assertTrue(_wait(lambda: not s.screen_still, 10))
        for _path, _still, screen in self.renders:
            self.assertEqual(screen.mode, ss.MODE_PROXY)
            self.assertFalse(screen.words.ticker, 'a relayed screen has no "tune in again" ticker')
        ch = self.game_comes_on()
        feed.recheck_waiting()
        self.assertTrue(_wait(lambda: s.channel_id == ch.id and s.state == feed.STATE_STREAMING, 15), s)
        self.assertTrue(s.live, 'never ended WINDOW_OPENED: the game came on in the same play')
        self.assertEqual(connlim._holders.get(self.account.id), [('viewer', s.id)])

    def test_a_plain_channel_beside_it_is_redirected(self):
        plain = self.plain_channel()
        resp = self.client.get(f'/live/{USER}/{PASS}/{feed.feed_id_for_channel(plain.id)}.ts')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers['Location'], plain.stream_url)


class WaitingScreenTeardownTests(_Waiting):
    """Every end of a play on the screen kills the clip's ffmpeg (CLAUDE.md teardown rule)."""

    def _on_screen(self):
        resp = self.play()
        self.read(resp)
        s = self.only_session()
        return resp, s, s.proc

    def assert_gone(self, s, proc, reason):
        self.assertTrue(_wait(lambda: not s.live, 10), s)
        self.assertEqual(s.reason, reason)
        self.assertIsNotNone(proc.poll(), "the clip's ffmpeg is dead")
        self.assertFalse(os.path.exists(s.stderr_path))

    def test_the_player_leaving(self):
        resp, s, proc = self._on_screen()
        resp.close()
        self.assert_gone(s, proc, feed.REASON_CLIENT_GONE)

    def test_shutdown_also_stops_a_clip_being_made(self):
        _resp, s, proc = self._on_screen()
        with mock.patch.object(ss, 'stop_makes') as stop_makes:
            feed.kill_all_viewers()
        stop_makes.assert_called_once_with()
        self.assert_gone(s, proc, feed.REASON_SHUTDOWN)

    def test_stop_from_the_page_and_stop_feed_id(self):
        _resp, s, proc = self._on_screen()
        self.assertTrue(feed.stop_session(s.id))
        self.assert_gone(s, proc, feed.REASON_STOPPED)
        resp = self.play()
        s2 = [x for x in feed.live_sessions()][0]
        proc2 = s2.proc
        self.read(resp)
        self.assertEqual(feed.stop_feed_id(self.feed_id, feed.REASON_TAKEN_OFF), 1)
        self.assert_gone(s2, proc2, feed.REASON_TAKEN_OFF)

    def test_stop_account_never_matches_a_play_with_no_account(self):
        _resp, s, _proc = self._on_screen()
        self.assertEqual(feed.stop_account(self.account.id), 0)
        self.assertTrue(s.live)

    def test_the_channel_no_longer_served_ends_the_play_at_its_recheck(self):
        _resp, s, proc = self._on_screen()
        sports_feed.set_team_channel(self.league.id, 101, False)
        feed.recheck_waiting()
        self.assert_gone(s, proc, feed.REASON_SCREEN_GONE)


class TurnedOffTests(_Waiting):
    """dev/docs/BUGS.md 2026-11-16 (turning a sports channel off): a person's act that stops a
    sports channel being served ends its plays at once, on a game or on the screen, as taking a
    channel or a group off the output does. A rule's follow ending is not that act."""

    def _on_game(self):
        ch = self.game_comes_on()
        resp = self.play()
        self.read(resp)
        s = self.only_session()
        self.assertEqual(s.channel_id, ch.id)
        return s, s.proc

    def assert_cut(self, s, proc):
        self.assertFalse(s.live)
        self.assertEqual(s.reason, feed.REASON_TAKEN_OFF)
        self.assertIsNotNone(proc.poll(), 'its ffmpeg is dead')
        self.assertEqual(connlim._holders.get(self.account.id, []), [])

    def test_the_switch_going_off_cuts_a_play_on_a_game(self):
        s, proc = self._on_game()
        resp = self.client.put(f'/api/sports/leagues/{self.league.id}/feed-channels',
                               json={'team_id': 101, 'enabled': False})
        self.assertEqual(resp.get_json(), {'success': True, 'cut': 1})
        self.assert_cut(s, proc)

    def test_the_switch_going_off_cuts_a_play_on_the_screen_at_once(self):
        self.read(self.play())
        s = self.only_session()
        proc = s.proc
        resp = self.client.put(f'/api/sports/leagues/{self.league.id}/feed-channels',
                               json={'team_id': 101, 'enabled': False})
        self.assertEqual(resp.get_json()['cut'], 1)
        self.assert_cut(s, proc)

    def test_the_switch_going_on_cuts_nothing(self):
        s, _proc = self._on_game()
        resp = self.client.put(f'/api/sports/leagues/{self.league.id}/feed-channels',
                               json={'team_id': 101, 'enabled': True})
        self.assertEqual(resp.get_json(), {'success': True, 'cut': 0})
        self.assertTrue(s.live)

    def test_stop_following_cuts_it(self):
        s, proc = self._on_game()
        resp = self.client.delete(f'/api/sports/leagues/{self.league.id}/following')
        self.assertEqual(resp.get_json()['cut'], 1)
        self.assert_cut(s, proc)

    def test_following_other_teams_only_cuts_it(self):
        s, proc = self._on_game()
        resp = self.client.put(f'/api/sports/leagues/{self.league.id}/following', json={'team_ids': [102]})
        self.assertEqual(resp.get_json()['cut'], 1)
        self.assert_cut(s, proc)

    def test_a_follow_that_still_covers_the_team_cuts_nothing(self):
        s, _proc = self._on_game()
        resp = self.client.put(f'/api/sports/leagues/{self.league.id}/following', json={'team_ids': [101]})
        self.assertEqual(resp.get_json()['cut'], 0)
        self.assertTrue(s.live)

    def test_deleting_the_league_cuts_it(self):
        s, proc = self._on_game()
        resp = self.client.delete(f'/api/sports/leagues/{self.league.id}')
        self.assertEqual(resp.get_json()['cut'], 1)
        self.assert_cut(s, proc)

    def test_a_game_left_on_by_a_rules_follow_ending_survives_an_act_about_something_else(self):
        """A rule's follow ending cuts nothing, and the game it left playing is not cut by a
        person's later act that did not stop it being served: only what an act itself stopped
        is cut."""
        from app import sports_data
        SportsFollow.query.delete()
        db.session.add(SportsFollow(league_id=self.league.id, team_tsdb_id=0, rule_id=5))
        db.session.add(SportsFollow(league_id=self.league.id, team_tsdb_id=102, rule_id=0))
        db.session.commit()
        sports_feed.set_team_channel(self.league.id, 102, True)
        s, _proc = self._on_game()
        sports_data.set_rule_follow(5, None, None, datetime.utcnow())
        db.session.commit()
        self.assertNotIn(self.feed_id, feed.sports_served_now())
        self.assertTrue(s.live, 'a game that is on is never cut for a rule ending')
        resp = self.client.put(f'/api/sports/leagues/{self.league.id}/feed-channels',
                               json={'team_id': 102, 'enabled': False})
        self.assertEqual(resp.get_json(), {'success': True, 'cut': 0})
        self.assertTrue(s.live, "another channel's switch must not cut it")

    def test_one_read_before_and_one_after_however_many_plays_and_none_with_no_play(self):
        with mock.patch.object(sports_feed, 'served_channels', wraps=sports_feed.served_channels) as served:
            self.assertIsNone(feed.sports_served_now())
            self.assertEqual(feed.stop_unserved_sports(None), 0)
            self.assertEqual(served.call_count, 0)
            self.t.sandbox_config(_cfg(max_viewers=5))
            for _ in range(3):
                self.read(self.play())
            served.reset_mock()     # a tune-in reads what is served too
            before = feed.sports_served_now()
            self.assertEqual(feed.stop_unserved_sports(before), 0)
        self.assertEqual(served.call_count, 2)

    def test_a_badge_question_that_raises_never_costs_the_play_its_screen(self):
        with mock.patch.object(ss, 'unread_badges', side_effect=MemoryError('a hostile file')):
            with self.assertLogs('app.output_feed', level='WARNING'):
                self.read(self.play())
        s = self.only_session()
        self.assertTrue(s.live)
        self.assertIsNotNone(s.proc)
        self.assertEqual(s.notes, [])


class AdoptClaimTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-11-13: an ffmpeg started while _finish() was tearing the play down
    was adopted, because the session reads as live until the teardown is done; nothing then
    killed it."""

    def test_a_session_being_finished_never_adopts_a_new_ffmpeg(self):
        session = feed.RelaySession(id='x', feed_id=2, label='l', client='c', group_id=None, channel_id=1,
                                    channel_name='n', account_id=1, ring=feed.Ring(feed.READ_CHUNK, 0))
        session._finishing = True
        proc = subprocess.Popen(['sleep', '30'], stdout=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertIsNone(feed._adopt(session, proc))
        self.assertIsNotNone(proc.wait(timeout=5))
        self.assertIsNone(session.proc)


if __name__ == '__main__':
    unittest.main()
