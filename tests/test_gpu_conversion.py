"""Hardware-encoded conversion through VAAPI (dev/changelog/1125).

recording.post_process.video_encoder chooses what does a video re-encode: libx264 on the CPU
(the default, and everything that existed before) or h264_vaapi on a GPU reached through a
DRM render node. Nothing here can touch a real GPU - the machine running the suite has no
render node and the one that does is a different box - so the trial encode and the
conversion runner are both stubbed, and what is asserted is the command that would have
been spawned, the events written on the recording, the standing alert and the Readiness
answer.

Guarded:
  1. The encoder is chosen in one place and the command shapes are right: -vaapi_device
     before -i, format=nv12,hwupload then h264_vaapi with -qp, and nothing of libx264's;
     the software command is byte-for-byte what it was.
  2. vaapi_qp is its own setting, never a reading of video_crf (the scales differ).
  3. A GPU that fails its trial encode is never a silent fallback: the conversion runs in
     software from the start, the recording says why, and a standing alert stands until
     the device passes.
  4. A died attempt that blames the hardware path switches the rest of the conversion to
     libx264 and discards the GPU-encoded parts; any other death is handled exactly as
     before, on the same encoder.
  5. Readiness answers READY "not in use" while the encoder is software - never NOTHING,
     which would flip the convert capability to "not set up" for every install.
  6. The container: the driver is installed and the entrypoint joins the device's group.
  7. Every trial reaches Readiness, whoever ran it; a settings save that turns the GPU on
     runs the trial at once and says the result; a startup with the GPU on runs it once in
     the background; one binary spelled two ways is one cache entry (dev/changelog/1127).
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
from app import db, readiness, toolchain  # noqa: E402
from app.alerts import GPU_ENCODER_UNAVAILABLE  # noqa: E402
from app.database import (Alert, Recording, RecordingEvent, CONVERSION_DONE,  # noqa: E402
                          CONVERSION_RESTARTED, CONVERSION_STARTED, DIAGNOSTICS)
from app.postprocessor import (ConversionResult, VIDEO_ENCODERS, do_postprocess,  # noqa: E402
                               names_gpu_failure, part_path, parts_signature)
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.config_sandbox import ConfigSandbox  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEVICE = '/dev/dri/renderD128'
# What ffmpeg 7.1 prints when the node cannot be opened, measured on this machine.
NO_DISPLAY = ('[AVHWDeviceContext @ 0x5590] No VA display found for device '
              '/dev/dri/renderD128.\nDevice creation failed: -22.')


def _trial(rc, tail=''):
    """A _run_gpu_trial stand-in with a fixed answer, counting its calls."""
    calls = []

    def run(ffmpeg_path, device):
        calls.append((ffmpeg_path, device))
        return rc, tail
    run.calls = calls
    return run


def _config(**pp_overrides):
    pp = dict({'enabled': True, 'format': 'mp4', 'delete_source': False,
               'reencode_mode': 'always', 'pre_output_timeout_seconds': 60,
               'auto_restart': True, 'max_restart_attempts': 3,
               'stall_seconds': 0, 'progress_interval_seconds': 5}, **pp_overrides)
    return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {
        'gather_health_data': False,
        'move_on_complete': {'enabled': False},
        'post_script': {'enabled': False},
        'post_process': pp,
    }})


class _ConversionCase(unittest.TestCase):
    def setUp(self):
        toolchain.reset_cache()
        self.t = make_test_app()
        self.rec = seed.make_recording(status='CONCATENATING', name='gpu',
                                       recorded_duration_seconds=3600.0)
        self.ts = os.path.join(self.t._tmpdir, f'rec_{self.rec.id}.ts')
        with open(self.ts, 'wb') as fh:
            fh.write(b'x' * 2048)
        self.rec.output_path = self.ts
        db.session.commit()
        self.rid = self.rec.id
        self.out = self.ts[:-3] + '.mp4'

    def tearDown(self):
        with ppmod._active_lock:
            ppmod._active_conversions.clear()
            ppmod._cancel_requested.clear()
        toolchain.reset_cache()
        self.t.cleanup()

    def _new_recording(self):
        """A second recording in the SAME app, for a case that converts twice."""
        self.rec = seed.make_recording(status='CONCATENATING', name='gpu2',
                                       recorded_duration_seconds=3600.0)
        self.ts = os.path.join(self.t._tmpdir, f'rec_{self.rec.id}.ts')
        with open(self.ts, 'wb') as fh:
            fh.write(b'x' * 2048)
        self.rec.output_path = self.ts
        db.session.commit()
        self.rid = self.rec.id
        self.out = self.ts[:-3] + '.mp4'

    def _run(self, cfg, side_effect, trial=None):
        """Convert with the encode and the assembly both stubbed. Every attempt writes the
        part file it was asked for, so a death leaves a GPU-encoded part on disk the way a
        real one does, and the join writes the output so a success completes."""
        def attempt(app, rid, cmd, part_file, **kw):
            with open(part_file, 'wb') as fh:
                fh.write(b'p' * 64)
            return side_effect(app, rid, cmd, part_file, **kw)

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
        return stub

    def _cmds(self, stub):
        return [c[0][2] for c in stub.call_args_list]

    def _events(self, event_type):
        return [e for e in RecordingEvent.query.filter_by(recording_id=self.rid).all()
                if e.event_type == event_type]

    def _open_alerts(self):
        return Alert.query.filter_by(alert_type=GPU_ENCODER_UNAVAILABLE,
                                     dismissed_at=None).all()


def _ok(*a, **k):
    return ConversionResult(True, 'success', out_time=3600.0)


class CommandShapeTests(_ConversionCase):
    def test_the_gpu_command_uploads_to_the_device_and_encodes_with_h264_vaapi(self):
        cmd = self._cmds(self._run(_config(video_encoder='vaapi'), _ok))[0]
        self.assertIn('-vaapi_device', cmd)
        self.assertEqual(cmd[cmd.index('-vaapi_device') + 1], DEVICE)
        self.assertLess(cmd.index('-vaapi_device'), cmd.index('-i'),
                        'a global option has to precede the input')
        self.assertEqual(cmd[cmd.index('-vf') + 1], 'format=nv12,hwupload')
        self.assertEqual(cmd[cmd.index('-c:v') + 1], 'h264_vaapi')
        self.assertEqual(cmd[cmd.index('-qp') + 1], '26')
        self.assertIn('-force_key_frames', cmd, 'the 2s IDRs are an encoder-independent rule')
        for flag in ('libx264', '-crf', '-preset', '-pix_fmt'):
            self.assertNotIn(flag, cmd)

    def test_decoding_stays_on_the_cpu(self):
        cmd = self._cmds(self._run(_config(video_encoder='vaapi'), _ok))[0]
        self.assertNotIn('-hwaccel', cmd)
        self.assertNotIn('-hwaccel_output_format', cmd)

    def test_the_configured_device_is_the_one_opened(self):
        cmd = self._cmds(self._run(_config(video_encoder='vaapi',
                                           vaapi_device='/dev/dri/renderD129'), _ok))[0]
        self.assertEqual(cmd[cmd.index('-vaapi_device') + 1], '/dev/dri/renderD129')

    def test_software_is_the_default_and_the_command_is_what_it_was(self):
        cmd = self._cmds(self._run(_config(), _ok))[0]
        self.assertEqual(cmd[cmd.index('-c:v') + 1], 'libx264')
        self.assertEqual(cmd[cmd.index('-crf') + 1], '20')
        self.assertEqual(cmd[cmd.index('-pix_fmt') + 1], 'yuv420p')
        for flag in ('-vaapi_device', 'hwupload', 'h264_vaapi', '-qp'):
            self.assertNotIn(flag, cmd)

    def test_software_never_runs_the_trial(self):
        trial = _trial(0)
        self._run(_config(), _ok, trial=trial)
        self.assertEqual(trial.calls, [])

    def test_a_stream_copy_never_touches_the_gpu(self):
        cmd = self._cmds(self._run(_config(video_encoder='vaapi', reencode_mode='never'),
                                   _ok))[0]
        self.assertEqual(cmd[cmd.index('-c:v') + 1], 'copy')
        self.assertNotIn('-vaapi_device', cmd)

    def test_the_two_encoders_never_share_a_parts_signature(self):
        gpu = self._cmds(self._run(_config(video_encoder='vaapi'), _ok))[0]
        self._new_recording()
        sw = self._cmds(self._run(_config(), _ok))[0]
        self.assertNotEqual(parts_signature(gpu, self.out), parts_signature(sw, self.out),
                            'parts encoded by different encoders must never be joined')

    def test_the_started_and_done_events_name_the_encoder(self):
        self._run(_config(video_encoder='vaapi'), _ok)
        self.assertIn('on the GPU', self._events(CONVERSION_STARTED)[0].detail)
        self.assertIn('h264_vaapi', self._events(CONVERSION_DONE)[0].detail)
        self._new_recording()
        self._run(_config(), _ok)
        self.assertIn('libx264', self._events(CONVERSION_STARTED)[0].detail)
        self.assertIn('libx264', self._events(CONVERSION_DONE)[0].detail)


class QualitySettingTests(_ConversionCase):
    def test_vaapi_qp_is_its_own_setting(self):
        cmd = self._cmds(self._run(_config(video_encoder='vaapi', vaapi_qp=23), _ok))[0]
        self.assertEqual(cmd[cmd.index('-qp') + 1], '23')

    def test_video_crf_never_reaches_the_gpu_encoder(self):
        """crf 20 as a qp made a file 2.3x the size at the same picture (dev/changelog/1124)."""
        cmd = self._cmds(self._run(_config(video_encoder='vaapi', video_crf=18), _ok))[0]
        self.assertEqual(cmd[cmd.index('-qp') + 1], '26')
        self.assertNotIn('-crf', cmd)
        self.assertNotIn('18', cmd)

    def test_video_crf_still_governs_the_software_fallback(self):
        cmd = self._cmds(self._run(_config(video_encoder='vaapi', video_crf=18), _ok,
                                   trial=_trial(234, NO_DISPLAY)))[0]
        self.assertEqual(cmd[cmd.index('-crf') + 1], '18')


class TrialFailureTests(_ConversionCase):
    """A GPU asked for and not working is loud on the recording, in the alerts, and in the
    log - and the conversion still completes (principle 1 over a silent principle 2)."""

    def test_a_failed_trial_converts_in_software_from_the_start(self):
        stub = self._run(_config(video_encoder='vaapi'), _ok, trial=_trial(234, NO_DISPLAY))
        cmd = self._cmds(stub)[0]
        self.assertEqual(cmd[cmd.index('-c:v') + 1], 'libx264')
        self.assertNotIn('-vaapi_device', cmd)
        self.assertEqual(db.session.get(Recording, self.rid).status, 'COMPLETED')

    def test_the_recording_says_why_and_quotes_ffmpeg(self):
        self._run(_config(video_encoder='vaapi'), _ok, trial=_trial(234, NO_DISPLAY))
        notes = [e for e in self._events(DIAGNOSTICS)
                 if 'conversion_gpu_unavailable' in (e.extra_data or '')]
        self.assertEqual(len(notes), 1)
        self.assertIn('No VA display found', notes[0].detail)
        self.assertIn(DEVICE, notes[0].detail)
        self.assertIn('libx264', notes[0].detail)
        self.assertIn('libx264', self._events(CONVERSION_STARTED)[0].detail)

    def test_a_failed_trial_raises_a_standing_alert_naming_the_device(self):
        self._run(_config(video_encoder='vaapi'), _ok, trial=_trial(234, NO_DISPLAY))
        alerts = self._open_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].source, f'{toolchain.GPU_ALERT_SOURCE_PREFIX}{DEVICE}')
        self.assertIn('No VA display found', alerts[0].body)
        self.assertIn('--device', alerts[0].body, 'the body names the usual cause')

    def test_the_alert_stands_rather_than_stacking(self):
        self._run(_config(video_encoder='vaapi'), _ok, trial=_trial(234, NO_DISPLAY))
        self._new_recording()
        self._run(_config(video_encoder='vaapi'), _ok, trial=_trial(234, NO_DISPLAY))
        self.assertEqual(len(self._open_alerts()), 1)

    def test_the_alert_clears_when_the_device_passes(self):
        self._run(_config(video_encoder='vaapi'), _ok, trial=_trial(234, NO_DISPLAY))
        self.assertEqual(len(self._open_alerts()), 1)
        self._new_recording()
        self._run(_config(video_encoder='vaapi'), _ok, trial=_trial(0))
        self.assertEqual(self._open_alerts(), [])

    def test_a_clean_gpu_conversion_raises_nothing_and_writes_no_fallback_note(self):
        self._run(_config(video_encoder='vaapi'), _ok)
        self.assertEqual(self._open_alerts(), [])
        self.assertEqual([e for e in self._events(DIAGNOSTICS)
                          if 'gpu' in (e.extra_data or '')], [])

    def test_an_unknown_encoder_value_is_software_and_says_so(self):
        stub = self._run(_config(video_encoder='cuda'), _ok)
        cmd = self._cmds(stub)[0]
        self.assertEqual(cmd[cmd.index('-c:v') + 1], 'libx264')
        notes = [e for e in self._events(DIAGNOSTICS)
                 if 'conversion_encoder_unknown' in (e.extra_data or '')]
        self.assertEqual(len(notes), 1)
        self.assertIn('cuda', notes[0].detail)


class HardwareDeathFallbackTests(_ConversionCase):
    """A death that blames the device, the upload or the encoder switches the rest of the
    conversion to software. A death that does not is the decoder on a damaged source and
    is handled exactly as before, on the same encoder."""

    def test_a_hardware_death_retries_with_libx264(self):
        outcomes = iter([ConversionResult(False, 'died', NO_DISPLAY, out_time=120.0), _ok()])
        stub = self._run(_config(video_encoder='vaapi'), lambda *a, **k: next(outcomes))
        first, second = self._cmds(stub)
        self.assertEqual(first[first.index('-c:v') + 1], 'h264_vaapi')
        self.assertEqual(second[second.index('-c:v') + 1], 'libx264')
        self.assertNotIn('-vaapi_device', second)
        self.assertEqual(db.session.get(Recording, self.rid).status, 'COMPLETED')

    def test_the_switch_is_announced_and_the_done_event_names_the_software_encoder(self):
        outcomes = iter([ConversionResult(False, 'died', NO_DISPLAY, out_time=120.0), _ok()])
        self._run(_config(video_encoder='vaapi'), lambda *a, **k: next(outcomes))
        said = [e for e in self._events(CONVERSION_RESTARTED)
                if 'GPU encoder failed' in (e.detail or '')]
        self.assertEqual(len(said), 1)
        self.assertIn('2m 0s', said[0].detail)
        self.assertIn('No VA display found', said[0].detail)
        self.assertIn('conversion_gpu_fallback', said[0].extra_data)
        self.assertIn('libx264', self._events(CONVERSION_DONE)[0].detail)

    def test_the_gpu_encoded_parts_are_discarded(self):
        seen = []
        outcomes = iter([ConversionResult(False, 'died', NO_DISPLAY, out_time=120.0), _ok()])

        def attempt(app, rid, cmd, part_file, **kw):
            seen.append((part_file, os.path.exists(part_path(self.out, 1))))
            return next(outcomes)
        self._run(_config(video_encoder='vaapi'), attempt)
        # The second attempt writes part 1 again, from the start - and the GPU's part 1 was
        # already gone when it began (the stub wrote it, the fallback removed it).
        self.assertEqual([p for p, _ in seen], [part_path(self.out, 1)] * 2)
        self.assertEqual([present for _, present in seen], [True, True],
                         'each attempt finds its own freshly-written part')
        said = self._events(CONVERSION_RESTARTED)[0].detail
        self.assertIn('1 part(s) the GPU already encoded', said)
        self.assertIn('discarded', said)

    def test_the_fallback_costs_one_restart_attempt(self):
        outcomes = iter([ConversionResult(False, 'died', NO_DISPLAY, out_time=120.0), _ok()])
        self._run(_config(video_encoder='vaapi'), lambda *a, **k: next(outcomes))
        self.assertEqual(db.session.get(Recording, self.rid).conversion_attempts, 1)

    def test_a_death_that_does_not_blame_the_hardware_keeps_the_gpu(self):
        outcomes = iter([
            ConversionResult(False, 'died', 'Error submitting packet to decoder', out_time=120.0),
            _ok()])
        stub = self._run(_config(video_encoder='vaapi'), lambda *a, **k: next(outcomes))
        second = self._cmds(stub)[1]
        self.assertEqual(second[second.index('-c:v') + 1], 'h264_vaapi')
        self.assertEqual([e for e in self._events(CONVERSION_RESTARTED)
                          if 'GPU encoder failed' in (e.detail or '')], [])

    def test_the_audio_copy_fallback_still_fires_on_the_gpu(self):
        """Two deaths at one offset are the source's fault, not the device's."""
        outcomes = iter([
            ConversionResult(False, 'died', 'boom', out_time=500.0),
            ConversionResult(False, 'died', 'boom', out_time=500.0),
            _ok()])
        stub = self._run(_config(video_encoder='vaapi'), lambda *a, **k: next(outcomes))
        last = self._cmds(stub)[-1]
        self.assertEqual(last[last.index('-c:a') + 1], 'copy')
        self.assertEqual(last[last.index('-c:v') + 1], 'h264_vaapi')

    def test_a_stall_is_not_read_as_a_hardware_death(self):
        outcomes = iter([ConversionResult(False, 'stalled', 'No conversion progress for 300s '
                                          'vaapi', out_time=120.0), _ok()])
        stub = self._run(_config(video_encoder='vaapi'), lambda *a, **k: next(outcomes))
        second = self._cmds(stub)[1]
        self.assertEqual(second[second.index('-c:v') + 1], 'h264_vaapi')


class GpuFailureMarkerTests(unittest.TestCase):
    def test_the_measured_no_display_message_is_recognized(self):
        self.assertTrue(names_gpu_failure(NO_DISPLAY))

    def test_each_component_of_the_hardware_path_is_recognized(self):
        for tail in ('[Parsed_hwupload_1 @ 0x1] Failed to upload frame',
                     '[h264_vaapi @ 0x1] Failed to create encode pipeline',
                     '[AVHWFramesContext @ 0x1] Failed to allocate surface'):
            self.assertTrue(names_gpu_failure(tail), tail)

    def test_a_decoder_error_is_not(self):
        self.assertFalse(names_gpu_failure('[h264 @ 0x1] Error submitting packet to decoder'))
        self.assertFalse(names_gpu_failure(''))
        self.assertFalse(names_gpu_failure(None))


class TrialCacheTests(unittest.TestCase):
    """A pass is remembered; a failure is asked again, so a device that comes back is picked
    up by the next conversion with no restart."""

    def setUp(self):
        toolchain.reset_cache()

    def tearDown(self):
        toolchain.reset_cache()

    def test_a_pass_is_served_from_cache(self):
        trial = _trial(0)
        with mock.patch.object(toolchain, '_run_gpu_trial', trial):
            first = toolchain.check_gpu_encoder('ffmpeg', DEVICE)
            second = toolchain.check_gpu_encoder('ffmpeg', DEVICE)
        self.assertTrue(first.ok and second.ok)
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(len(trial.calls), 1)

    def test_a_failure_is_never_cached(self):
        trial = _trial(234, NO_DISPLAY)
        with mock.patch.object(toolchain, '_run_gpu_trial', trial):
            for _ in range(3):
                got = toolchain.check_gpu_encoder('ffmpeg', DEVICE)
        self.assertFalse(got.ok)
        self.assertEqual(got.error, NO_DISPLAY)
        self.assertEqual(len(trial.calls), 3)

    def test_the_cache_is_keyed_on_the_device_and_the_binary(self):
        trial = _trial(0)
        with mock.patch.object(toolchain, '_run_gpu_trial', trial):
            toolchain.check_gpu_encoder('ffmpeg', DEVICE)
            toolchain.check_gpu_encoder('ffmpeg', '/dev/dri/renderD129')
            toolchain.check_gpu_encoder('/opt/ffmpeg', DEVICE)
        self.assertEqual(len(trial.calls), 3)

    def test_reset_cache_forgets_a_pass(self):
        trial = _trial(0)
        with mock.patch.object(toolchain, '_run_gpu_trial', trial):
            toolchain.check_gpu_encoder('ffmpeg', DEVICE)
            toolchain.reset_cache()
            toolchain.check_gpu_encoder('ffmpeg', DEVICE)
        self.assertEqual(len(trial.calls), 2)

    def test_the_trial_is_the_conversion_shape_on_a_synthetic_clip(self):
        cmd = toolchain.gpu_trial_cmd('/usr/bin/ffmpeg', DEVICE)
        self.assertEqual(cmd[0], '/usr/bin/ffmpeg')
        self.assertEqual(cmd[cmd.index('-vaapi_device') + 1], DEVICE)
        self.assertEqual(cmd[cmd.index('-vf') + 1], 'format=nv12,hwupload')
        self.assertEqual(cmd[cmd.index('-c:v') + 1], 'h264_vaapi')
        self.assertIn('-nostdin', cmd)
        self.assertEqual(cmd[-2:], ['null', '-'], 'nothing is written to disk')
        self.assertNotIn('http', ' '.join(cmd))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
    def test_the_real_trial_fails_honestly_on_a_machine_with_no_render_node(self):
        """Not a stub: the real ffmpeg against a node that is not there. The machine
        running the suite has no GPU, so a pass here would itself be the bug."""
        rc, tail = toolchain._run_gpu_trial(shutil.which('ffmpeg'), '/dev/dri/no-such-node')
        self.assertNotEqual(rc, 0)
        self.assertTrue(tail, 'ffmpeg says why, and that text is what the alert quotes')


class ReadinessCheckTests(unittest.TestCase):
    def setUp(self):
        toolchain.reset_cache()

    def tearDown(self):
        toolchain.reset_cache()

    def _ctx(self, encoder, found=True):
        cfg = cfgmod._deep_merge(cfgmod.load_config(), {'recording': {'post_process': {
            'video_encoder': encoder}}})
        return SimpleNamespace(cfg=cfg, tools={'ffmpeg': {'found': found, 'path': '/x/ffmpeg'}})

    def test_software_is_ready_and_says_not_in_use_never_nothing(self):
        got = readiness._check_gpu_encoder(self._ctx('software'))
        self.assertEqual(got.status, readiness.READY)
        self.assertIn('Not in use', got.found)

    def test_software_never_spawns(self):
        trial = _trial(0)
        with mock.patch.object(toolchain, '_run_gpu_trial', trial):
            readiness._check_gpu_encoder(self._ctx('software'))
        self.assertEqual(trial.calls, [])

    def test_a_passing_device_is_ready_and_named(self):
        with mock.patch.object(toolchain, '_run_gpu_trial', _trial(0)):
            got = readiness._check_gpu_encoder(self._ctx('vaapi'))
        self.assertEqual(got.status, readiness.READY)
        self.assertIn(DEVICE, got.found)

    def test_a_failing_device_needs_attention_not_a_problem(self):
        """A re-encode still completes on the CPU, so this degrades conversion rather
        than blocking it."""
        with mock.patch.object(toolchain, '_run_gpu_trial', _trial(234, NO_DISPLAY)):
            got = readiness._check_gpu_encoder(self._ctx('vaapi'))
        self.assertEqual(got.status, readiness.ATTENTION)
        self.assertIn('No VA display found', got.found)
        self.assertIn('libx264', got.found)

    def test_no_ffmpeg_is_nothing_to_test(self):
        got = readiness._check_gpu_encoder(self._ctx('vaapi', found=False))
        self.assertEqual(got.status, readiness.NOTHING)

    def test_the_check_stands_behind_convert_and_is_on_demand(self):
        check = readiness.CHECKS_BY_ID['gpu_encoder']
        self.assertEqual(check.cost, readiness.ON_DEMAND)
        convert = next(c for c in readiness.CAPABILITIES if c.id == 'convert')
        self.assertIn('gpu_encoder', convert.needs)


class SettingsSaveTests(ConfigSandbox):
    def setUp(self):
        super().setUp()
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client

    def tearDown(self):
        self.t.cleanup()

    def _post(self, path, value):
        return self.client.post('/api/settings/field', json={'path': path, 'value': value})

    def test_the_encoder_is_whitelisted(self):
        self.assertEqual(self._post('recording.post_process.video_encoder', 'cuda').status_code,
                         400)
        for value in VIDEO_ENCODERS:
            self.assertEqual(self._post('recording.post_process.video_encoder', value)
                             .status_code, 200)
        self.assertEqual(cfgmod.load_config()['recording']['post_process']['video_encoder'],
                         'vaapi')

    def test_vaapi_qp_is_clamped_like_the_crf(self):
        self._post('recording.post_process.vaapi_qp', 999)
        self.assertEqual(cfgmod.load_config()['recording']['post_process']['vaapi_qp'], 51)
        self._post('recording.post_process.vaapi_qp', -1)
        self.assertEqual(cfgmod.load_config()['recording']['post_process']['vaapi_qp'], 0)

    def test_the_defaults_are_software_and_qp_26(self):
        pp = cfgmod.load_config()['recording']['post_process']
        self.assertEqual(pp['video_encoder'], 'software')
        self.assertEqual(pp['vaapi_qp'], 26)
        self.assertEqual(pp['vaapi_device'], DEVICE)


class ContainerTests(unittest.TestCase):
    """The image carries the driver and the entrypoint joins the device's group. The
    group-join logic is driven for real, in bash, with stand-ins for the commands that
    need root or a device on PATH."""

    HELPER = os.path.join(ROOT, 'docker', 'render-groups.sh')

    def test_the_dockerfile_installs_the_driver_from_non_free(self):
        with open(os.path.join(ROOT, 'Dockerfile')) as fh:
            text = fh.read()
        self.assertIn('intel-media-va-driver-non-free', text)
        self.assertIn('Components: main non-free', text)

    def test_the_entrypoint_joins_the_render_groups_after_the_uid_remap(self):
        with open(os.path.join(ROOT, 'docker', 'entrypoint.sh')) as fh:
            text = fh.read()
        self.assertIn('render-groups.sh', text)
        self.assertLess(text.index('usermod -o -u'), text.index('join_render_groups'),
                        'the supplementary group has to land on the final account')
        self.assertTrue(os.access(self.HELPER, os.X_OK))

    def _run_helper(self, nodes, held='100', known_groups=None):
        """nodes: {name: gid}. Returns the stand-ins' call log and the helper's stdout."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        dri = os.path.join(tmp, 'dri')
        os.makedirs(dri)
        bindir = os.path.join(tmp, 'bin')
        os.makedirs(bindir)
        calls = os.path.join(tmp, 'calls')
        gids = ' '.join(f'{name}:{gid}' for name, gid in nodes.items())
        groups = ' '.join(f'{gid}:{name}' for gid, name in (known_groups or {}).items())
        stubs = {
            'stat': f'#!/bin/bash\nfor pair in {gids}; do [ "${{pair%%:*}}" = "$(basename "$3")" ] '
                    f'&& echo "${{pair##*:}}"; done; true',
            'id': f'#!/bin/bash\necho "{held}"',
            'getent': f'#!/bin/bash\nfor pair in {groups}; do [ "${{pair%%:*}}" = "$2" ] '
                      f'&& echo "${{pair##*:}}:x:$2:"; done; false',
            'groupadd': f'#!/bin/bash\necho "groupadd $*" >> {calls}',
            'usermod': f'#!/bin/bash\necho "usermod $*" >> {calls}',
        }
        for name, body in stubs.items():
            path = os.path.join(bindir, name)
            with open(path, 'w') as fh:
                fh.write(body + '\n')
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        for name in nodes:
            open(os.path.join(dri, name), 'w').close()
        script = (f'set -euo pipefail; log() {{ echo "$*"; }}; . "{self.HELPER}"; '
                  f'join_render_groups "{dri}" channelbin')
        proc = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                              env={**os.environ, 'PATH': bindir + os.pathsep + os.environ['PATH']},
                              timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        made = open(calls).read().splitlines() if os.path.exists(calls) else []
        return made, proc.stdout

    def test_a_node_owned_by_an_unknown_group_gets_a_group_and_the_user_joins_it(self):
        made, out = self._run_helper({'renderD128': 18})
        self.assertEqual(made, ['groupadd -o -g 18 hostgpu18', 'usermod -aG hostgpu18 channelbin'])
        self.assertIn('renderD128', out)
        self.assertIn('18', out)

    def test_an_existing_group_of_that_gid_is_reused(self):
        made, _ = self._run_helper({'renderD128': 44}, known_groups={44: 'video'})
        self.assertEqual(made, ['usermod -aG video channelbin'])

    def test_a_group_already_held_is_skipped(self):
        made, _ = self._run_helper({'renderD128': 100}, held='100')
        self.assertEqual(made, [])

    def test_the_root_group_is_never_joined(self):
        made, out = self._run_helper({'card0': 0})
        self.assertEqual(made, [])
        self.assertIn('group 0', out)

    def test_two_nodes_in_one_group_join_it_once(self):
        made, _ = self._run_helper({'card0': 44, 'renderD128': 44})
        self.assertEqual(made, ['groupadd -o -g 44 hostgpu44', 'usermod -aG hostgpu44 channelbin'])

    def test_no_dri_directory_is_silent(self):
        proc = subprocess.run(
            ['bash', '-c', f'set -euo pipefail; log() {{ echo "$*"; }}; . "{self.HELPER}"; '
                           f'join_render_groups /no/such/dri channelbin'],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, '')


