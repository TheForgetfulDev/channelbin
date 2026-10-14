"""Tier 2 - every re-encode normalizes a recording to its own biggest format, with real ffmpeg
on small synthesized local files (dev/changelog/1184). Guards dev/docs/BUGS.md 2026-10-11
09:37 PM: "The re-encode squeezed a mixed-format recording down to its first segment's
format", "The GPU re-encode died on a recording whose picture size changed partway through"
(argv only) and "A GPU encoder that died mid-conversion stayed 'working'".

The conversion command is captured from do_postprocess (run_conversion_supervised stubbed)
and then executed, so what is asserted is the file the command really makes. Nothing here
can run VAAPI - the suite's machine has no render node - so the GPU half asserts argv
shapes against a stubbed trial, the way tests/test_gpu_conversion.py does.
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
from app.alerts import GPU_ENCODER_UNAVAILABLE  # noqa: E402
from app.database import (Alert, RecordingEvent, RecordingSegment, CONVERSION_DONE,  # noqa: E402
                          DIAGNOSTICS)
from app.postprocessor import ConversionResult, do_postprocess  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))
DEVICE = '/dev/dri/renderD128'
NORMALIZE_EVENT = 'FORMAT_NORMALIZE_NEEDED'


def _ff(*args):
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *args],
                   check=True, timeout=120)


def _clip(path, size, rate, channels, *, seconds=2, vf=None, extra=()):
    """`seconds` of testsrc2 at size/rate with an AC-3 tone of `channels` channels, as
    mpegts, a keyframe every half second so a seek always lands near one."""
    tone = ['-f', 'lavfi', '-i', 'sine=f=440:sample_rate=48000']
    cmd = ['-f', 'lavfi', '-i', f'testsrc2=size={size}:rate={rate}', *tone]
    if channels == 6:
        cmd += ['-filter_complex',
                '[1:a]pan=5.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0[a]', '-map', '0:v',
                '-map', '[a]']
    else:
        cmd += ['-map', '0:v', '-map', '1:a', '-ac', str(channels)]
    if vf:
        cmd += ['-vf', vf]
    cmd += ['-t', str(seconds), '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '12',
            '-pix_fmt', 'yuv420p', *extra, '-c:a', 'ac3', '-f', 'mpegts', path]
    _ff(*cmd)


def _concat(out, parts, workdir):
    """Joined the way concatenator.py joins segments: concat demuxer, -fflags +genpts."""
    listfile = os.path.join(workdir, os.path.basename(out) + '.txt')
    with open(listfile, 'w') as fh:
        fh.writelines(f"file '{p}'\n" for p in parts)
    _ff('-f', 'concat', '-safe', '0', '-fflags', '+genpts', '-i', listfile, '-c', 'copy', out)


def _frames(path, stream='v', entries='width,height'):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', stream,
                          '-show_entries', f'frame={entries}', '-of', 'csv=p=0', path],
                         capture_output=True, text=True, check=True).stdout
    return [ln.strip().rstrip(',') for ln in out.splitlines() if ln.strip()]


def _stream(path, stream='v:0'):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', stream, '-show_entries',
                          'stream=width,height,r_frame_rate,sample_aspect_ratio,field_order,'
                          'channels,nb_frames', '-of', 'json', path],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)['streams'][0]


def _config(**pp_overrides):
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


def _trial(rc, tail='', by_codec=None):
    """A _run_gpu_trial stand-in; by_codec overrides the answer per codec."""
    calls = []

    def run(ffmpeg_path, device, codec='h264'):
        calls.append(codec)
        if by_codec and codec in by_codec:
            return by_codec[codec]
        return rc, tail
    run.calls = calls
    return run


@unittest.skipUnless(_HAVE_FFMPEG, 'needs ffmpeg and ffprobe on PATH')
class _NormalizeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = tempfile.mkdtemp(prefix='normalize-src-')
        d = cls.src
        # 720p29.97 stereo, then 1080p59.94 5.1: a failover onto a bigger member.
        _clip(os.path.join(d, 'a720.ts'), '1280x720', '30000/1001', 2)
        _clip(os.path.join(d, 'b1080.ts'), '1920x1080', '60000/1001', 6)
        _concat(os.path.join(d, 'mixed.ts'),
                [os.path.join(d, 'a720.ts'), os.path.join(d, 'b1080.ts')], d)
        _clip(os.path.join(d, 'int.ts'), '1920x1080', '60000/1001', 2,
              vf='tinterlace=mode=interleave_top,setfield=tff',
              extra=('-flags', '+ildct+ilme', '-x264opts', 'tff=1'))
        _clip(os.path.join(d, 'ana.ts'), '1440x1080', '25', 2, vf='setsar=4/3')
        # setparams, not -color_trc: the encoder takes its color tags from the frames.
        pq = 'setparams=color_trc=smpte2084:color_primaries=bt2020:colorspace=bt2020nc'
        _clip(os.path.join(d, 'hdr.ts'), '1280x720', '25', 2, vf=pq)
        _clip(os.path.join(d, 'hdr1080.ts'), '1920x1080', '50', 2, vf=pq)
        _concat(os.path.join(d, 'hdrmixed.ts'),
                [os.path.join(d, 'hdr.ts'), os.path.join(d, 'hdr1080.ts')], d)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.src, ignore_errors=True)

    def setUp(self):
        toolchain.reset_cache()
        self.t = make_test_app()
        now = datetime.utcnow()
        self.rec = seed.make_recording(status='CONCATENATING', name='normalize',
                                       start_time=now - timedelta(seconds=4), stop_time=now,
                                       recorded_duration_seconds=4.0)
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

    def _use(self, name, segments):
        """The recording's joined .ts is source `name`; `segments` are the capture-time
        probes as (resolution, fps, channels, interlaced)."""
        shutil.copyfile(os.path.join(self.src, name), self.ts)
        start = datetime.utcnow()
        for i, (res, fps, ch, inter) in enumerate(segments):
            db.session.add(RecordingSegment(
                recording_id=self.rid, segment_number=i,
                file_path=os.path.join(self.t._tmpdir, f'seg_{i}.ts'),
                started_at=start + timedelta(seconds=2 * i),
                ended_at=start + timedelta(seconds=2 * i + 2),
                exit_reason='STALL_KILLED', bytes_recorded=4096, probe_resolution=res,
                probe_fps=fps, probe_audio_channels=ch, probe_interlaced=inter,
                content_duration_seconds=2.0))
        db.session.commit()

    def _convert(self, cfg, trial=None):
        """Run do_postprocess with the encode stubbed; return every command it would run."""
        def attempt(app, rid, cmd, part_file, **kw):
            with open(part_file, 'wb') as fh:
                fh.write(b'p' * 64)
            return ConversionResult(True, 'success', out_time=4.0)

        def join(parts, output_path, *a, **k):
            with open(output_path, 'wb') as fh:
                fh.write(b'o' * 128)
            return ppmod.PartJoinResult(True, 'success', parts=len(parts))

        stub = mock.Mock(side_effect=attempt)
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', stub), \
             mock.patch.object(ppmod, 'join_conversion_parts', join), \
             mock.patch.object(ppmod, 'finalize_part', lambda part, *a, **k: 0.0), \
             mock.patch.object(toolchain, '_run_gpu_trial', trial or _trial(0)):
            do_postprocess(self.t.app, self.rid, self.ts)
        db.session.expire_all()
        return [c[0][2] for c in stub.call_args_list]

    def _execute(self, cmd, name='out.mp4'):
        out = os.path.join(self.t._tmpdir, name)
        run = subprocess.run(cmd[:-1] + [out], stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True, timeout=300)
        self.assertEqual(run.returncode, 0, run.stderr[-600:])
        return out

    def _events(self, event_type, kind=None):
        evs = RecordingEvent.query.filter_by(recording_id=self.rid, event_type=event_type).all()
        if kind is None:
            return evs
        return [e for e in evs if json.loads(e.extra_data or '{}').get('kind') == kind]

    @staticmethod
    def _arg(cmd, flag):
        return cmd[cmd.index(flag) + 1] if flag in cmd else None


MIXED_ROWS = [('1280x720', 29.97, 2, False), ('1920x1080', 59.94, 6, False)]


class BiggestFormatTests(_NormalizeCase):
    def test_a_720p30_then_1080p60_recording_comes_out_1080p60_throughout(self):
        """The defect: the re-encode squeezed the whole file to its FIRST segment's format,
        so the 1080p60 half came out 720p30. Every frame must now be 1920x1080 at 59.94."""
        self._use('mixed.ts', MIXED_ROWS)
        out = self._execute(self._convert(_config())[0])
        self.assertEqual(set(_frames(out)), {'1920,1080'})
        st = _stream(out)
        self.assertEqual(st['r_frame_rate'], '60000/1001')
        self.assertGreaterEqual(len(_frames(out)), 230, 'both halves at the higher rate')

    def test_the_size_change_is_its_own_trigger_and_names_both_sizes(self):
        self._use('mixed.ts', MIXED_ROWS)
        self._convert(_config())
        evs = self._events(NORMALIZE_EVENT, 'mixed_resolution')
        self.assertEqual(len(evs), 1)
        self.assertIn('1280x720 and 1920x1080', evs[0].detail)
        self.assertIn('1920x1080', evs[0].detail)

    def test_a_720p_only_recording_stays_720p_and_keeps_its_command(self):
        self._use('a720.ts', [('1280x720', 29.97, 2, False)])
        cmd = self._convert(_config(reencode_mode='always'))[0]
        self.assertNotIn('-vf', cmd, 'a clean single-format file needs no filter')
        out = self._execute(cmd)
        self.assertEqual(set(_frames(out)), {'1280,720'})
        self.assertEqual(self._events(NORMALIZE_EVENT), [])

    def test_a_cap_brings_a_mixed_recording_down_to_the_cap_only(self):
        """720 + 1080 under a 720 cap: the 1080 stretch comes down, the 720 is untouched."""
        self._use('mixed.ts', MIXED_ROWS)
        out = self._execute(self._convert(_config(max_height=720))[0])
        self.assertEqual(set(_frames(out)), {'1280,720'})

    def test_a_recording_over_the_cap_is_re_encoded_under_damaged_and_says_why(self):
        self._use('b1080.ts', [('1920x1080', 59.94, 6, False)])
        cmd = self._convert(_config(max_height=720))[0]
        self.assertEqual(self._arg(cmd, '-c:v'), 'libx264')
        self.assertEqual(len(self._events(NORMALIZE_EVENT, 'above_max_resolution')), 1)
        self.assertEqual(set(_frames(self._execute(cmd))), {'1280,720'})

    def test_never_still_never_re_encodes(self):
        self._use('mixed.ts', MIXED_ROWS)
        cmd = self._convert(_config(reencode_mode='never', max_height=720))[0]
        self.assertEqual(self._arg(cmd, '-c:v'), 'copy')
        self.assertEqual(self._events(NORMALIZE_EVENT), [])

    def test_the_done_event_names_what_it_was_normalized_from_and_to(self):
        self._use('mixed.ts', MIXED_ROWS)
        self._convert(_config())
        done = self._events(CONVERSION_DONE)[0].detail
        self.assertIn('the recording held 720p and 1080p at 29.97 and 59.94 fps', done)
        self.assertIn('normalized to 1080p59.94 H.264', done)
        self.assertIn('audio 5.1', done)


class SurveyTests(_NormalizeCase):
    """The whole-file survey sees what a segment row's first-frames probe cannot."""

    def test_the_survey_reports_every_size_rate_and_layout_in_the_file(self):
        from app.probe import survey_formats
        got = survey_formats(os.path.join(self.src, 'mixed.ts'))
        self.assertEqual([s[:2] for s in got['video_sizes']], [[1280, 720], [1920, 1080]])
        self.assertEqual(got['audio_channels'], [2, 6])
        self.assertEqual(len(got['video_rates']), 2)
        self.assertAlmostEqual(max(got['video_rates']), 59.94, delta=0.05)

    def test_a_change_inside_one_segment_still_comes_out_at_the_biggest_format(self):
        """One row, which saw only the 720p30 start: the survey finds the rest."""
        self._use('mixed.ts', [('1280x720', 29.97, 2, False)])
        cmd = self._convert(_config())[0]
        self.assertEqual(self._arg(cmd, '-r'), '60000/1001')
        self.assertEqual(len(self._events('MIXED_FRAME_RATE_DETECTED')), 1)
        self.assertEqual(len(self._events(NORMALIZE_EVENT, 'mixed_resolution')), 1)
        self.assertEqual(set(_frames(self._execute(cmd))), {'1920,1080'})


