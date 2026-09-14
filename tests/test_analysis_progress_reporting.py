"""Tier 2 - the post-capture analysis says which pass is reading and how far it has got
(dev/changelog/960).

While a recording sits at ANALYZING the detail page read "Segments joined - the recorded
file (42.6 GB) is being checked for damage before conversion" and then did not move: on
recording 19, 2026-09-13, that was six minutes looking identical to a hung process. The
phase is really two whole-file ffprobe reads back to back - the capture-health probe
(parse_ffprobe with -count_packets) and the damage scan (scan_video_timeline) - and both
already sample the bytes their child has read, because that is how probe.py tells a slow
probe from a stalled one. The number existed and was simply never published.

Covers, in order:
  - AnalysisPassPlanTests: how many passes this recording will actually run, which is what
    "pass 1 of 2" promises and is not always 2.
  - AnalysisProgressArithmeticTests: the registry's own maths - percent against file size,
    the clamp, elapsed derived at read time.
  - AnalysisPassLifecycleTests: an entry exists while a pass reads and on NO path outlives
    it, including an exception, and none is published at all outside the phase.
  - ProbeProgressHookTests: both probes really call the hook, against real child processes
    on local files, and a hook that raises cannot harm the probe.
  - AnalysisWiringTests: the two phase helpers pass the hook down - the guard against a
    registry nothing ever writes to.
  - AnalysisSurfaceTests: what the three surfaces render - the detail strip, the recordings
    list relative line, the Dashboard background-task chip.

No network anywhere: the probe tests read local files only (see CLAUDE.md section Testing).
Run standalone:
  python3 -m unittest tests.test_analysis_progress_reporting
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.postprocessor as ppmod  # noqa: E402
import app.probe as probemod  # noqa: E402
from app import db  # noqa: E402
from app.database import Recording  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def _cfg(gather=True):
    return {'recording': {'gather_health_data': gather}}


def _pp(enabled=True, fmt='mp4', mode='damaged'):
    return {'enabled': enabled, 'format': fmt, 'reencode_mode': mode}


class AnalysisPassPlanTests(unittest.TestCase):
    """Pure, no app: the denominator "pass N of M" is measured against."""

    def test_the_ordinary_recording_runs_both_reads(self):
        self.assertEqual(ppmod.analysis_pass_plan(_cfg(), _pp()),
                         [ppmod.ANALYSIS_PASS_HEALTH, ppmod.ANALYSIS_PASS_TIMELINE])

    def test_health_data_off_leaves_only_the_scan_the_reencode_decision_needs(self):
        """do_postprocess still scans in that case, or turning health data off would
        silently disable damage repair - so the phase has one pass, not zero."""
        self.assertEqual(ppmod.analysis_pass_plan(_cfg(gather=False), _pp()),
                         [ppmod.ANALYSIS_PASS_TIMELINE])

    def test_health_data_off_and_reencode_always_reads_nothing(self):
        """reencode_mode 'always' does not ask whether the file is damaged, so no pass runs
        and an empty plan is the honest answer rather than a bar with no work behind it."""
        self.assertEqual(ppmod.analysis_pass_plan(_cfg(gather=False),
                                                  _pp(mode='always')), [])

    def test_health_data_off_and_a_ts_output_reads_nothing(self):
        self.assertEqual(ppmod.analysis_pass_plan(_cfg(gather=False), _pp(fmt='ts')), [])

    def test_health_data_off_and_no_conversion_reads_nothing(self):
        self.assertEqual(ppmod.analysis_pass_plan(_cfg(gather=False),
                                                  _pp(enabled=False)), [])


class AnalysisProgressArithmeticTests(unittest.TestCase):
    """The registry's maths, with no app and no probe anywhere near it."""

    PLAN = [ppmod.ANALYSIS_PASS_HEALTH, ppmod.ANALYSIS_PASS_TIMELINE]

    def setUp(self):
        self.rid = 5151
        ppmod._clear_analysis_progress(self.rid)

    def tearDown(self):
        ppmod._clear_analysis_progress(self.rid)

    def _seed(self, label=ppmod.ANALYSIS_PASS_HEALTH, total=1000):
        ppmod._start_analysis_pass(self.rid, label, self.PLAN, total)

    def test_nothing_is_reported_for_a_recording_with_no_pass_reading(self):
        """Absent is a real state: an ANALYZING row can be parked behind another recording,
        or sitting between the two passes, and reporting 0% for that is a fabrication."""
        self.assertIsNone(ppmod.analysis_progress(self.rid))

    def test_the_pass_names_itself_before_the_first_sample(self):
        self._seed()
        prog = ppmod.analysis_progress(self.rid)
        self.assertEqual(prog['pass_label'], ppmod.ANALYSIS_PASS_HEALTH)
        self.assertEqual((prog['pass_number'], prog['of_passes']), (1, 2))
        self.assertIsNone(prog['pct'])

    def test_the_second_pass_counts_itself_second(self):
        self._seed(label=ppmod.ANALYSIS_PASS_TIMELINE)
        prog = ppmod.analysis_progress(self.rid)
        self.assertEqual((prog['pass_number'], prog['of_passes']), (2, 2))

    def test_a_single_pass_plan_numbers_that_pass_first(self):
        ppmod._start_analysis_pass(self.rid, ppmod.ANALYSIS_PASS_TIMELINE,
                                   [ppmod.ANALYSIS_PASS_TIMELINE], 1000)
        prog = ppmod.analysis_progress(self.rid)
        self.assertEqual((prog['pass_number'], prog['of_passes']), (1, 1))

    def test_percent_is_bytes_read_against_the_file_size(self):
        self._seed(total=1000)
        ppmod._publish_analysis_progress(self.rid, 430)
        self.assertAlmostEqual(ppmod.analysis_progress(self.rid)['pct'], 43.0)

    def test_percent_is_clamped_below_a_hundred(self):
        """The counter is bytes the process has READ, and ffprobe seeks and re-reads, so it
        can pass the file's own size before the pass is finished."""
        self._seed(total=1000)
        ppmod._publish_analysis_progress(self.rid, 1400)
        self.assertEqual(ppmod.analysis_progress(self.rid)['pct'], 99.0)

    def test_percent_is_unknown_rather_than_zero_without_a_size(self):
        """A file that could not be stat'ed loses the denominator only - the pass still
        names itself and still reports elapsed."""
        self._seed(total=0)
        ppmod._publish_analysis_progress(self.rid, 500)
        prog = ppmod.analysis_progress(self.rid)
        self.assertIsNone(prog['pct'])
        self.assertEqual(prog['pass_label'], ppmod.ANALYSIS_PASS_HEALTH)

    def test_elapsed_is_current_at_the_moment_it_is_read(self):
        self._seed()
        first = ppmod.analysis_progress(self.rid)['elapsed_seconds']
        time.sleep(0.05)
        self.assertGreater(ppmod.analysis_progress(self.rid)['elapsed_seconds'], first)

    def test_the_read_does_not_hand_out_the_live_entry(self):
        """A surface that mutated what it was handed would corrupt the running pass."""
        self._seed(total=1000)
        out = ppmod.analysis_progress(self.rid)
        out['pct'] = 12.0
        ppmod._publish_analysis_progress(self.rid, 500)
        self.assertAlmostEqual(ppmod.analysis_progress(self.rid)['pct'], 50.0)

    def test_a_sample_for_a_pass_that_has_ended_is_dropped(self):
        """The probe's poll thread can land one last sample after the pass cleared its
        entry; it must not resurrect one."""
        self._seed()
        ppmod._clear_analysis_progress(self.rid)
        ppmod._publish_analysis_progress(self.rid, 500)
        self.assertIsNone(ppmod.analysis_progress(self.rid))


