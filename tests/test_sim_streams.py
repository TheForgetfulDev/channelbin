"""The stream engine over real HTTP: every URL spelling, the burst, real-time pacing, channel
time, HLS, radio mounts, refusals and the ledger (DESIGN-simulator.md §3.3; dev/changelog/1219).

Characterization tests of the simulator itself - no ChannelBin code runs here. Each read is a
fraction of a second to two seconds of wall time on the tiny test loop.
"""
import datetime as dt
import os
import re
import sys
import tempfile
import threading
import shutil
import unittest

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import Library, serve_for_test  # noqa: E402
from sim import ts as sim_ts  # noqa: E402
from sim.mp3 import Mp3Loop  # noqa: E402
from sim.server import THREAD_PREFIX  # noqa: E402
from sim.streams import ChannelClock, Feed, TsConnection  # noqa: E402

from tests.support.sim_streams import (first_pcr_seconds, library, live_ids, origin,  # noqa: E402
                                       read_for, stream_world, video_seconds, wait_until,
                                       whole_packets)


class _Streams(unittest.TestCase):
    backend = {}
    faults = ()

    def setUp(self):
        self.lib = library()
        self.world = stream_world(**self.backend)
        self.sim = serve_for_test(self, self.world, content=self.lib)
        for kind, target, settings in self.faults:
            self.sim.inject(kind, target, **settings)
        self.ids = live_ids(self.world)
        self.urls = self.sim.stream_urls('cove-tv', 'reeduser')
        self.url = self.urls[self.ids[0]]

    def free(self, user='reeduser'):
        self.assertTrue(wait_until(lambda: self.sim.connections(user) == 0),
                        f'{user} still holds a connection')

    def stream_rows(self):
        return self.sim.ledger.entries(endpoint='stream')