class AudioTests(_NormalizeCase):
    def test_ac_follows_the_widest_layout_even_on_the_stream_copy(self):
        """A stereo start no longer downmixes a later 5.1 stretch (dev/changelog/1163)."""
        self._use('mixed.ts', MIXED_ROWS)
        cmd = self._convert(_config(reencode_mode='never'))[0]
        self.assertEqual(self._arg(cmd, '-ac'), '6')
        out = self._execute(cmd)
        self.assertEqual(set(_frames(out, 'a', 'channels')), {'6'})

    def test_the_survey_finds_the_widest_layout_when_the_rows_do_not(self):
        """A change inside one segment: its row saw only the stereo start."""
        self._use('mixed.ts', [('1280x720', 29.97, 2, False)])
        cmd = self._convert(_config(reencode_mode='never'))[0]
        self.assertEqual(self._arg(cmd, '-ac'), '6')

    def test_stereo_overrides_the_source(self):
        self._use('mixed.ts', MIXED_ROWS)
        cmd = self._convert(_config(reencode_mode='never', audio_channels='stereo'))[0]
        self.assertEqual(self._arg(cmd, '-ac'), '2')
        self.assertEqual(self._arg(cmd, '-b:a'), '192k')

    def test_the_bitrate_is_per_stereo_pair(self):
        self._use('mixed.ts', MIXED_ROWS)
        cmd = self._convert(_config(reencode_mode='never'))[0]
        self.assertEqual(self._arg(cmd, '-b:a'), '576k')

    def test_a_mixed_layout_is_named_but_triggers_no_video_re_encode(self):
        self._use('b1080.ts', [('1920x1080', 59.94, 2, False), ('1920x1080', 59.94, 6, False)])
        cmd = self._convert(_config())[0]
        self.assertEqual(self._arg(cmd, '-c:v'), 'copy')
        self.assertEqual(len(self._events(DIAGNOSTICS, 'mixed_audio_layout')), 1)

    def test_loudness_leveling_applies_the_measured_gain(self):
        self._use('a720.ts', [('1280x720', 29.97, 2, False)])
        cmd = self._convert(_config(reencode_mode='never', loudness_normalize=True))[0]
        af = self._arg(cmd, '-af')
        self.assertIsNotNone(af)
        self.assertTrue(af.startswith('volume='))
        measured = self._events(DIAGNOSTICS, 'loudness_measured')
        self.assertEqual(len(measured), 1)
        lufs = json.loads(measured[0].extra_data)['lufs']
        out = self._execute(cmd)
        from app.probe import measure_loudness
        self.assertAlmostEqual(measure_loudness(out, 'ffmpeg'), -24.0, delta=1.0,
                               msg=f'source measured {lufs} LUFS')


