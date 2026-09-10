"""Tier 2 - a complete capture must not report missing frames (dev/docs/BUGS.md 2026-09-09
"short tests scored as if they dropped frames").

ChannelTest.frame_pct divided the video packet count by `fps * container duration`. Both
halves of that denominator are biased long by a fixed amount per clip, so a complete capture
measured as incomplete, and because the bias is a fixed count of FRAMES rather than a rate,
it is invisible on a long recording and dominates a short test:

  - The container duration spans every stream. Audio that starts before the first video
    frame lengthens it while contributing no frames.
  - The presentation span overshoots the capture window by the reorder depth. `-t` bounds a
    stream copy on DECODE order, so the last packets written are anchor frames presenting
    several frames into the future, while the B-frames between them fall past the cut.

Measured on a real 4K50 HEVC feed with reorder depth 9: a 5-second capture read 96.9%
complete with zero DTS gaps, while a 20-second capture of the same feed in the same minute
read 99.8%. Six channels each lost 3 points of lifetime health score per test for frames
that were never missing.

Expected frames are now counted over the DECODE span, which is the same discipline
scan_video_timeline already applies to gap counting for the same reason (dev/docs/BUGS.md
2026-07-23). Fixtures are synthesized locally with ffmpeg - no app context, no network, no
provider streams.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.probe import (  # noqa: E402
    expected_frame_count, parse_ffprobe, scan_video_timeline,
)

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=120)


def _measured_pct(path):
    """frame_pct exactly as channel_tester._run_one_test computes it."""
    probe = parse_ffprobe(path)
    timeline = scan_video_timeline(path)
    expected = expected_frame_count(
        probe.get('fps'),
        dts_span_seconds=(timeline or {}).get('dts_span_seconds'),
        fallback_duration=probe.get('duration'))
    return round(probe['frame_count'] / expected * 100, 1)


class ExpectedFrameCountTests(unittest.TestCase):
    """Pure-function half - the real numbers off the 4K50 HEVC feed, no fixtures."""

    def test_decode_span_is_preferred_over_container_duration(self):
        """The clip that started this: 251 frames over a 5.000s decode span, in a container
        whose duration reads 5.183s because its audio track starts 43ms before the video."""
        self.assertEqual(expected_frame_count(50.0, 5.0, 5.183), 251.0)

    def test_container_duration_is_only_a_fallback(self):
        """With no decode span available the old denominator is still used - a worse answer
        is better than no answer, and it is what every clip without DTS can offer."""
        self.assertAlmostEqual(expected_frame_count(50.0, None, 5.183), 259.15)

    def test_frames_span_one_fewer_interval_than_their_count(self):
        """N frames span N-1 intervals. Without the +1 a clean capture reads 100.4%, which
        is a defect in the other direction rather than a harmless rounding choice."""
        self.assertEqual(expected_frame_count(50.0, 5.0), 251.0)
        self.assertEqual(expected_frame_count(25.0, 4.0), 101.0)

    def test_unknowable_inputs_return_none_rather_than_a_guess(self):
        self.assertIsNone(expected_frame_count(None, 5.0, 5.183))
        self.assertIsNone(expected_frame_count(0, 5.0, 5.183))
        self.assertIsNone(expected_frame_count(50.0, None, None))


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class TruncatedCaptureTests(unittest.TestCase):
    """Fixtures are built once for the class - each is well under a second to encode."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='framepct-')

        # A complete 20s source with deep B-pyramid reordering. b-adapt=0/scenecut=0 force a
        # fixed B-frame pattern so the fixture is deterministic across ffmpeg builds.
        cls.source = os.path.join(cls._dir, 'source.ts')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=10', '-t', '20',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '8',
                '-x264-params', 'bframes=8:b-adapt=0:scenecut=0:b-pyramid=normal',
                '-pix_fmt', 'yuv420p', cls.source)

        # The defect: the same stream, stream-copied under a -t bound exactly as a channel
        # test captures it. Nothing is missing from the content - the cut simply lands
        # mid-reorder, leaving a sparse tail of anchor frames.
        cls.truncated = os.path.join(cls._dir, 'truncated.ts')
        _ffmpeg('-i', cls.source, '-t', '5', '-c', 'copy', cls.truncated)

        # Genuinely missing content: 5 of 20 seconds dropped, surviving frames keeping their
        # original timestamps, so the file really is short.
        cls.gappy = os.path.join(cls._dir, 'gappy.ts')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=10', '-t', '20',
                '-vf', "select='not(between(t,5,10))'", '-fps_mode', 'passthrough',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '3',
                '-x264-params', 'bframes=3:b-adapt=0:scenecut=0',
                '-pix_fmt', 'yuv420p', cls.gappy)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    # ── the false positive the fix removes ────────────────────────────────────
    def test_truncated_capture_is_not_missing_frames(self):
        """The whole bug. Every frame the cut admitted is present, so the clip is 100%."""
        self.assertGreaterEqual(_measured_pct(self.truncated), 99.5)

    def test_the_old_denominator_is_what_invented_the_shortfall(self):
        """Names the defect directly: the container duration reports the same complete clip
        as measurably short, which is the number that reached the health score."""
        probe = parse_ffprobe(self.truncated)
        old_pct = probe['frame_count'] / (probe['fps'] * probe['duration']) * 100
        self.assertLess(old_pct, 97.0,
                        'fixture no longer reproduces the container-duration shortfall')

    def test_the_reorder_tail_is_absent_from_the_decode_span(self):
        """Why it works: the presentation span runs past the decode span by the reorder
        depth, and only the decode span describes the window actually captured."""
        timeline = scan_video_timeline(self.truncated)
        self.assertGreater(timeline['span_seconds'], timeline['dts_span_seconds'])
        self.assertEqual(timeline['gap_count'], 0,
                         'the truncated fixture must have no real decode-timeline gaps')

    def test_an_untruncated_capture_was_already_correct(self):
        """Characterization guard - this passed before the fix too. It is here so a future
        change cannot fix the truncated case by breaking the ordinary one."""
        self.assertGreaterEqual(_measured_pct(self.source), 99.5)

    # ── the true positive that must survive the fix ───────────────────────────
    def test_genuinely_missing_frames_are_still_counted(self):
        """A clip really missing a quarter of its frames must still measure as missing them,
        or the fix has traded a false alarm for a blind spot."""
        self.assertLess(_measured_pct(self.gappy), 80.0)

    # ── the guard on the new value ────────────────────────────────────────────
    def test_decode_span_is_withheld_when_the_timeline_restarts(self):
        """max-min DTS spans the joins rather than the capture on a file whose decode
        timeline restarts, so it is withheld rather than reported wrong."""
        joined = os.path.join(self._dir, 'joined.ts')
        with open(joined, 'wb') as out:
            for _ in range(2):
                with open(self.truncated, 'rb') as part:
                    shutil.copyfileobj(part, out)
        timeline = scan_video_timeline(joined)
        self.assertGreater(timeline['backward_count'], 0,
                           'fixture did not produce a backward decode-timeline step')
        self.assertIsNone(timeline['dts_span_seconds'])


if __name__ == '__main__':
    unittest.main()
