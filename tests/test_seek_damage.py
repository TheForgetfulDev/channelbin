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
        """Divided by the header rate, as the deficit was before dev/changelog/866, this file
        is short by more than the damage threshold with zero gaps - proof the fixture
        exercises the rate defect rather than a file that was always clean."""
        _damaged, metrics, _summary = assess_seek_damage(self.mixed, joined_segments=2)
        header_deficit = metrics['span_seconds'] - metrics['packet_count'] / metrics['fps']
        self.assertGreater(header_deficit, DAMAGE_MIN_MISSING_SECONDS)
        self.assertEqual(metrics['gap_count'], 0,
                         'fixture has real gaps, so it cannot isolate the rate defect')

    def test_a_rate_change_is_not_missing_video_even_with_no_segment_rates(self):
        """dev/docs/BUGS.md 2026-10-15 @ 06:12:01 PM. Every packet carries its own duration,
        so the deficit no longer needs the segments' probes to get a rate change right -
        which matters because those probes read only each segment's opening."""
        damaged, metrics, summary = assess_seek_damage(self.mixed, joined_segments=2)
        self.assertFalse(damaged, summary)
        self.assertEqual(metrics['deficit_basis'], 'frame_durations')
        self.assertLess(metrics['deficit_seconds'], 0.5, summary)

    def test_the_summary_does_not_explain_the_number_with_an_unused_rate(self):
        """With every packet dated, the blended rate divided nothing, so the summary names
        the rate change without claiming the deficit was measured against 37.5 fps."""
        _damaged, metrics, summary = assess_seek_damage(
            self.mixed, joined_segments=2, segment_rates=self.rates)
        self.assertEqual(metrics['capture_fps_values'], [15.0, 60.0])
        self.assertIn('Capture frame rate changed mid-recording', summary)
        self.assertIn('counted at its own duration', summary)
        self.assertNotIn('37.50 fps', summary)

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



class _FedPackets:
    """Stands in for ffprobe: scan_video_timeline reads these packet lines instead of a
    file's, so a case no encoder will produce - a packet with no duration - is testable."""

    def __init__(self, lines, header_fps):
        self.lines, self.header_fps = lines, header_fps

    def __enter__(self):
        from unittest import mock
        fd, self.path = tempfile.mkstemp(suffix='.ts')
        os.close(fd)

        def _feed(cmd, on_line, **_kw):
            for line in self.lines:
                on_line(line)
            return 0

        self._patches = [mock.patch('app.probe._stream_probe_lines', _feed),
                         mock.patch('app.probe._nominal_fps', lambda _p: self.header_fps)]
        for patch in self._patches:
            patch.start()
        return self.path

    def __exit__(self, *exc):
        for patch in self._patches:
            patch.stop()
        os.unlink(self.path)


def _packets(start, seconds, fps, dated=True):
    """CSV packet lines as the scan's ffprobe prints them: pts, dts, duration."""
    step = 1.0 / fps
    return [f'{start + i * step:.6f},{start + i * step:.6f},'
            + (f'{step:.6f}' if dated else 'N/A') for i in range(int(seconds * fps))]