class MpegTsTests(_Streams):
    def test_every_url_spelling_serves_the_channel(self):
        sid = self.ids[0]
        base = origin(self.url)
        plus = self.sim.stream_urls('cove-plus', 'reeduser')[sid]
        self.assertTrue(plus.startswith(self.sim.url('cove-plus')), 'listed on the API host')
        for url in (self.url, f'{base}/live/reeduser/reedpass1/{sid}.ts',
                    f'{base}/reeduser/reedpass1/{sid}.ts', f'{base}/live/reeduser/reedpass1/{sid}',
                    plus):
            status, data = read_for(url, 0.3)
            self.assertEqual(status, 200, url)
            r = sim_ts.check(whole_packets(data))
            self.assertTrue(r.clean, (url, r.problems()))
            streams, _, _ = sim_ts.programs(data)
            self.assertEqual(sorted(streams.values()), [0x0F, 0x1B], url)
            self.free()
        self.assertEqual({row.channel for row in self.stream_rows()}, {f'cove:{sid}'})

    def test_a_new_connection_gets_the_burst_at_once_then_real_time(self):
        """burst_seconds of channel at once (dev/changelog/942, 997), then real time. The
        burst opens at a cut point, so it may run one keyframe interval (2.14 s) longer."""
        _, burst = read_for(self.url, 10, first_piece_by=1.0)
        self.assertGreater(video_seconds(burst), 3.0)      # without a burst: about 1 s
        self.assertLess(video_seconds(burst), 7.8)
        self.free()
        _, data = read_for(self.url, 2.0)
        self.assertGreater(video_seconds(data), 5.0)
        self.assertLess(video_seconds(data), 9.4)

    def test_a_reconnect_later_lands_later_in_the_content(self):
        """Channel time moves with the clock: 45 s later is 45 s on in the content and in the
        PCR, and the burst comes again (DESIGN-simulator.md §3.3)."""
        _, first = read_for(self.url, 0.3)
        self.free()
        later = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(seconds=45)
        self.sim.set_clock(later)
        _, second = read_for(self.url, 0.3)
        self.free()
        moved = (first_pcr_seconds(second) - first_pcr_seconds(first)) % (sim_ts.PTS_WRAP
                                                                         / sim_ts.PTS_HZ)
        self.assertGreater(moved, 44.5)
        self.assertLess(moved, 48.0)
        rows = [r for r in self.stream_rows() if r.offered]
        # Each burst opens at a cut point, so the offered starts move by the clock to within
        # one keyframe interval.
        self.assertAlmostEqual(rows[1].offered[0] - rows[0].offered[0], moved, delta=2.2)

    def test_two_channels_never_share_a_clock(self):
        _, a = read_for(self.urls[self.ids[0]], 0.2)
        self.free()
        _, b = read_for(self.urls[self.ids[1]], 0.2)
        self.assertGreater(abs(first_pcr_seconds(a) - first_pcr_seconds(b)), 60)

    def test_the_ledger_records_the_channel_time_sent(self):
        read_for(self.url, 1.0)
        self.free()
        row = self.stream_rows()[-1]
        self.assertEqual((row.status, row.login, row.channel), (200, 'reeduser',
                                                                f'cove:{self.ids[0]}'))
        self.assertGreater(row.bytes, 0)
        start, end = row.offered
        self.assertGreater(end - start, 4.0)         # the burst and a second of real time,
        self.assertLess(end - start, 8.7)            # plus up to one keyframe interval

    def test_a_head_request_takes_no_seat(self):
        r = requests.head(self.url, timeout=5)
        self.assertEqual(r.status_code, 302)
        r = requests.head(self.url, timeout=5, allow_redirects=True)
        self.assertEqual((r.status_code, r.headers['Content-Type']), (200, 'video/mp2t'))
        self.assertEqual(self.sim.connections('reeduser'), 0)

    def test_a_stream_url_redirects_to_a_play_url_on_each_stream_host_in_turn(self):
        """Measured on a real provider (DESIGN-simulator.md §3.10, dev/changelog/1220): the
        listed URL answers a 302 to /live/play/<token>/<id> on another host, which serves the
        stream close-delimited, the same channel landing on different hosts."""
        hosts = set()
        for _ in range(2):
            first = requests.get(self.url, allow_redirects=False, stream=True, timeout=5)
            first.close()
            self.assertEqual(first.status_code, 302)
            where = first.headers['Location']
            self.assertRegex(where, r'^http://[\d.]+:\d+/live/play/[0-9a-f]{16}/%d$'
                             % self.ids[0])
            hosts.add(origin(where))
            with requests.get(where, stream=True, timeout=5) as r:
                self.assertEqual(r.headers['Content-Type'], 'video/mp2t')
                self.assertEqual(r.headers.get('Connection'), 'close')
                self.assertNotIn('Content-Length', r.headers)
                next(r.iter_content(4096))
            self.free()
        self.assertEqual(hosts, {self.sim.url(h) for h in
                                 self.world.backends['cove'].stream_host_names()})

    def test_a_connection_opens_with_the_pat_the_pmt_and_a_keyframe(self):
        """A real provider's stream opened with its tables, then video (dev/changelog/1220)."""
        _, data = read_for(self.url, 0.3)
        pids = [((data[i + 1] & 0x1F) << 8) | data[i + 2] for i in range(0, 188 * 3, 188)]
        self.assertEqual(pids[:2], [0, 4096])
        self.assertTrue(data[2 * 188 + 1] & 0x40, 'the video PES starts in the third packet')

    def test_the_auth_answer_counts_open_connections(self):
        acct = self.sim.account('cove-tv', 'reeduser')
        q = {'username': acct.username, 'password': acct.password}
        with requests.get(self.url, stream=True, timeout=5) as r:
            next(r.iter_content(4096))
            info = requests.get(f'{acct.base_url}/player_api.php', params=q,
                                timeout=5).json()['user_info']
            self.assertEqual(info['active_cons'], '1')
        self.free()

    def test_stop_ends_an_open_stream_and_leaves_nothing_running(self):
        done = threading.Event()

        def reader():
            try:
                with requests.get(self.url, stream=True, timeout=10) as r:
                    for _ in r.iter_content(4096):
                        pass
            except requests.RequestException:
                pass
            done.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        self.assertTrue(wait_until(lambda: self.sim.connections('reeduser') == 1))
        self.sim.stop()
        self.assertTrue(done.wait(3), 'the stream did not end when the simulator stopped')
        self.assertEqual([x.name for x in threading.enumerate()
                          if x.name.startswith(THREAD_PREFIX)], [])