if __name__ == '__main__':
    unittest.main()


class TrialReachesReadinessTests(_ConversionCase):
    """dev/changelog/1127: a conversion's own trial used to leave Readiness at "Not run yet"
    while the alert and the recording said the opposite."""

    def setUp(self):
        super().setUp()
        readiness.reset_for_tests()

    def test_a_conversions_passing_trial_is_the_readiness_answer(self):
        self._run(_config(video_encoder='vaapi'), _ok)
        stored = readiness._ondemand.get('gpu_encoder')
        self.assertIsNotNone(stored, 'the conversion trial never reached Readiness')
        self.assertEqual(stored['status'], readiness.READY)
        self.assertIn(DEVICE, stored['found'])

    def test_a_conversions_failing_trial_is_the_readiness_answer(self):
        self._run(_config(video_encoder='vaapi'), _ok, trial=_trial(234, NO_DISPLAY))
        stored = readiness._ondemand.get('gpu_encoder')
        self.assertIsNotNone(stored, 'the conversion trial never reached Readiness')
        self.assertEqual(stored['status'], readiness.ATTENTION)
        self.assertIn('No VA display found', stored['found'])


class TrialCacheKeyTests(unittest.TestCase):
    def setUp(self):
        toolchain.reset_cache()
        readiness.reset_for_tests()

    def tearDown(self):
        toolchain.reset_cache()
        readiness.reset_for_tests()

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
    def test_one_binary_spelled_two_ways_is_one_cache_entry(self):
        """A real Unraid log (dev/changelog/1127): the trial ran as /usr/bin/ffmpeg from Readiness
        and again as ffmpeg from the conversion, in one process."""
        trial = _trial(0)
        with mock.patch.object(toolchain, '_run_gpu_trial', trial):
            toolchain.check_gpu_encoder('ffmpeg', DEVICE)
            second = toolchain.check_gpu_encoder(shutil.which('ffmpeg'), DEVICE)
        self.assertEqual(len(trial.calls), 1)
        self.assertTrue(second.cached)


