"""A recording of an HLS master playlist keeps one quality (dev/changelog/1408).

Guards dev/docs/BUGS.md 2026-11-02 (`-map 0:v?` against a master playlist wrote every variant
into the segment), through the real demuxer: on-disk masters for which program holds which
picture, and the simulator's third-party channel for the recorder's own argv over HTTP.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, recorder  # noqa: E402
from app.config import load_config  # noqa: E402
from app.proc_utils import (HlsVariant, best_hls_variant, build_capture_cmd,  # noqa: E402
                            pick_hls_variant)
from sim import serve_for_test  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.sim_streams import library, stream_world  # noqa: E402

TWO_QUALITIES = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=320x180
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360
high/index.m3u8
"""
CFG = {'ffmpeg': {'path': 'ffmpeg', 'read_timeout_seconds': 5, 'extra_output_args': []},
       'http': {'user_agent': 'test-agent'}}
THIRD_PARTY = 'http://public-streams.example/live/1/index.m3u8'
PROVIDER_HLS = 'http://cdn.example/live/someuser/somepass/123.m3u8'
PLAIN_TS = 'http://cdn.example/someuser/somepass/123'


def _maps(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == '-map']


def _streams(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                          'stream=codec_type,width', '-of', 'json', path],
                         capture_output=True, text=True, timeout=30).stdout
    return json.loads(out).get('streams', [])


class ChoosingTests(unittest.TestCase):
    def test_the_highest_resolution_wins_wherever_it_is_listed(self):
        self.assertEqual(best_hls_variant(TWO_QUALITIES), HlsVariant(1, 2, '640x360'))
        flipped = """#EXTM3U
#EXT-X-STREAM-INF:RESOLUTION=640x360,BANDWIDTH=900000
high/index.m3u8
#EXT-X-STREAM-INF:RESOLUTION=320x180,BANDWIDTH=300000
low/index.m3u8
"""
        self.assertEqual(best_hls_variant(flipped), HlsVariant(0, 2, '640x360'))

    def test_bandwidth_breaks_a_tie_and_never_reads_average_bandwidth(self):
        tied = """#EXTM3U
#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=9000000,BANDWIDTH=300000,RESOLUTION=640x360
a.m3u8
#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=100,BANDWIDTH=900000,RESOLUTION=640x360
b.m3u8
"""
        self.assertEqual(best_hls_variant(tied).program, 1)

    def test_a_quality_with_no_resolution_never_beats_one_with(self):
        mixed = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"
audio.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=900,RESOLUTION=320x180
video.m3u8
"""
        self.assertEqual(best_hls_variant(mixed), HlsVariant(1, 2, '320x180'))

    def test_a_media_playlist_has_no_choice_to_make(self):
        self.assertIsNone(best_hls_variant('#EXTM3U\n#EXTINF:6.0,\n1.ts\n'))
        self.assertIsNone(best_hls_variant(''))


class CaptureCommandTests(unittest.TestCase):
    def test_an_hls_input_maps_one_program_and_a_plain_one_every_stream(self):
        self.assertEqual(_maps(build_capture_cmd(CFG, THIRD_PARTY, '/o.ts', keep_video_track=True,
                                                 hls_program=2)),
                         ['0:p:2:v?', '0:p:2:a?'])
        self.assertEqual(_maps(build_capture_cmd(CFG, PLAIN_TS, '/o.ts', keep_video_track=True)),
                         ['0:v?', '0:a?'])

    def test_a_test_capture_still_leaves_the_choice_to_ffmpeg(self):
        self.assertEqual(_maps(build_capture_cmd(CFG, THIRD_PARTY, '/o.ts')), [])


class DemuxerProgramTests(unittest.TestCase):
    """The claim the choice rests on: a variant's place in the playlist is its program number,
    and a media playlist has a program 0."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which('ffmpeg'):
            raise unittest.SkipTest('ffmpeg not on PATH')
        cls.tmp = tempfile.mkdtemp(prefix='hls-variant-')
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        for name, size in (('low', '320x180'), ('high', '640x360')):
            os.makedirs(os.path.join(cls.tmp, name))
            subprocess.run(
                ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                 f'testsrc2=size={size}:rate=25:duration=4', '-f', 'lavfi', '-i',
                 'sine=frequency=440:duration=4', '-c:v', 'libx264', '-preset', 'ultrafast',
                 '-g', '25', '-c:a', 'aac', '-f', 'hls', '-hls_time', '2', '-hls_list_size', '0',
                 '-hls_playlist_type', 'vod', os.path.join(cls.tmp, name, 'index.m3u8')],
                check=True, timeout=60)
        cls.master = os.path.join(cls.tmp, 'master.m3u8')
        with open(cls.master, 'w') as fh:
            fh.write(TWO_QUALITIES)

    def _capture(self, url, program):
        out = os.path.join(self.tmp, f'out{program}.ts')
        argv = build_capture_cmd(CFG, url, out, keep_video_track=True, hls_program=program)
        subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)
        return _streams(out)

    def test_the_chosen_program_is_the_only_picture_written(self):
        program = best_hls_variant(TWO_QUALITIES).program
        streams = self._capture(self.master, program)
        self.assertEqual([s['codec_type'] for s in streams], ['video', 'audio'])
        self.assertEqual(streams[0]['width'], 640)

    def test_a_media_playlist_is_captured_through_program_zero(self):
        streams = self._capture(os.path.join(self.tmp, 'low', 'index.m3u8'), 0)
        self.assertEqual([s['codec_type'] for s in streams], ['video', 'audio'])
        self.assertEqual(streams[0]['width'], 320)