class RefusalTests(_Streams):
    def test_what_is_refused_and_how(self):
        sid = self.ids[0]
        base = origin(self.url)
        cases = [(f'{base}/reeduser/wrongpass/{sid}', 401),
                 (f'{base}/reeduser/reedpass1/99999999', 404),
                 (f'{base}/movie/reeduser/reedpass1/{sid}.mkv', 404),
                 (f'{self.sim.url("cove-tv")}/nope/at/all', 404)]
        for url, want in cases:
            self.assertEqual(requests.get(url, timeout=5).status_code, want, url)
        self.sim.set_login_status('reeduser', 'Expired')
        self.assertEqual(requests.get(self.url, timeout=5).status_code, 403)
        self.assertEqual(self.sim.connections('reeduser'), 0)

    def test_a_loop_that_is_not_rendered_answers_503_and_says_how_to_render_it(self):
        empty = tempfile.mkdtemp(prefix='sim-empty-')
        self.addCleanup(shutil.rmtree, empty, True)
        sim = serve_for_test(self, stream_world(), content=Library(empty))
        url = sim.stream_urls('cove-tv', 'reeduser')[self.ids[0]]
        self.assertEqual(requests.get(url, timeout=5).status_code, 503)
        self.assertIn('content build --profile test_tiny', sim.ledger.entries()[-1].note)


class HlsTests(_Streams):
    SEGMENT = r'^/hls/[0-9a-f]{16}/%d_(\d+)\.ts$'

    def m3u8(self, user='fernuser', password='fernpass1'):
        return requests.get(f'{origin(self.url)}/live/{user}/{password}/{self.ids[0]}.m3u8',
                            timeout=5)

    def test_an_xtream_m3u8_redirects_to_a_play_url_that_answers_a_rolling_playlist(self):
        """The shape a real provider answered (DESIGN-simulator.md §3.10, dev/changelog/1220):
        a 302 to the play URL, which is the media playlist itself - no master - with root-
        relative segments named after the stream id."""
        sid = self.ids[0]
        r = self.m3u8()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.history[0].status_code, 302)
        self.assertRegex(r.url, r'/live/play/[0-9a-f]{16}/%d$' % sid)
        self.assertEqual(r.headers['Content-Type'], 'application/x-mpegurl')
        text = r.text
        self.assertNotIn('#EXT-X-ENDLIST', text)
        self.assertNotIn('#EXT-X-STREAM-INF', text)
        self.assertEqual(re.findall(r'^#EXT[^:\n]*', text, re.M)[:5],
                         ['#EXTM3U', '#EXT-X-VERSION', '#EXT-X-MEDIA-SEQUENCE',
                          '#EXT-X-ALLOW-CACHE', '#EXT-X-TARGETDURATION'])
        self.assertIn('#EXT-X-TARGETDURATION:5', text)
        seq = int(re.search(r'#EXT-X-MEDIA-SEQUENCE:(\d+)', text)[1])
        uris = [ln for ln in text.splitlines() if ln and not ln.startswith('#')]
        segments = [int(re.match(self.SEGMENT % sid, u)[1]) for u in uris]
        self.assertEqual(segments, list(range(seq, seq + 6)))
        self.assertEqual(re.findall(r'#EXTINF:([\d.]+),', text), ['4.270933'] * 6)
        self.assertEqual(self.sim.connections('fernuser'), 1, 'the client holds a seat')
        seg = requests.get(origin(r.url) + uris[-1], timeout=5)
        self.assertEqual((seg.status_code, seg.headers['Content-Type']), (200, 'video/mp2t'))
        report = sim_ts.check(seg.content)
        self.assertTrue(report.clean, report.problems())
        self.assertEqual(((seg.content[1] & 0x1F) << 8) | seg.content[2], 0, 'PAT first')
        gone = uris[-1].replace(f'_{segments[-1]}.ts', f'_{seq + 50}.ts')
        self.assertEqual(requests.get(origin(r.url) + gone, timeout=5).status_code, 404)

    def test_consecutive_segments_join_seamlessly(self):
        r = self.m3u8()
        uris = [ln for ln in r.text.splitlines() if ln and not ln.startswith('#')]
        self.assertEqual(len(uris), 6)
        joined = b''.join(requests.get(origin(r.url) + u, timeout=5).content for u in uris)
        report = sim_ts.check(joined)
        self.assertTrue(report.clean, report.problems())

    def test_a_play_url_fetched_again_after_its_seat_lapsed_takes_a_new_one(self):
        r = self.m3u8()
        self.assertEqual(self.sim.connections('fernuser'), 1)
        self.sim.headend.seats.release(self.sim.headend._plays[r.url.split('/')[-2]].seat)
        self.assertEqual(self.sim.connections('fernuser'), 0)
        self.assertEqual(requests.get(r.url, timeout=5).status_code, 200)
        self.assertEqual(self.sim.connections('fernuser'), 1)

    def test_a_play_token_for_another_stream_id_is_not_found(self):
        r = self.m3u8()
        other = r.url.rsplit('/', 1)[0] + f'/{self.ids[1]}'
        self.assertEqual(requests.get(other, timeout=5).status_code, 404)

    def test_a_third_party_hls_channel_answers_a_master_with_two_variants(self):
        master = next(u for u in self.urls.values() if u.endswith('/index.m3u8'))
        r = requests.get(master, timeout=5)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(re.findall(r'RESOLUTION=(\d+x\d+)', r.text), ['320x180', '192x108'])
        media = requests.get(master.replace('index.m3u8', 'v1/index.m3u8'), timeout=5)
        last = re.findall(r'^(\d+)\.ts$', media.text, re.M)[-1]
        seg = requests.get(master.replace('index.m3u8', f'v1/{last}.ts'), timeout=5)
        self.assertTrue(sim_ts.check(seg.content).clean)
        self.assertEqual(self.sim.connections('reeduser'), 0, 'a third-party stream holds no seat')