class SaveRunsTheTrialTests(ConfigSandbox):
    """Turning the GPU on proves it at once, rather than at the first re-encode days later."""

    def setUp(self):
        super().setUp()
        toolchain.reset_cache()
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        readiness.reset_for_tests()
        listing = mock.patch.object(toolchain, '_run_listing', return_value=None)
        listing.start()
        self.addCleanup(listing.stop)

    def tearDown(self):
        toolchain.reset_cache()
        self.t.cleanup()

    def _post(self, path, value, trial):
        with mock.patch.object(toolchain, '_run_gpu_trial', trial):
            return self.client.post('/api/settings/field', json={'path': path, 'value': value})

    def test_turning_the_gpu_on_runs_the_trial_and_reports_a_pass(self):
        trial = _trial(0)
        resp = self._post('recording.post_process.video_encoder', 'vaapi', trial)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(trial.calls), 1)
        body = resp.get_json()
        self.assertTrue(body['gpu_trial']['ok'])
        self.assertIn(DEVICE, body['gpu_trial']['message'])
        self.assertEqual(readiness._ondemand['gpu_encoder']['status'], readiness.READY)
        self.assertIn('ffmpeg_build', readiness._ondemand,
                      'the build listing is answered on the same save')

    def test_a_failing_trial_is_reported_with_ffmpegs_reason(self):
        resp = self._post('recording.post_process.video_encoder', 'vaapi',
                          _trial(234, NO_DISPLAY))
        body = resp.get_json()
        self.assertFalse(body['gpu_trial']['ok'])
        self.assertIn('No VA display found', body['gpu_trial']['message'])
        self.assertEqual(readiness._ondemand['gpu_encoder']['status'], readiness.ATTENTION)

    def test_moving_the_device_while_on_re_runs_it(self):
        trial = _trial(0)
        self._post('recording.post_process.video_encoder', 'vaapi', trial)
        self._post('recording.post_process.vaapi_device', '/dev/dri/renderD129', trial)
        self.assertEqual(trial.calls[-1][1], '/dev/dri/renderD129')
        self.assertEqual(len(trial.calls), 2)

    def test_software_spawns_nothing_and_readiness_says_not_in_use(self):
        trial = _trial(0)
        self._post('recording.post_process.video_encoder', 'vaapi', trial)
        resp = self._post('recording.post_process.video_encoder', 'software', trial)
        self.assertEqual(len(trial.calls), 1, 'only the vaapi save ran a trial')
        self.assertNotIn('gpu_trial', resp.get_json())
        self.assertIn('Not in use', readiness._ondemand['gpu_encoder']['found'])

    def test_an_unrelated_save_runs_nothing(self):
        trial = _trial(0)
        resp = self._post('recording.post_process.vaapi_qp', 22, trial)
        self.assertEqual(trial.calls, [])
        self.assertNotIn('gpu_trial', resp.get_json())