class SimulatedMasterTests(unittest.TestCase):
    """The simulator's third-party channel: a master with two variants, over real HTTP."""

    def setUp(self):
        if not shutil.which('ffmpeg'):
            self.skipTest('ffmpeg not on PATH')
        self.tmp = tempfile.mkdtemp(prefix='hls-sim-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sim = serve_for_test(self, stream_world(), content=library())
        urls = self.sim.stream_urls('cove-tv', 'reeduser').values()
        self.master = next(u for u in urls if u.endswith('/index.m3u8'))

    def test_the_recorders_argv_writes_one_picture(self):
        variant = pick_hls_variant(CFG, self.master)
        self.assertEqual(variant, HlsVariant(0, 2, '320x180'))
        out = os.path.join(self.tmp, 'cap.ts')
        argv = build_capture_cmd(CFG, self.master, out, 3, pace_realtime=True,
                                 keep_video_track=True, hls_program=variant.program)
        subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)
        self.assertEqual(sorted(s['codec_type'] for s in _streams(out)), ['audio', 'video'])

    def test_a_playlist_that_cannot_be_read_says_so_and_leaves_the_first_program(self):
        with self.assertLogs('app.proc_utils', level='WARNING') as logs:
            self.assertEqual(pick_hls_variant(CFG, self.master.replace('index.m3u8', 'gone/x')),
                             HlsVariant(0, 0, None))
        self.assertIn('first one listed will be recorded', logs.output[0])


class TricklingPlaylistTests(unittest.TestCase):
    """Found by review: the read's timeout is per socket read, so a server sending a byte at a
    time could hold a capture's launch without limit."""

    def test_the_read_has_one_deadline(self):
        def trickle(*a, **k):
            resp = mock.MagicMock()
            resp.__enter__.return_value = resp
            resp.iter_content.side_effect = lambda n: iter(lambda: time.sleep(30), None)
            return resp
        started = time.monotonic()
        with mock.patch('requests.get', trickle), \
                mock.patch('app.proc_utils._HLS_PLAYLIST_TIMEOUT_SECONDS', 0.2), \
                self.assertLogs('app.proc_utils', level='WARNING'):
            self.assertEqual(pick_hls_variant(CFG, THIRD_PARTY), HlsVariant(0, 0, None))
        self.assertLess(time.monotonic() - started, 5)


class NotAPlaylistAfterAllTests(unittest.TestCase):
    """A URL ending .m3u8 that answers plain MPEG-TS has program numbers of its own, so `0:p:0`
    matches nothing. ffmpeg falls back to its default selection rather than writing nothing;
    this holds that, since every recording of such a stream depends on it."""

    def test_the_capture_still_holds_the_stream(self):
        if not shutil.which('ffmpeg'):
            self.skipTest('ffmpeg not on PATH')
        tmp = tempfile.mkdtemp(prefix='hls-ts-')
        self.addCleanup(shutil.rmtree, tmp, True)
        src = os.path.join(tmp, 'really.m3u8')
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=192x108:rate=25:duration=2', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=2', '-c:v', 'libx264', '-preset',
                        'ultrafast', '-c:a', 'aac', '-f', 'mpegts', src],
                       check=True, timeout=60)
        out = os.path.join(tmp, 'cap.ts')
        argv = build_capture_cmd(CFG, src, out, keep_video_track=True, hls_program=0)
        self.assertIn('0:p:0:v?', argv)
        subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)
        self.assertEqual(sorted(s['codec_type'] for s in _streams(out)), ['audio', 'video'])


