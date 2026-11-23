"""A browser preview of one of ChannelBin's own channels on the IPTV output (app/preview.py's feed
session, app/output_feed.py's source marks, dev/changelog/1645).

What a real ffmpeg proves here: the preview is a real play - listed as ChannelBin's preview,
holding a viewer seat only while it carries a game, none on the waiting screen - its segmenter
starts a new round at every source mark (a failover, the waiting screen handing over to the
game), a round's playlist and segments are served only while it is the current one, a player is
never sent a mark, and every end stops the round's ffmpeg and the play and names why. The
waiting screen's clip is a copy of a synthesized file (sports_screen.render_clip is replaced), as
in tests/test_sports_waiting.py.

  python3 -m unittest tests.test_feed_preview
"""
import os
import re
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
from tests.support.seed import make_account, make_channel, make_group, make_guide_match  # noqa: E402

import app.connection_limits as connlim  # noqa: E402
import app.output_feed as feed  # noqa: E402
import app.preview as preview  # noqa: E402
import app.sports_screen as ss  # noqa: E402
from app import db, sports_feed  # noqa: E402
from app.database import Alert, SportsEvent, SportsFeedChannel, SportsFollow, SportsLeague  # noqa: E402
from app.proc_utils import build_feed_preview_cmd, feed_preview_names  # noqa: E402

_HAVE_FFMPEG = shutil.which('ffmpeg') is not None
USER, PASS = 'tv', 'tv-secret'


def _cfg(**overrides):
    block = {'enabled': True, 'username': USER, 'password': PASS, 'reserve_seconds': 0}
    block.update(overrides)
    return {'integrations': {'output_feed': block}}


def _wait(predicate, timeout=20.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _build(d):
    for name, size, audio in (('a.ts', '192x108', 'aac'), ('b.ts', '256x144', 'aac'),
                              ('ac3.ts', '192x108', 'ac3'), ('screen.ts', '160x90', 'aac')):
        subprocess.run(
            ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc=size={size}:rate=30',
             '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '40',
             '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '30', '-bf', '0', '-pix_fmt', 'yuv420p',
             '-c:a', audio, '-f', 'mpegts', os.path.join(d, name)],
            check=True, timeout=180)