class RadioTests(_Streams):
    def test_a_radio_mount_is_mp3_with_a_small_burst(self):
        url = next(u for u in self.urls.values() if u.endswith('.mp3'))
        with requests.get(url, stream=True, timeout=5) as r:
            self.assertEqual(r.headers['Content-Type'], 'audio/mpeg')
            data = b''
            for piece in r.iter_content(4096):
                data += piece
                if len(data) >= 64 * 1024:
                    break
        loop = Mp3Loop.parse(data)
        self.assertEqual(loop.sample_rate, 44100)
        self.assertGreater(loop.frames, 150)


class RespelledUrlTests(_Streams):
    """Faults that re-spell a panel's stream URLs (dev/changelog/1218) still name a stream."""
    faults = (('cuid_url_mismatch', 'cove-tv', {}),)

    def test_a_url_whose_tail_is_not_the_stream_id_plays(self):
        url = self.urls[self.ids[0]]
        self.assertTrue(url.endswith(str(self.ids[0] + 1_000_000)))
        self.assertEqual(read_for(url, 0.2)[0], 200)
        self.assertEqual(self.stream_rows()[-1].channel, f'cove:{self.ids[0]}')


class DriftedCredentialsTests(_Streams):
    faults = (('url_drift', 'cove-tv', {'to': 'credentials'}),)

    def test_drifted_credentials_play_as_the_panels_login(self):
        self.assertNotIn('reeduser', self.url)
        self.assertEqual(read_for(self.url, 0.2)[0], 200)
        self.assertEqual(self.stream_rows()[-1].login, 'reeduser')


class DriftedHostTests(_Streams):
    faults = (('url_drift', 'cove-tv', {'to': 'host'}),)

    def test_a_url_moved_to_another_stream_host_plays(self):
        before = self.world.panel_lineups['cove-tv'].rows[0]
        self.assertEqual(read_for(self.url, 0.2)[0], 200, before.channel.host)


class ChannelTimeTests(unittest.TestCase):
    """Without HTTP: the stream is a pure function of channel time."""

    class Held(ChannelClock):
        def __init__(self, ns):
            self.ns = ns

        def now_ns(self):
            return self.ns

    def test_the_same_moment_gives_the_same_bytes(self):
        loop = library().load('test_tiny')
        feed = Feed('cove', '1234', 'test_tiny', 5)
        clock = self.Held(1_790_000_000 * 10 ** 9)
        stop = threading.Event()
        a = next(TsConnection(loop, feed, clock, 4.0).pieces(stop))
        b = next(TsConnection(loop, feed, clock, 4.0).pieces(stop))
        self.assertEqual(a, b)
        other = next(TsConnection(loop, Feed('cove', '1235', 'test_tiny', 5), clock,
                                  4.0).pieces(stop))
        self.assertNotEqual(a, other)


if __name__ == '__main__':
    unittest.main()
