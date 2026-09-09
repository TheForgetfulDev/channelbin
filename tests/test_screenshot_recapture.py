"""Tier 1 unit for the screenshot recapture path
(app/channel_tester.py::_capture_screenshot_with_recapture) - item 12 of the easy-bugs batch,
dev/changelog/542.

The test clip's screenshot is pulled from the already-recorded local file, not a live stream,
so recapture is just a second ffmpeg seek+grab on the same file at a later offset. These clips
are synthesized with `geq` so luma is 0 (black) before T=6s and random noise after - the
default 5s capture always lands in the blank region, and a later offset always lands in real
content, giving a deterministic case for each recapture outcome.

Fixtures are generated on this machine with the real ffmpeg (Class I: verify external tools
empirically), so the test self-skips if ffmpeg is unavailable.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.channel_tester import _capture_screenshot_with_recapture  # noqa: E402
from app.config import resolve_ffmpeg_path  # noqa: E402

_FFMPEG = resolve_ffmpeg_path('ffmpeg')
_HAVE_FFMPEG = shutil.which(_FFMPEG) is not None or (
    os.path.isabs(_FFMPEG) and os.path.exists(_FFMPEG))


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class ScreenshotRecaptureTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix='dvr_recapture_test_')

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def _gen_clip(self, name, duration, switch_at=None):
        """Build a clip whose luma is black before switch_at (seconds) and random noise
        from switch_at onward. switch_at=None means noise the whole way through;
        switch_at >= duration means black the whole way through."""
        path = os.path.join(self._dir, name)
        if switch_at is None:
            lum_expr = 'random(1)*255'
        else:
            lum_expr = f"if(lt(T\\,{switch_at})\\,0\\,random(1)*255)"
        source = f"nullsrc=s=64x64:d={duration}:r=1,geq=lum='{lum_expr}':cb=128:cr=128,format=yuv420p"
        subprocess.run(
            [_FFMPEG, '-y', '-f', 'lavfi', '-i', source, '-c:v', 'mpeg4', path],
            capture_output=True, check=True)
        return path

    def test_blank_first_frame_recaptures_real_content(self):
        clip = self._gen_clip('blank_then_real.mp4', duration=12, switch_at=6)
        out = os.path.join(self._dir, 'out.jpg')
        ok, warn, recaptured = _capture_screenshot_with_recapture(clip, out, _FFMPEG, None, 12)
        self.assertTrue(ok)
        self.assertIsNone(warn)
        self.assertTrue(recaptured)

    def test_good_first_frame_skips_recapture(self):
        clip = self._gen_clip('real_throughout.mp4', duration=12, switch_at=None)
        out = os.path.join(self._dir, 'out.jpg')
        ok, warn, recaptured = _capture_screenshot_with_recapture(clip, out, _FFMPEG, None, 12)
        self.assertTrue(ok)
        self.assertIsNone(warn)
        self.assertFalse(recaptured)

    def test_blank_throughout_reports_warning_after_recapture(self):
        clip = self._gen_clip('blank_throughout.mp4', duration=12, switch_at=999)
        out = os.path.join(self._dir, 'out.jpg')
        ok, warn, recaptured = _capture_screenshot_with_recapture(clip, out, _FFMPEG, None, 12)
        self.assertTrue(ok)
        self.assertIsNotNone(warn)
        self.assertTrue(recaptured)

    def test_short_duration_skips_recapture_gracefully(self):
        # actual_duration below the retry threshold - no room to pick a meaningfully
        # different offset, so the first (blank) frame is kept without a second attempt.
        clip = self._gen_clip('short_blank.mp4', duration=8, switch_at=999)
        out = os.path.join(self._dir, 'out.jpg')
        ok, warn, recaptured = _capture_screenshot_with_recapture(clip, out, _FFMPEG, None, 8)
        self.assertTrue(ok)
        self.assertIsNotNone(warn)
        self.assertFalse(recaptured)

    def test_unknown_duration_skips_recapture_gracefully(self):
        clip = self._gen_clip('unknown_duration_blank.mp4', duration=12, switch_at=999)
        out = os.path.join(self._dir, 'out.jpg')
        ok, warn, recaptured = _capture_screenshot_with_recapture(clip, out, _FFMPEG, None, None)
        self.assertTrue(ok)
        self.assertIsNotNone(warn)
        self.assertFalse(recaptured)

    def test_capture_failure_returns_false(self):
        missing = os.path.join(self._dir, 'nope.mp4')
        out = os.path.join(self._dir, 'out.jpg')
        ok, warn, recaptured = _capture_screenshot_with_recapture(missing, out, _FFMPEG, None, 12)
        self.assertFalse(ok)
        self.assertIsNone(warn)
        self.assertFalse(recaptured)


if __name__ == '__main__':
    unittest.main(verbosity=2)
