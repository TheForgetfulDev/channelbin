"""Tier 1 unit for the blank-frame detector (app/channel_tester.py::_check_screenshot_uniform).

BUGS.md-adjacent (Class B "one flag one meaning" - the 64×64 rework that stopped
field-dominant sports frames from being smoothed into a false "solid color" reading).
The invariant: a genuinely uniform frame is flagged; a frame with real local detail is not.

Fixtures are generated on this machine with the real ffmpeg (Class I: verify external tools
empirically), so the test self-skips if ffmpeg is unavailable. Files are tiny and land in a
temp dir, cleaned up in tearDown.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.channel_tester import _check_screenshot_uniform  # noqa: E402
from app.config import resolve_ffmpeg_path  # noqa: E402

_FFMPEG = resolve_ffmpeg_path('ffmpeg')
_HAVE_FFMPEG = shutil.which(_FFMPEG) is not None or (
    os.path.isabs(_FFMPEG) and os.path.exists(_FFMPEG))


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class ScreenshotUniformTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix='dvr_shot_test_')

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def _gen(self, source, name):
        path = os.path.join(self._dir, name)
        subprocess.run(
            [_FFMPEG, '-y', '-f', 'lavfi', '-i', source, '-frames:v', '1', path],
            capture_output=True, check=True)
        return path

    def test_solid_black_flagged(self):
        p = self._gen('color=c=black:s=320x180', 'black.jpg')
        warn = _check_screenshot_uniform(p, _FFMPEG)
        self.assertIsNotNone(warn)
        self.assertIn('black', warn.lower())

    def test_solid_white_flagged(self):
        p = self._gen('color=c=white:s=320x180', 'white.jpg')
        warn = _check_screenshot_uniform(p, _FFMPEG)
        self.assertIsNotNone(warn)
        self.assertIn('white', warn.lower())

    def test_solid_midcolor_flagged(self):
        p = self._gen('color=c=0x808080:s=320x180', 'grey.jpg')
        self.assertIsNotNone(_check_screenshot_uniform(p, _FFMPEG))

    def test_high_detail_frame_not_flagged(self):
        # random noise has high per-pixel variance → real content, must NOT be flagged
        p = self._gen('nullsrc=s=320x180,geq=random(1)*255:128:128', 'noise.jpg')
        self.assertIsNone(_check_screenshot_uniform(p, _FFMPEG))

    def test_a_dim_scene_with_lit_pixels_is_a_picture_not_black(self):
        """dev/docs/BUGS.md 2026-11-09 (the black case). 8% of the frame at luma 50 on black:
        the 64x64 contrast is 13.1 (under the 15 that reads as solid) so the old black case
        called this blank, while the full-size count says 8% of its pixels are lit - the
        shape of the three real dim scenes with people in them (dev/changelog/1433)."""
        p = self._gen('color=c=black:s=320x180,drawbox=x=10:y=10:w=92:h=50:color=#323232:t=fill',
                      'dim.jpg')
        self.assertIsNone(_check_screenshot_uniform(p, _FFMPEG))

    def test_a_logo_alone_on_black_is_black(self):
        """The other direction: a 24x12 white mark on black lifts the 64x64 contrast to 16.5,
        so the old test passed it as a picture; 0.5% of the pixels are lit, under the 1% the
        full-size rule calls black."""
        p = self._gen('color=c=black:s=320x180,drawbox=x=280:y=10:w=24:h=12:color=white:t=fill',
                      'logo.jpg')
        warn = _check_screenshot_uniform(p, _FFMPEG)
        self.assertIsNotNone(warn)
        self.assertIn('black', warn.lower())

    def test_missing_file_returns_none(self):
        # unreadable input → warning swallowed, returns None (no crash)
        self.assertIsNone(_check_screenshot_uniform(
            os.path.join(self._dir, 'nope.jpg'), _FFMPEG))


if __name__ == '__main__':
    unittest.main(verbosity=2)