class PictureTests(_NormalizeCase):
    def test_interlaced_input_comes_out_progressive_at_field_rate(self):
        self._use('int.ts', [('1920x1080', 29.97, 2, True)])
        out = self._execute(self._convert(_config(reencode_mode='always'))[0])
        st = _stream(out)
        self.assertIn(st.get('field_order', 'progressive'), ('progressive', 'unknown'))
        self.assertEqual(st['r_frame_rate'], '60000/1001')
        self.assertGreaterEqual(len(_frames(out)), 115, 'one frame per field')

    def test_non_square_pixels_come_out_square(self):
        self._use('ana.ts', [('1440x1080', 25.0, 2, False)])
        out = self._execute(self._convert(_config(reencode_mode='always'))[0])
        st = _stream(out)
        self.assertEqual((st['width'], st['height']), (1920, 1080))
        self.assertEqual(st.get('sample_aspect_ratio'), '1:1')

    def test_an_hdr_source_is_not_normalized_in_software_and_says_why(self):
        self._use('hdrmixed.ts', [('1280x720', 25.0, 2, False), ('1920x1080', 50.0, 2, False)])
        cmd = self._convert(_config(reencode_mode='always'))[0]
        self.assertNotIn('-vf', cmd)
        self.assertEqual(len(self._events(DIAGNOSTICS, 'normalize_skipped_hdr')), 1)

    def test_an_hdr_source_alone_is_never_a_normalize_trigger_in_software(self):
        self._use('hdrmixed.ts', [('1280x720', 25.0, 2, False), ('1920x1080', 25.0, 2, False)])
        self._convert(_config())
        self.assertEqual(self._events(NORMALIZE_EVENT), [])


