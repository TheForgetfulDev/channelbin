"""A dead conversion is judged on the error lines of its WHOLE stderr, and leaves its evidence
on the recording (dev/changelog/1197).

dev/docs/BUGS.md 2026-10-15 @ 03:09:45 PM: recording 16's GPU conversion died twice, and both
times its last 20 stderr lines - all the classifier read - were audio warnings from ffmpeg's
run-on after the video thread had died. The GPU failure above them was never seen, the deaths
were read as a damaged source, and the conversion resumed on GPU-encoded parts instead of
falling back to the CPU. Reading the whole spool is only safe on error-level lines: the banner
names `h264_vaapi` on every GPU run. And "Error reinitializing filters" blamed the GPU from the
AUDIO graph too, which is recording 4's corrupt-audio death on any encoder.

Guarded:
  1. A GPU error line followed by more than a tail's worth of warnings is still a GPU death,
     through the real supervisor and a real child writing the spool.
  2. An audio-graph reinit failure on a GPU encode is not a GPU death.
  3. Banner and warning lines never count, however much they name the device.
  4. Every CONVERSION_RESTARTED, and a give-up's CONVERSION_FAILED, carries the dead
     attempt's evidence in extra_data: reason, how far ffmpeg got, the kept length, the tail,
     the last error lines and the GPU line.
"""
import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.postprocessor as ppmod  # noqa: E402
from app.database import CONVERSION_FAILED, CONVERSION_RESTARTED  # noqa: E402
from app.postprocessor import (ConversionResult, names_gpu_failure,  # noqa: E402
                               read_conversion_errors)
from app.proc_utils import STDERR_LEVEL_ARGS, iter_stderr_error_lines  # noqa: E402
from tests.test_gpu_conversion import _ConversionCase, _config, _ok  # noqa: E402

# Level-tagged lines as ffmpeg 7.1 writes them under -loglevel level+info. The death is
# 0.23.0's command at a 720p switch, measured in the GPU lab; the whole spools are in
# dev/changelog/evidence/1197-gpu-death-classify/.
BANNER = (
    '[info] ffmpeg version 7.1.5-0+deb13u1 Copyright (c) 2000-2026 the FFmpeg developers\n'
    '[info] Stream mapping:\n'
    '[info]   Stream #0:0 -> #0:0 (h264 (native) -> h264 (h264_vaapi))\n'
    '[info]   Stream #0:1 -> #0:1 (aac (native) -> aac (native))\n'
    '[info] Output #0, mp4, to \'/dvr/rec.part001.mp4\':\n'
    '[info]   Stream #0:0: Video: h264 (High) (avc1 / 0x31637661), vaapi(tv, bt709, '
    'progressive), 1920x1080 [SAR 1:1 DAR 16:9], q=2-31, 59.94 fps, 60k tbn\n'
    '[info]         encoder         : Lavc61.19.101 h264_vaapi\n'
    '[h264_vaapi @ 0x5570] [warning] Driver does not support some wanted packed headers '
    '(wanted 0xd, found 0x1).\n')
GPU_LINE = ("[error] Impossible to convert between the formats supported by the filter "
            "'Parsed_hwupload_1' and the filter 'auto_scale_1'")
GPU_DEATH = (
    '[info] frame= 3555 fps=547 q=-0.0 size=   45824KiB time=00:00:59.29 bitrate=6331.2kbits/s'
    '\r[vf#0:0 @ 0x55706ca1f500] [info] Reconfiguring filter graph because video parameters '
    'changed to yuv420p(tv, bt709), 1280x720\n'
    + GPU_LINE + '\n'
    '[vf#0:0 @ 0x55706ca1f500] [error] Error reinitializing filters!\n'
    '[vf#0:0 @ 0x55706ca1f500] [error] Task finished with error code: -38 (Function not '
    'implemented)\n')
# Recording 16's tail: the audio run-on after the video died.
AUDIO_FLOOD = ''.join(
    '[aac @ 0x5570cd43140] [warning] Queue input is backward in time\n'
    '[mpegts @ 0x5570c9a1200] [warning] DTS discontinuity in stream 1: packet 4 with DTS '
    f'{1000000 + i}, packet 5 with DTS {1200000 + i}\n' for i in range(20))
