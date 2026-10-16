"""Tier 2 - a hole in a recording's video converts to a held picture in sync with the audio,
never to video that runs ahead of its sound or stops under it (dev/changelog/1199). Guards
dev/docs/BUGS.md 2026-10-15 @ 04:24:24 PM: "A video gap over 10 seconds converted out of sync,
and a video that stopped before its audio was not padded".

Real ffmpeg on small synthesized local files. The conversion command is captured from
do_postprocess (run_conversion_supervised stubbed) and then executed, the way
tests/test_normalize_conversion.py does, so what is asserted is the file the command makes.
The GPU half is argv only: the suite's machine has no render node, and the lab job
dev/tools/gpu-lab/jobs/gap-fill-matrix.sh is where those commands ran on the GPU.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
from app import db, toolchain  # noqa: E402
from app.database import Recording, RecordingEvent, RecordingSegment, DIAGNOSTICS  # noqa: E402
from app.postprocessor import ConversionResult, do_postprocess  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))
RATE = '30000/1001'


def _ff(*args):
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *args],
                   check=True, timeout=120)


def _clip(path, video_seconds, audio_seconds):
    """testsrc2 for video_seconds and a tone for audio_seconds, as mpegts."""
    _ff('-f', 'lavfi', '-i', f'testsrc2=size=320x240:rate={RATE}:d={video_seconds}',
        '-f', 'lavfi', '-i', f'sine=f=440:sample_rate=48000:d={audio_seconds}',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '15', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '96k', '-f', 'mpegts', path)


def _concat(out, parts, workdir):
    """Joined with app/concatenator.py's flags."""
    listfile = os.path.join(workdir, os.path.basename(out) + '.txt')
    with open(listfile, 'w') as fh:
        fh.writelines(f"file '{p}'\n" for p in parts)
    _ff('-err_detect', 'ignore_err', '-fflags', '+genpts+discardcorrupt', '-f', 'concat',
        '-safe', '0', '-i', listfile, '-c', 'copy', out)


def _video_dts(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                          'packet=dts_time', '-of', 'csv=p=0', path],
                         capture_output=True, text=True, check=True).stdout
    return [float(x.strip(',')) for x in out.split() if x.strip(',') not in ('', 'N/A')]


def _config(**pp_overrides):
    # duplicated from tests/test_normalize_conversion.py - a test module stays self-contained
    pp = dict({'enabled': True, 'format': 'mp4', 'delete_source': False,
               'reencode_mode': 'damaged', 'pre_output_timeout_seconds': 60,
               'auto_restart': True, 'max_restart_attempts': 3, 'stall_seconds': 0,
               'progress_interval_seconds': 5, 'video_encoder': 'software',
               'video_codec': 'h264', 'max_height': 0, 'audio_channels': 'source',
               'loudness_normalize': False, 'vaapi_decode': False}, **pp_overrides)
    return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {
        'gather_health_data': False,
        'move_on_complete': {'enabled': False},
        'post_script': {'enabled': False},
        'post_process': pp,
    }})