class RecorderTests(unittest.TestCase):
    """Which streams earn the playlist read, off the recorder's real launch path."""

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        self.acct = seed.make_account(name='Variant Acct')

    def _launch(self, url, variant=None):
        ch = seed.make_channel(self.acct, stream_id=91, name='Variant Channel')
        ch.stream_url = ch.raw_stream_url = url
        rec = seed.make_recording(status='IN_PROGRESS', channel_id=ch.id, url=url)
        db.session.commit()
        cfg = load_config()
        cfg['recording']['dvr_output_dir'] = self.t._tmpdir
        cfg['ffmpeg']['extra_output_args'] = []
        fake = mock.MagicMock(pid=4242)
        fake.poll.return_value = None
        with mock.patch('app.recorder.load_config', return_value=cfg), \
                mock.patch('app.recorder._reresolve_channel_url'), \
                mock.patch('app.recorder.pick_hls_variant', return_value=variant) as pick, \
                mock.patch('app.recorder.subprocess.Popen', return_value=fake) as popen:
            recorder._launch_segment(self.t.app, rec.id, seg_num=1)
        return rec, pick, popen.call_args[0][0]

    def test_a_third_party_master_is_read_and_its_best_quality_mapped(self):
        rec, pick, argv = self._launch(THIRD_PARTY, HlsVariant(1, 2, '640x360'))
        pick.assert_called_once()
        self.assertEqual(_maps(argv), ['0:p:1:v?', '0:p:1:a?'])
        db.session.expire_all()
        detail = [e.detail for e in rec.events if e.event_type == 'SEGMENT_STARTED'][0]
        self.assertIn('recording 640x360 of the 2 qualities', detail)

    def test_a_restart_stays_on_the_quality_the_first_read_chose(self):
        """Found by review: re-picking at a restart, when a read is likeliest to fail, dropped
        the rest of the run to the first quality listed."""
        state = recorder.RecordingState()
        chosen = HlsVariant(1, 2, '640x360')
        cfg = {}
        with mock.patch('app.recorder.pick_hls_variant', return_value=chosen) as pick:
            self.assertEqual(recorder._hls_variant_for(state, THIRD_PARTY, cfg), chosen)
            self.assertEqual(recorder._hls_variant_for(state, THIRD_PARTY, cfg), chosen)
        pick.assert_called_once()

    def test_a_read_that_failed_is_asked_again_and_said_on_the_segment(self):
        state = recorder.RecordingState()
        with mock.patch('app.recorder.pick_hls_variant',
                        return_value=HlsVariant(0, 0, None)) as pick:
            recorder._hls_variant_for(state, THIRD_PARTY, {})
            recorder._hls_variant_for(state, THIRD_PARTY, {})
        self.assertEqual(pick.call_count, 2)
        rec, _pick, argv = self._launch(THIRD_PARTY, HlsVariant(0, 0, None))
        self.assertEqual(_maps(argv), ['0:p:0:v?', '0:p:0:a?'])
        db.session.expire_all()
        detail = [e.detail for e in rec.events if e.event_type == 'SEGMENT_STARTED'][0]
        self.assertIn('could not be read', detail)

    def test_a_stream_with_a_login_in_its_url_gets_no_extra_connection(self):
        """A connection of its own on a single-seat login could cost the capture itself."""
        _rec, pick, argv = self._launch(PROVIDER_HLS)
        pick.assert_not_called()
        self.assertEqual(_maps(argv), ['0:p:0:v?', '0:p:0:a?'])

    def test_a_plain_stream_is_untouched(self):
        _rec, pick, argv = self._launch(PLAIN_TS)
        pick.assert_not_called()
        self.assertEqual(_maps(argv), ['0:v?', '0:a?'])


if __name__ == '__main__':
    unittest.main()
