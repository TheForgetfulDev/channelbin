"""Tier 2 - seek-damage detection (dev/docs/BUGS.md 2026-07-23 "B-frame reordering counted
as timeline damage").

`scan_video_timeline` used to walk video-packet PTS with a monotonic high-water mark and
count every forward jump over the threshold as missing time. PTS is reordered relative to
decode order in any stream with B-frames, so a perfectly complete capture measured as
massively damaged: recording #64 (25fps, reorder depth 3) reported 12,325s missing out of a
15,553s timeline and was forced into a multi-hour libx264 re-encode it did not need.

Gaps are now counted on the decode timeline (DTS), which reordering leaves untouched. These
tests are pure functions over locally-built fixtures - no app context, no network, no
provider streams. The fixtures are synthesized with ffmpeg at 10fps so that a reorder depth
of 3 produces PTS excursions (~0.4s) comfortably above the 0.25s gap threshold, which is what
makes the old defect reproducible in a 20-second clip.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.probe import (  # noqa: E402
    assess_seek_damage, effective_capture_fps, scan_video_timeline,
    DAMAGE_MIN_MISSING_SECONDS, DAMAGE_MIN_MISSING_FRACTION,
)

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=120)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class SeekDamageTests(unittest.TestCase):
    """Fixtures are built once for the class - each is well under a second to encode."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='seekdamage-')

        # Complete, undamaged, but heavily B-frame reordered: every frame is present and
        # DTS is a clean 0.1s cadence, while PTS oscillates by ~0.4s. b-adapt=0/scenecut=0
        # force a fixed B-frame pattern so the fixture is deterministic across ffmpeg builds.
        cls.reordered = os.path.join(cls._dir, 'reordered.ts')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=10', '-t', '20',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '3',
                '-x264-params', 'bframes=3:b-adapt=0:scenecut=0',
                '-pix_fmt', 'yuv420p', cls.reordered)

        # Genuinely damaged: 40s of timeline with the 10s-25s stretch dropped and the
        # surviving frames keeping their original timestamps (no setpts compaction), so the
        # file really is missing 15s of content. No B-frames, to keep the two defects apart.
        cls.gappy = os.path.join(cls._dir, 'gappy.ts')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=10', '-t', '40',
                '-vf', "select='not(between(t,10,25))'", '-fps_mode', 'passthrough',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0',
                '-pix_fmt', 'yuv420p', cls.gappy)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    # ── the false positive the fix removes ────────────────────────────────────
    def test_b_frame_reordering_is_not_damage(self):
        """A complete capture with reordered PTS must not be classified damaged.

        This is the whole bug: reordering is not a timeline gap, and players handle it
        natively. Flagging it forces a re-encode that repairs nothing.
        """
        damaged, metrics, summary = assess_seek_damage(self.reordered)
        self.assertFalse(damaged,
                         f'reordered-but-complete capture flagged as damaged: {summary}')
        self.assertLess(metrics['missing_seconds'], DAMAGE_MIN_MISSING_SECONDS,
                        f'reordering leaked into missing_seconds: {summary}')

    def test_gaps_are_counted_on_the_decode_timeline(self):
        """The reordered fixture has zero DTS gaps; only a PTS-based scanner sees gaps."""
        m = scan_video_timeline(self.reordered)
        self.assertTrue(m, 'timeline scan returned nothing for the reordered fixture')
        self.assertEqual(m['gap_basis'], 'dts')
        self.assertEqual(m['gap_count'], 0,
                         'gaps counted on a stream whose decode timeline is continuous')
        self.assertEqual(m['gap_seconds'], 0.0)
        self.assertEqual(m['backward_count'], 0)

    def test_frame_deficit_is_reorder_immune(self):
        """span - frames/fps stays ~0 on a complete file regardless of reordering."""
        m = scan_video_timeline(self.reordered)
        self.assertLess(m['deficit_seconds'], 1.0,
                        f"deficit should be ~0 on a complete capture, got {m['deficit_seconds']}")

    # ── the true positive that must survive the fix ───────────────────────────
    def test_genuine_missing_content_is_still_damage(self):
        """A file really missing a contiguous stretch must still be flagged.

        Characterization guard, not a regression guard: this passed before the fix too. It
        exists so a later change cannot quietly disable damage detection while making
        test_b_frame_reordering_is_not_damage pass.
        """
        damaged, metrics, summary = assess_seek_damage(self.gappy)
        self.assertTrue(damaged, f'a file missing 15s of content was not flagged: {summary}')
        self.assertGreater(metrics['gap_count'], 0, 'the real gap was not counted')
        self.assertGreater(metrics['missing_seconds'],
                           max(DAMAGE_MIN_MISSING_SECONDS,
                               metrics['span_seconds'] * DAMAGE_MIN_MISSING_FRACTION))

    def test_gap_is_located_where_the_content_was_removed(self):
        """The single gap matches the 15s stretch that was dropped, not some artifact."""
        m = scan_video_timeline(self.gappy)
        self.assertEqual(m['gap_count'], 1, f"expected exactly one gap, got {m['gap_count']}")
        self.assertAlmostEqual(m['max_gap_seconds'], 15.0, delta=1.0)

    # ── reporting + safety ────────────────────────────────────────────────────
    def test_summary_reports_the_threshold_actually_used(self):
        """The summary used to hardcode '>0.25s' independently of the scan's threshold."""
        m = scan_video_timeline(self.gappy, gap_threshold=2.0)
        self.assertEqual(m['gap_threshold'], 2.0)
        _damaged, _m, summary = assess_seek_damage(self.gappy)
        self.assertIn(f">{_m['gap_threshold']}s", summary)

    def test_probe_failure_is_never_damage(self):
        """A scan that fails must not force a re-encode."""
        damaged, metrics, _summary = assess_seek_damage(
            os.path.join(self._dir, 'does-not-exist.ts'))
        self.assertFalse(damaged)
        self.assertEqual(metrics, {})