@unittest.skipUnless(_HAVE_FFMPEG, 'needs ffmpeg and ffprobe on PATH')
class _GapCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = tempfile.mkdtemp(prefix='gapfill-src-')
        d = cls.src
        # A capture segment whose video stopped 15s before its audio, then a whole one: the
        # concat demuxer places segment 2 after segment 1's audio, leaving a 15s video gap.
        _clip(os.path.join(d, 'short.ts'), 10, 25)
        _clip(os.path.join(d, 'whole.ts'), 20, 20)
        _concat(os.path.join(d, 'seam.ts'),
                [os.path.join(d, 'short.ts'), os.path.join(d, 'whole.ts')], d)
        # The video stops 15s before the audio at the end of the file.
        _clip(os.path.join(d, 'tail.ts'), 10, 25)
        _clip(os.path.join(d, 'clean.ts'), 10, 10)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.src, ignore_errors=True)

    def setUp(self):
        toolchain.reset_cache()
        self.t = make_test_app()
        now = datetime.utcnow()
        self.rec = seed.make_recording(status='CONCATENATING', name='gapfill',
                                       start_time=now - timedelta(seconds=45), stop_time=now,
                                       recorded_duration_seconds=45.0)
        self.ts = os.path.join(self.t._tmpdir, f'rec_{self.rec.id}.ts')
        self.rec.output_path = self.ts
        db.session.commit()
        self.rid = self.rec.id

    def tearDown(self):
        with ppmod._active_lock:
            ppmod._active_conversions.clear()
            ppmod._cancel_requested.clear()
        toolchain.reset_cache()
        self.t.cleanup()

    def _use(self, name):
        shutil.copyfile(os.path.join(self.src, name), self.ts)
        start = datetime.utcnow()
        db.session.add(RecordingSegment(
            recording_id=self.rid, segment_number=0,
            file_path=os.path.join(self.t._tmpdir, 'seg_0.ts'),
            started_at=start, ended_at=start + timedelta(seconds=45),
            exit_reason='COMPLETED', bytes_recorded=4096, probe_resolution='320x240',
            probe_fps=29.97, probe_audio_channels=1, probe_interlaced=False,
            content_duration_seconds=45.0))
        db.session.commit()

    def _convert(self, cfg, trial_rc=0):
        """Run do_postprocess with the encode stubbed; return the first command it would run."""
        # duplicated from tests/test_normalize_conversion.py::_NormalizeCase._convert - the
        # same capture, without that module's fixture clips
        def attempt(app, rid, cmd, part_file, **kw):
            with open(part_file, 'wb') as fh:
                fh.write(b'p' * 64)
            return ConversionResult(True, 'success', out_time=45.0)

        def join(parts, output_path, *a, **k):
            with open(output_path, 'wb') as fh:
                fh.write(b'o' * 128)
            return ppmod.PartJoinResult(True, 'success', parts=len(parts))

        stub = mock.Mock(side_effect=attempt)
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', stub), \
             mock.patch.object(ppmod, 'join_conversion_parts', join), \
             mock.patch.object(ppmod, 'finalize_part', lambda part, *a, **k: 0.0), \
             mock.patch.object(toolchain, '_run_gpu_trial',
                               lambda *a, **k: (trial_rc, '')):
            do_postprocess(self.t.app, self.rid, self.ts)
        db.session.expire_all()
        return stub.call_args_list[0][0][2]

    def _execute(self, cmd):
        out = os.path.join(self.t._tmpdir, 'out.mp4')
        run = subprocess.run(cmd[:-1] + [out], stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True, timeout=300)
        self.assertEqual(run.returncode, 0, run.stderr[-600:])
        return out

    def _events(self, kind):
        evs = RecordingEvent.query.filter_by(recording_id=self.rid, event_type=DIAGNOSTICS).all()
        return [e for e in evs if json.loads(e.extra_data or '{}').get('kind') == kind]

    @staticmethod
    def _arg(cmd, flag):
        return cmd[cmd.index(flag) + 1] if flag in cmd else None


class LongVideoGapTests(_GapCase):
    def test_a_15s_video_gap_is_held_as_a_picture_and_stays_in_sync(self):
        """The defect: ffmpeg closed up the 15s video-only gap as a timestamp discontinuity,
        so the rest of the video played 15s ahead of its audio and ended 15s early, and the
        output scanned clean. The gap must come out filled, with the video reaching the end
        of the audio."""
        self._use('seam.ts')
        out = self._execute(self._convert(_config()))
        dts = _video_dts(out)
        gaps = [b - a for a, b in zip(dts, dts[1:]) if b - a > 0.25]
        self.assertEqual(gaps, [], 'every second of the gap is a repeated frame')
        from app.probe import stream_ends
        ends = stream_ends(out)
        self.assertAlmostEqual(ends['video'][0], ends['audio'][0], delta=0.5)
        self.assertGreater(ends['video'][0], 44.0, 'not collapsed to 30s')

    def test_the_command_keeps_exactly_the_gap_the_scan_measured(self):
        self._use('seam.ts')
        cmd = self._convert(_config())
        gap = db.session.get(Recording, self.rid).timeline_max_gap_seconds
        self.assertAlmostEqual(gap, 15.0, delta=0.1)
        self.assertEqual(self._arg(cmd, '-dts_delta_threshold'), '16', 'the scan\'s gap, plus 1')
        self.assertLess(cmd.index('-dts_delta_threshold'), cmd.index('-i'))

    def test_a_stream_copy_keeps_the_gap_in_sync_too(self):
        self._use('seam.ts')
        cfg = _config(reencode_mode='never')
        cfg['recording']['gather_health_data'] = True
        cmd = self._convert(cfg)
        self.assertEqual(self._arg(cmd, '-c:v'), 'copy')
        self.assertEqual(self._arg(cmd, '-dts_delta_threshold'), '16')
        from app.probe import stream_ends
        ends = stream_ends(self._execute(cmd))
        self.assertAlmostEqual(ends['video'][0], ends['audio'][0], delta=0.5)

    def test_the_threshold_is_only_raised_past_ffmpegs_own(self):
        f = ppmod.dts_delta_threshold
        self.assertIsNone(f(None))
        self.assertIsNone(f(0.0))
        self.assertIsNone(f(8.1), "recording 4's 8.1s gap is already kept by the default")
        self.assertIsNone(f(9.0))
        self.assertEqual(f(9.5), '11')
        self.assertEqual(f(15.004), '17')