RECORDING_16_SPOOL = BANNER + GPU_DEATH + AUDIO_FLOOD
AUDIO_GRAPH_DEATH = BANNER + (
    "[graph_-1_in_0:1 @ 0x7c8e40001d40] [error] Changing audio frame properties on the fly is "
    "not supported.\n"
    '[af#0:1 @ 0x59ff3f49dac0] [error] Error reinitializing filters!\n'
    '[af#0:1 @ 0x59ff3f49dac0] [error] Task finished with error code: -22 (Invalid argument)\n')


def _spool(case, text):
    path = os.path.join(case.tmp, 'spool.log')
    with open(path, 'w') as fh:
        fh.write(text)
    return path


class GpuErrorLineTests(unittest.TestCase):
    def test_a_video_graph_reinit_failure_is_the_gpu(self):
        self.assertTrue(names_gpu_failure(
            '[vf#0:0 @ 0x1] [error] Error reinitializing filters!'))

    def test_an_audio_graph_reinit_failure_is_the_source(self):
        """Recording 4's corrupt audio frame (dev/changelog/799) prints these words from the
        audio graph on any encoder; on a GPU encode it used to raise the GPU alert."""
        self.assertFalse(names_gpu_failure(
            '[af#0:1 @ 0x1] [error] Error reinitializing filters!'))

    def test_the_device_markers_still_name_it(self):
        for line in ('[AVHWDeviceContext @ 0x1] [error] No VA display found for device '
                     '/dev/dri/renderD128.', GPU_LINE,
                     '[h264_vaapi @ 0x1] [error] Failed to create encode pipeline'):
            self.assertTrue(names_gpu_failure(line), line)


class SpoolReaderTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def test_the_gpu_line_is_found_above_a_tail_of_warnings(self):
        gpu, last = read_conversion_errors(_spool(self, RECORDING_16_SPOOL))
        self.assertEqual(gpu[0], GPU_LINE)
        self.assertEqual(last[-1], '[vf#0:0 @ 0x55706ca1f500] [error] Task finished with error '
                                   'code: -38 (Function not implemented)')

    def test_banner_and_warnings_never_count(self):
        """The banner names h264_vaapi on every GPU run (dev/changelog/1196), and some Intel
        drivers warn under [h264_vaapi @ ...] on every encode."""
        self.assertEqual(read_conversion_errors(_spool(self, BANNER + AUDIO_FLOOD)), ([], []))

    def test_an_audio_graph_death_names_no_gpu_line(self):
        gpu, last = read_conversion_errors(_spool(self, AUDIO_GRAPH_DEATH))
        self.assertEqual(gpu, [])
        self.assertEqual(len(last), 3)

    def test_untagged_output_has_no_error_lines(self):
        untagged = RECORDING_16_SPOOL.replace('[error] ', '').replace('[warning] ', '')
        self.assertEqual(list(iter_stderr_error_lines(_spool(self, untagged))), [])

    def test_the_read_is_bounded_to_the_end_of_the_spool(self):
        early = '[x @ 0x1] [error] early\n'
        path = _spool(self, early + 'y' * 5000 + '\n[z @ 0x1] [error] late\n')
        self.assertEqual(list(iter_stderr_error_lines(path, max_bytes=100)),
                         ['[z @ 0x1] [error] late'])

    def test_a_missing_spool_is_nothing_never_a_raise(self):
        self.assertEqual(read_conversion_errors(os.path.join(self.tmp, 'gone.log')), ([], []))
        self.assertEqual(list(iter_stderr_error_lines(None)), [])


