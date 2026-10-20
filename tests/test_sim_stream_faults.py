"""The stream faults, read back off the wire: each does what its measurement says, only when
and where it says (sim/faults.py, sim/stream_faults.py, DESIGN-simulator.md §3.11, §7).

What ffmpeg makes of each fault - the signature a changelog measured - is
tests/test_sim_stream_signatures.py; what ChannelBin does about it is the capture tier's.
These are characterization tests of the simulator, not guards for a past defect. Every read is
under a few seconds of wall time on the tiny test loop: a channel-time window in the past sits
inside a long burst, so a 12-second video gap is read in a moment.
"""
import ast
import datetime as dt
import os
import pathlib
import subprocess
import sys
import time
import unittest

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import ts as sim_ts  # noqa: E402
from sim import serve_for_test  # noqa: E402
from sim.faults import (STREAM_KINDS, FaultBook, FaultError, check_catalog,  # noqa: E402
                        make_fault)
from sim.stream_faults import LiveFaults, epoch_ns  # noqa: E402

from tests.support.sim_streams import (AUDIO_PID, VIDEO_PID, first_pcr_seconds,  # noqa: E402
                                       library, live_ids, origin, pcrs, pes_dts, read_for,
                                       read_stream, stream_world, video_seconds, wait_until,
                                       whole_packets)

HERE = pathlib.Path(__file__).resolve()
WRAP = sim_ts.PTS_WRAP
HZ = sim_ts.PTS_HZ


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def at(seconds: float) -> dt.datetime:
    return utcnow() + dt.timedelta(seconds=seconds)


def span(ticks: list) -> float:
    """Seconds from the first timestamp to the last, across the 33-bit wrap."""
    return ((ticks[-1] - ticks[0]) % WRAP) / HZ if ticks else 0.0


def steps(pts: list) -> list:
    """Gaps between consecutive timestamps, in seconds, across the 33-bit wrap."""
    return [((b - a) % WRAP) / HZ for a, b in zip(pts, pts[1:])]


def _drain(pieces, got: bytearray, seconds: float) -> int:
    """Read on for `seconds` - past whatever was already cut and in flight when a fault was
    injected - and return how much has arrived."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        got += next(pieces)
    return len(got)


class _Faulted(unittest.TestCase):
    backend = {}
    extra = ()

    def setUp(self):
        self.lib = library(*self.extra)
        self.world = stream_world(**self.backend)
        self.sim = serve_for_test(self, self.world, content=self.lib)
        self.ids = live_ids(self.world)
        self.urls = self.sim.stream_urls('cove-tv', 'reeduser')
        self.sid = self.ids[0]
        self.url = self.urls[self.sid]
        self.label = f'cove:{self.sid}'

    def free(self, user='reeduser'):
        self.assertTrue(wait_until(lambda: self.sim.connections(user) == 0),
                        f'{user} still holds a connection')

    def served(self) -> list:
        """The ledger's stream rows that served something - not the 302s ahead of them."""
        return [r for r in self.sim.ledger.entries(endpoint='stream') if r.status != 302]

    def noted(self, text: str, seconds: float = 3.0):
        """The last served row, once its note says `text` - a stream's row is amended with
        how it ended a moment after its connection closes."""
        self.assertTrue(wait_until(lambda: text in self.served()[-1].note, seconds),
                        self.served()[-1].note)
        return self.served()[-1]


# --- the record --------------------------------------------------------------------------------