class EndPadTests(_GapCase):
    def test_a_video_that_stops_before_its_audio_is_held_to_the_end(self):
        """The defect: -fps_mode cfr never pads past the last frame, so the last 15s of
        sound played under no video. The video must now reach the audio's end."""
        self._use('tail.ts')
        cmd = self._convert(_config(reencode_mode='always'))
        self.assertIn('tpad=stop_mode=clone', self._arg(cmd, '-vf'))
        from app.probe import stream_ends
        ends = stream_ends(self._execute(cmd))
        self.assertAlmostEqual(ends['video'][0], ends['audio'][0], delta=0.5)

    def test_the_pad_is_said_on_the_recording(self):
        self._use('tail.ts')
        self._convert(_config(reencode_mode='always'))
        evs = self._events('conversion_end_padded')
        self.assertEqual(len(evs), 1)
        extra = json.loads(evs[0].extra_data)
        self.assertAlmostEqual(extra['pad_seconds'], 15.0, delta=0.5)
        self.assertIn('sound but no new video', evs[0].detail)
        self.assertIn('ends 15s before the audio (video to 10s, audio to 25s)', evs[0].detail)

    def test_a_clean_recording_keeps_its_command(self):
        """Characterization: a file with no long gap and tracks ending together gets neither
        flag, so its command is the one it always had."""
        self._use('clean.ts')
        cmd = self._convert(_config(reencode_mode='always'))
        self.assertNotIn('-vf', cmd)
        self.assertNotIn('-dts_delta_threshold', cmd)
        self.assertEqual(self._events('conversion_end_padded'), [])

    def test_the_end_of_a_ts_is_read_where_its_header_says_nothing(self):
        """stream_ends() reads no duration for a TS video track that stopped a minute before
        the audio; tail_track_ends() finds where it stopped."""
        from app.probe import stream_ends, tail_track_ends
        # Stream-copied together from separate sources, as a capture is: an encode that
        # wrote both tracks itself leaves ffprobe enough to state the video's duration.
        d = self.t._tmpdir
        v, a, path = (os.path.join(d, n) for n in ('v.ts', 'a.ts', 'long_tail.ts'))
        _ff('-f', 'lavfi', '-i', f'testsrc2=size=320x240:rate={RATE}:d=5', '-c:v', 'libx264',
            '-preset', 'ultrafast', '-g', '15', '-pix_fmt', 'yuv420p', '-f', 'mpegts', v)
        _ff('-f', 'lavfi', '-i', 'sine=f=440:sample_rate=48000:d=60', '-c:a', 'aac',
            '-b:a', '96k', '-f', 'mpegts', a)
        _ff('-i', v, '-i', a, '-map', '0:v', '-map', '1:a', '-c', 'copy', '-f', 'mpegts', path)
        self.assertEqual(stream_ends(path)['video'], [])
        ends = tail_track_ends(path)
        self.assertAlmostEqual(ends['video'], 5.0, delta=0.2)
        self.assertAlmostEqual(ends['audio'], 60.0, delta=0.2)


class GpuEndPadCommandTests(_GapCase):
    """argv only: VAAPI cannot run here."""

    def test_the_pad_runs_on_software_frames_ahead_of_the_upload(self):
        self._use('tail.ts')
        vf = self._arg(self._convert(_config(video_encoder='vaapi', reencode_mode='always')),
                       '-vf')
        self.assertIn('tpad=stop_mode=clone', vf)
        self.assertLess(vf.index('tpad'), vf.index('hwupload'))

    def test_a_padded_recording_is_not_decoded_on_the_gpu(self):
        """GPU decode would otherwise run: the scan finds this file clean and single-format,
        and the cap is what makes it re-encode."""
        self._use('tail.ts')
        cmd = self._convert(_config(video_encoder='vaapi', vaapi_decode=True, max_height=120))
        self.assertNotIn('-hwaccel', cmd)
        self.assertIn('tpad=stop_mode=clone', self._arg(cmd, '-vf'))

    def test_gpu_decode_still_runs_on_the_same_file_without_a_pad(self):
        self._use('clean.ts')
        cmd = self._convert(_config(video_encoder='vaapi', vaapi_decode=True, max_height=120))
        self.assertEqual(self._arg(cmd, '-hwaccel'), 'vaapi')


if __name__ == '__main__':
    unittest.main()