class WholeSpoolClassificationTests(_ConversionCase):
    """The real run_conversion_supervised and supervise_ffmpeg, with a child that writes a
    recorded spool and dies - so the flag, the spool and the reader are all the real ones."""

    def _fake_ffmpeg(self, spool_text):
        fixture = os.path.join(self.t._tmpdir, 'fixture.stderr')
        with open(fixture, 'w') as fh:
            fh.write(spool_text)
        self.args_file = os.path.join(self.t._tmpdir, 'fake-args')
        script = os.path.join(self.t._tmpdir, 'fake-ffmpeg')
        with open(script, 'w') as fh:
            fh.write(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{self.args_file}"\n'
                     f'cat "{fixture}" >&2\nexit 218\n')
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
        return script

    def _convert(self, spool_text):
        script = self._fake_ffmpeg(spool_text)
        real = ppmod.run_conversion_supervised
        calls = []

        def attempt(app, rid, cmd, part_file, **kw):
            calls.append(cmd)
            if len(calls) == 1:
                kw['interval'] = 0.05
                return real(app, rid, [script] + cmd[1:], part_file, **kw)
            return _ok()

        stub = self._run(_config(video_encoder='vaapi'), attempt)
        return self._cmds(stub)

    def test_a_gpu_death_under_a_tail_of_warnings_falls_back_to_the_cpu(self):
        first, second = self._convert(RECORDING_16_SPOOL)
        self.assertEqual(first[first.index('-c:v') + 1], 'h264_vaapi')
        self.assertEqual(second[second.index('-c:v') + 1], 'libx264',
                         'the GPU death 20+ lines up was missed')
        restarted = self._events(CONVERSION_RESTARTED)
        self.assertEqual(len(restarted), 1)
        self.assertIn(GPU_LINE, restarted[0].detail)
        extra = json.loads(restarted[0].extra_data)
        self.assertEqual(extra['kind'], 'conversion_gpu_fallback')
        self.assertEqual(extra['gpu_error'], GPU_LINE)
        self.assertIn('Queue input is backward in time', extra['error_tail'])
        self.assertTrue(extra['error_lines'])

    def test_the_conversion_asks_ffmpeg_for_level_tags(self):
        self._convert(RECORDING_16_SPOOL)
        with open(self.args_file) as fh:
            args = fh.read().split('\n')
        i = args.index(STDERR_LEVEL_ARGS[0])
        self.assertEqual(args[i:i + 2], STDERR_LEVEL_ARGS)

    def test_an_audio_graph_death_on_the_gpu_keeps_the_gpu(self):
        first, second = self._convert(AUDIO_GRAPH_DEATH)
        self.assertEqual(second[second.index('-c:v') + 1], 'h264_vaapi',
                         'source damage in the audio graph was blamed on the GPU')
        self.assertEqual(self._open_alerts(), [])


class RestartEvidenceTests(_ConversionCase):
    """dev/changelog/1193: a death that did not blame the GPU left nothing on the event, so
    a support bundle lost the stderr, the progress time and the kept length at rotation."""

    def _death(self, out_time):
        return ConversionResult(False, 'died', '[aac @ 0x1] [warning] Queue input is backward',
                                out_time=out_time,
                                error_lines=['[h264 @ 0x1] [error] Invalid NAL unit size'])

    def _convert(self, outcomes, kept=100.0, **cfg):
        it = iter(outcomes)
        self._run(_config(**cfg), lambda *a, **k: next(it), kept=kept)

    def test_a_restart_carries_the_dead_attempts_evidence(self):
        self._convert([self._death(120.0), _ok()])
        restarted = self._events(CONVERSION_RESTARTED)
        self.assertEqual(len(restarted), 1)
        self.assertIn('2m 0s into the source', restarted[0].detail)
        extra = json.loads(restarted[0].extra_data)
        self.assertEqual(extra['kind'], 'conversion_restart')
        self.assertEqual(extra['reason'], 'died')
        self.assertEqual(extra['out_time_seconds'], 120.0)
        self.assertEqual(extra['kept_seconds'], 100.0)
        self.assertIn('Queue input', extra['error_tail'])
        self.assertEqual(extra['error_lines'], ['[h264 @ 0x1] [error] Invalid NAL unit size'])
        self.assertNotIn('gpu_error', extra)

    def test_a_give_up_carries_the_last_deaths_evidence(self):
        self._convert([self._death(120.0), self._death(900.0)], max_restart_attempts=1)
        failed = self._events(CONVERSION_FAILED)
        self.assertEqual(len(failed), 1)
        extra = json.loads(failed[0].extra_data)
        self.assertEqual(extra['kind'], 'conversion_gave_up')
        self.assertEqual(extra['out_time_seconds'], 900.0)

    def test_the_audio_copy_restart_carries_it_too(self):
        self._convert([self._death(500.0), self._death(500.0), _ok()])
        said = [json.loads(e.extra_data) for e in self._events(CONVERSION_RESTARTED)]
        self.assertEqual([e['kind'] for e in said],
                         ['conversion_restart', 'conversion_audio_copy_restart'])
        self.assertEqual(said[1]['out_time_seconds'], 500.0)


if __name__ == '__main__':
    unittest.main()