class GpuCommandTests(_NormalizeCase):
    """argv only: VAAPI cannot run here."""

    def test_a_mixed_recording_on_the_gpu_scales_before_the_upload(self):
        self._use('mixed.ts', MIXED_ROWS)
        cmd = self._convert(_config(video_encoder='vaapi'))[0]
        self.assertEqual(self._arg(cmd, '-reinit_filter:v'), '0')
        self.assertLess(cmd.index('-reinit_filter:v'), cmd.index('-i'))
        vf = self._arg(cmd, '-vf')
        self.assertIn('pad=w=1920:h=1080', vf)
        self.assertTrue(vf.endswith('format=nv12,hwupload'))
        self.assertEqual(self._arg(cmd, '-c:v'), 'h264_vaapi')
        self.assertNotIn('-reinit_filter:a', cmd)

    def test_hevc_uses_its_own_quality_and_the_apple_tag(self):
        self._use('mixed.ts', MIXED_ROWS)
        cmd = self._convert(_config(video_encoder='vaapi', video_codec='hevc'))[0]
        self.assertEqual(self._arg(cmd, '-c:v'), 'hevc_vaapi')
        self.assertEqual(self._arg(cmd, '-qp'), '27')
        self.assertEqual(self._arg(cmd, '-tag:v'), 'hvc1')

    def test_a_device_without_hevc_encodes_h264_on_the_gpu_and_says_so(self):
        self._use('mixed.ts', MIXED_ROWS)
        trial = _trial(0, by_codec={'hevc': (1, "Unknown encoder 'hevc_vaapi'")})
        cmd = self._convert(_config(video_encoder='vaapi', video_codec='hevc'), trial)[0]
        self.assertEqual(self._arg(cmd, '-c:v'), 'h264_vaapi')
        evs = self._events(DIAGNOSTICS, 'conversion_hevc_unavailable')
        self.assertEqual(len(evs), 1)
        self.assertTrue(json.loads(evs[0].extra_data)['gpu_h264'])
        self.assertIn('encodes H.264 on the GPU instead', evs[0].detail)
        # The HEVC failure stands as the alert even though H.264 passed.
        self.assertEqual(Alert.query.filter_by(alert_type=GPU_ENCODER_UNAVAILABLE,
                                               dismissed_at=None).count(), 1)

    def test_no_gpu_at_all_falls_back_to_software_h264(self):
        self._use('mixed.ts', MIXED_ROWS)
        cmd = self._convert(_config(video_encoder='vaapi', video_codec='hevc'),
                            _trial(1, 'No VA display found'))[0]
        self.assertEqual(self._arg(cmd, '-c:v'), 'libx264')

    def test_gpu_decode_only_on_a_clean_single_format_recording(self):
        self._use('b1080.ts', [('1920x1080', 59.94, 6, False)])
        cmd = self._convert(_config(video_encoder='vaapi', vaapi_decode=True,
                                    reencode_mode='always'))[0]
        # 'always' with health data off never scanned for damage, so nothing proved it clean.
        self.assertNotIn('-hwaccel', cmd)

    def test_gpu_decode_is_refused_on_a_mixed_recording(self):
        self._use('mixed.ts', MIXED_ROWS)
        cmd = self._convert(_config(video_encoder='vaapi', vaapi_decode=True))[0]
        self.assertNotIn('-hwaccel', cmd)

    def test_gpu_decode_runs_where_the_scan_found_a_clean_single_format(self):
        self._use('b1080.ts', [('1920x1080', 59.94, 6, False)])
        cfg = _config(video_encoder='vaapi', vaapi_decode=True, max_height=720)
        cmd = self._convert(cfg)[0]
        self.assertEqual(self._arg(cmd, '-hwaccel'), 'vaapi')
        self.assertEqual(self._arg(cmd, '-hwaccel_device'), DEVICE)
        self.assertIn('scale_vaapi=w=1280:h=720:format=nv12', self._arg(cmd, '-vf'))

    def test_hdr_on_the_gpu_keeps_hdr10_with_hevc(self):
        self._use('hdrmixed.ts', [('1280x720', 25.0, 2, False), ('1920x1080', 50.0, 2, False)])
        cmd = self._convert(_config(video_encoder='vaapi', video_codec='hevc'))[0]
        self.assertTrue(self._arg(cmd, '-vf').endswith('format=p010,hwupload'))
        self.assertEqual(self._arg(cmd, '-profile:v'), 'main10')

    def test_hdr_on_the_gpu_is_tone_mapped_for_h264(self):
        self._use('hdrmixed.ts', [('1280x720', 25.0, 2, False), ('1920x1080', 50.0, 2, False)])
        cmd = self._convert(_config(video_encoder='vaapi'))[0]
        self.assertIn('tonemap_vaapi', self._arg(cmd, '-vf'))
        self.assertNotIn('-profile:v', cmd)

    def test_the_trial_and_the_conversion_spell_the_encoder_one_way(self):
        trial = toolchain.gpu_trial_cmd('ffmpeg', DEVICE, 'hevc')
        enc = toolchain.vaapi_encode_args('hevc')
        self.assertEqual(trial[trial.index('-c:v'):trial.index('-c:v') + len(enc)], enc)
        self.assertEqual(trial[trial.index('-vf') + 1], toolchain.VAAPI_UPLOAD)