class StreamCatalogTests(unittest.TestCase):
    def setUp(self):
        self.world = stream_world()
        self.sid = live_ids(self.world)[0]

    def test_the_catalog_is_well_formed(self):
        self.assertEqual(check_catalog(), [])

    def test_every_stream_kind_is_read_back_in_this_module(self):
        """A kind nothing reads back is a claim nobody checked."""
        tree = ast.parse(HERE.read_text(encoding='utf-8'))
        named = {n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertEqual(sorted(set(STREAM_KINDS) - named), [])

    def test_targets(self):
        w, label = self.world, f'cove:{self.sid}'
        self.assertEqual(make_fault(w, 'video_stops', label).target, label)
        self.assertEqual(make_fault(w, 'placeholder', 'cove').target, 'cove')
        self.assertEqual(make_fault(w, 'silent_socket', 'reeduser').target, 'reeduser')
        radio = next(c for c in w.lineups['cove'].channels if c.kind == 'radio')
        make_fault(w, 'refuse_at_open', f'cove:radio/{radio.slug}')
        for kind, target, said in (
                ('video_stops', 'reeduser', 'a channel (backend:stream id) or a backend'),
                ('video_stops', 'cove:99999999', 'a channel'),
                ('silent_socket', 'nobody', 'a channel (backend:stream id), a backend or a login'),
                ('refuse_at_open', 'cove-tv', 'a channel')):
            with self.assertRaises(FaultError, msg=(kind, target)) as cm:
                make_fault(w, kind, target)
            self.assertIn(said, str(cm.exception))

    def test_a_channel_stream_fault_takes_a_window_not_a_count(self):
        with self.assertRaises(FaultError) as cm:
            make_fault(self.world, 'throttle', 'cove', times=1)
        self.assertIn('start and end, not after or times', str(cm.exception))
        make_fault(self.world, 'silent_socket', 'cove', after=1, times=1)
        make_fault(self.world, 'refuse_at_open', 'cove', times=3)

    def test_a_jump_needs_the_moment_it_falls_at(self):
        for kind in ('timestamp_jump', 'hls_discontinuity'):
            with self.assertRaises(FaultError) as cm:
                make_fault(self.world, kind, 'cove')
            self.assertIn('start is required', str(cm.exception))
            make_fault(self.world, kind, 'cove', start=utcnow())

    def test_a_profile_setting_must_name_an_mpeg_ts_loop(self):
        with self.assertRaises(FaultError):
            make_fault(self.world, 'format_change', 'cove', profile='radio_mp3')

    def test_a_world_file_can_declare_one(self):
        data = {'faults': [{'kind': 'refuse_at_open', 'target': 'cove', 'status': 403}]}
        from tests.support.sim_streams import STREAM_WORLD
        import copy
        from sim import build_world
        world = copy.deepcopy(STREAM_WORLD)
        world.update(data)
        self.assertEqual(build_world(world).faults[0].params, {'status': 403})

    def test_a_throttle_slides_from_rate_to_rate_end_across_its_window(self):
        start, end = utcnow(), at(10)
        book = FaultBook([make_fault(self.world, 'throttle', 'cove', rate=1.0, rate_end=0.0,
                                     start=start, end=end)])
        live = LiveFaults(book, f'cove:{self.sid}', 'cove').refresh()
        s = epoch_ns(start)
        self.assertAlmostEqual(live.rate(s), 1.0)
        self.assertAlmostEqual(live.rate(s + 5 * 10**9), 0.5, places=3)
        self.assertIsNone(live.rate(epoch_ns(end)))
        self.assertEqual(live.applied, ['throttle'])


# --- connection faults -------------------------------------------------------------------------

class ConnectionFaultTests(_Faulted):
    def test_silent_socket_stops_sending_and_keeps_the_connection_open(self):
        """dev/changelog/958: no bytes, no close. Counted per connection, not per request: the
        302 ahead of the play URL is never faulted, so times=1 silences the first stream and
        the reconnect flows."""
        self.sim.inject('silent_socket', self.label, after_bytes=200_000, times=1)
        r = read_stream(self.url, 3, read_timeout=0.8)
        self.assertEqual((r['status'], r['ended']), (200, 'silent'))
        self.assertEqual(len(r['data']), 200_000 // 188 * 188)
        self.free()
        self.noted(f'silent after {200_000 // 188 * 188} bytes; the client went away')
        again = read_stream(self.url, 0.6, read_timeout=0.8)
        self.assertEqual(again['ended'], 'time')
        self.assertGreater(len(again['data']), 200_000)
        self.free()
        self.assertEqual(self.served()[-1].faults, ())

    def test_a_silence_that_runs_out_closes_the_connection(self):
        self.sim.inject('silent_socket', self.label, after_bytes=100_000, hold_seconds=0.5)
        r = read_stream(self.url, 3, read_timeout=2.0)
        self.assertEqual(r['ended'], 'eof')
        self.assertGreater(r['wall'], 0.5)
        self.noted('for 0.5 s, then closed')

    def test_close_mid_stream_ends_the_body_as_if_the_stream_had_finished(self):
        """Prod's capture tails: the stream ends early and ffmpeg reconnects (dev/changelog/430)."""
        self.sim.inject('close_mid_stream', self.label, after_bytes=150_000)
        r = read_stream(self.url, 3)
        self.assertEqual((r['status'], r['ended']), (200, 'eof'))
        self.assertEqual(len(r['data']), 150_000 // 188 * 188)
        self.assertTrue(sim_ts.check(r['data']).clean)
        self.noted(f'closed after {150_000 // 188 * 188} bytes by close_mid_stream')

    def test_slow_first_byte_holds_the_answer_back(self):
        """dev/changelog/958 measured 0.25 to 1.26 s; the 302 is answered at once."""
        self.sim.inject('slow_first_byte', self.label, seconds=1.0)
        r = read_stream(self.url, 0.2, read_timeout=3)
        first = r['pieces'][0][0]
        self.assertGreaterEqual(first, 1.0)
        self.assertLess(first, 1.8)
        hop, served = self.sim.ledger.entries(endpoint='stream')[:2]
        self.assertEqual((hop.status, hop.faults), (302, ()))
        self.assertEqual(served.faults, ('slow_first_byte',))

    def test_no_answer_accepts_the_connection_and_never_replies(self):
        self.sim.inject('no_answer', 'reeduser')
        with self.assertRaises(requests.exceptions.ReadTimeout):
            requests.get(self.url, timeout=(5, 0.8))
        self.assertTrue(wait_until(lambda: 'the client gave up waiting' in self.served()[-1].note))
        row = self.served()[-1]
        self.assertEqual((row.status, row.bytes, row.faults), (0, 0, ('no_answer',)))
        self.assertEqual(self.sim.connections('reeduser'), 0, 'a held request takes no seat')

    def test_after_and_times_count_connections(self):
        self.sim.inject('close_mid_stream', self.label, after_bytes=0, after=1, times=1)
        lengths = []
        for _ in range(3):
            lengths.append(len(read_stream(self.url, 0.3)['data']))
            self.free()
        self.assertGreater(lengths[0], 0)
        self.assertEqual(lengths[1], 0)
        self.assertGreater(lengths[2], 0)


# --- what a connection is answered with ------------------------------------------------------

class OpenFaultTests(_Faulted):
    def test_refuse_at_open_answers_every_attempt_with_405(self):
        """dev/changelog/436: 405 at input open on every attempt, the zero-byte death spiral -
        and 105's dead stream that reconnects and dies is the same answer, again and again."""
        self.sim.inject('refuse_at_open', self.label)
        for _ in range(3):
            r = requests.get(self.url, timeout=5)
            self.assertEqual(r.status_code, 405)
            self.assertEqual(r.history[0].status_code, 302)
            self.assertIn(b'405 Method Not Allowed', r.content)
        self.assertEqual(self.sim.connections('reeduser'), 0)
        self.assertEqual({row.faults for row in self.served()}, {('refuse_at_open',)})
        other = requests.get(self.urls[self.ids[1]], stream=True, timeout=5)
        self.assertEqual(other.status_code, 200, 'another channel plays')
        other.close()

    def test_a_dead_stream_comes_back_when_its_window_ends(self):
        """dev/changelog/593, dev recording 18: a member that gave nothing on restart served
        again about 90 s later."""
        now = utcnow()
        self.sim.inject('refuse_at_open', self.label, start=now,
                        end=now + dt.timedelta(seconds=90))
        self.assertEqual(requests.get(self.url, timeout=5).status_code, 405)
        self.sim.set_clock(now + dt.timedelta(seconds=91))
        status, data = read_for(self.url, 0.3)
        self.assertEqual(status, 200)
        self.assertGreater(len(data), 0)

    def test_a_backend_target_reaches_every_channel(self):
        self.sim.inject('refuse_at_open', 'cove', status=403)
        for sid in self.ids[:3]:
            self.assertEqual(requests.get(self.urls[sid], timeout=5).status_code, 403)

    def test_the_placeholder_is_a_finite_clip_answered_as_a_file(self):
        """dev/changelog/957: the same clip every time, a size in its answer, drained far
        faster than real time, then a clean end."""
        self.sim.inject('placeholder', self.label, profile='test_tiny_black', clip_seconds=60)
        a = read_stream(self.url, 5)
        self.free()
        b = read_stream(self.url, 5)
        self.free()
        self.assertEqual((a['status'], a['ended'], b['ended']), (200, 'eof', 'eof'))
        self.assertEqual(a['data'], b['data'])
        self.assertEqual(int(a['headers']['Content-Length']), len(a['data']))
        self.assertEqual(a['headers']['Accept-Ranges'], 'bytes')
        self.assertTrue(sim_ts.check(a['data']).clean)
        video = pes_dts(a['data'], VIDEO_PID)
        self.assertAlmostEqual(span(video), 60.0, delta=0.2)
        self.assertGreater(a['wall'], 60 / 117 * 0.7)        # 117x, the measured median
        self.assertLess(a['wall'], 60 / 117 * 3)
        self.assertIsNone(self.noted('the placeholder clip ended').offered)

    def test_the_placeholder_answers_a_range_and_nothing_past_its_end(self):
        """The real clip's capture reconnected at its last byte twice and read End of file each
        time (dev recording 2's stderr)."""
        self.sim.inject('placeholder', self.label, profile='test_tiny_black', clip_seconds=20)
        whole = read_stream(self.url, 5)['data']
        self.free()
        part = read_stream(self.url, 5, headers={'Range': 'bytes=1000-'})
        self.free()
        self.assertEqual(part['status'], 206)
        self.assertEqual(part['data'], whole[1000:])
        self.assertEqual(part['headers']['Content-Range'],
                         f'bytes 1000-{len(whole) - 1}/{len(whole)}')
        past = read_stream(self.url, 5, headers={'Range': f'bytes={len(whole)}-'})
        self.assertEqual((past['status'], past['data']), (200, b''))
        self.assertEqual(self.sim.connections('reeduser'), 0)


class ContentSwapTests(_Faulted):
    extra = ('test_tiny_5994', 'test_tiny_ac3_51', 'test_tiny_black', 'test_tiny_lowrate')

    def test_format_change_reaches_the_next_connection_on_the_same_clock(self):
        """dev/changelog/866: the feed came back at another frame rate after a restart."""
        _, before = read_for(self.url, 0.3)
        self.free()
        self.sim.inject('format_change', self.label, profile='test_tiny_5994')
        _, after = read_for(self.url, 0.3)
        self.free()
        self.assertEqual(set(steps(pes_dts(before, VIDEO_PID))), {3003 / HZ})
        self.assertEqual(set(steps(pes_dts(after, VIDEO_PID))), {1501 / HZ, 1502 / HZ})
        moved = (first_pcr_seconds(whole_packets(after)) - first_pcr_seconds(
            whole_packets(before))) % (WRAP / HZ)
        self.assertLess(moved, 3.0, 'the new loop runs on the channel clock')

    def test_a_connection_already_open_keeps_its_loop(self):
        with requests.get(self.url, stream=True, timeout=5) as r:
            pieces = r.iter_content(2048)
            got = bytearray()
            end = time.monotonic() + 0.5
            while time.monotonic() < end:
                got += next(pieces)
            self.sim.inject('format_change', self.label, profile='test_tiny_5994')
            mark = _drain(pieces, got, 0.3)
            end = time.monotonic() + 1.0
            while time.monotonic() < end:
                got += next(pieces)
        later = whole_packets(bytes(got[mark - mark % 188:]))
        self.assertEqual(set(steps(pes_dts(later, VIDEO_PID))), {3003 / HZ})

    def test_audio_layout_change_brings_5_1_ac3(self):
        self.sim.inject('audio_layout_change', self.label, profile='test_tiny_ac3_51')
        _, data = read_for(self.url, 0.3)
        streams, _, _ = sim_ts.programs(data)
        self.assertEqual(sorted(streams.values()), [0x1B, 0x81])

    def test_picture_serves_a_black_picture(self):
        self.sim.inject('picture', self.label, profile='test_tiny_black')
        _, data = read_for(self.url, 0.3)
        out = subprocess.run(
            ['ffmpeg', '-v', 'error', '-f', 'mpegts', '-i', 'pipe:0', '-frames:v', '1', '-vf',
             'scale=8:8,format=gray', '-f', 'rawvideo', 'pipe:1'],
            input=data, capture_output=True, timeout=30)
        self.assertEqual(len(out.stdout), 64, out.stderr[-300:])
        self.assertLessEqual(max(out.stdout), 20)

    def test_low_bitrate_serves_far_fewer_bytes_for_the_same_picture_size(self):
        _, normal = read_for(self.url, 0.3)
        self.free()
        self.sim.inject('low_bitrate', self.label, profile='test_tiny_lowrate')
        _, low = read_for(self.url, 0.3)
        rate = len(normal) / video_seconds(normal)
        self.assertLess(len(low) / video_seconds(low), rate / 2)

    def test_a_format_change_reaches_an_hls_client_on_its_first_fetch(self):
        self.sim.inject('format_change', self.label, profile='test_tiny_5994')
        r = requests.get(f'{origin(self.url)}/live/fernuser/fernpass1/{self.sid}.m3u8',
                         timeout=5)
        uri = [ln for ln in r.text.splitlines() if ln and not ln.startswith('#')][-1]
        seg = requests.get(origin(r.url) + uri, timeout=5).content
        self.assertEqual(set(steps(pes_dts(seg, VIDEO_PID))), {1501 / HZ, 1502 / HZ})


# --- while a connection plays -----------------------------------------------------------------

class PaceFaultTests(_Faulted):
    def test_live_edge_chunks_arrive_in_lumps_after_the_burst(self):
        """dev/changelog/997: about 25 s on connect, then 5-second chunks; 1 s here."""
        self.sim.inject('live_edge_chunks', self.label, chunk_seconds=1.0)
        r = read_stream(self.url, 3.3, read_timeout=3)
        times = [t for t, _ in r['pieces'] if t > 0.3]
        gaps = [b - a for a, b in zip(times, times[1:]) if b - a > 0.4]
        self.assertGreaterEqual(len(gaps), 2, times)
        for g in gaps:
            self.assertGreater(g, 0.8)
            self.assertLess(g, 1.5)
        self.free()
        plain = read_stream(self.urls[self.ids[1]], 1.0, read_timeout=3)
        times = [t for t, _ in plain['pieces']]
        self.assertLess(max(b - a for a, b in zip(times, times[1:])), 0.3)

    def _content_rate(self, r, after: float) -> float:
        """Seconds of video per second of wall clock received after `after` seconds."""
        cut = sum(n for t, n in r['pieces'] if t <= after)
        rest = whole_packets(r['data'][cut - cut % 188:])
        span = r['pieces'][-1][0] - after
        return video_seconds(rest) / span

    def test_fast_reserve_runs_ahead_of_the_clock(self):
        """dev/changelog/964: 3.76x for 2h18m."""
        self.sim.inject('fast_reserve', self.label)
        r = read_stream(self.url, 3.0)
        self.assertAlmostEqual(self._content_rate(r, 1.0), 3.76, delta=0.6)

    def test_throttle_sends_a_fraction_of_real_time(self):
        """dev/changelog/429: recording 71's feed fell to about 60% of its rate."""
        self.sim.inject('throttle', self.label, rate=0.5)
        r = read_stream(self.url, 3.0)
        self.assertAlmostEqual(video_seconds(whole_packets(r['data'])) / r['wall'], 0.5,
                               delta=0.2)

    def test_the_ledger_names_a_fault_that_started_mid_stream(self):
        with requests.get(self.url, stream=True, timeout=5) as r:
            pieces = r.iter_content(2048)
            next(pieces)
            self.sim.inject('throttle', self.label, rate=0.9)
            end = time.monotonic() + 0.5
            while time.monotonic() < end:
                next(pieces)
        self.free()
        self.assertTrue(wait_until(lambda: 'throttle' in self.served()[-1].faults))


class TimelineFaultTests(_Faulted):
    """A 20 s burst carries 20 s of the channel's past at once, so a window there is read in a
    moment."""
    backend = {'burst_seconds': 20.0}

    def test_video_stops_while_audio_flows(self):
        """dev/changelog/1209 and 1199: the video withheld for 12 s, past the 10 s at which
        ffmpeg's -dts_delta_threshold closes a gap; audio and the tables carry on."""
        self.sim.inject('video_stops', self.label, start=at(-15), end=at(-3))
        _, data = read_for(self.url, 0.5)
        video, audio = pes_dts(data, VIDEO_PID), pes_dts(data, AUDIO_PID)
        self.assertAlmostEqual(max(steps(video)), 12.0, delta=0.2)
        self.assertLess(max(steps(audio)), 0.4)     # as without the fault: a PES is 0.36 s
        streams, _, _ = sim_ts.programs(data)
        self.assertIn(0x1B, streams.values(), 'the PMT still declares the video')
        self.free()
        self.assertTrue(wait_until(lambda: 'video_stops' in self.served()[-1].faults))

    def test_connecting_while_the_video_is_out_gets_audio_only(self):
        """dev/changelog/1210: a restart that lands during the outage."""
        self.sim.inject('video_stops', self.label, start=at(-40), end=at(20))
        _, data = read_for(self.url, 0.5)
        self.assertEqual(pes_dts(data, VIDEO_PID), [])
        self.assertGreater(span(pes_dts(data, AUDIO_PID)), 19)

    def test_a_video_stop_reaches_a_connection_already_open(self):
        with requests.get(self.url, stream=True, timeout=5) as r:
            pieces = r.iter_content(2048)
            got = bytearray()
            end = time.monotonic() + 0.3
            while time.monotonic() < end:
                got += next(pieces)
            self.sim.inject('video_stops', self.label)
            mark = _drain(pieces, got, 0.3)
            end = time.monotonic() + 1.6
            while time.monotonic() < end:
                got += next(pieces)
        later = bytes(got[mark - mark % 188:])
        self.assertEqual(pes_dts(later, VIDEO_PID), [])
        # Audio comes 0.36 s to a PES, so 1.6 s holds at least 1.08 s between the first and last.
        self.assertGreater(span(pes_dts(later, AUDIO_PID)), 0.9)

    def test_timestamp_jump_moves_every_timestamp_from_its_start(self):
        self.sim.inject('timestamp_jump', self.label, start=at(-10), seconds=30)
        _, data = read_for(self.url, 0.5)
        gaps = steps(pcrs(data))
        big = [g for g in gaps if g > 1]
        self.assertEqual(len(big), 1)
        self.assertAlmostEqual(big[0], 30.0, delta=0.15)
        self.assertLess(max(g for g in gaps if g <= 1), 0.11)

    def test_pts_wrap_falls_where_it_is_put(self):
        self.sim.inject('pts_wrap', self.label, wrap_at=at(-10))
        _, data = read_for(self.url, 0.5)
        clock = pcrs(data)
        backwards = [i for i, (a, b) in enumerate(zip(clock, clock[1:])) if b < a]
        self.assertEqual(len(backwards), 1)
        i = backwards[0]
        self.assertGreater(clock[i], WRAP - HZ)
        self.assertLess(clock[i + 1], HZ)
        report = sim_ts.check(whole_packets(data))
        self.assertEqual((report.pcr_backwards, dict(report.dts_backwards)), (0, {}))


# --- HLS --------------------------------------------------------------------------------------

class HlsFaultTests(_Faulted):
    def playlist(self):
        r = requests.get(f'{origin(self.url)}/live/fernuser/fernpass1/{self.sid}.m3u8',
                         timeout=5)
        self.assertEqual(r.status_code, 200)
        return r

    @staticmethod
    def sequence(text) -> int:
        return int(text.split('#EXT-X-MEDIA-SEQUENCE:')[1].split('\n')[0])

    @staticmethod
    def uris(text) -> list:
        return [ln for ln in text.splitlines() if ln and not ln.startswith('#')]

    def timing(self, r):
        return self.sim.headend._plays[r.url.split('/')[-2]].hls

    def test_hls_stale_freezes_the_playlist(self):
        now = utcnow()
        self.sim.inject('hls_stale', self.label, start=now)
        r = self.playlist()
        self.sim.set_clock(now + dt.timedelta(seconds=30))
        again = requests.get(r.url, timeout=5)
        self.assertEqual(self.sequence(again.text), self.sequence(r.text))
        self.sim.clear()
        moved = requests.get(r.url, timeout=5)
        self.assertGreater(self.sequence(moved.text), self.sequence(r.text) + 5)

    def test_hls_missing_segment_is_listed_and_not_served(self):
        r = self.playlist()
        uris = self.uris(r.text)
        j = self.sequence(r.text) + 3
        start = dt.datetime(1970, 1, 1) + dt.timedelta(seconds=self.timing(r).span(j)[0])
        self.sim.inject('hls_missing_segment', self.label,
                        start=start - dt.timedelta(milliseconds=5),
                        end=start + dt.timedelta(milliseconds=5))
        codes = [requests.get(origin(r.url) + u, timeout=5).status_code for u in uris[2:5]]
        self.assertEqual(codes, [200, 404, 200])
        self.assertIn(uris[3], self.uris(requests.get(r.url, timeout=5).text))

    def test_hls_discontinuity_marks_the_jump_and_moves_the_timestamps(self):
        r = self.playlist()
        hls = self.timing(r)
        j = self.sequence(r.text) + 2
        start = dt.datetime(1970, 1, 1) + dt.timedelta(seconds=hls.span(j)[0] + 1.0)
        self.sim.inject('hls_discontinuity', self.label, start=start, seconds=30)
        text = requests.get(r.url, timeout=5).text
        lines = text.splitlines()
        uri = next(u for u in self.uris(text) if u.endswith(f'_{j + 1}.ts'))
        self.assertEqual(lines[lines.index(uri) - 2], '#EXT-X-DISCONTINUITY')
        self.assertEqual(text.count('#EXT-X-DISCONTINUITY'), 1)
        first = [pes_dts(requests.get(origin(r.url) + u, timeout=5).content, VIDEO_PID)
                 for u in (uri.replace(f'_{j + 1}.ts', f'_{j}.ts'), uri)]
        gap = ((first[1][0] - first[0][0]) % WRAP) / HZ
        self.assertAlmostEqual(gap, hls.duration(j) + 30, delta=0.05)

    def test_video_stops_reaches_an_hls_segment(self):
        self.sim.inject('video_stops', self.label)
        r = self.playlist()
        seg = requests.get(origin(r.url) + self.uris(r.text)[-1], timeout=5).content
        self.assertEqual(pes_dts(seg, VIDEO_PID), [])
        self.assertGreater(span(pes_dts(seg, AUDIO_PID)), 3.5)


if __name__ == '__main__':
    unittest.main()
