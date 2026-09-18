"""Live channel preview (app/preview.py, app/routes/preview.py, dev/changelog/1018).

The lifecycle tests run a real ffmpeg against a real local clip - never a URL (netguard) -
so what they prove is the actual teardown: the ffmpeg is dead, the slot is released, the
directory is gone, and the reason is named. Each terminal path the module claims to
handle has a case here; a path without one is a path that can leak a provider
connection nobody is watching.
"""
import copy
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_channel_test  # noqa: E402

import app.connection_limits as connlim  # noqa: E402
import app.preview as preview  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.proc_utils import build_capture_cmd, build_preview_cmd, input_args  # noqa: E402

_HAVE_FFMPEG = shutil.which('ffmpeg') is not None
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(REPO, 'static', 'vendor', 'hls.js')

_CFG = {'ffmpeg': {'path': 'ffmpeg', 'read_timeout_seconds': 20}, 'http': {}}


def _wait(predicate, timeout=15.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class BuildPreviewCmdTests(unittest.TestCase):
    def test_copies_video_and_audio_into_a_rolling_hls_window(self):
        cmd = build_preview_cmd(_CFG, 'http://h/live/u/p/1', '/x')
        joined = ' '.join(cmd)
        self.assertIn('-c:v copy', joined)
        self.assertIn('-c:a copy', joined)
        self.assertIn('-f hls', joined)
        self.assertIn('delete_segments', joined)
        self.assertIn('-hls_delete_threshold 3', joined)   # a burst on connect must not 404 the first segment
        self.assertIn('omit_endlist', joined)
        self.assertIn('-map 0:v:0? -map 0:a:0?', joined)   # audio-only channels still preview
        self.assertTrue(cmd[-1].endswith('/x/index.m3u8'))
        self.assertNotIn('libx264', joined)                 # video is never re-encoded

    def test_audio_transcode_is_stereo_aac_and_nothing_else_changes(self):
        cmd = ' '.join(build_preview_cmd(_CFG, 'http://h/live/u/p/1', '/x', transcode_audio=True))
        self.assertIn('-c:a aac', cmd)
        self.assertIn('-ac 2', cmd)
        self.assertIn('-c:v copy', cmd)

    def test_input_half_is_the_capture_commands_input_half(self):
        """A provider must see a preview exactly as it sees a recording: same user agent,
        same read timeout, same reconnect flags, same HLS exclusion."""
        for url in ('http://h/live/u/p/1', 'http://h/live/u/p/1.m3u8', '/local/file.ts'):
            cap = build_capture_cmd(_CFG, url, '/o.ts', pace_realtime=False)
            pre = build_preview_cmd(_CFG, url, '/x', pace_realtime=False)
            shared = input_args(_CFG, url, pace_realtime=False)
            self.assertEqual(cap[1:1 + len(shared)], shared, url)
            self.assertEqual(pre[1:1 + len(shared)], shared, url)
        self.assertNotIn('-reconnect', input_args(_CFG, 'http://h/a.m3u8', pace_realtime=False))
        self.assertIn('-reconnect', input_args(_CFG, 'http://h/a', pace_realtime=False))
        self.assertIn('-re', input_args(_CFG, '/f.ts', pace_realtime=True))
        self.assertNotIn('-re', input_args(_CFG, '/f.ts', pace_realtime=False))


class AudioPolicyTests(unittest.TestCase):
    def test_only_codecs_a_browser_decodes_are_copied(self):
        for codec in ('aac', 'AAC', 'mp3', None, ''):
            self.assertFalse(preview.audio_needs_transcode(codec), codec)
        for codec in ('ac3', 'eac3', 'mp2', 'dts', 'opus'):
            self.assertTrue(preview.audio_needs_transcode(codec), codec)


class ReasonVocabularyTests(unittest.TestCase):
    def test_every_reason_has_user_facing_text(self):
        reasons = [v for k, v in vars(preview).items() if k.startswith('REASON_') and isinstance(v, str)]
        self.assertGreaterEqual(len(reasons), 9)
        for r in reasons:
            self.assertIn(r, preview.REASON_TEXT, r)
            self.assertTrue(preview.REASON_TEXT[r].strip(), r)


class ConnectTimeoutTests(unittest.TestCase):
    """The one terminal path a local file cannot produce: ffmpeg alive, nothing playable."""

    def test_a_session_with_no_playlist_by_the_deadline_stops_with_connect_timeout(self):
        class _Alive:
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                pass

        d = tempfile.mkdtemp(prefix='cb-preview-test-')
        s = preview.PreviewSession(
            id='t', channel_id=1, channel_name='c', account_id=1, dir=d,
            stderr_path=os.path.join(d, 'ffmpeg.stderr'), transcode_audio=False,
            idle_timeout=15, max_seconds=600, connect_timeout=0.01,
            proc=_Alive(), started_mono=time.monotonic() - 1)
        with mock.patch.object(preview, 'terminate_or_kill') as kill:
            preview._check(s)
        self.assertEqual(s.state, preview.STATE_STOPPED)
        self.assertEqual(s.reason, preview.REASON_CONNECT_TIMEOUT)
        kill.assert_called_once()
        self.assertFalse(os.path.exists(d))


class VendoredPlayerTests(unittest.TestCase):
    """hls.js ships in the repo under its own license; the notice must describe the file
    that is actually there, or the license compliance it documents is fiction."""

    def test_library_license_and_notice_are_present(self):
        for name in ('hls.light.min.js', 'LICENSE', 'NOTICE.md'):
            self.assertTrue(os.path.isfile(os.path.join(VENDOR_DIR, name)), name)
        with open(os.path.join(VENDOR_DIR, 'LICENSE'), encoding='utf-8') as fh:
            self.assertIn('Apache License', fh.read())

    def test_notice_names_the_hash_of_the_shipped_file(self):
        with open(os.path.join(VENDOR_DIR, 'hls.light.min.js'), 'rb') as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        with open(os.path.join(VENDOR_DIR, 'NOTICE.md'), encoding='utf-8') as fh:
            notice = fh.read()
        self.assertIn(digest, notice)
        self.assertRegex(notice, r'Version: \d+\.\d+\.\d+')

    def test_page_loads_the_player_from_the_vendored_path_only(self):
        with open(os.path.join(REPO, 'templates', 'channels', 'detail.html'), encoding='utf-8') as fh:
            html = fh.read()
        self.assertIn('vendor/hls.js/hls.light.min.js', html)
        self.assertNotRegex(html, r'https?://[^"\']*hls')


class _Live(unittest.TestCase):
    """Shared scaffolding: one real clip per class, a fresh app per test."""

    CLIP_SECONDS = 40

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='cb-preview-clip-')
        cls.clip = os.path.join(cls.tmp, 'clip.ts')
        subprocess.run(
            ['ffmpeg', '-v', 'error', '-y',
             '-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=30',
             '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
             '-t', str(cls.CLIP_SECONDS), '-c:v', 'libx264', '-preset', 'ultrafast',
             '-g', '30', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-f', 'mpegts', cls.clip],
            check=True, timeout=180)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.addCleanup(self.t.cleanup)
        # Every test ends its own session; cleanup's leak check then has nothing to report.
        self.addCleanup(preview.stop_all)
        self.account = make_account('Acct', max_connections=1)
        # Paced, so ffmpeg reads the 40 s clip at 1x and stays alive for the test's asserts.
        self.channel = self._clip_channel('Paced')
        db.session.commit()

    def _clip_channel(self, name, paced=True):
        ch = make_channel(self.account, name=name, pace_realtime=paced)
        ch.stream_url = self.clip
        db.session.flush()
        return ch

    def _start(self, channel=None):
        return self.client.post(f'/api/channels/{(channel or self.channel).id}/preview')

    def _status(self, sid):
        return self.client.get(f'/api/preview/{sid}/status').get_json()

    def _wait_state(self, sid, state, timeout=20.0):
        ok = _wait(lambda: self._status(sid)['state'] == state, timeout)
        self.assertTrue(ok, f'expected {state}, got {self._status(sid)}')

    def _holders(self):
        return list(connlim._holders.get(self.account.id, []))

    def _config_with(self, **preview_keys):
        cfg = copy.deepcopy(load_config())
        cfg['preview'].update(preview_keys)
        return mock.patch('app.config.load_config', return_value=cfg)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class LifecycleTests(_Live):
    def test_start_serves_a_playable_window_and_stop_releases_everything(self):
        resp = self._start()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        data = resp.get_json()
        sid = data['session_id']
        self.assertEqual(data['state'], preview.STATE_STARTING)
        # The browser learns this app's URLs and nothing about where the stream lives.
        self.assertNotIn(self.clip, resp.get_data(as_text=True))
        self.assertIn(('preview', sid), self._holders())

        self._wait_state(sid, preview.STATE_READY)
        session = preview.get_session(sid)
        self.assertTrue(os.path.isdir(session.dir))

        pl = self.client.get(data['playlist_url'])
        self.assertEqual(pl.status_code, 200)
        self.assertEqual(pl.mimetype, 'application/vnd.apple.mpegurl')
        self.assertEqual(pl.headers['Cache-Control'], 'no-store')
        body = pl.get_data(as_text=True)
        self.assertIn('#EXTINF', body)
        self.assertNotIn('#EXT-X-ENDLIST', body)             # a live window, never a finished file
        seg_name = re.search(r'^(seg\d{5}\.ts)$', body, re.M).group(1)
        seg = self.client.get(f'/api/preview/{sid}/{seg_name}')
        self.assertEqual(seg.status_code, 200)
        self.assertEqual(seg.mimetype, 'video/mp2t')
        self.assertGreater(len(seg.get_data()), 1000)
        self.assertEqual(preview.get_session(sid).playlist_fetches, 1)

        stop = self.client.post(data['stop_url'])
        self.assertEqual(stop.status_code, 200)
        self.assertTrue(stop.get_json()['stopped'])
        st = self._status(sid)
        self.assertEqual(st['state'], preview.STATE_STOPPED)
        self.assertEqual(st['reason'], preview.REASON_USER)
        # STOPPED is the contract that everything is already released, not a promise.
        self.assertIsNotNone(session.proc.poll(), 'ffmpeg must be dead after Stop')
        self.assertNotIn(('preview', sid), self._holders())
        self.assertFalse(os.path.exists(session.dir), 'segment directory must be removed')
        # ...which is what lets a Preview started the instant after take the slot.
        again = self._start()
        self.assertEqual(again.status_code, 200, again.get_json())
        self.client.post(again.get_json()['stop_url'])
        # The playlist is gone with it, and says so rather than serving a stale window.
        self.assertEqual(self.client.get(data['playlist_url']).status_code, 404)
        # A second Stop is a no-op, not an error.
        self.assertFalse(self.client.post(data['stop_url']).get_json()['stopped'])

    def test_starting_a_second_preview_stops_the_first(self):
        other = self._clip_channel('Other')
        db.session.commit()
        first = self._start().get_json()['session_id']
        second = self._start(other).get_json()['session_id']
        st = self._status(first)
        self.assertEqual(st['state'], preview.STATE_STOPPED)
        self.assertEqual(st['reason'], preview.REASON_REPLACED)
        self.assertEqual(preview.live_session().id, second)
        self.assertEqual(self._holders(), [('preview', second)])

    def test_refused_at_the_connection_limit_naming_the_holder(self):
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)
        resp = self._start()
        self.assertEqual(resp.status_code, 409)
        self.assertIn('a recording', resp.get_json()['error'])
        self.assertIsNone(preview.live_session())
        self.assertEqual(self._holders(), [('recording', 4242)])

    def test_a_recording_preempts_the_preview_and_takes_its_slot(self):
        from app.recorder import _try_acquire_slot_with_preemption
        sid = self._start().get_json()['session_id']
        self._wait_state(sid, preview.STATE_READY)
        proc = preview.get_session(sid).proc
        self.assertTrue(_try_acquire_slot_with_preemption(self.t.app, 77, self.account.id))
        self.addCleanup(connlim.release, self.account.id, 'recording', 77)
        st = self._status(sid)
        self.assertEqual(st['state'], preview.STATE_STOPPED)
        self.assertEqual(st['reason'], preview.REASON_PREEMPTED)
        self.assertIn('recording', st['reason_text'])
        self.assertIsNotNone(proc.poll())
        self.assertEqual(self._holders(), [('recording', 77)])

    def test_preempted_inside_the_launch_window_never_runs_slotless(self):
        """A recording strips the slot between acquire and Popen: the preview must kill
        the ffmpeg it just started rather than run it on a connection it no longer holds."""
        real_popen = subprocess.Popen

        def popen_then_preempt(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            connlim.preempt_previews_for_slot(self.account.id)
            preview.preempt_for_account(self.account.id)
            return proc

        with mock.patch.object(preview.subprocess, 'Popen', side_effect=popen_then_preempt):
            resp = self._start()
        self.assertEqual(resp.status_code, 409)
        session = next(iter(preview._sessions.values()))
        self.assertEqual(session.reason, preview.REASON_PREEMPTED)
        self.assertIsNotNone(session.proc.poll())
        self.assertEqual(self._holders(), [])

    def test_idle_reaper_stops_a_preview_nobody_is_playing(self):
        with self._config_with(idle_timeout_seconds=1):
            sid = self._start().get_json()['session_id']
        self._wait_state(sid, preview.STATE_READY)
        session = preview.get_session(sid)
        self.assertTrue(_wait(lambda: not session.live, 6.0))
        self.assertEqual(session.reason, preview.REASON_IDLE)
        self.assertIsNotNone(session.proc.poll())
        self.assertEqual(self._holders(), [])

    def test_playlist_fetches_keep_an_idle_preview_alive(self):
        with self._config_with(idle_timeout_seconds=1):
            data = self._start().get_json()
        sid = data['session_id']
        self._wait_state(sid, preview.STATE_READY)
        for _ in range(6):
            self.assertEqual(self.client.get(data['playlist_url']).status_code, 200)
            time.sleep(0.5)
        self.assertTrue(preview.get_session(sid).live)

    def test_hard_cap_stops_a_long_preview(self):
        with self._config_with(max_seconds=2):
            sid = self._start().get_json()['session_id']
        session = preview.get_session(sid)
        self.assertTrue(_wait(lambda: not session.live, 8.0))
        self.assertEqual(session.reason, preview.REASON_MAX_DURATION)
        self.assertEqual(self._holders(), [])

    def test_ffmpeg_ending_on_its_own_is_named_with_its_exit_code(self):
        # Unpaced: ffmpeg reads the whole clip in well under a second and exits 0.
        self.channel.pace_realtime = False
        db.session.commit()
        sid = self._start().get_json()['session_id']
        session = preview.get_session(sid)
        self.assertTrue(_wait(lambda: not session.live, 20.0))
        self.assertEqual(session.reason, preview.REASON_FFMPEG_EXITED)
        self.assertIn('exited with code 0', session.detail)
        self.assertEqual(self._holders(), [])

    def test_a_source_ffmpeg_cannot_open_is_named_from_its_stderr(self):
        self.channel.stream_url = os.path.join(self.tmp, 'does-not-exist.ts')
        self.channel.pace_realtime = False
        db.session.commit()
        sid = self._start().get_json()['session_id']
        session = preview.get_session(sid)
        self.assertTrue(_wait(lambda: not session.live, 20.0))
        self.assertEqual(session.reason, preview.REASON_FFMPEG_EXITED)
        self.assertIn('does-not-exist', session.detail)

    def test_shutdown_kills_the_live_preview(self):
        sid = self._start().get_json()['session_id']
        session = preview.get_session(sid)
        preview.kill_all_previews()
        self.assertEqual(session.reason, preview.REASON_SHUTDOWN)
        self.assertIsNotNone(session.proc.poll())
        self.assertEqual(self._holders(), [])

    def test_audio_policy_follows_the_channels_latest_test(self):
        make_channel_test(self.channel, all_null=False, status='COMPLETED', audio_codec='ac3')
        db.session.commit()
        data = self._start().get_json()
        self.assertTrue(data['transcode_audio'])
        self.client.post(data['stop_url'])
        make_channel_test(self.channel, all_null=False, status='COMPLETED', audio_codec='aac')
        db.session.commit()
        data = self._start().get_json()
        self.assertFalse(data['transcode_audio'])


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class RouteGuardTests(_Live):
    def test_only_ffmpegs_own_segment_names_are_served(self):
        sid = self._start().get_json()['session_id']
        self._wait_state(sid, preview.STATE_READY)
        for name in ('ffmpeg.stderr', '..%2Fclip.ts', 'seg1.ts', 'seg00001.ts.bak', 'index.m3u8.bak'):
            self.assertEqual(self.client.get(f'/api/preview/{sid}/{name}').status_code, 404, name)
        self.assertIsNone(preview.segment_path(sid, '../ffmpeg.stderr'))
        self.assertIsNone(preview.segment_path(sid, 'ffmpeg.stderr'))

    def test_unknown_session_is_404_on_every_route(self):
        self.assertEqual(self.client.get('/api/preview/nope/status').status_code, 404)
        self.assertEqual(self.client.get('/api/preview/nope/index.m3u8').status_code, 404)
        self.assertEqual(self.client.get('/api/preview/nope/seg00001.ts').status_code, 404)
        self.assertEqual(self.client.post('/api/preview/nope/stop').status_code, 404)

    def test_unknown_channel_is_404(self):
        resp = self.client.post('/api/channels/999999/preview')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.get_json())


if __name__ == '__main__':
    unittest.main()