class UndatedPacketFallbackTests(unittest.TestCase):
    """A packet with no duration is counted at a rate: the segments' blended rate when they
    disagree (dev/changelog/866), the header's otherwise. Pinned here because no fixture an
    encoder produces leaves a packet undated (dev/changelog/1201)."""

    LINES = _packets(0.0, 20, 60, dated=False) + _packets(20.0, 20, 15, dated=False)
    RATES = [(20.0, 60.0), (20.0, 15.0)]

    def test_deficit_is_divided_by_the_weighted_rate(self):
        """20s at 60 and 20s at 15 weight to 37.5fps, and deficit_fps says so while fps
        keeps reporting what the header claimed."""
        with _FedPackets(self.LINES, 60.0) as path:
            damaged, metrics, summary = assess_seek_damage(
                path, joined_segments=2, segment_rates=self.RATES)
        self.assertFalse(damaged, summary)
        self.assertEqual(metrics['deficit_basis'], 'nominal_rate')
        self.assertAlmostEqual(metrics['deficit_fps'], 37.5, places=3)
        self.assertAlmostEqual(metrics['fps'], 60.0, places=3)
        self.assertEqual(metrics['capture_fps_values'], [15.0, 60.0])

    def test_summary_says_the_rate_changed_and_names_both_rates(self):
        """A corrected number nobody can reproduce is no better than a wrong one."""
        with _FedPackets(self.LINES, 60.0) as path:
            _damaged, _metrics, summary = assess_seek_damage(
                path, joined_segments=2, segment_rates=self.RATES)
        self.assertIn('Capture frame rate changed mid-recording', summary)
        self.assertIn('15, 60 fps', summary)
        self.assertIn('37.50 fps', summary)

    def test_without_rates_an_undated_file_is_measured_at_the_header_rate(self):
        with _FedPackets(self.LINES, 60.0) as path:
            damaged, metrics, _summary = assess_seek_damage(path, joined_segments=2)
        self.assertTrue(damaged)
        self.assertAlmostEqual(metrics['deficit_seconds'], 40 - 1500 / 60.0, delta=0.1)

    def test_dated_packets_count_their_own_duration_and_undated_ones_the_rate(self):
        lines = _packets(0.0, 20, 60) + _packets(20.0, 20, 15, dated=False)
        with _FedPackets(lines, 60.0) as path:
            metrics = scan_video_timeline(path)
        self.assertEqual(metrics['deficit_basis'], 'partial_durations')
        # 20s dated exactly, then 300 undated packets at the header's 60fps = 5s.
        self.assertAlmostEqual(metrics['deficit_seconds'], 40 - 20 - 5, delta=0.1)

    def test_an_implausible_duration_counts_as_undated(self):
        """A duration of a second or more is not one frame and must not paper over a gap."""
        lines = [line.rsplit(',', 1)[0] + ',5.000000' for line in _packets(0.0, 10, 60)]
        with _FedPackets(lines, 60.0) as path:
            metrics = scan_video_timeline(path)
        self.assertEqual(metrics['deficit_basis'], 'nominal_rate')
        self.assertLess(metrics['deficit_seconds'], 0.1)


_NTSC60 = 1001 / 60000          # one 59.94 fps frame, exactly
_NTSC60_PRINTED = '0.016678'    # what ffprobe prints for it: 1501 whole ticks of 1501.5


def _ntsc60_packets(frames, skip=()):
    """CSV packet lines for `frames` frames at 59.94 fps as ffprobe prints them off an
    MPEG-TS: timestamps at the exact rate, durations rounded to whole 90 kHz ticks. Frames
    whose index is in `skip` are left out, as a dropped frame would be."""
    return [f'{i * _NTSC60:.6f},{i * _NTSC60:.6f},{_NTSC60_PRINTED}'
            for i in range(frames) if i not in skip]


class TickRoundedDurationTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-10-26 - a flawless 59.94 fps capture read about a second an
    hour missing, because the scan summed packet durations as ffprobe prints them (1501
    ticks, half a tick short of a true frame) and measured them against a span that stops
    at the last frame's start (dev/changelog/1304)."""

    TEN_MINUTES = 36_000

    def test_a_complete_59_94_capture_reads_nothing_missing(self):
        with _FedPackets(_ntsc60_packets(self.TEN_MINUTES), 60000 / 1001) as path:
            metrics = scan_video_timeline(path)
        self.assertEqual(metrics['deficit_basis'], 'frame_durations')
        self.assertLess(metrics['deficit_seconds'], 0.001,
                        f'{metrics["deficit_seconds"]:.3f}s missing on a capture that held '
                        f'every frame - the half-tick drift is back')

    def test_the_drift_was_real(self):
        """The arithmetic this replaced, on the same packets: half a tick a frame, less the
        one frame the span stops short by. Pins the size of what the fix removed."""
        drift = self.TEN_MINUTES * (_NTSC60 - float(_NTSC60_PRINTED)) - _NTSC60
        self.assertGreater(drift, 0.15)
        self.assertLess(drift, 0.25)

    def test_one_dropped_frame_reads_as_one_frame(self):
        """The extent runs to the end of the last frame, so a single lost frame is no longer
        hidden behind the span's one-frame shortfall and the clamp at zero."""
        with _FedPackets(_ntsc60_packets(self.TEN_MINUTES, skip={18_000}),
                         60000 / 1001) as path:
            metrics = scan_video_timeline(path)
        self.assertAlmostEqual(metrics['deficit_seconds'], _NTSC60, delta=0.002)

    def test_a_real_hole_still_reads_its_full_size(self):
        skip = set(range(18_000, 18_000 + 300))   # 5 s of frames gone
        with _FedPackets(_ntsc60_packets(self.TEN_MINUTES, skip=skip), 60000 / 1001) as path:
            metrics = scan_video_timeline(path)
        self.assertAlmostEqual(metrics['deficit_seconds'], 300 * _NTSC60, delta=0.002)
        self.assertEqual(metrics['gap_count'], 1)

    def test_durations_snap_only_to_a_rate_they_are_a_tick_from(self):
        from app.probe import exact_frame_seconds
        tick = 1 / 90000
        self.assertAlmostEqual(exact_frame_seconds(1501 * tick), 1001 / 60000, places=12)
        self.assertAlmostEqual(exact_frame_seconds(1502 * tick), 1001 / 60000, places=12)
        self.assertAlmostEqual(exact_frame_seconds(1500 * tick), 1 / 60, places=12)
        self.assertAlmostEqual(exact_frame_seconds(3753 * tick), 1001 / 24000, places=12)
        self.assertAlmostEqual(exact_frame_seconds(3003 * tick), 1001 / 30000, places=12)
        self.assertEqual(exact_frame_seconds(0.04), 0.04)        # 25 fps is already exact
        self.assertEqual(exact_frame_seconds(0.05), 0.05)        # 20 fps: not a broadcast rate
        self.assertEqual(exact_frame_seconds(1510 * tick), 1510 * tick)   # too far from any


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class TickRoundedDurationOnDiskTests(unittest.TestCase):
    """The same drift on a clip this box's ffmpeg wrote and its ffprobe read - the fed packets
    above assume what ffprobe prints, this measures it. Three minutes: long enough for the
    drift to clear the one frame the span stops short by (10,789 frames x half a tick)."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='ntsc60-')
        cls.clip = os.path.join(cls._dir, 'ntsc60.ts')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=60000/1001', '-t', '180',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0',
                '-pix_fmt', 'yuv420p', cls.clip)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def test_ffprobe_prints_tick_rounded_durations(self):
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
             'packet=pts_time,duration_time', '-of', 'csv=p=0', self.clip],
            capture_output=True, text=True, check=True, timeout=120).stdout
        pts, printed = [], 0.0
        for line in out.splitlines():
            p, d = line.strip().rstrip(',').split(',')[:2]
            pts.append(float(p))
            printed += float(d)
        self.assertGreater(len(pts), 10_000)
        self.assertGreater(max(pts) - min(pts) - printed, 0.03,
                           'ffprobe no longer rounds a 59.94 frame short - the snap may be moot')

    def test_the_scan_reads_nothing_missing(self):
        metrics = scan_video_timeline(self.clip)
        self.assertEqual(metrics['deficit_basis'], 'frame_durations')
        self.assertLess(metrics['deficit_seconds'], 0.001, metrics)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class ShortLowRateOpeningTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-10-15 @ 06:12:01 PM - recording 16's segment 4 was probed at
    720p29.97 two seconds in and then ran at 59.94 for three and a half hours. The deficit
    was divided by a rate blended from the segments' opening probes (30.62 fps), and read
    0.0s missing on the page whose capture health said 96.3% of expected frames.

    In miniature: 20s at 60, then a segment that opens on 2s at 30 and runs 18s at 60 with
    every fifth frame lost - 3.6s of video missing, in steps too small to count as gaps.
    Its probe says 30 for the whole 20s."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='lowopen-')
        first = os.path.join(cls._dir, 'first60.ts')
        opening = os.path.join(cls._dir, 'open30.ts')
        rest = os.path.join(cls._dir, 'rest60lossy.ts')
        enc = ['-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0', '-pix_fmt', 'yuv420p']
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=60', '-t', '20', *enc, first)
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=30', '-t', '2', *enc, opening)
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=60', '-t', '18',
                '-vf', "select='not(eq(mod(n,5),0))'", '-fps_mode', 'passthrough', *enc, rest)
        listfile = os.path.join(cls._dir, 'list.txt')
        with open(listfile, 'w') as fh:
            for part in (first, opening, rest):
                fh.write(f"file '{part}'\n")
        cls.joined = os.path.join(cls._dir, 'joined.ts')
        _ffmpeg('-f', 'concat', '-safe', '0', '-fflags', '+genpts', '-i', listfile,
                '-c', 'copy', cls.joined)
        cls.probed_rates = [(20.0, 60.0), (20.0, 30.0)]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def test_the_lost_frames_are_counted(self):
        _damaged, metrics, summary = assess_seek_damage(
            self.joined, joined_segments=2, segment_rates=self.probed_rates)
        self.assertEqual(metrics['gap_count'], 0, 'fixture has real gaps')
        self.assertAlmostEqual(metrics['deficit_seconds'], 3.6, delta=0.3, msg=summary)

    def test_the_blended_rate_would_have_read_nothing_missing(self):
        """The fixture reproduces the defect: divided by the probes' blend (45 fps) the
        same packets read as more than complete."""
        _damaged, metrics, _summary = assess_seek_damage(
            self.joined, joined_segments=2, segment_rates=self.probed_rates)
        self.assertLessEqual(metrics['span_seconds'] - metrics['packet_count'] / 45.0, 0.0)


if __name__ == '__main__':
    unittest.main()