class SourceMarkTests(unittest.TestCase):
    """The mark between two sources in a play's ring: zero-length, in order, never sent to a player."""

    def test_a_mark_is_released_in_order_and_counts_nothing(self):
        ring = feed.Ring(1024 * 1024, 0.0)
        ring.append(b'one')
        ring.mark()
        ring.append(b'two')
        self.assertEqual(ring.size, 6)
        self.assertEqual(ring.total_in, 6)
        got = [ring.take(0.1) for _ in range(3)]
        self.assertEqual(got[0], b'one')
        self.assertIs(got[1], feed.SOURCE_MARK)
        self.assertEqual(got[2], b'two')

    def _session(self):
        ring = feed.Ring(1024 * 1024, 0.0)
        s = feed.RelaySession(id='x', feed_id=1, label='l', client='c', group_id=None, channel_id=1,
                              channel_name='n', account_id=None, ring=ring)
        return s

    def test_a_player_is_never_sent_a_mark(self):
        s = self._session()
        s.ring.append(b'one')
        s.ring.mark()
        s.ring.append(b'two')
        body = feed.stream(s)
        self.assertEqual(next(body), b'one')
        self.assertEqual(next(body), b'two')
        body.close()

    def test_the_preview_is_handed_the_mark(self):
        s = self._session()
        s.ring.append(b'one')
        s.ring.mark()
        body = feed.stream(s, marks=True)
        self.assertEqual(next(body), b'one')
        self.assertIs(next(body), feed.SOURCE_MARK)
        body.close()


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class OneSegmenterCannotFollowACodecChangeTests(unittest.TestCase):
    """Why the preview starts a new round at each mark: measured with ffmpeg 7.1, one segmenter
    over an AAC source followed by an AC3 one labels the AC3 audio as AAC, while a round per source
    gives each its own. A characterization of ffmpeg, kept so a later ffmpeg that copes shows up."""

    @classmethod
    def setUpClass(cls):
        cls.media = clips.shared('feed-preview', _build)

    def _relay_bytes(self, name):
        return subprocess.run(['ffmpeg', '-v', 'error', '-i', os.path.join(self.media, name), '-t', '6',
                               '-map', '0:v:0?', '-map', '0:a:0?', '-c', 'copy', '-f', 'mpegts', 'pipe:1'],
                              check=True, capture_output=True, timeout=60).stdout

    def _audio_codecs(self, out, round_no):
        _playlist, pattern = feed_preview_names(round_no)
        found = set()
        prefix = pattern.split('%')[0]
        for name in sorted(os.listdir(out)):
            if name.startswith(prefix) and name.endswith('.ts'):
                said = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                                       'stream=codec_name', '-of', 'csv=p=0', os.path.join(out, name)],
                                      capture_output=True, text=True, timeout=30).stdout
                found.update(word for word in said.split() if word)
        return found

    def _segment(self, data, out, round_no, copy_audio):
        cmd = build_feed_preview_cmd({'ffmpeg': {'path': 'ffmpeg'}}, out, round_no, list_size=50)
        if copy_audio:
            # The same segmenter with audio copied, to see what the input's audio track reads as.
            i = cmd.index('-c:a')
            cmd[i:i + 6] = ['-c:a', 'copy']
        subprocess.run(cmd, input=data, check=True, capture_output=True, timeout=120)

    def test_one_segmenter_over_both_mislabels_the_second_codec(self):
        out = os.path.join(self.media, '..', f'one-{os.getpid()}')
        os.makedirs(out, exist_ok=True)
        self.addCleanup(shutil.rmtree, out, True)
        self._segment(self._relay_bytes('a.ts') + self._relay_bytes('ac3.ts'), out, 1, copy_audio=True)
        self.assertEqual(self._audio_codecs(out, 1), {'aac'}, 'the AC3 half came out labeled AAC')

    def test_a_round_per_source_gives_each_its_own_codec(self):
        out = os.path.join(self.media, '..', f'two-{os.getpid()}')
        os.makedirs(out, exist_ok=True)
        self.addCleanup(shutil.rmtree, out, True)
        self._segment(self._relay_bytes('ac3.ts'), out, 2, copy_audio=True)
        self.assertEqual(self._audio_codecs(out, 2), {'ac3'})


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class _FeedPreview(unittest.TestCase):
    FEED = _cfg()

    @classmethod
    def setUpClass(cls):
        cls.media = clips.shared('feed-preview', _build)

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.client
        self.addCleanup(self.t.cleanup)
        self.addCleanup(feed.stop_all)
        self.addCleanup(preview.stop_all)
        self.t.sandbox_config(self.FEED)
        self.account = make_account('Acct', max_connections=1)
        db.session.commit()
        patcher = mock.patch.object(ss, 'render_clip', self._fake_render)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_render(self, cfg, screen, path, *, still=False, background=False):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(os.path.join(self.media, 'screen.ts'), path)

    def clip_channel(self, name, clip='a.ts', account=None):
        ch = make_channel(account or self.account, name=name, pace_realtime=True)
        ch.stream_url = os.path.join(self.media, clip)
        db.session.flush()
        return ch

    def start(self, feed_id, expect=200):
        resp = self.client.post(f'/api/feed/{feed_id}/preview')
        self.assertEqual(resp.status_code, expect, resp.get_data(as_text=True)[:400])
        return resp.get_json()

    def status(self, sid):
        return self.client.get(f'/api/preview/{sid}/status').get_json()

    def wait_ready(self, sid, round_no=None):
        ok = _wait(lambda: self.status(sid)['state'] == preview.STATE_READY
                   and (round_no is None or self.status(sid)['round'] == round_no))
        self.assertTrue(ok, self.status(sid))
        return self.status(sid)

    def only_play(self):
        live = feed.live_sessions()
        self.assertEqual(len(live), 1, live)
        return live[0]


