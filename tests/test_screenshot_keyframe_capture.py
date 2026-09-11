"""Tier 1 unit for the keyframe-only frame grab (app/screenshot.py::capture_screenshot).

Guards dev/docs/BUGS.md 2026-09-09 11:24 - an ffmpeg input seek into an MPEG-TS lands at a
byte offset rather than a keyframe, so decoding began mid-GOP. ffmpeg's h264 decoder hides
that by suppressing output until a recovery point; its hevc decoder emits a gray canvas with
inter-prediction residuals painted on, which the tester's uniformity check then reported as
"Screenshot appears solid color". Every HEVC channel carried that unearned warning and a
health score depressed by it (dev/changelog/894).

The invariant, in one sentence: a frame grabbed from a mid-GOP-joined clip has real content
in it, whatever the codec.

Fixtures are encoded on this machine with the real ffmpeg (Class I: verify external tools
empirically) at a long GOP, then sliced on a 188-byte TS packet boundary so the clip begins
mid-GOP exactly as a capture that joined a live stream does. The class self-skips without
ffmpeg or libx265; the command-shape and seek-widening tests below need neither and always
run.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.channel_tester import _check_screenshot_uniform  # noqa: E402
from app.screenshot import (  # noqa: E402
    _capture_attempts,
    capture_screenshot,
    widened_seek_args,
)

# Gated on a SYSTEM ffmpeg, which is what every other ffmpeg-dependent test file here does.
# app.config.resolve_ffmpeg_path is deliberately not used: a resolver whose job is to
# supply a path is the wrong source for "is this tool installed", and it used to prove it -
# its imageio-ffmpeg fallback returned a path that existed on a machine with no ffmpeg at
# all, these fixtures produce no frame on that build, and the class ran instead of skipping
# and failed five ways (dev/changelog/903). That fallback is gone (dev/changelog/911); the
# reason not to gate a skip on a resolver is not.
_FFMPEG = 'ffmpeg'
_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def _have_encoder(name: str) -> bool:
    if not _HAVE_FFMPEG:
        return False
    try:
        r = subprocess.run([_FFMPEG, '-hide_banner', '-encoders'],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return name.encode() in r.stdout


_HAVE_X265 = _have_encoder('libx265')


class SeekWideningTests(unittest.TestCase):
    """The fallback seek, and the ladder built from it. Pure - no ffmpeg needed."""

    def test_forward_seek_widens_to_the_top_of_the_file(self):
        self.assertEqual(widened_seek_args(['-ss', '2.50']), ['-ss', '0'])

    def test_a_seek_already_at_zero_has_nowhere_wider_to_go(self):
        self.assertIsNone(widened_seek_args(['-ss', '0']))

    def test_end_relative_seek_widens_by_a_bounded_amount(self):
        self.assertEqual(widened_seek_args(['-sseof', '-3']), ['-sseof', '-12.00'])

    def test_unrecognized_seek_shapes_decline_rather_than_guess(self):
        self.assertIsNone(widened_seek_args(['-fflags', '+genpts']))
        self.assertIsNone(widened_seek_args(['-ss']))
        self.assertIsNone(widened_seek_args(['-ss', 'not-a-number']))

    def test_last_attempt_reproduces_the_pre_fix_command(self):
        """A file with no decodable keyframe must still get whatever it got before."""
        attempts = _capture_attempts(['-ss', '2.50'])
        self.assertEqual(attempts[-1], (['-ss', '2.50'], False))
        self.assertTrue(all(kf for _, kf in attempts[:-1]))

    def test_widened_seek_is_tried_before_giving_up_on_keyframes(self):
        self.assertEqual(_capture_attempts(['-ss', '2.50']), [
            (['-ss', '2.50'], True),
            (['-ss', '0'], True),
            (['-ss', '2.50'], False),
        ])

    def test_unwidenable_seek_still_gets_a_keyframe_attempt_first(self):
        self.assertEqual(_capture_attempts(['-ss', '0']), [
            (['-ss', '0'], True),
            (['-ss', '0'], False),
        ])


class CaptureCommandShapeTests(unittest.TestCase):
    """The decoder flag reaches ffmpeg, and reaches it as an *input* option.

    Placement matters: -skip_frame configures the decoder attached to the input, so it is
    only honored before -i. After -i it would be parsed as an output option and silently
    do nothing, which is the whole defect back again with no error to notice it by.
    """

    def _argv_of_first_run(self, **kwargs):
        seen = []

        def fake_run(cmd, **_):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, b'', b'')

        with mock.patch('app.screenshot.subprocess.run', side_effect=fake_run), \
                mock.patch('app.screenshot.os.makedirs'):
            capture_screenshot('/nonexistent/clip.ts', '/tmp/x/out.jpg', 'ffmpeg', **kwargs)
        return seen

    def test_first_attempt_decodes_keyframes_only(self):
        argv = self._argv_of_first_run(seek_args=['-ss', '2.50'])[0]
        self.assertIn('-skip_frame', argv)
        self.assertEqual(argv[argv.index('-skip_frame') + 1], 'nokey')

    def test_keyframe_flag_precedes_the_input(self):
        argv = self._argv_of_first_run(seek_args=['-ss', '2.50'])[0]
        self.assertLess(argv.index('-skip_frame'), argv.index('-i'))

    def test_seek_precedes_the_keyframe_flag_and_the_input(self):
        argv = self._argv_of_first_run(seek_args=['-ss', '2.50'])[0]
        self.assertLess(argv.index('-ss'), argv.index('-skip_frame'))

    def test_every_rung_is_walked_before_reporting_failure(self):
        runs = self._argv_of_first_run(seek_args=['-ss', '2.50'])
        self.assertEqual(len(runs), 3)
        self.assertNotIn('-skip_frame', runs[-1])

    def test_a_wide_frame_alone_does_not_change_the_ladder(self):
        """Frame size is not a colorspace - the HDR branch turns on the transfer only.

        This replaces a test that fed probe={'vid_width': 3840} to reach the tonemap
        chain and asserted the chain was tried exactly once. Both halves of it were the
        defect rather than the invariant: width is what wrongly dragged SDR content in,
        and latching the chain off after one failure denied it to the wider seek that
        existed precisely because the first held no frame. tests/
        test_screenshot_hdr_tonemap.py owns that branch now (dev/changelog/915).
        """
        runs = self._argv_of_first_run(seek_args=['-ss', '2.50'],
                                       probe={'vid_width': 3840})
        self.assertEqual(len(runs), 3)
        self.assertEqual([], [c for c in runs if any('tonemap' in a for a in c)])


@unittest.skipUnless(_HAVE_FFMPEG and _HAVE_X265, 'ffmpeg with libx265 not available')
class MidGopClipCaptureTests(unittest.TestCase):
    """The real defect, on real encoded video."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='dvr_kf_shot_')
        cls._sources = {
            'hevc': cls._encode('hevc', 'libx265', [
                '-x265-params', 'keyint=125:min-keyint=125:scenecut=0:log-level=none']),
            'h264': cls._encode('h264', 'libx264', [
                '-g', '125', '-keyint_min', '125', '-sc_threshold', '0']),
        }

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    @classmethod
    def _encode(cls, name, encoder, opts):
        """A 6s 25fps clip with a 5s GOP - one keyframe at the top, one at 5s."""
        path = os.path.join(cls._dir, f'src_{name}.ts')
        subprocess.run(
            [_FFMPEG, '-y', '-v', 'error', '-f', 'lavfi',
             '-i', 'testsrc2=size=320x180:rate=25:duration=6',
             '-c:v', encoder, '-preset', 'ultrafast', *opts,
             '-pix_fmt', 'yuv420p', '-f', 'mpegts', path],
            capture_output=True, check=True)
        return path

    def _slice(self, codec, name, start_frac, end_frac):
        """Cut a byte range on a TS packet boundary - a clip that joined mid-GOP.

        188 is the MPEG-TS packet size; cutting on a multiple of it leaves a stream a
        demuxer can still read, which is exactly what capturing from a live stream
        already in progress produces.
        """
        with open(self._sources[codec], 'rb') as fh:
            data = fh.read()
        a = (int(len(data) * start_frac) // 188) * 188
        b = (int(len(data) * end_frac) // 188) * 188
        path = os.path.join(self._dir, f'{name}.ts')
        with open(path, 'wb') as fh:
            fh.write(data[a:b])
        return path

    def _grab(self, clip, seek_args=None):
        out = os.path.join(self._dir, 'shot.jpg')
        if os.path.exists(out):
            os.unlink(out)
        ok = capture_screenshot(clip, out, _FFMPEG,
                                seek_args=seek_args or ['-ss', '2.5'])
        return ok, out

    def test_hevc_clip_joined_mid_gop_yields_a_frame_with_real_content(self):
        clip = self._slice('hevc', 'hevc_midgop', 0.30, 0.95)
        ok, out = self._grab(clip)
        self.assertTrue(ok, 'no screenshot produced at all')
        self.assertIsNone(_check_screenshot_uniform(out, _FFMPEG))

    def test_the_pre_fix_command_is_what_produced_the_blank_frame(self):
        """Characterizes the defect itself, so a future reader can see it is real.

        Not a guard on app code - it runs the old argv directly - but it is what makes
        the test above meaningful rather than an assertion that was already true.
        """
        clip = self._slice('hevc', 'hevc_midgop_legacy', 0.30, 0.95)
        out = os.path.join(self._dir, 'legacy.jpg')
        subprocess.run(
            [_FFMPEG, '-v', 'quiet', '-ss', '2.5', '-i', clip,
             '-vf', 'scale=min(iw\\,1920):min(ih\\,1080):force_original_aspect_ratio=decrease',
             '-vframes', '1', '-q:v', '3', '-y', out],
            capture_output=True)
        self.assertTrue(os.path.getsize(out) > 0)
        self.assertIsNotNone(_check_screenshot_uniform(out, _FFMPEG))

    def test_h264_clip_joined_mid_gop_is_unaffected(self):
        clip = self._slice('h264', 'h264_midgop', 0.30, 0.95)
        ok, out = self._grab(clip)
        self.assertTrue(ok)
        self.assertIsNone(_check_screenshot_uniform(out, _FFMPEG))

    def test_keyframe_only_before_the_seek_point_falls_back_to_the_wider_seek(self):
        """The clip holds its one keyframe near the top, so scanning forward finds none."""
        clip = self._slice('hevc', 'hevc_kf_at_top', 0.0, 0.60)
        ok, out = self._grab(clip, seek_args=['-ss', '2.5'])
        self.assertTrue(ok, 'widened seek did not recover a frame')
        self.assertIsNone(_check_screenshot_uniform(out, _FFMPEG))

    def test_end_relative_grab_used_by_live_thumbnails_is_fixed_too(self):
        """recorder.py and the live-thumbnail route both seek with -sseof."""
        ok, out = self._grab(self._sources['hevc'], seek_args=['-sseof', '-3'])
        self.assertTrue(ok)
        self.assertIsNone(_check_screenshot_uniform(out, _FFMPEG))


if __name__ == '__main__':
    unittest.main()