class StartupTrialTests(unittest.TestCase):
    def setUp(self):
        toolchain.reset_cache()
        self.t = make_test_app()
        readiness.reset_for_tests()

    def tearDown(self):
        toolchain.reset_cache()
        self.t.cleanup()

    def _cfg(self, encoder):
        return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {'post_process': {
            'video_encoder': encoder}}})

    def test_software_starts_no_thread_and_spawns_nothing(self):
        trial = _trial(0)
        with mock.patch.object(toolchain, '_run_gpu_trial', trial):
            got = readiness.start_gpu_trial_at_startup(self.t.app, self._cfg('software'))
        self.assertIsNone(got)
        self.assertEqual(trial.calls, [])

    def test_vaapi_answers_readiness_from_a_background_thread(self):
        trial = _trial(0)
        with mock.patch.object(toolchain, '_run_gpu_trial', trial), \
             mock.patch.object(toolchain, '_run_listing', return_value=None):
            thread = readiness.start_gpu_trial_at_startup(self.t.app, self._cfg('vaapi'))
            self.assertIsNotNone(thread)
            thread.join(10)
        self.assertEqual(len(trial.calls), 1)
        self.assertEqual(readiness._ondemand['gpu_encoder']['status'], readiness.READY)

    def test_create_app_calls_it_only_with_the_live_scheduler(self):
        """make_test_app() builds with start_scheduler=False, so a test app never trials."""
        import app as app_pkg
        with mock.patch.object(readiness, 'start_gpu_trial_at_startup') as hook:
            extra = make_test_app()
            extra.cleanup()
        hook.assert_not_called()
        with open(os.path.join(os.path.dirname(app_pkg.__file__), '__init__.py')) as fh:
            text = fh.read()
        block = text[text.index('    if start_scheduler:\n        from .scheduler'):]
        self.assertIn('start_gpu_trial_at_startup(app, cfg)', block.split('return app')[0])