class GroupPreviewTests(_FeedPreview):
    def group(self, *clips_, accounts=None):
        members = []
        for i, clip in enumerate(clips_):
            account = (accounts or [self.account] * len(clips_))[i]
            ch = self.clip_channel(f'Feed {i}', clip=clip, account=account)
            ch.health_score = 90.0 - i * 10
            members.append(ch)
        group = make_group('Game feeds', members=members)
        group.in_guide = True
        db.session.commit()
        return group, members

    def test_it_is_a_real_play_on_a_viewer_seat_and_stop_ends_everything(self):
        group, (a,) = self.group('a.ts')
        data = self.start(feed.feed_id_for_group(group.id))
        sid = data['session_id']
        self.assertEqual(data['source'], preview.SOURCE_FEED)
        st = self.wait_ready(sid, 1)
        play = self.only_play()
        self.assertTrue(play.preview)
        self.assertEqual(play.client, feed.PREVIEW_CLIENT)
        self.assertEqual(play.channel_id, a.id)
        # The play holds the seat; the preview session holds none of its own.
        self.assertEqual(connlim._holders.get(self.account.id), [('viewer', play.id)])
        self.assertEqual(st['now_playing'], '"Feed 0"')

        self.assertIn('round=1', st['playlist_url'])
        pl = self.client.get(st['playlist_url'])
        self.assertEqual(pl.status_code, 200)
        seg = re.search(r'^(r001-seg\d{5}\.ts)$', pl.get_data(as_text=True), re.M).group(1)
        self.assertEqual(self.client.get(f'/api/preview/{sid}/{seg}').status_code, 200)
        # Only the round it is on: another round's playlist and a channel preview's names are 404s.
        self.assertEqual(self.client.get(f'/api/preview/{sid}/index.m3u8?round=2').status_code, 404)
        self.assertEqual(self.client.get(f'/api/preview/{sid}/index.m3u8').status_code, 404)
        self.assertEqual(self.client.get(f'/api/preview/{sid}/seg00000.ts').status_code, 404)
        self.assertEqual(self.client.get(f'/api/preview/{sid}/r002-seg00000.ts').status_code, 404)

        session = preview.get_session(sid)
        proc = session.proc
        self.assertEqual(self.client.post(data['stop_url']).status_code, 200)
        self.assertEqual(self.status(sid)['reason'], preview.REASON_USER)
        self.assertIsNotNone(proc.poll(), "the round's ffmpeg must be dead after Stop")
        self.assertFalse(play.live)
        self.assertEqual(play.reason, feed.REASON_CLIENT_GONE)
        self.assertIn('The preview in ChannelBin ended', play.detail)
        self.assertEqual(connlim._holders.get(self.account.id, []), [])
        self.assertFalse(os.path.exists(session.dir))

    def test_a_failover_starts_a_new_round_on_the_new_channel(self):
        other = make_account('Other', max_connections=1)
        group, (a, b) = self.group('a.ts', 'b.ts', accounts=[self.account, other])
        sid = self.start(feed.feed_id_for_group(group.id))['session_id']
        self.wait_ready(sid, 1)
        session = preview.get_session(sid)
        first = session.proc
        first_files = feed_preview_names(1)[0].rsplit('.', 1)[0]
        play = self.only_play()
        play.proc.kill()
        st = self.wait_ready(sid, 2)
        self.assertEqual(play.channel_id, b.id)
        self.assertEqual(st['now_playing'], '"Feed 1"')
        self.assertIsNotNone(first.poll(), "the last round's ffmpeg is stopped at the mark")
        self.assertFalse([n for n in os.listdir(session.dir) if n.startswith(first_files)],
                         "the last round's files are deleted")
        self.assertEqual(self.client.get(f'/api/preview/{sid}/index.m3u8?round=1').status_code, 404)

    def test_a_play_that_ends_on_its_own_ends_the_preview_with_its_reason(self):
        group, (a,) = self.group('a.ts')
        sid = self.start(feed.feed_id_for_group(group.id))['session_id']
        self.wait_ready(sid)
        session = preview.get_session(sid)
        proc = session.proc
        self.only_play().proc.kill()      # the group's one channel dies: nothing left
        self.assertTrue(_wait(lambda: self.status(sid)['state'] == preview.STATE_STOPPED), self.status(sid))
        st = self.status(sid)
        self.assertEqual(st['reason'], preview.REASON_PLAY_ENDED)
        self.assertIn('Nothing left to play', st['detail'])
        self.assertTrue(_wait(lambda: proc.poll() is not None, 5))
        self.assertFalse(os.path.exists(session.dir))
        self.assertEqual(connlim._holders.get(self.account.id, []), [])

    def test_a_segmenter_that_dies_by_itself_is_named_with_its_exit_code(self):
        """The round's ffmpeg exiting on its own is the preview's ffmpeg failing, not the play ending -
        and the play's record says the preview ended and why."""
        group, (a,) = self.group('a.ts')
        sid = self.start(feed.feed_id_for_group(group.id))['session_id']
        self.wait_ready(sid)
        session = preview.get_session(sid)
        play = self.only_play()
        session.proc.kill()
        self.assertTrue(_wait(lambda: self.status(sid)['state'] == preview.STATE_STOPPED), self.status(sid))
        st = self.status(sid)
        self.assertEqual(st['reason'], preview.REASON_FFMPEG_EXITED)
        self.assertIn('ffmpeg exited with code -9', st['detail'])
        self.assertEqual(st['reason_text'], preview.FEED_REASON_TEXT[preview.REASON_FFMPEG_EXITED])
        self.assertTrue(_wait(lambda: not play.live, 5))
        self.assertIn('The preview in ChannelBin ended', play.detail)
        self.assertEqual(connlim._holders.get(self.account.id, []), [])

    def test_a_round_that_dies_decoding_on_the_gpu_restarts_on_the_cpu(self):
        """A re-encoding round whose GPU decode dies (a stand-in here: no GPU in the suite) is
        taken off and the next round decodes on the CPU, on the same play, and the session says
        why; the dead round's files go with it (dev/changelog/1688)."""
        group, (a,) = self.group('a.ts')
        real = preview.build_feed_preview_cmd
        built = []

        def build(*args, **kw):
            built.append(kw.get('gpu_decode'))
            if kw.get('gpu_decode'):
                return [sys.executable, '-c',
                        'import sys; sys.stdin.buffer.read(65536); '
                        'sys.stderr.write("[vf#0:0 @ 0x1] [error] Error reinitializing filters!\\n"); '
                        'sys.exit(218)']
            return real(*args, **{**kw, 'reencode_device': None})
        with mock.patch.object(preview, 'build_feed_preview_cmd', side_effect=build), \
                mock.patch.object(preview, 'reencode_device', return_value=('/dev/dri/renderD128', '')):
            resp = self.client.post(f'/api/feed/{feed.feed_id_for_group(group.id)}/preview',
                                    json={'reencode': True})
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:400])
            data = resp.get_json()
            self.assertTrue(data['gpu_decode'])
            sid = data['session_id']
            st = self.wait_ready(sid, 2)
            self.assertEqual(built, [True, False])
            self.assertFalse(st['gpu_decode'])
            self.assertIn('could not decode this stream', st['decode_note'])
            self.assertIn('Error reinitializing filters!', st['decode_note'])
            session = preview.get_session(sid)
            first = feed_preview_names(1)[0].rsplit('.', 1)[0]
            self.assertFalse([n for n in os.listdir(session.dir) if n.startswith(first)])
            play = self.only_play()
            self.assertTrue(play.live)
            self.assertEqual(self.client.post(data['stop_url']).status_code, 200)
        self.assertEqual(self.status(sid)['reason'], preview.REASON_USER)
        self.assertFalse(play.live)
        self.assertEqual(connlim._holders.get(self.account.id, []), [])

    def test_stop_ends_a_segmenter_that_stopped_reading(self):
        """A round's ffmpeg alive but reading nothing leaves the pump blocked in a write: Stop still
        returns at once, kills it and lets the play go."""
        import signal
        import threading
        group, (a,) = self.group('a.ts')
        sid = self.start(feed.feed_id_for_group(group.id))['session_id']
        self.wait_ready(sid)
        session = preview.get_session(sid)
        proc = session.proc
        os.kill(proc.pid, signal.SIGSTOP)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        time.sleep(3)   # the pipe fills and the pump blocks in its write
        done = threading.Event()
        threading.Thread(target=lambda: (preview.stop_preview(sid), done.set()), daemon=True).start()
        self.assertTrue(done.wait(15), 'Stop hung on a segmenter that stopped reading')
        self.assertIsNotNone(proc.poll(), "the round's ffmpeg is dead")
        self.assertEqual(preview.get_session(sid).reason, preview.REASON_USER)
        self.assertEqual(feed.live_sessions(), [])
        self.assertEqual(connlim._holders.get(self.account.id, []), [])

    def test_a_group_the_output_does_not_offer_still_previews(self):
        group, (a,) = self.group('a.ts')
        group.in_guide = False
        group.on_output = False
        db.session.commit()
        fid = feed.feed_id_for_group(group.id)
        self.assertIsNone(feed.resolve_play(fid), 'a player is not offered it')
        sid = self.start(fid)['session_id']
        self.wait_ready(sid)
        self.assertEqual(self.only_play().channel_id, a.id)

    def test_a_refused_preview_says_why_and_raises_no_alert(self):
        self.t.sandbox_config(_cfg(max_viewers=1))
        group, (a,) = self.group('a.ts')
        busy = self.client.get(f'/live/{USER}/{PASS}/{feed.feed_id_for_group(group.id)}.ts')
        self.addCleanup(busy.close)
        next(iter(busy.response))
        alerts = Alert.query.count()
        resp = self.client.post(f'/api/feed/{feed.feed_id_for_group(group.id)}/preview')
        self.assertEqual(resp.status_code, 503)
        self.assertIn('as many IPTV players as it allows', resp.get_json()['error'])
        self.assertEqual(Alert.query.count(), alerts)
        self.assertIsNone(preview.live_session())

    def test_the_route_turns_away_a_plain_channel_and_an_unknown_feed(self):
        ch = self.clip_channel('Plain')
        db.session.commit()
        resp = self.client.post(f'/api/feed/{feed.feed_id_for_channel(ch.id)}/preview')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('channel page', resp.get_json()['error'])
        self.assertEqual(self.client.post(f'/api/feed/{feed.feed_id_for_group(999)}/preview').status_code, 404)
        self.assertEqual(feed.live_sessions(), [])

    def test_a_second_preview_replaces_the_first_and_its_play(self):
        group, (a,) = self.group('a.ts')
        fid = feed.feed_id_for_group(group.id)
        first = self.start(fid)['session_id']
        self.wait_ready(first)
        first_play = self.only_play()
        self.start(fid)
        self.assertEqual(self.status(first)['reason'], preview.REASON_REPLACED)
        self.assertFalse(first_play.live)
        self.assertTrue(_wait(lambda: len(feed.live_sessions()) == 1, 5))


