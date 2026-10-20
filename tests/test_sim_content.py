"""The content library: loops rendered byte-identically, recorded with their length, kept under
a disk budget, and chosen per channel from its measured format (DESIGN-simulator.md §3.3,
§9.5; dev/changelog/1219).
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from fractions import Fraction
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import ContentError, Library, PROFILES, WorldError, build_world  # noqa: E402
from sim import content as content_mod  # noqa: E402
from sim.content import check_profiles, profile_for_format  # noqa: E402
from sim.ts import PTS_HZ  # noqa: E402

from tests.support.sim_streams import STREAM_WORLD, library  # noqa: E402


class ProfileTests(unittest.TestCase):
    def test_every_profile_loops_on_whole_frames(self):
        """Whole video and audio frames, whole GOPs, whole 90 kHz ticks - or the seam is not
        seamless (DESIGN-simulator.md §9.1)."""
        self.assertEqual(check_profiles(), [])

    def test_the_loop_length_is_the_frame_count_over_the_rate(self):
        p = PROFILES['h264_720p5994_aac']
        self.assertEqual(p.seconds, Fraction(64064, 1000))
        self.assertEqual(p.length90, 5_765_760)

    def test_a_render_is_keyed_on_what_decides_its_bytes_and_nothing_else(self):
        """A profile's summary is not part of its picture, so changing it must not make every
        cached render stale - a full library is 25 minutes of rendering."""
        lib = Library(tempfile.mkdtemp(prefix='sim-key-'))
        self.addCleanup(shutil.rmtree, lib.directory, True)
        p = PROFILES['h264_720p5994_aac']
        before = lib.spec_key(p.name)
        with mock.patch.dict(PROFILES, {p.name: replace(p, summary='reworded')}):
            self.assertEqual(lib.spec_key(p.name), before)
        with mock.patch.dict(PROFILES, {p.name: replace(p, video_kbps=p.video_kbps + 1)}):
            self.assertNotEqual(lib.spec_key(p.name), before)

    def test_formats_map_to_the_nearest_loop(self):
        cases = {
            '720p59.94/h264/aac/2': 'h264_720p5994_aac',
            '720p29.97/h264/aac/2': 'h264_720p2997_aac',
            '1080p59.94/h264/aac/2': 'h264_1080p5994_aac',
            '1080p30/h264/aac/2': 'h264_1080p2997_aac',
            '1080p50/h264/aac/2': 'h264_1080p5994_aac',
            '1080p25/h264/aac/2': 'h264_1080p2997_aac',
            '1080p59.94/h264/aac/6': 'h264_1080p5994_aac_51',
            '720p59.94/h264/ac3/6': 'h264_1080p5994_ac3_51',
            '1080p59.94/h264/eac3/6': 'h264_1080p5994_eac3_51',
            '1080p59.94/hevc/aac/2': 'hevc_1080p5994_hdr10',
            '2160p50/hevc/aac/2': 'hevc_2160p5994_hdr10',
            '576p25/h264/aac/2': 'h264_576p25_aac',
            '480p29.97/h264/aac/2': 'h264_576p25_aac',
            'audio/mp3/2': 'radio_mp3',
        }
        for fmt, want in cases.items():
            self.assertEqual(profile_for_format(fmt), want, fmt)


class RenderTests(unittest.TestCase):
    """Rendering into throwaway directories, so these see real renders, not the cache. One
    render is shared; a test that changes the cache works on a copy of it."""

    @classmethod
    def setUpClass(cls):
        library()       # skips without ffmpeg
        cls.tmp = tempfile.mkdtemp(prefix='sim-content-')
        cls.base = os.path.join(cls.tmp, 'base')
        Library(cls.base).build(['test_tiny_low'])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def copy(self, name) -> Library:
        path = os.path.join(self.tmp, name)
        shutil.copytree(self.base, path)
        return Library(path)

    def test_two_renders_are_byte_identical_and_recorded(self):
        a = Library(self.base)
        b = Library(os.path.join(self.tmp, 'again'))
        b.build(['test_tiny_low'])
        ma, mb = a.find('test_tiny_low'), b.find('test_tiny_low')
        self.assertEqual(ma['sha256'], mb['sha256'])
        p = PROFILES['test_tiny_low']
        self.assertEqual(ma['length90'], p.frames * PTS_HZ * 1001 // 30000)
        self.assertEqual(ma['video_frames'], p.frames)
        self.assertEqual(ma['audio_frames'], p.samples // 1024)
        self.assertEqual(ma['audio_offset90'], 0, 'audio starts with the video once primed')
        self.assertEqual(a.load('test_tiny_low').L90, ma['length90'])

    def test_connections_arriving_together_parse_a_loop_once(self):
        """Fifty connections arriving at once each parsed their own copy of the loop and the
        box's OOM guard killed the simulator at 4.3 GB (dev/changelog/1219)."""
        lib = self.copy('together')
        real = content_mod.Loop
        made = []

        def counting(*a, **kw):
            made.append(1)
            return real(*a, **kw)

        with mock.patch.object(content_mod, 'Loop', counting):
            threads = [threading.Thread(target=lib.load, args=('test_tiny_low',))
                       for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(len(made), 1)

    def test_a_cached_loop_is_not_rendered_again(self):
        self.assertEqual([r.rendered for r in self.copy('c').build(['test_tiny_low'])], [False])

    def test_a_render_past_the_budget_is_refused_before_it_starts(self):
        lib = Library(os.path.join(self.tmp, 'd'), budget_bytes=100_000)
        with self.assertRaises(ContentError) as cm:
            lib.build(['test_tiny'])
        self.assertIn('budget', str(cm.exception))
        self.assertFalse(lib.has('test_tiny'))

    def test_a_missing_loop_names_the_command_that_renders_it(self):
        lib = Library(os.path.join(self.tmp, 'e'))
        with self.assertRaises(ContentError) as cm:
            lib.load('test_tiny')
        self.assertIn('python3 -m sim content build --profile test_tiny', str(cm.exception))

    def test_a_loop_that_no_longer_matches_its_manifest_is_refused(self):
        lib = self.copy('f')
        with open(lib.path('test_tiny_low'), 'r+b') as f:
            f.seek(1000)
            f.write(b'\x00\x01\x02')
        with self.assertRaises(ContentError):
            lib.load('test_tiny_low')

    def test_prune_removes_a_render_no_profile_has(self):
        lib = self.copy('g')
        stale = os.path.join(lib.directory, 'gone-0000000000000000-00000000.json')
        with open(stale, 'w') as f:
            json.dump({'profile': 'gone', 'file': 'gone-0000000000000000-00000000.ts'}, f)
        open(os.path.join(lib.directory, 'gone-0000000000000000-00000000.ts'), 'wb').close()
        removed = lib.prune()
        self.assertIn('gone-0000000000000000-00000000.ts', removed)
        self.assertTrue(lib.has('test_tiny_low'))


class WorldContentTests(unittest.TestCase):
    def world(self, **backend):
        data = json.loads(json.dumps(STREAM_WORLD))
        data['backends'][0].update(backend)
        return build_world(data)

    def test_a_backend_names_its_loop_or_takes_the_calibrated_mapping(self):
        self.assertEqual(self.world().backends['cove'].content, 'test_tiny')
        self.assertEqual(self.world(content='calibrated').backends['cove'].content,
                         'calibrated')

    def test_bad_stream_settings_are_refused(self):
        for bad in ({'content': 'no_such_loop'}, {'content': 'radio_mp3'},
                    {'hls_variant': 'radio_mp3'}, {'burst_seconds': -1},
                    {'hls_segment_seconds': 0.5}, {'over_limit': {'policy': 'shrug'}},
                    {'over_limit': {'status': 200}}, {'over_limit': {'colour': 'red'}}):
            with self.assertRaises(WorldError, msg=bad):
                self.world(**bad)

    def test_the_burst_defaults_to_the_measured_typical(self):
        data = json.loads(json.dumps(STREAM_WORLD))
        del data['backends'][0]['burst_seconds']
        self.assertEqual(build_world(data).backends['cove'].burst_seconds, 25.0)


if __name__ == '__main__':
    unittest.main()