class AnalysisPassLifecycleTests(unittest.TestCase):
    """_analysis_pass: an entry for exactly as long as the pass runs."""

    PLAN = [ppmod.ANALYSIS_PASS_HEALTH, ppmod.ANALYSIS_PASS_TIMELINE]

    def setUp(self):
        self.rid = 5252
        self._dir = tempfile.mkdtemp(prefix='analysisprog-')
        self.path = os.path.join(self._dir, 'joined.ts')
        with open(self.path, 'wb') as fh:
            fh.write(b'\x00' * 2048)
        ppmod._clear_analysis_progress(self.rid)

    def tearDown(self):
        ppmod._clear_analysis_progress(self.rid)
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_the_entry_exists_inside_the_pass_and_is_gone_after(self):
        with ppmod._analysis_pass(self.rid, ppmod.ANALYSIS_PASS_HEALTH,
                                  self.path, self.PLAN) as hook:
            hook(1024)
            inside = ppmod.analysis_progress(self.rid)
        self.assertAlmostEqual(inside['pct'], 50.0)
        self.assertIsNone(ppmod.analysis_progress(self.rid))

    def test_nothing_is_left_behind_when_the_pass_raises(self):
        """A crashed probe must not leave a strip reporting a read that stopped."""
        with self.assertRaises(RuntimeError):
            with ppmod._analysis_pass(self.rid, ppmod.ANALYSIS_PASS_HEALTH,
                                      self.path, self.PLAN):
                raise RuntimeError('probe blew up')
        self.assertIsNone(ppmod.analysis_progress(self.rid))

    def test_the_total_is_the_file_size(self):
        with ppmod._analysis_pass(self.rid, ppmod.ANALYSIS_PASS_HEALTH,
                                  self.path, self.PLAN):
            self.assertEqual(ppmod.analysis_progress(self.rid)['total_bytes'], 2048)

    def test_a_missing_file_still_names_the_pass(self):
        with ppmod._analysis_pass(self.rid, ppmod.ANALYSIS_PASS_HEALTH,
                                  os.path.join(self._dir, 'gone.ts'), self.PLAN) as hook:
            hook(500)
            prog = ppmod.analysis_progress(self.rid)
        self.assertEqual(prog['pass_label'], ppmod.ANALYSIS_PASS_HEALTH)
        self.assertIsNone(prog['pct'])

    def test_an_empty_plan_publishes_nothing_and_hands_back_no_hook(self):
        """A configuration whose analysis reads nothing must not put a bar on the page."""
        with ppmod._analysis_pass(self.rid, ppmod.ANALYSIS_PASS_HEALTH,
                                  self.path, []) as hook:
            self.assertIsNone(hook)
            self.assertIsNone(ppmod.analysis_progress(self.rid))