class SportsPreviewTests(_FeedPreview):
    """A sports channel between games plays its waiting screen with no seat, and the game coming on
    is a new round on a seat - in redirect mode too, where a player's screen would end instead."""
    FEED = _cfg(mode='redirect')

    def setUp(self):
        super().setUp()
        league = SportsLeague(tsdb_id=9001, name='Pretend League', match_terms='[]')
        db.session.add(league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0, rule_id=0))
        start = datetime.utcnow() + timedelta(days=1)
        self.event = SportsEvent(tsdb_id=7001, league_id=league.id, name='g', home_team_id=101,
                                 away_team_id=102, home_name='Ironvale Bears', away_name='Harborview Gulls',
                                 start_at=start, local_date=start.date())
        db.session.add(self.event)
        db.session.commit()
        sports_feed.set_team_channel(league.id, 101, True)
        self.feed_id = feed.feed_id_for_sports(SportsFeedChannel.query.one().id)

    def test_the_waiting_screen_takes_no_seat_and_the_game_is_a_new_round(self):
        sid = self.start(self.feed_id)['session_id']
        st = self.wait_ready(sid, 1)
        self.assertEqual(st['now_playing'], feed.SCREEN_NAME)
        play = self.only_play()
        self.assertEqual(play.state, feed.STATE_WAITING)
        self.assertEqual(connlim._holders.get(self.account.id, []), [])

        start = datetime.utcnow() - timedelta(minutes=5)
        self.event.start_at = start
        self.event.local_date = start.date()
        ch = self.clip_channel('Local game', clip='b.ts')
        make_guide_match(self.event, ch, start, title='g', reason='r')
        db.session.commit()
        feed.recheck_waiting()
        st = self.wait_ready(sid, 2)
        self.assertTrue(play.live, 'a preview relays the game in redirect mode, in place')
        self.assertEqual(play.channel_id, ch.id)
        self.assertEqual(st['now_playing'], '"Local game"')
        self.assertEqual(connlim._holders.get(self.account.id), [('viewer', play.id)])



