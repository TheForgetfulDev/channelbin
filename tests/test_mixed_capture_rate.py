"""Tier 2 - the postprocessor half of the mixed-capture-rate correction
(dev/docs/BUGS.md 2026-08-29 "Timeline damage scan divides by one frame rate").

`app/probe.py` can only correct the frame deficit if somebody hands it the rate each
segment actually captured at. That is `_segment_capture_rates`, reading the `probe_fps` the
watchdog already stored on `recording_segments` - the app knew all four of recording 2's
off-rate segments had changed rate (each logged RECORDING_FORMAT_CHANGED) and the scan was
dividing by the header rate anyway.

These tests cover the reading and the labelling: which rows contribute a rate, and that a
rate change reaches the recording's event log under its own name rather than as damage.
The math itself is tested in tests/test_seek_damage.py.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock

import app.config as cfgmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    DIAGNOSTICS, MIXED_FRAME_RATE_DETECTED, SEEK_DAMAGE_DETECTED,
    Recording, RecordingEvent, RecordingSegment,
)
from app.postprocessor import (  # noqa: E402
    ConversionResult, _scan_recording_timeline, _segment_capture_rates, do_postprocess,
)
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=120)


def _build_mixed(path, workdir):
    """20s at 60fps joined to 20s at 15fps through the same concat demuxer and
    `-fflags +genpts` concatenator.py uses. The joined file's header reports 60fps, so its
    honest 1,500 packets divide out to 25s of frames against a 40s span - a 15s "deficit"
    that is entirely the rate change."""
    fast = os.path.join(workdir, 'fast60.ts')
    slow = os.path.join(workdir, 'slow15.ts')
    _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=60', '-t', '20',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0', '-pix_fmt', 'yuv420p', fast)
    _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=15', '-t', '20',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0', '-pix_fmt', 'yuv420p', slow)
    listfile = os.path.join(workdir, 'concat.txt')
    with open(listfile, 'w') as fh:
        fh.write(f"file '{fast}'\nfile '{slow}'\n")
    _ffmpeg('-f', 'concat', '-safe', '0', '-fflags', '+genpts', '-i', listfile,
            '-c', 'copy', path)


class _MixedRateFixture:
    """The fixture and seeding both test classes below share. Not a TestCase - inheriting
    one would re-run every test in the parent under the child's name."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='mixedrate-app-')
        cls.mixed = os.path.join(cls._dir, 'mixed.ts')
        _build_mixed(cls.mixed, cls._dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def setUp(self):
        self.t = make_test_app()
        self.dvr_dir = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr_dir, exist_ok=True)
        acc = seed.make_account()
        self.channel = seed.make_channel(acc, name='Rate Change Feed')
        now = datetime.utcnow()
        rec = seed.make_recording(
            status='CONCATENATING', name='rate change', channel_id=self.channel.id,
            start_time=now - timedelta(seconds=40), stop_time=now)
        self.rid = rec.id
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _segments(self, *specs):
        """specs are (seconds, bytes_recorded, probe_fps) per segment."""
        start = datetime.utcnow()
        offset = 0
        for i, (seconds, n, fps) in enumerate(specs):
            db.session.add(RecordingSegment(
                recording_id=self.rid, segment_number=i,
                file_path=os.path.join(self.dvr_dir, f'seg_{i:03d}.ts'),
                started_at=start + timedelta(seconds=offset),
                ended_at=start + timedelta(seconds=offset + seconds),
                exit_reason='STALL_KILLED', bytes_recorded=n, probe_fps=fps))
            offset += seconds
        db.session.commit()

    def _ts(self):
        dest = os.path.join(self.dvr_dir, f'rec_{self.rid}.ts')
        shutil.copy2(self.mixed, dest)
        return dest

    def _diag_extra(self):
        evs = [e for e in RecordingEvent.query.filter_by(
                   recording_id=self.rid, event_type=DIAGNOSTICS).all()
               if json.loads(e.extra_data or '{}').get('kind') == 'timeline_scan']
        self.assertEqual(len(evs), 1, 'expected exactly one timeline_scan diagnostic')
        return evs[0], json.loads(evs[0].extra_data)

    def _events(self, event_type):
        return RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=event_type).all()


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class SegmentCaptureRateTests(_MixedRateFixture, unittest.TestCase):
    """Which rows contribute a rate, and what the scan does with them."""

    # ── which rows contribute ─────────────────────────────────────────────────
    def test_rates_are_read_from_the_segments_the_concat_used(self):
        self._segments((20, 4096, 60.0), (20, 8192, 15.0))

        self.assertEqual(sorted(_segment_capture_rates(self.rid)),
                         [(20.0, 15.0), (20.0, 60.0)])

    def test_a_segment_that_wrote_no_bytes_contributes_no_rate(self):
        """concatenator.py skips a zero-length segment, so its rate was never in the file
        and weighting the deficit with it would describe footage that is not there."""
        self._segments((20, 4096, 60.0), (20, 0, 15.0), (20, None, 15.0))

        self.assertEqual(_segment_capture_rates(self.rid), [(20.0, 60.0)])

    def test_an_unprobed_segment_contributes_no_rate(self):
        """The watchdog probes a segment that is still growing and leaves a field it could
        not read NULL rather than guessing. Inventing a rate for it would put the corrected
        number back where the wrong one was."""
        self._segments((20, 4096, 60.0), (20, 4096, None))

        self.assertEqual(_segment_capture_rates(self.rid), [(20.0, 60.0)])

    # ── what the scan does with them ──────────────────────────────────────────
    def test_a_rate_change_is_not_written_as_timeline_damage(self):
        """The defect end to end: the recording's stored verdict and its DIAGNOSTICS event
        both used to say a clean 40s file was 37% missing video."""
        self._segments((20, 4096, 60.0), (20, 8192, 15.0))
        damaged, _metrics, _summary = _scan_recording_timeline(self.rid, self._ts())
        db.session.expire_all()

        rec = db.session.get(Recording, self.rid)
        self.assertFalse(damaged, 'a rate change was written as timeline damage')
        self.assertIs(rec.timeline_damaged, False)
        self.assertLess(rec.timeline_deficit_seconds, 1.0)

    def test_the_event_carries_what_makes_the_number_reproducible(self):
        """capture_fps_values and deficit_fps have no columns, so extra_data is where they
        belong - and without them the corrected deficit cannot be checked against the rate
        in the file header."""
        self._segments((20, 4096, 60.0), (20, 8192, 15.0))
        _scan_recording_timeline(self.rid, self._ts())

        ev, extra = self._diag_extra()
        self.assertEqual(extra['capture_fps_values'], [15.0, 60.0])
        self.assertAlmostEqual(extra['deficit_fps'], 37.5, places=3)
        self.assertEqual(extra['fps'], 60.0, 'the header rate must still be reported as-is')
        self.assertIn('Capture frame rate changed mid-recording', ev.detail)

    def test_a_steady_rate_recording_says_nothing_about_rates(self):
        """Present only when the rate actually changed: their presence is itself the signal
        that this recording was rate-corrected, so a null would destroy the distinction."""
        self._segments((20, 4096, 60.0), (20, 8192, 60.0))
        _scan_recording_timeline(self.rid, self._ts())

        ev, extra = self._diag_extra()
        self.assertNotIn('capture_fps_values', extra)
        self.assertNotIn('deficit_fps', extra)
        self.assertNotIn('Capture frame rate changed', ev.detail)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class MixedRateReencodeTests(_MixedRateFixture, unittest.TestCase):
    """The treatment half. Getting the number right and leaving the label wrong fixes
    nothing: a mixed-rate file is still worth re-encoding, because `-fps_mode:v cfr` gives
    it one constant rate and that is what makes it seek predictably. What must not survive
    is reaching that re-encode by calling an intact timeline damaged.
    """

    def _config(self, *, reencode_mode='damaged'):
        return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {
            'dvr_output_dir': self.dvr_dir,
            'gather_health_data': True,
            'move_on_complete': {'enabled': False},
            'post_script': {'enabled': False},
            'post_process': {'enabled': True, 'format': 'mp4', 'delete_source': False,
                             'reencode_mode': reencode_mode, 'pre_output_timeout_seconds': 60,
                             'auto_restart': False, 'max_restart_attempts': 0,
                             'stall_seconds': 0, 'progress_interval_seconds': 5},
        }})

    def _run(self, cfg):
        stub = mock.Mock(return_value=ConversionResult(True))
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', stub):
            do_postprocess(self.t.app, self.rid, self._ts())
        db.session.expire_all()
        return stub

    def tearDown(self):
        with ppmod._active_lock:
            ppmod._active_conversions.clear()
            ppmod._cancel_requested.clear()
        super().tearDown()

    def test_mixed_rate_still_re_encodes_but_never_as_damage(self):
        """Both halves in one assertion set: the file is normalized, and the event log says
        it was a rate change rather than damage. SEEK_DAMAGE_DETECTED asserts damage and
        one flag must mean one thing."""
        self._segments((20, 4096, 60.0), (20, 8192, 15.0))
        stub = self._run(self._config())

        self.assertEqual(self._events(SEEK_DAMAGE_DETECTED), [],
                         'an intact timeline was reported as damaged')
        events = self._events(MIXED_FRAME_RATE_DETECTED)
        self.assertEqual(len(events), 1, 'the rate change reached no surface at all')
        self.assertIn('not to repair damage', events[0].detail)
        self.assertIn('libx264', stub.call_args.args[2],
                      'the CFR normalization was silently dropped')

    def test_a_steady_rate_clean_recording_still_stream_copies(self):
        """The correction must not become a blanket re-encode: with one rate throughout,
        this fixture's deficit is real and large, so it is damage and stays damage."""
        self._segments((20, 4096, 60.0), (20, 8192, 60.0))
        self._run(self._config())

        self.assertEqual(self._events(MIXED_FRAME_RATE_DETECTED), [],
                         'a steady-rate recording was labelled a rate change')
        self.assertEqual(len(self._events(SEEK_DAMAGE_DETECTED)), 1,
                         'the header-rate deficit stopped being reported as damage')

    def test_reencode_never_does_not_act_on_a_rate_change_either(self):
        """The measure/act split covers the new trigger too - a user who set
        reencode_mode: never must not start getting re-encodes for rate changes."""
        self._segments((20, 4096, 60.0), (20, 8192, 15.0))
        stub = self._run(self._config(reencode_mode='never'))

        self.assertEqual(self._events(MIXED_FRAME_RATE_DETECTED), [],
                         'the rate-change trigger fired despite reencode_mode: never')
        cmd = stub.call_args.args[2]
        self.assertNotIn('libx264', cmd)
        self.assertIn('copy', cmd)

    def test_the_measurement_still_happens_under_reencode_never(self):
        """Measuring runs on gather_health_data; only acting is gated. The corrected
        deficit has to be recorded either way or turning re-encoding off would also turn
        off the honest number."""
        self._segments((20, 4096, 60.0), (20, 8192, 15.0))
        self._run(self._config(reencode_mode='never'))

        rec = db.session.get(Recording, self.rid)
        self.assertIs(rec.timeline_damaged, False)
        self.assertLess(rec.timeline_deficit_seconds, 1.0)
        _ev, extra = self._diag_extra()
        self.assertEqual(extra['capture_fps_values'], [15.0, 60.0])


if __name__ == '__main__':
    unittest.main()