class GpuDeathTests(_NormalizeCase):
    """dev/docs/BUGS.md 2026-10-11 09:37 PM: a GPU that dies on real content must not stay
    'working' for the next conversion."""

    def _died_then_ok(self):
        calls = []

        def attempt(app, rid, cmd, part_file, **kw):
            calls.append(cmd)
            with open(part_file, 'wb') as fh:
                fh.write(b'p' * 64)
            if len(calls) == 1:
                return ConversionResult(False, 'died', '[AVHWFramesContext @ 0x1] '
                                        'Failed to create surface', out_time=1.0)
            return ConversionResult(True, 'success', out_time=4.0)
        return attempt, calls

    def _convert_dying(self, cfg):
        attempt, calls = self._died_then_ok()

        def join(parts, output_path, *a, **k):
            with open(output_path, 'wb') as fh:
                fh.write(b'o' * 128)
            return ppmod.PartJoinResult(True, 'success', parts=len(parts))

        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', mock.Mock(side_effect=attempt)), \
             mock.patch.object(ppmod, 'join_conversion_parts', join), \
             mock.patch.object(ppmod, 'finalize_part', lambda part, *a, **k: 0.0), \
             mock.patch.object(toolchain, '_run_gpu_trial', _trial(0)):
            do_postprocess(self.t.app, self.rid, self.ts)
        db.session.expire_all()
        return calls

    def test_a_mid_run_death_drops_the_cached_pass_and_raises_the_alert(self):
        self._use('b1080.ts', [('1920x1080', 59.94, 6, False)])
        calls = self._convert_dying(_config(video_encoder='vaapi', reencode_mode='always'))
        self.assertEqual(self._arg(calls[-1], '-c:v'), 'libx264')
        self.assertEqual(toolchain._gpu_trial, {}, 'the cached pass must not outlive the death')
        self.assertEqual(Alert.query.filter_by(alert_type=GPU_ENCODER_UNAVAILABLE,
                                               dismissed_at=None).count(), 1)

    def test_a_death_under_gpu_decode_first_retries_with_cpu_decode_on_the_gpu(self):
        self._use('b1080.ts', [('1920x1080', 59.94, 6, False)])
        calls = self._convert_dying(_config(video_encoder='vaapi', vaapi_decode=True,
                                            max_height=720))
        self.assertIn('-hwaccel', calls[0])
        self.assertNotIn('-hwaccel', calls[-1])
        self.assertEqual(self._arg(calls[-1], '-c:v'), 'h264_vaapi')
        self.assertEqual(len(self._events('CONVERSION_RESTARTED',
                                          'conversion_gpu_decode_fallback')), 1)


if __name__ == '__main__':
    unittest.main()