class ProbeProgressHookTests(unittest.TestCase):
    """Both probes really report, measured against real children reading local files."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='probehook-')
        cls.big = os.path.join(cls._dir, 'big.bin')
        with open(cls.big, 'wb') as fh:
            fh.write(b'\x00' * (24 * 1024 * 1024))
        cls.media = os.path.join(cls._dir, 'clip.ts')
        if _HAVE_FFMPEG:
            subprocess.run(
                ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                 '-i', 'testsrc=size=192x108:rate=10', '-t', '3',
                 '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                 cls.media], check=True, timeout=120)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def test_the_whole_file_probe_reports_what_its_child_has_read(self):
        """run_probe_until_stalled already samples this counter to decide whether the probe
        has stalled - the hook is the same sample, handed on."""
        seen = []
        # A local read, no network and no ffprobe needed: this function spawns whatever
        # argv it is given and watches /proc, so any reader proves the wiring.
        rc, _out = probemod.run_probe_until_stalled(
            [sys.executable, '-c',
             f'open({self.big!r}, "rb").read(); import time; time.sleep(1.2)'],
            stall_timeout=30, fallback_timeout=60, on_progress=seen.append)

        self.assertEqual(rc, 0)
        self.assertTrue(seen, 'the probe reported nothing at all')
        self.assertGreater(max(seen), 20 * 1024 * 1024)

    def test_a_hook_that_raises_cannot_break_the_probe(self):
        """A progress report is a diagnostic, and a diagnostic that can kill the pass it
        describes is worse than no diagnostic (CLAUDE.md)."""
        def _boom(_read):
            raise ValueError('surface bug')

        rc, _out = probemod.run_probe_until_stalled(
            [sys.executable, '-c',
             f'open({self.big!r}, "rb").read(); import time; time.sleep(1.2)'],
            stall_timeout=30, fallback_timeout=60, on_progress=_boom)

        self.assertEqual(rc, 0)

    @unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
    def test_the_damage_scan_reports_before_it_finishes(self):
        """The scan's own loop turns once per packet, so it samples on a clock - and it
        samples once up front, or a file that scans in under the interval would report
        nothing at all."""
        seen = []
        metrics = probemod.scan_video_timeline(self.media, on_progress=seen.append)

        self.assertTrue(metrics, 'the scan itself failed, so it proves nothing here')
        self.assertTrue(seen, 'the scan reported nothing at all')

    @unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
    def test_the_capture_health_probe_passes_the_hook_down(self):
        seen = []
        probe = probemod.parse_ffprobe(self.media, on_progress=seen.append)

        self.assertTrue(probe, 'the probe itself failed, so it proves nothing here')
        self.assertTrue(seen, 'the probe reported nothing at all')


class AnalysisWiringTests(unittest.TestCase):
    """The phase helpers hand the hook down - a registry nothing writes to is the shape
    that lets a feature ship unwired."""

    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status='ANALYZING', name='Long Race')
        db.session.commit()
        self.rid = self.rec.id
        self.path = os.path.join(self.t._tmpdir, 'joined.ts')
        with open(self.path, 'wb') as fh:
            fh.write(b'\x00' * 4000)
        self.plan = [ppmod.ANALYSIS_PASS_HEALTH, ppmod.ANALYSIS_PASS_TIMELINE]

    def tearDown(self):
        ppmod._clear_analysis_progress(self.rid)
        self.t.cleanup()

    def test_the_capture_health_pass_publishes_while_it_reads(self):
        seen = {}

        def _fake_probe(path, **kwargs):
            kwargs['on_progress'](2000)
            seen.update(ppmod.analysis_progress(self.rid) or {})
            return {}

        with mock.patch.object(probemod, 'parse_ffprobe', side_effect=_fake_probe):
            ppmod._gather_recording_health(self.rid, self.path, self.rec,
                                           {'recording': {'gather_health_data': True}},
                                           analysis_plan=self.plan)

        self.assertEqual(seen.get('pass_label'), ppmod.ANALYSIS_PASS_HEALTH)
        self.assertAlmostEqual(seen.get('pct'), 50.0)
        self.assertIsNone(ppmod.analysis_progress(self.rid))

    def test_the_damage_scan_publishes_while_it_reads(self):
        seen = {}

        def _fake_assess(path, **kwargs):
            kwargs['on_progress'](1000)
            seen.update(ppmod.analysis_progress(self.rid) or {})
            return False, {}, 'clean'

        with mock.patch.object(probemod, 'assess_seek_damage', side_effect=_fake_assess):
            ppmod._scan_recording_timeline(self.rid, self.path, analysis_plan=self.plan)

        self.assertEqual(seen.get('pass_label'), ppmod.ANALYSIS_PASS_TIMELINE)
        self.assertEqual(seen.get('pass_number'), 2)
        self.assertAlmostEqual(seen.get('pct'), 25.0)
        self.assertIsNone(ppmod.analysis_progress(self.rid))

    def test_a_scan_outside_the_phase_publishes_nothing(self):
        seen = {}

        def _fake_assess(path, **kwargs):
            seen['during'] = ppmod.analysis_progress(self.rid)
            return False, {}, 'clean'

        with mock.patch.object(probemod, 'assess_seek_damage', side_effect=_fake_assess):
            ppmod._scan_recording_timeline(self.rid, self.path)

        self.assertIsNone(seen.get('during'))


class AnalysisSurfaceTests(unittest.TestCase):
    """What the three surfaces render. Each asserts on the page, not on a helper."""

    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status='ANALYZING', name='Long Race')
        db.session.commit()
        self.rid = self.rec.id

    def tearDown(self):
        ppmod._clear_analysis_progress(self.rid)
        self.t.cleanup()

    def _live_pass(self, label=ppmod.ANALYSIS_PASS_TIMELINE, read=430, total=1000):
        ppmod._start_analysis_pass(
            self.rid, label,
            [ppmod.ANALYSIS_PASS_HEALTH, ppmod.ANALYSIS_PASS_TIMELINE], total)
        ppmod._publish_analysis_progress(self.rid, read)

    def _html(self, url):
        with self.t.app.test_client() as c:
            return c.get(url).get_data(as_text=True)

    def test_the_detail_strip_names_the_pass_and_how_far_it_has_got(self):
        """The asked-for minimum, per dev/changelog/960: prove it is doing something."""
        self._live_pass()

        html = self._html(f'/recordings/{self.rid}')

        self.assertIn(ppmod.ANALYSIS_PASS_TIMELINE, html)
        self.assertIn('pass 2 of 2', html)
        self.assertIn('43%', html)
        self.assertIn('elapsed', html)

    def test_the_detail_strip_keeps_the_plain_sentence_when_no_pass_is_reading(self):
        """"No pass running" and "a pass at 0%" are different facts, and the frozen strip
        this replaced is what a fabricated 0% would read as."""
        html = self._html(f'/recordings/{self.rid}')

        self.assertIn('is being checked for damage before conversion', html)
        self.assertNotIn('pass 1 of 2', html)

    def test_the_recordings_list_row_shows_the_pass(self):
        """The list's relative line for this status was only "ended Nm ago"."""
        self._live_pass()

        html = self._html('/recordings')

        self.assertIn('43%', html)
        self.assertIn('2 of 2', html)

    def test_the_recordings_list_row_falls_back_when_nothing_is_reading(self):
        html = self._html('/recordings')

        self.assertIn('ended', html)

    def _chip(self):
        with self.t.app.test_client() as c:
            bg = c.get('/api/activity/status').get_json()['background']
        rows = [t for t in bg['tasks'] if t['label'] == 'Checking the joined file']
        self.assertEqual(len(rows), 1, f'expected one analysis chip, got {bg["tasks"]}')
        return rows[0]['detail']

    def test_the_dashboard_chip_names_the_pass_and_the_percent(self):
        self._live_pass()

        detail = self._chip()

        self.assertIn('43%', detail)
        self.assertIn(ppmod.ANALYSIS_PASS_TIMELINE, detail)

    def test_the_dashboard_chip_is_just_the_name_when_nothing_is_reading(self):
        self.assertEqual(self._chip(), 'Long Race')

    def test_a_parked_recording_reports_waiting_rather_than_a_pass(self):
        """A row parked behind another recording has already finished its passes, so the
        parked wording owns the strip - the two must not fight over it
        (dev/changelog/954)."""
        rec = db.session.get(Recording, self.rid)
        rec.postprocess_waiting_since = rec.start_time
        rec.postprocess_waiting_on_name = 'Another Race'
        rec.postprocess_waiting_on_state = 'is still recording'
        db.session.commit()

        html = self._html(f'/recordings/{self.rid}')

        self.assertIn('Another Race', html)
        self.assertNotIn('pass 1 of 2', html)


if __name__ == '__main__':
    unittest.main()
