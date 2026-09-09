"""Tier 1 unit for the short-capture verdict and the clip-length-aware screenshot seek
(dev/docs/BUGS.md 2026-08-28 09:35 pm - a 5s health-check profile hard-failed every channel
it tested, because the verdict compared the captured length against an absolute 10s floor
rather than against the duration the test asked for).

The verdict cases are pure. The seek case runs the real ffmpeg over a locally synthesized
clip (Class I: verify external tools empirically) and self-skips if ffmpeg is unavailable.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.channel_tester import short_capture_verdict  # noqa: E402
from app.config import resolve_ffmpeg_path  # noqa: E402
from app.screenshot import capture_screenshot, seek_args_for_clip  # noqa: E402

_FFMPEG = resolve_ffmpeg_path('ffmpeg')
_HAVE_FFMPEG = shutil.which(_FFMPEG) is not None or (
    os.path.isabs(_FFMPEG) and os.path.exists(_FFMPEG))


class ShortCaptureVerdictTests(unittest.TestCase):
    def test_five_second_profile_that_delivered_its_full_length_passes(self):
        """The reported defect: both measured captures were marked FAILED with
        'Stream disconnected after only 5s (expected 5s)'."""
        for actual in (5.063678, 5.052678, 5.189890):
            verdict, msg = short_capture_verdict(actual, 5)
            self.assertIsNone(verdict, f'{actual}s of a 5s test should be a clean pass')
            self.assertIsNone(msg)

    def test_five_second_profile_still_fails_when_the_stream_dies_early(self):
        verdict, msg = short_capture_verdict(1.0, 5)
        self.assertEqual(verdict, 'fail')
        self.assertIn('1.0s', msg)
        self.assertIn('5s', msg)

    def test_five_second_profile_warns_between_the_two_thresholds(self):
        # fail below 2.5s, warn below 4.0s (and only past the 0.5s grace).
        self.assertEqual(short_capture_verdict(3.0, 5)[0], 'warn')
        self.assertEqual(short_capture_verdict(2.49, 5)[0], 'fail')

    def test_thirty_second_profile_thresholds_are_unchanged(self):
        """Every duration of 20s or more keeps the historical absolute 10s hard floor."""
        self.assertEqual(short_capture_verdict(9.9, 30)[0], 'fail')
        self.assertEqual(short_capture_verdict(10.5, 30)[0], 'warn')
        self.assertIsNone(short_capture_verdict(30.05, 30)[0])

    def test_two_minute_profile_thresholds_are_unchanged(self):
        self.assertEqual(short_capture_verdict(9.9, 120)[0], 'fail')
        self.assertEqual(short_capture_verdict(25.0, 120)[0], 'warn')
        self.assertIsNone(short_capture_verdict(120.2, 120)[0])

    def test_one_second_profile_is_judged_honestly(self):
        """A -t 1 stream copy measures ~1.19s on this box, so a healthy 1s test passes;
        the sub-half-second grace keeps container rounding from spending a warn penalty."""
        self.assertIsNone(short_capture_verdict(1.189890, 1)[0])
        self.assertIsNone(short_capture_verdict(0.79, 1)[0])   # inside the grace
        self.assertEqual(short_capture_verdict(0.4, 1)[0], 'fail')

    def test_unprobed_or_unconfigured_duration_is_never_a_verdict(self):
        self.assertIsNone(short_capture_verdict(None, 30)[0])
        self.assertIsNone(short_capture_verdict(5.0, 0)[0])
        self.assertIsNone(short_capture_verdict(5.0, None)[0])


class SeekArgsForClipTests(unittest.TestCase):
    def test_long_clips_keep_the_five_second_default(self):
        for duration in (10, 12.5, 30, 120):
            self.assertEqual(seek_args_for_clip(duration), ['-ss', '5.00'])

    def test_short_clips_seek_to_the_midpoint(self):
        self.assertEqual(seek_args_for_clip(5.18), ['-ss', '2.59'])
        self.assertEqual(seek_args_for_clip(3.18), ['-ss', '1.59'])
        self.assertEqual(seek_args_for_clip(1.18), ['-ss', '0.59'])

    def test_seek_point_is_always_inside_the_clip(self):
        for duration in (0.5, 1, 2, 3.19, 5.19, 9.9, 30):
            seek = float(seek_args_for_clip(duration)[1])
            self.assertLess(seek, duration)

    def test_unknown_duration_keeps_the_default(self):
        self.assertEqual(seek_args_for_clip(None), ['-ss', '5'])
        self.assertEqual(seek_args_for_clip(0), ['-ss', '5'])


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class ShortClipScreenshotTests(unittest.TestCase):
    """A seek past the end of a clip returns no frame at all, which the tester reads as
    'screenshot capture failed' - so the seek point has to follow the clip's length."""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix='dvr_short_shot_test_')

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def _gen_clip(self, name, duration):
        path = os.path.join(self._dir, name)
        source = f'testsrc=s=160x90:r=30:d={duration}'
        subprocess.run([_FFMPEG, '-y', '-f', 'lavfi', '-i', source, '-c:v', 'mpeg4', path],
                       capture_output=True, check=True)
        return path

    def test_three_second_clip_yields_a_frame_with_the_clip_aware_seek(self):
        clip = self._gen_clip('short.mp4', 3)
        out = os.path.join(self._dir, 'ok.jpg')
        self.assertTrue(capture_screenshot(clip, out, _FFMPEG,
                                           seek_args=seek_args_for_clip(3.0)))
        self.assertGreater(os.path.getsize(out), 0)

    def test_the_fixed_five_second_seek_yields_nothing_on_that_same_clip(self):
        clip = self._gen_clip('short_fixed_seek.mp4', 3)
        out = os.path.join(self._dir, 'fail.jpg')
        self.assertFalse(capture_screenshot(clip, out, _FFMPEG, seek_args=['-ss', '5']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
