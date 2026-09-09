"""Tier 2 - recording post-process diagnostics (dev/changelog/331, dev/changelog/332).

The seek-damage scan measured eleven values per recording and discarded all of them
(`damaged, _metrics, summary = assess_seek_damage(ts_path)`), and wrote a RecordingEvent
only when it found damage - so a clean recording's event log could not distinguish "checked
and fine" from "never checked". The scan also ran only under `fmt == 'mp4' and
reencode_mode == 'damaged'`, so most configurations produced no timeline stats at all.

These are mostly characterization tests, not regression guards: nothing here was a defect
producing wrong output, it was measurement being thrown away. The exception is
test_reencode_never_measures_but_does_not_act, which guards a genuine hazard the change
introduces - measuring is now gated on gather_health_data while acting stays gated on
reencode_mode, and merging them back together would re-encode files for users who set
`reencode_mode: never`.

The capture_health half (dev/changelog/332) is the same shape: _gather_recording_health
computed resolution, fps, frames-vs-expected and bitrate, wrote them to the row, and logged
one line to dvr.log that only someone tailing a file on the server would ever see.

Fixtures are synthesized locally with ffmpeg (same recipes as tests/test_seek_damage.py) -
no network, no provider streams, no /dvr. The conversion runner is stubbed; no real ffmpeg
conversion runs.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
import app.probe as probemod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    DIAGNOSTICS, SEEK_DAMAGE_DETECTED, Recording, RecordingEvent,
)
from app.postprocessor import ConversionResult, do_postprocess  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=120)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class RecordingDiagnosticsTests(unittest.TestCase):
    """Fixtures are built once for the class - each is well under a second to encode."""

    @classmethod
    def setUpClass(cls):
        cls._dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_diagfix')
        os.makedirs(cls._dir, exist_ok=True)

        # Complete and undamaged: continuous decode timeline, nothing missing.
        cls.clean_src = os.path.join(cls._dir, 'clean.ts')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=10', '-t', '20',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0',
                '-pix_fmt', 'yuv420p', cls.clean_src)

        # Genuinely damaged: 40s of timeline with 10s-25s dropped, surviving frames keeping
        # their original timestamps, so the file really is missing 15s of content.
        cls.gappy_src = os.path.join(cls._dir, 'gappy.ts')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=10', '-t', '40',
                '-vf', "select='not(between(t,10,25))'", '-fps_mode', 'passthrough',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0',
                '-pix_fmt', 'yuv420p', cls.gappy_src)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def setUp(self):
        self.t = make_test_app()
        self.dvr_dir = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr_dir, exist_ok=True)

        acc = seed.make_account()
        self.channel = seed.make_channel(acc, name='Diag Feed')
        now = datetime.utcnow()
        rec = seed.make_recording(
            status='CONCATENATING', name='diagnostics', channel_id=self.channel.id,
            start_time=now - timedelta(seconds=40), stop_time=now)
        self.rid = rec.id
        db.session.commit()

    def tearDown(self):
        with ppmod._active_lock:
            ppmod._active_conversions.clear()
            ppmod._cancel_requested.clear()
        self.t.cleanup()

    # ── helpers ───────────────────────────────────────────────────────────────
    def _ts(self, src):
        """A per-test copy of a fixture, inside the sandbox (never /dvr)."""
        dest = os.path.join(self.dvr_dir, f'rec_{self.rid}.ts')
        shutil.copy2(src, dest)
        return dest

    def _config(self, *, convert=False, fmt='mp4', reencode_mode='damaged',
                gather_health_data=True):
        return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {
            'dvr_output_dir': self.dvr_dir,
            'gather_health_data': gather_health_data,
            'move_on_complete': {'enabled': False},
            'post_script': {'enabled': False},
            'post_process': {'enabled': convert, 'format': fmt, 'delete_source': False,
                             'reencode_mode': reencode_mode, 'pre_output_timeout_seconds': 60,
                             'auto_restart': False, 'max_restart_attempts': 0,
                             'stall_seconds': 0, 'progress_interval_seconds': 5},
        }})

    def _run(self, ts_path, cfg, conversion=None):
        """Drive do_postprocess with the conversion runner stubbed. Returns the stub so a
        caller can inspect the ffmpeg command it would have run."""
        stub = mock.Mock(return_value=conversion or ConversionResult(True))
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', stub):
            do_postprocess(self.t.app, self.rid, ts_path)
        db.session.expire_all()
        return stub

    def _events(self, event_type):
        return RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=event_type).all()

    def _diags(self, kind):
        """DIAGNOSTICS events of one kind. The event type is a generic carrier - the
        specific measurement is named in extra_data['kind'], so a post-process emits one
        per measurement (capture_health from the health gather, timeline_scan from the
        seek scan) and a test must say which it means."""
        return [e for e in self._events(DIAGNOSTICS)
                if json.loads(e.extra_data or '{}').get('kind') == kind]

    def _rec(self):
        return db.session.get(Recording, self.rid)

    # ── always emit, on both verdicts ─────────────────────────────────────────
    def test_clean_recording_emits_diagnostics_and_no_damage_event(self):
        """The headline gap: a healthy recording used to say nothing at all, so silence
        meant either 'clean' or 'never scanned' with no way to tell them apart."""
        self._run(self._ts(self.clean_src), self._config())

        self.assertEqual(len(self._diags('timeline_scan')), 1,
                         'a clean recording emitted no timeline diagnostics')
        self.assertEqual(self._events(SEEK_DAMAGE_DETECTED), [],
                         'SEEK_DAMAGE_DETECTED fired on an undamaged recording')
        self.assertIs(self._rec().timeline_damaged, False)

    def test_damaged_recording_emits_both_events(self):
        """The two events are complementary: DIAGNOSTICS says what was measured,
        SEEK_DAMAGE_DETECTED says it was bad enough to change what happens next."""
        self._run(self._ts(self.gappy_src), self._config(convert=True))

        self.assertEqual(len(self._diags('timeline_scan')), 1)
        self.assertEqual(len(self._events(SEEK_DAMAGE_DETECTED)), 1,
                         'SEEK_DAMAGE_DETECTED lost its existing firing condition')
        self.assertIs(self._rec().timeline_damaged, True)

    def test_diagnostics_emitted_for_non_mp4_output(self):
        """Before the split the scan only ran for mp4 + reencode_mode 'damaged', so an
        mkv recording produced no timeline stats whatsoever."""
        self._run(self._ts(self.clean_src), self._config(convert=True, fmt='mkv'))

        self.assertEqual(len(self._diags('timeline_scan')), 1)
        self.assertIsNotNone(self._rec().timeline_gap_count)

    def test_no_diagnostics_when_health_gathering_is_off(self):
        """gather_health_data is the measure gate - with it off and nothing acting on a
        verdict, the scan must not run at all."""
        self._run(self._ts(self.clean_src),
                  self._config(convert=True, fmt='mkv', gather_health_data=False))

        self.assertEqual(self._events(DIAGNOSTICS), [])
        self.assertIsNone(self._rec().timeline_gap_count)

    def test_damage_repair_survives_health_gathering_being_off(self):
        """The act path has always run its own scan regardless of gather_health_data;
        turning health data off must not silently disable damage repair."""
        stub = self._run(self._ts(self.gappy_src),
                         self._config(convert=True, gather_health_data=False))

        cmd = stub.call_args.args[2]
        self.assertIn('libx264', cmd, 'damaged file was not re-encoded')
        self.assertEqual(len(self._events(SEEK_DAMAGE_DETECTED)), 1)

    # ── the columns ───────────────────────────────────────────────────────────
    def test_timeline_columns_match_the_scanner(self):
        ts = self._ts(self.gappy_src)
        expected = probemod.scan_video_timeline(ts)
        self._run(ts, self._config())

        rec = self._rec()
        self.assertEqual(rec.timeline_gap_count, expected['gap_count'])
        self.assertAlmostEqual(rec.timeline_gap_seconds, expected['gap_seconds'], places=3)
        self.assertAlmostEqual(rec.timeline_max_gap_seconds,
                               expected['max_gap_seconds'], places=3)
        self.assertAlmostEqual(rec.timeline_deficit_seconds,
                               expected['deficit_seconds'], places=3)

    def test_scan_does_not_overwrite_the_health_columns(self):
        """span_seconds / packet_count / fps deliberately have no columns because
        recorded_duration_seconds / recorded_frame_count / recorded_fps already hold them.
        The scan must not become a second writer of those three."""
        self._run(self._ts(self.clean_src), self._config())

        rec = self._rec()
        # Written by the health gather, which runs before the scan - all three must survive.
        self.assertIsNotNone(rec.recorded_fps)
        self.assertIsNotNone(rec.recorded_frame_count)
        self.assertIsNotNone(rec.recorded_duration_seconds)

    def test_extra_data_round_trips_as_json(self):
        self._run(self._ts(self.clean_src), self._config())

        extra = json.loads(self._diags('timeline_scan')[0].extra_data)
        for key in ('kind', 'gap_basis', 'gap_threshold', 'backward_count',
                    'missing_seconds', 'span_seconds', 'packet_count', 'fps'):
            self.assertIn(key, extra, f'{key} missing from the diagnostics payload')
        for key in ('gap_count', 'gap_seconds', 'max_gap_seconds', 'deficit_seconds'):
            self.assertNotIn(key, extra,
                             f'{key} has a column; duplicating it in extra_data creates '
                             f'two sources of truth for one fact')

    # ── measure vs. act (the load-bearing one) ────────────────────────────────
    def test_reencode_never_measures_but_does_not_act(self):
        """The load-bearing case here. Measuring runs on gather_health_data;
        acting still requires mp4 + reencode_mode 'damaged'. Collapsing the two conditions
        would start re-encoding files for users who explicitly asked never to."""
        stub = self._run(self._ts(self.gappy_src),
                         self._config(convert=True, reencode_mode='never'))

        rec = self._rec()
        self.assertIsNotNone(rec.timeline_gap_count, 'the scan did not run')
        self.assertIs(rec.timeline_damaged, True, 'damage was measured but not recorded')
        self.assertEqual(len(self._diags('timeline_scan')), 1)
        self.assertEqual(self._events(SEEK_DAMAGE_DETECTED), [],
                         'SEEK_DAMAGE_DETECTED must only fire when the verdict is acted on')

        cmd = stub.call_args.args[2]
        self.assertNotIn('libx264', cmd,
                         're-encode triggered despite reencode_mode: never')
        self.assertIn('copy', cmd)

    def test_scan_runs_exactly_once_per_postprocess(self):
        """assess_seek_damage is a full-file ffprobe (14s for a 3.0 GB file over CIFS).
        The conversion phase must read the health phase's result, not re-scan."""
        real = probemod.assess_seek_damage
        with mock.patch.object(probemod, 'assess_seek_damage', side_effect=real) as spy:
            self._run(self._ts(self.gappy_src), self._config(convert=True))

        self.assertEqual(spy.call_count, 1,
                         f'file scanned {spy.call_count} times in one post-process')

    # ── observable failure ────────────────────────────────────────────────────
    def test_failed_scan_still_emits_an_event_saying_so(self):
        """A recording with NULL timeline columns and no event would render as 'never
        scanned' with no explanation - failure paths must name themselves."""
        garbage = os.path.join(self.dvr_dir, f'rec_{self.rid}.ts')
        with open(garbage, 'wb') as fh:
            fh.write(b'not a transport stream' * 100)

        self._run(garbage, self._config())

        diags = self._diags('timeline_scan')
        self.assertEqual(len(diags), 1, 'a failed scan emitted nothing')
        extra = json.loads(diags[0].extra_data)
        self.assertTrue(extra.get('scan_failed'))
        self.assertIsNone(self._rec().timeline_gap_count,
                          'a failed scan must leave the columns NULL, not zeroed')

    # ── capture health (dev/changelog/332) ────────────────────────────────────
    def test_capture_health_emits_one_event_alongside_the_columns(self):
        """The health gather wrote its numbers to the row and logged one summary line to
        dvr.log, but emitted no event - so the check was visible only to someone tailing a
        file on the server. The event joins the existing fields commit as one unit of
        work, so a recording cannot end up with one and not the other."""
        self._run(self._ts(self.clean_src), self._config())

        diags = self._diags('capture_health')
        self.assertEqual(len(diags), 1, 'the health gather emitted no event')
        self.assertIn('fps', diags[0].detail)
        self.assertIn('expected frames', diags[0].detail)

        rec = self._rec()
        self.assertIsNotNone(rec.recorded_fps)
        self.assertIsNotNone(rec.health_gathered_at)

    def test_capture_health_detail_names_which_duration_each_number_is(self):
        """Scheduled, requested, actual and content duration are four different values;
        a display surface has to say which one it is showing."""
        detail = self._run_and_health_detail()

        self.assertIn('content duration', detail)
        self.assertIn('adjusted window', detail)
        self.assertIn('scheduled', detail)

    def test_capture_health_extra_carries_only_what_has_no_column(self):
        """Same partition rule as timeline_scan: everything in the summary already has a
        column, so extra_data holds only the three values nothing on the row records."""
        self._run(self._ts(self.clean_src), self._config())

        extra = json.loads(self._diags('capture_health')[0].extra_data)
        for key in ('expected_frame_count', 'adjusted_window_seconds',
                    'scheduled_duration_seconds'):
            self.assertIn(key, extra, f'{key} missing from the capture-health payload')
        for key in ('recorded_fps', 'recorded_resolution', 'recorded_frame_count',
                    'recorded_frame_pct', 'recorded_bitrate_kbps', 'fps', 'resolution',
                    # The format profile got columns too (dev/changelog/335), so it obeys
                    # the same partition and rides in the detail string, not here.
                    'recorded_video_codec', 'recorded_pix_fmt', 'recorded_bit_depth',
                    'recorded_chroma_subsampling', 'recorded_interlaced',
                    'recorded_is_vfr', 'recorded_bits_per_pixel_frame',
                    'video_codec', 'pix_fmt', 'bit_depth'):
            self.assertNotIn(key, extra,
                             f'{key} has a column; duplicating it in extra_data creates '
                             f'two sources of truth for one fact')

    def test_no_capture_health_event_when_health_gathering_is_off(self):
        """Characterization: gather_health_data has always been the gate on this phase,
        and the event must not escape it."""
        self._run(self._ts(self.clean_src),
                  self._config(convert=True, fmt='mkv', gather_health_data=False))

        self.assertEqual(self._diags('capture_health'), [])

    def test_failed_health_probe_still_emits_an_event_saying_so(self):
        """A recording with no health numbers and no event renders as 'never checked'
        with no explanation - failure paths must name themselves."""
        garbage = os.path.join(self.dvr_dir, f'rec_{self.rid}.ts')
        with open(garbage, 'wb') as fh:
            fh.write(b'not a transport stream' * 100)

        self._run(garbage, self._config())

        diags = self._diags('capture_health')
        self.assertEqual(len(diags), 1, 'a failed health probe emitted nothing')
        self.assertTrue(json.loads(diags[0].extra_data).get('probe_failed'))
        self.assertIsNone(self._rec().recorded_fps)
        self.assertIsNone(self._rec().recorded_video_codec,
                          'a failed probe must leave the format columns NULL, not guessed')

    # ── output format profile (dev/changelog/335) ─────────────────────────────
    def test_capture_health_persists_the_output_format_profile(self):
        """parse_ffprobe already returned the whole profile and _gather_recording_health
        kept six keys of it, so the app could describe a channel test's codec and bit depth
        but not the recording made from that channel."""
        self._run(self._ts(self.clean_src), self._config())

        rec = self._rec()
        self.assertEqual(rec.recorded_video_codec, 'h264')
        self.assertEqual(rec.recorded_pix_fmt, 'yuv420p')
        self.assertEqual(rec.recorded_bit_depth, 8)
        self.assertEqual(rec.recorded_chroma_subsampling, '420')
        self.assertIs(rec.recorded_interlaced, False)
        self.assertIsNotNone(rec.recorded_is_vfr)
        self.assertIsNotNone(rec.recorded_bits_per_pixel_frame)

    def test_capture_health_detail_says_the_profile_describes_the_output(self):
        """A re-encode changes codec and pixel format, so these columns are not necessarily
        what the provider sent - the event has to say which file it is describing."""
        detail = self._run_and_health_detail()

        self.assertIn('output format:', detail)
        self.assertIn('h264', detail)

    def test_capture_health_detail_names_the_content_shortfall(self):
        """dev/changelog/432: the fixture is 20s of content in a 40s window, so half the
        recording is missing and the one line the user reads has to say so. It rides in
        the detail string rather than extra_data because both inputs already have columns
        (the partition test above is the guard on that half)."""
        detail = self._run_and_health_detail()

        self.assertIn('missing 20s (50%)', detail)

    def _run_and_health_detail(self):
        self._run(self._ts(self.clean_src), self._config())
        return self._diags('capture_health')[0].detail


if __name__ == '__main__':
    unittest.main()
