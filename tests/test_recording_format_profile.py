"""Tier 2 - the stream format profile on recordings and their segments (dev/changelog/335).

app/probe.py::parse_ffprobe() has returned the full format profile since the quality-profile
work - video_codec, pix_fmt, bit_depth, chroma_subsampling, interlaced, coded_resolution,
is_vfr - and ChannelTest stored all of it. Both recording-side callers threw it away: the
watchdog's per-segment probe kept four keys, the post-processor's kept six. So the app could
say a channel test was 10-bit HEVC 4:2:2 interlaced and could not say the same about the
recording made from that channel five minutes later.

These are characterization tests for the most part - the discarded fields were never wrong,
they were never stored - except the parse-parity test, which guards the empirical claim the
capture-time half rests on: that the seven format fields read the same off a partial file as
off the finished one. If that stops holding, the watchdog is storing a guess.

Fixtures are synthesized locally with ffmpeg; no network, no provider streams, no /dvr.
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

import app.config as cfgmod  # noqa: E402
import app.watchdog as wdmod  # noqa: E402
from app import db  # noqa: E402
from app.database import RecordingSegment  # noqa: E402
from app.postprocessor import _format_profile_summary  # noqa: E402
from app.probe import parse_ffprobe  # noqa: E402
from app.recorder import RecordingState  # noqa: E402
from app.watchdog import WatchdogThread  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=120)


class FormatProfileSummaryTests(unittest.TestCase):
    """_format_profile_summary is pure and every column it reads is nullable, so the
    common case on a pre-feature row is all-NULL, not a full profile."""

    def test_full_profile_renders_every_part(self):
        s = _format_profile_summary({
            'recorded_video_codec': 'hevc', 'recorded_pix_fmt': 'yuv422p10le',
            'recorded_bit_depth': 10, 'recorded_chroma_subsampling': '422',
            'recorded_interlaced': True, 'recorded_is_vfr': False,
            'recorded_bits_per_pixel_frame': 0.0965,
        })
        for part in ('hevc', 'yuv422p10le', '10-bit', '422', 'interlaced', 'CFR',
                     '0.0965 bits/pixel/frame'):
            self.assertIn(part, s)

    def test_progressive_and_vfr_render_as_their_own_words(self):
        s = _format_profile_summary({'recorded_interlaced': False, 'recorded_is_vfr': True})
        self.assertIn('progressive', s)
        self.assertIn('VFR', s)

    def test_unknown_scan_and_rate_are_omitted_not_guessed(self):
        """NULL means unknown on both columns; rendering it as 'progressive'/'CFR' would be
        the one-flag-one-meaning defect, a False that really meant 'we could not tell'."""
        s = _format_profile_summary({'recorded_video_codec': 'h264',
                                     'recorded_interlaced': None, 'recorded_is_vfr': None})
        self.assertIn('h264', s)
        for word in ('progressive', 'interlaced', 'CFR', 'VFR'):
            self.assertNotIn(word, s)

    def test_all_null_row_says_unknown_rather_than_rendering_empty(self):
        self.assertEqual(_format_profile_summary({}), 'output format: unknown')

    def test_summary_names_the_file_it_describes(self):
        """A re-encode changes codec and pixel format, so 'output' is load-bearing - these
        columns are not necessarily the profile of what the provider sent."""
        self.assertTrue(_format_profile_summary({}).startswith('output format:'))


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class PartialFileProfileParityTests(unittest.TestCase):
    """The watchdog probes a segment that is still growing. Verified on this machine
    2026-07-26 for progressive H.264 and interlaced MPEG-2: all seven format fields read
    identically off a truncated file and the finished one, so a capture-time read is a
    measurement rather than a guess. Empirical and version-specific - this is the test that
    notices if a future ffmpeg stops filling field_order or avg_frame_rate from the header.
    """

    _PROFILE_KEYS = ('video_codec', 'pix_fmt', 'bit_depth', 'chroma_subsampling',
                     'interlaced', 'coded_resolution', 'is_vfr')

    @classmethod
    def setUpClass(cls):
        cls._dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_fmtfix')
        os.makedirs(cls._dir, exist_ok=True)

        cls.progressive = os.path.join(cls._dir, 'progressive.ts')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=10', '-t', '20',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-b:v', '1500k',
                '-pix_fmt', 'yuv420p', cls.progressive)

        cls.interlaced = os.path.join(cls._dir, 'interlaced.ts')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=30', '-t', '10',
                '-vf', 'interlace', '-c:v', 'mpeg2video', '-b:v', '1500k',
                '-flags', '+ilme+ildct', '-top', '1', cls.interlaced)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def _truncated(self, src):
        dest = os.path.join(self._dir, 'partial.ts')
        shutil.copyfile(src, dest)
        with open(dest, 'r+b') as fh:
            fh.truncate(os.path.getsize(src) // 3)
        return dest

    def _assert_parity(self, src):
        full = parse_ffprobe(src, count_packets=False, timeout=15)
        partial = parse_ffprobe(self._truncated(src), count_packets=False, timeout=15)
        for key in self._PROFILE_KEYS:
            self.assertEqual(partial.get(key), full.get(key),
                             f'{key} differs between a partial read and the finished file, '
                             f'so the watchdog would be storing a guess')

    def test_progressive_h264_profile_survives_a_partial_read(self):
        self._assert_parity(self.progressive)
        self.assertIs(parse_ffprobe(self.progressive, count_packets=False).get('interlaced'),
                      False, 'fixture is not progressive - the parity check proves nothing')

    def test_interlaced_mpeg2_profile_survives_a_partial_read(self):
        self._assert_parity(self.interlaced)
        self.assertIs(parse_ffprobe(self.interlaced, count_packets=False).get('interlaced'),
                      True, 'fixture is not interlaced - the parity check proves nothing')


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class WatchdogSegmentProfileTests(unittest.TestCase):
    """The real WatchdogThread against a real segment file: the format columns must be
    written by the same single probe that already fills probe_resolution, not a second one.
    """

    @classmethod
    def setUpClass(cls):
        cls._dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_wdfix')
        os.makedirs(cls._dir, exist_ok=True)
        cls.src = os.path.join(cls._dir, 'seg.ts')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=10', '-t', '10',
                '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '10',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', cls.src)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def setUp(self):
        self.t = make_test_app()
        self.wd = None
        self.state = RecordingState(current_segment_num=0)

        now = datetime.utcnow()
        rec = seed.make_recording(status='IN_PROGRESS', name='fmt', started_at=now,
                                  start_time=now, stop_time=now + timedelta(hours=1))
        self.rid = rec.id
        self.seg_path = os.path.join(self.t._tmpdir, f'rec_{self.rid}_seg_000.ts')
        shutil.copyfile(self.src, self.seg_path)
        db.session.add(RecordingSegment(recording_id=self.rid, segment_number=0,
                                        file_path=self.seg_path, started_at=now))
        db.session.commit()

    def tearDown(self):
        self.state.stop_event.set()
        if self.wd is not None:
            self.wd.join(timeout=10)
        self.t.cleanup()

    def _run_watchdog_until_probed(self):
        """Start the real thread and stop it the moment the probe lands. The stall timeout
        is set an hour out so a static file never reaches the restart path - restarts are
        not what is under test and would spawn ffmpeg."""
        cfg = cfgmod._deep_merge(cfgmod.load_config(), {'watchdog': {
            'poll_interval_seconds': 1, 'stall_timeout_seconds': 3600,
            'restart_delay_seconds': 1, 'max_consecutive_failures': 99,
        }})
        # The 2 MB gate exists so a header read can't hammer every poll; it is not what
        # this test is about, and honoring it would mean a multi-megabyte fixture.
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(wdmod, '_PROBE_MIN_BYTES', 1024):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                db.session.expire_all()
                seg = RecordingSegment.query.filter_by(
                    recording_id=self.rid, segment_number=0).first()
                if seg.probed_at is not None:
                    break
                time.sleep(0.25)
            self.state.stop_event.set()
            self.wd.join(timeout=10)
        db.session.expire_all()
        return RecordingSegment.query.filter_by(
            recording_id=self.rid, segment_number=0).first()

    def test_segment_probe_persists_the_capture_format_profile(self):
        seg = self._run_watchdog_until_probed()

        self.assertIsNotNone(seg.probed_at, 'the watchdog never probed the segment')
        self.assertEqual(seg.probe_video_codec, 'h264')
        self.assertEqual(seg.probe_pix_fmt, 'yuv420p')
        self.assertEqual(seg.probe_bit_depth, 8)
        self.assertEqual(seg.probe_chroma_subsampling, '420')
        self.assertIs(seg.probe_interlaced, False)
        self.assertIsNotNone(seg.probe_is_vfr)

    def test_the_format_fields_come_from_the_existing_probe_not_a_second_one(self):
        """_PROBE_MAX_ATTEMPTS bounds the per-segment probe precisely so a header read
        cannot hammer every poll; adding a probe for the format half would be a real
        regression on every active recording."""
        real = wdmod.parse_ffprobe
        spy = mock.Mock(side_effect=real)
        with mock.patch.object(wdmod, 'parse_ffprobe', spy):
            seg = self._run_watchdog_until_probed()

        self.assertIsNotNone(seg.probe_video_codec)
        self.assertEqual(spy.call_count, 1,
                         f'segment probed {spy.call_count} times; the format fields must '
                         f'come out of the same single call as probe_resolution')

    def test_the_old_four_fields_are_still_written(self):
        """Characterization: extending the closure must not disturb what it already did."""
        seg = self._run_watchdog_until_probed()

        self.assertEqual(seg.probe_resolution, '320x180')
        self.assertIsNotNone(seg.probe_fps)
        self.assertEqual(seg.probe_audio_codec, 'aac')
        self.assertIsNotNone(seg.probe_audio_channels)


if __name__ == '__main__':
    unittest.main()