class UpNextPreviewTests(_FeedPreview):
    """An Up next channel is ChannelBin's own picture: its preview takes no seat, and plays switched off."""

    def test_it_plays_the_picture_on_no_seat_even_switched_off(self):
        league = SportsLeague(tsdb_id=9001, name='Pretend League', match_terms='[]')
        db.session.add(league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0, rule_id=0))
        db.session.commit()
        fid = feed.feed_id_for_up_next(0)
        self.assertIsNone(feed.resolve_play(fid), 'switched off, a player is not offered it')
        st = self.wait_ready(self.start(fid)['session_id'], 1)
        self.assertEqual(st['now_playing'], feed.PICTURE_NAME)
        play = self.only_play()
        self.assertEqual(play.state, feed.STATE_SHOWING)
        self.assertFalse(play.counted)
        self.assertEqual(connlim._holders.get(self.account.id, []), [])


class PreviewButtonTests(_FeedPreview):
    """Where the Preview buttons are drawn and what each one plays (dev/changelog/1645)."""

    def _buttons(self, html):
        return re.findall(r'<button class="([^"]*)" data-act="preview"([^>]*)>', html)

    def test_the_iptv_output_rows_preview_a_channel_as_itself_and_a_group_through_the_output(self):
        ch = self.clip_channel('Plain')
        ch.in_guide = True
        group = make_group('Game feeds', members=[self.clip_channel('Member')])
        db.session.commit()
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn(f'data-act="preview" data-channel="{ch.id}" data-name="Plain"', html)
        self.assertIn(f'data-act="preview" data-feed="{feed.feed_id_for_group(group.id)}" data-name="Game feeds"', html)
        self.assertIn('js/live-preview.js', html)

    def test_a_group_with_nothing_to_play_says_why_instead(self):
        group = make_group('Idle feeds', members=[self.clip_channel('Off')], recording=False)
        db.session.commit()
        html = self.client.get('/iptv-output').get_data(as_text=True)
        m = re.search(rf'data-act="preview" data-feed="{feed.feed_id_for_group(group.id)}"[^>]*>', html)
        self.assertIsNotNone(m, 'the group row draws a Preview')
        self.assertIn('aria-disabled="true" data-why="Nothing to play', m.group(0))

    def test_a_group_page_previews_the_group_in_the_guide_or_not(self):
        group = make_group('Game feeds', members=[self.clip_channel('Member')], in_guide=False)
        db.session.commit()
        html = self.client.get(f'/channel-groups/{group.id}').get_data(as_text=True)
        fid = feed.feed_id_for_group(group.id)
        buttons = [b for b in self._buttons(html) if f'data-feed="{fid}"' in b[1]]
        self.assertEqual(len(buttons), 2, 'the bar button and the phone sheet\'s kebab row')
        self.assertIn('gd-phone-hide', buttons[0][0])
        self.assertIn('gd-phone-only', buttons[1][0])
        self.assertIn('js/live-preview.js', html)

    def test_a_group_nobody_records_from_has_no_preview(self):
        group = make_group('Check only', members=[self.clip_channel('Member')], recording=False)
        db.session.commit()
        html = self.client.get(f'/channel-groups/{group.id}').get_data(as_text=True)
        self.assertNotIn(f'data-feed="{feed.feed_id_for_group(group.id)}"', html)

    def test_the_league_card_previews_a_team_channel_and_dims_one_switched_off(self):
        league = SportsLeague(tsdb_id=9001, name='Pretend League', match_terms='[]')
        db.session.add(league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=101, rule_id=0))
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=102, rule_id=0))
        start = datetime.utcnow() + timedelta(days=1)
        db.session.add(SportsEvent(tsdb_id=7001, league_id=league.id, name='g', home_team_id=101,
                                   away_team_id=102, home_name='Ironvale Bears', away_name='Harborview Gulls',
                                   start_at=start, local_date=start.date()))
        db.session.commit()
        sports_feed.set_team_channel(league.id, 101, True)
        row = SportsFeedChannel.query.filter_by(team_tsdb_id=101).one()
        html = self.client.get(f'/sports/{league.id}').get_data(as_text=True)
        self.assertIn(f'data-act="preview" data-feed="{feed.feed_id_for_sports(row.id)}"', html)
        self.assertIn('data-why="Switch it on to preview it."', html)
        self.assertIn('js/live-preview.js', html)


if __name__ == '__main__':
    unittest.main()
