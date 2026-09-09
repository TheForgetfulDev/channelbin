"""mp4 conversion compression tuning (dev/changelog/549).

video_crf and audio_bitrate_kbps used to be hardcoded ('-crf 20', '-b:a 192k') in every mp4
ffmpeg invocation app/postprocessor.py builds. These are now config.yaml-configurable, with
the CRF only reaching the re-encode branch and the audio bitrate reaching every mp4 branch.

Fixtures are synthesized locally with ffmpeg (same minimal recipe as
tests/test_recording_diagnostics.py) - no network. The conversion runner is stubbed via
run_conversion_supervised so no real conversion ffmpeg runs; the built cmd list is inspected
from the stub's call_args instead.
"""
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
from tests.support.app import make_test_app  # noqa: E402
from tests.support.config_sandbox import ConfigSandbox  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.postprocessor import ConversionResult, do_postprocess  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=120)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class Mp4CompressionSettingsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_crffix')
        os.makedirs(cls._dir, exist_ok=True)
        cls.clean_src = os.path.join(cls._dir, 'clean.ts')
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=10', '-t', '5',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0',
                '-pix_fmt', 'yuv420p', cls.clean_src)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def setUp(self):
        self.t = make_test_app()
        self.dvr_dir = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr_dir, exist_ok=True)

        acc = seed.make_account()
        channel = seed.make_channel(acc, name='CRF Feed')
        now = datetime.utcnow()
        rec = seed.make_recording(
            status='CONCATENATING', name='crf-test', channel_id=channel.id,
            start_time=now - timedelta(seconds=5), stop_time=now)
        self.rid = rec.id
        db.session.commit()

    def tearDown(self):
        with ppmod._active_lock:
            ppmod._active_conversions.clear()
            ppmod._cancel_requested.clear()
        self.t.cleanup()

    # ── helpers ───────────────────────────────────────────────────────────────
    def _ts(self):
        dest = os.path.join(self.dvr_dir, f'rec_{self.rid}.ts')
        shutil.copy2(self.clean_src, dest)
        return dest

    def _config(self, *, reencode_mode='always', post_process_overrides=None):
        pp = {
            'enabled': True, 'format': 'mp4', 'delete_source': False,
            'reencode_mode': reencode_mode, 'pre_output_timeout_seconds': 60,
            'auto_restart': False, 'max_restart_attempts': 0,
            'stall_seconds': 0, 'progress_interval_seconds': 5,
        }
        pp.update(post_process_overrides or {})
        return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {
            'dvr_output_dir': self.dvr_dir,
            'gather_health_data': False,
            'move_on_complete': {'enabled': False},
            'post_script': {'enabled': False},
            'post_process': pp,
        }})

    def _run(self, cfg):
        stub = mock.Mock(return_value=ConversionResult(True))
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', stub):
            do_postprocess(self.t.app, self.rid, self._ts())
        return stub

    def _cmd(self, stub):
        return stub.call_args.args[2]

    # ── the tests ─────────────────────────────────────────────────────────────
    def test_default_crf_and_audio_bitrate_on_reencode_path(self):
        cmd = self._cmd(self._run(self._config(reencode_mode='always')))
        self.assertIn('libx264', cmd)
        crf_idx = cmd.index('-crf')
        self.assertEqual(cmd[crf_idx + 1], '20')
        ba_idx = cmd.index('-b:a')
        self.assertEqual(cmd[ba_idx + 1], '192k')

    def test_custom_crf_and_audio_bitrate_on_reencode_path(self):
        cmd = self._cmd(self._run(self._config(
            reencode_mode='always',
            post_process_overrides={'video_crf': 28, 'audio_bitrate_kbps': 96})))
        crf_idx = cmd.index('-crf')
        self.assertEqual(cmd[crf_idx + 1], '28')
        ba_idx = cmd.index('-b:a')
        self.assertEqual(cmd[ba_idx + 1], '96k')

    def test_custom_audio_bitrate_applies_on_plain_copy_path(self):
        """reencode_mode 'never' takes the -c:v copy branch, which has no -crf flag at all
        but still re-encodes audio (ADTS -> raw AAC), so audio_bitrate_kbps still applies."""
        cmd = self._cmd(self._run(self._config(
            reencode_mode='never',
            post_process_overrides={'audio_bitrate_kbps': 128})))
        self.assertNotIn('-crf', cmd)
        self.assertIn('copy', cmd)
        ba_idx = cmd.index('-b:a')
        self.assertEqual(cmd[ba_idx + 1], '128k')


class SettingsFieldClampTests(ConfigSandbox):
    """api_settings_field clamps video_crf/audio_bitrate_kbps into their valid ranges,
    mirroring the existing clamp behavior for the sibling numeric post_process settings."""

    def setUp(self):
        super().setUp()
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client

    def tearDown(self):
        self.t.cleanup()

    def _post(self, path, value):
        return self.client.post('/api/settings/field', json={'path': path, 'value': value})

    def test_video_crf_clamped_to_valid_range(self):
        resp = self._post('recording.post_process.video_crf', 999)
        self.assertEqual(resp.status_code, 200)
        data = cfgmod.load_config()
        self.assertEqual(data['recording']['post_process']['video_crf'], 51)

        resp = self._post('recording.post_process.video_crf', -5)
        self.assertEqual(resp.status_code, 200)
        data = cfgmod.load_config()
        self.assertEqual(data['recording']['post_process']['video_crf'], 0)

    def test_audio_bitrate_kbps_clamped_to_valid_range(self):
        resp = self._post('recording.post_process.audio_bitrate_kbps', 5)
        self.assertEqual(resp.status_code, 200)
        data = cfgmod.load_config()
        self.assertEqual(data['recording']['post_process']['audio_bitrate_kbps'], 32)

        resp = self._post('recording.post_process.audio_bitrate_kbps', 5000)
        self.assertEqual(resp.status_code, 200)
        data = cfgmod.load_config()
        self.assertEqual(data['recording']['post_process']['audio_bitrate_kbps'], 320)


if __name__ == '__main__':
    unittest.main()