class EffectiveCaptureFpsTests(unittest.TestCase):
    """Pure math, no fixtures: the duration-weighted rate the deficit is divided by."""

    def test_weight_is_by_duration_not_segment_count(self):
        """Four seconds at 60 and one at 10 is 50fps, not the 35 an unweighted mean gives.
        A stall restart routinely produces one short off-rate segment among many long ones.
        """
        effective, _rates = effective_capture_fps([(4.0, 60.0), (1.0, 10.0)])
        self.assertAlmostEqual(effective, 50.0, places=6)

    def test_rates_differing_only_in_rounding_are_one_rate(self):
        """59.94 and 60000/1001 are the same rate spelled two ways. Reporting that as a
        change would re-target the deficit of any recording whose probes disagreed on
        rounding."""
        _effective, rates = effective_capture_fps([(10.0, 59.94), (10.0, 60000 / 1001)])
        self.assertEqual(len(rates), 1, f'rounding noise read as a rate change: {rates}')

    def test_unknown_stays_distinguishable_from_measured(self):
        """A segment the watchdog could not probe has probe_fps NULL. No usable pair must
        return None rather than a guessed rate - inventing one puts the corrected number
        back where the wrong one was."""
        self.assertEqual(effective_capture_fps([]), (None, []))
        self.assertEqual(effective_capture_fps([(10.0, None), (0, 30.0)]), (None, []))


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class MixedCaptureRateTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-08-29 - a recording whose capture rate changed partway through
    was measured against the single nominal rate in the concatenated file's header (whatever
    its FIRST segment was), so every frame captured at the lower rate counted as a missing
    frame. Recording 2 reported 1,200.1s missing from a 12,949s timeline (9.3%) and was sent
    through a full libx264 re-encode; its real measured damage was one gap of 0.41s.

    The fixture is that defect in miniature: 20s at 60fps joined to 20s at 15fps through the
    same concat demuxer + `-fflags +genpts` path concatenator.py uses. The header reports
    60fps, so the honest 1,500 packets divide out to 25s of frames against a 40s span.
    """

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='mixedrate-')
        fast = os.path.join(cls._dir, 'fast60.ts')
        slow = os.path.join(cls._dir, 'slow15.ts')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=60', '-t', '20',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0',
                '-pix_fmt', 'yuv420p', fast)
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=15', '-t', '20',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0',
                '-pix_fmt', 'yuv420p', slow)

        # Same second segment, but the first now really is missing its 10s-25s stretch -
        # a file that is BOTH mixed-rate and genuinely damaged.
        gappy_fast = os.path.join(cls._dir, 'fast60gap.ts')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=60', '-t', '40',
                '-vf', "select='not(between(t,10,25))'", '-fps_mode', 'passthrough',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0',
                '-pix_fmt', 'yuv420p', gappy_fast)

        cls.mixed = cls._concat(os.path.join(cls._dir, 'mixed.ts'), fast, slow)
        cls.mixed_gappy = cls._concat(os.path.join(cls._dir, 'mixed_gappy.ts'),
                                      gappy_fast, slow)
        # Wall-clock spans the way recording_segments stores them, paired with each
        # segment's own capture-time probe_fps.
        cls.rates = [(20.0, 60.0), (20.0, 15.0)]
        cls.gappy_rates = [(40.0, 60.0), (20.0, 15.0)]

    @classmethod
    def _concat(cls, out, *parts):
        listfile = out + '.txt'
        with open(listfile, 'w') as fh:
            for part in parts:
                fh.write(f"file '{part}'\n")
        _ffmpeg('-f', 'concat', '-safe', '0', '-fflags', '+genpts', '-i', listfile,
                '-c', 'copy', out)
        return out

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    # ── the false positive the fix removes ────────────────────────────────────
    def test_a_rate_change_is_not_missing_video(self):
        """The whole bug. A clean concatenation of two rates must not measure as damaged
        once each segment's own rate is supplied."""
        damaged, metrics, summary = assess_seek_damage(
            self.mixed, joined_segments=2, segment_rates=self.rates)
        self.assertFalse(damaged, f'a rate change was reported as damage: {summary}')
        self.assertLess(metrics['missing_seconds'], DAMAGE_MIN_MISSING_SECONDS, summary)

    def test_the_fixture_really_does_reproduce_the_defect(self):
        """Without the segment rates, the same file still measures DAMAGED with zero gaps -
        proof the fixture exercises the defect rather than a file that was always clean."""
        damaged, metrics, _summary = assess_seek_damage(self.mixed, joined_segments=2)
        self.assertTrue(damaged, 'fixture does not reproduce the header-rate defect')
        self.assertEqual(metrics['gap_count'], 0,
                         'fixture has real gaps, so it cannot isolate the rate defect')

    def test_deficit_is_divided_by_the_weighted_rate(self):
        """The number has to be explainable: 20s at 60 and 20s at 15 weight to 37.5fps, and
        deficit_fps says so while fps keeps reporting what the header claimed."""
        _damaged, metrics, _summary = assess_seek_damage(
            self.mixed, joined_segments=2, segment_rates=self.rates)
        self.assertAlmostEqual(metrics['deficit_fps'], 37.5, places=3)
        self.assertAlmostEqual(metrics['fps'], 60.0, places=3)
        self.assertEqual(metrics['capture_fps_values'], [15.0, 60.0])

    def test_summary_says_the_rate_changed_and_names_both_rates(self):
        """A corrected number nobody can reproduce is no better than a wrong one."""
        _damaged, _metrics, summary = assess_seek_damage(
            self.mixed, joined_segments=2, segment_rates=self.rates)
        self.assertIn('Capture frame rate changed mid-recording', summary)
        self.assertIn('15, 60 fps', summary)
        self.assertIn('37.50 fps', summary)

    # ── the true positives that must survive the fix ──────────────────────────
    def test_real_damage_in_a_mixed_rate_file_is_still_damage(self):
        """The load-bearing guard: correcting the rate must not become a way to make any
        damaged recording measure clean. This file is mixed-rate AND missing 15s."""
        damaged, metrics, summary = assess_seek_damage(
            self.mixed_gappy, joined_segments=2, segment_rates=self.gappy_rates)
        self.assertTrue(damaged, f'real damage was corrected away: {summary}')
        self.assertEqual(metrics['gap_count'], 1)
        self.assertAlmostEqual(metrics['max_gap_seconds'], 15.0, delta=1.0)

    def test_one_rate_throughout_is_measured_exactly_as_before(self):
        """A recording that never changed rate must be byte-identical whether or not its
        rates were supplied - the override exists to correct a CHANGE, and applying it
        otherwise would silently re-target the deficit of every recording in the app."""
        without = assess_seek_damage(self.mixed, joined_segments=2)
        supplied = assess_seek_damage(self.mixed, joined_segments=2,
                                      segment_rates=[(20.0, 60.0), (20.0, 60.0)])
        self.assertEqual(without[0], supplied[0])
        self.assertEqual(without[1], supplied[1])
        self.assertEqual(without[2], supplied[2])
        self.assertNotIn('capture_fps_values', supplied[1])

    def test_unprobed_segments_do_not_invent_a_rate(self):
        """Every segment NULL is the same as supplying nothing - never a guessed rate."""
        without = assess_seek_damage(self.mixed, joined_segments=2)
        unknown = assess_seek_damage(self.mixed, joined_segments=2,
                                     segment_rates=[(20.0, None), (20.0, None)])
        self.assertEqual(without[1]['deficit_seconds'], unknown[1]['deficit_seconds'])


if __name__ == '__main__':
    unittest.main()
