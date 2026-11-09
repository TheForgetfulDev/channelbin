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
from app.proc_utils import (build_capture_cmd, build_file_playback_cmd,  # noqa: E402
                            build_preview_cmd, input_args)
from tests.support.seed import make_recording  # noqa: E402

_HAVE_FFMPEG = shutil.which('ffmpeg') is not None
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(REPO, 'static', 'vendor', 'hls.js')
HLS_JS = 'hls.min.js'

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


class BuildFilePlaybackCmdTests(unittest.TestCase):
    """dev/changelog/1314: a recording's file through the preview's window."""

    def test_input_half_is_a_files_never_a_streams(self):
        cmd = build_file_playback_cmd(_CFG, '/dvr/a b:c.ts', '/x', offset=3600)
        joined = ' '.join(cmd)
        for flag in ('-user_agent', '-reconnect', '-rw_timeout', '-re '):
            self.assertNotIn(flag, joined)
        # Paced to real time after a burst, so a viewer never reads the share faster than
        # they watch.
        self.assertIn('-readrate 1 -readrate_initial_burst', joined)
        # Input seek: -ss before -i, and a colon in the path is never a protocol.
        self.assertLess(cmd.index('-ss'), cmd.index('-i'))
        self.assertEqual(cmd[cmd.index('-ss') + 1], '3600.000')
        self.assertEqual(cmd[cmd.index('-i') + 1], 'file:/dvr/a b:c.ts')

    def test_output_timestamps_are_positions_in_the_recording(self):
        """Measured: without the zero mux delay the muxer adds 1.4 s, without -copyts the
        output restarts at zero - either way the player cannot tell where it landed."""
        joined = ' '.join(build_file_playback_cmd(_CFG, '/a.ts', '/x', offset=10))
        self.assertIn('-copyts -start_at_zero -muxdelay 0 -muxpreload 0', joined)

    def test_a_file_playlist_ends_with_the_file(self):
        joined = ' '.join(build_file_playback_cmd(_CFG, '/a.ts', '/x'))
        self.assertNotIn('omit_endlist', joined)
        self.assertIn('delete_segments', joined)

    def test_offset_zero_does_not_seek(self):
        self.assertNotIn('-ss', build_file_playback_cmd(_CFG, '/a.ts', '/x', offset=0))

    def test_output_half_is_the_channel_previews(self):
        """One window for both sources, so a browser receives a file as it receives a channel;
        only the channel's never-ending playlist differs."""
        def tail(cmd):
            out = cmd[cmd.index('-map'):]
            flags = out.index('-hls_flags') + 1
            out[flags] = out[flags].replace('+omit_endlist', '')
            return out
        for transcode in (False, True):
            self.assertEqual(
                tail(build_file_playback_cmd(_CFG, '/a.ts', '/x', transcode_audio=transcode,
                                             list_size=6)),
                tail(build_preview_cmd(_CFG, 'http://h/1', '/x', transcode_audio=transcode)))
        self.assertNotIn('libx264', ' '.join(build_file_playback_cmd(_CFG, '/a.ts', '/x')))


    def test_following_a_segment_being_written(self):
        """dev/changelog/1316: read past the end of the file, from an input seek, paced like a
        finished file so a follower behind live never reads the share faster than it plays."""
        cmd = build_file_playback_cmd(_CFG, '/dvr/seg.ts', '/x', offset=30, follow=True)
        joined = ' '.join(cmd)
        self.assertLess(cmd.index('-follow'), cmd.index('-i'))
        self.assertEqual(cmd[cmd.index('-follow') + 1], '1')
        self.assertIn('-readrate 1 -readrate_initial_burst', joined)
        self.assertEqual(cmd[cmd.index('-ss') + 1], '30.000')
        # -sseof landed past what a young file held and then never produced a segment.
        self.assertNotIn('-sseof', joined)
        # The playlist never ends - neither does the file, as far as ffmpeg knows.
        self.assertIn('omit_endlist', joined)
        self.assertIn('-copyts -start_at_zero -muxdelay 0 -muxpreload 0', joined)

    def test_only_a_follower_follows(self):
        self.assertNotIn('-follow', build_file_playback_cmd(_CFG, '/a.ts', '/x'))


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


# ffmpeg 7.1's own lines for a refused stream request (app/proc_utils.py::stderr_cause and
# app/account_links.py::CREDENTIAL_REFUSAL_MARKERS carry the measurements).
_STDERR_405 = ('[http @ 0x55d0] HTTP error 405 Method Not Allowed\n'
               '[in#0 @ 0x55d1] Error opening input: Server returned 4XX Client Error, '
               'but not one of 40{0,1,3,4}')
_STDERR_403 = ('[http @ 0x55d0] HTTP error 403 Forbidden\n'
               '[in#0 @ 0x55d1] Error opening input: Server returned 403 Forbidden '
               '(access denied)')


class StopDetailTests(unittest.TestCase):
    """A preview that ends names the provider's answer before ffmpeg's exit code and log
    (dev/changelog/1397): the user read "ffmpeg exited with code 8" over a raw log and had
    to find the HTTP 405 in it themselves."""

    class _Exited:
        def __init__(self, rc):
            self.rc = rc

        def poll(self):
            return self.rc

    def _ended(self, stderr, rc=8):
        d = tempfile.mkdtemp(prefix='cb-preview-test-')
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, 'ffmpeg.stderr')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(stderr)
        s = preview.PreviewSession(
            id='t', channel_id=1, channel_name='c', account_id=1, dir=d, stderr_path=path,
            transcode_audio=False, idle_timeout=15, max_seconds=600, connect_timeout=10,
            proc=self._Exited(rc), started_mono=time.monotonic())
        with mock.patch.object(preview, 'terminate_or_kill'), \
                mock.patch.object(preview, '_roll_if_unresolved'):
            preview._check(s)
        self.assertEqual(s.reason, preview.REASON_FFMPEG_EXITED)
        return s.detail

    def test_a_refused_channel_leads_with_the_providers_answer(self):
        detail = self._ended(_STDERR_405)
        first, rest = detail.split('\n', 1)
        self.assertEqual(first, "The provider's server answered HTTP 405 Method Not Allowed.")
        self.assertTrue(rest.startswith('ffmpeg exited with code 8: '), rest)
        self.assertIn('HTTP error 405', rest)

    def test_a_refused_login_is_named_as_the_login(self):
        detail = self._ended(_STDERR_403, rc=1)
        self.assertTrue(detail.startswith('The provider refused the login (HTTP 403 Forbidden).\n'),
                        detail)

    def test_an_end_with_no_recognizable_cause_is_the_log_alone(self):
        self.assertEqual(self._ended('', rc=0), 'ffmpeg exited with code 0')
        self.assertEqual(self._ended('frame=  10 fps=0.0', rc=0),
                         'ffmpeg exited with code 0: frame=  10 fps=0.0')

    def test_a_connect_timeout_leads_with_the_cause_too(self):
        self.assertEqual(preview.stream_stop_detail(_STDERR_405).split('\n')[0],
                         "The provider's server answered HTTP 405 Method Not Allowed.")
        self.assertEqual(preview.stream_stop_detail(''), '')


class VendoredPlayerTests(unittest.TestCase):
    """hls.js ships in the repo under its own license; the notice must describe the file
    that is actually there, or the license compliance it documents is fiction."""

    def test_library_license_and_notice_are_present(self):
        for name in (HLS_JS, 'LICENSE', 'NOTICE.md'):
            self.assertTrue(os.path.isfile(os.path.join(VENDOR_DIR, name)), name)
        with open(os.path.join(VENDOR_DIR, 'LICENSE'), encoding='utf-8') as fh:
            self.assertIn('Apache License', fh.read())

    def test_notice_names_the_hash_of_the_shipped_file(self):
        with open(os.path.join(VENDOR_DIR, HLS_JS), 'rb') as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        with open(os.path.join(VENDOR_DIR, 'NOTICE.md'), encoding='utf-8') as fh:
            notice = fh.read()
        self.assertIn(digest, notice)
        self.assertRegex(notice, r'Version: \d+\.\d+\.\d+')

    def test_every_player_page_loads_the_vendored_file(self):
        for page in ('guide.html', 'recording_detail.html', 'channels/detail.html',
                     'channels/group_detail.html'):
            with open(os.path.join(REPO, 'templates', page), encoding='utf-8') as fh:
                html = fh.read()
            refs = re.findall(r"data-hls-js=\"\{\{ url_for\('static', filename='([^']+)'\) \}\}\"",
                              html)
            self.assertTrue(refs, page)
            self.assertEqual(set(refs), {f'vendor/hls.js/{HLS_JS}'}, page)
            self.assertNotRegex(html, r'https?://[^"\']*hls')

    def test_the_build_carries_hevc_in_mpeg_ts(self):
        # The light build refuses HEVC in a .ts before the browser's decoder is asked (fatal
        # fragParsingError, dev/docs/BUGS.md 2026-11-06), and every preview segment is a .ts.
        with open(os.path.join(VENDOR_DIR, HLS_JS), encoding='utf-8') as fh:
            self.assertNotIn('Unsupported HEVC in M2TS', fh.read())


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
        self.t = make_test_app(csrf=False)
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
        """Fetches span 2.5x the idle timeout, with 1.5 s of slack per gap: a 0.5 s margin
        against a 1 s timeout lost to one slow request on a loaded box (dev/docs/BUGS.md
        2026-09-29)."""
        with self._config_with(idle_timeout_seconds=2):
            data = self._start().get_json()
        sid = data['session_id']
        self._wait_state(sid, preview.STATE_READY)
        for _ in range(10):
            self.assertEqual(self.client.get(data['playlist_url']).status_code, 200)
            time.sleep(0.5)
        session = preview.get_session(sid)
        self.assertTrue(session.live, f'preview stopped: {session.reason} {session.detail}')

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


def _first_pts(data: bytes, tmpdir: str) -> float:
    """The earliest packet timestamp in one served segment."""
    path = os.path.join(tmpdir, 'probe-seg.ts')
    with open(path, 'wb') as fh:
        fh.write(data)
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'packet=pts_time',
                          '-read_intervals', '%+#10', '-of', 'csv=p=0', path],
                         capture_output=True, text=True, timeout=30).stdout
    return min(float(x.strip(',')) for x in out.split() if x.strip(',') not in ('', 'N/A'))


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class FileSessionTests(_Live):
    """A recording's finished file played through the preview pipeline (dev/changelog/1314).

    What these prove with a real ffmpeg: the file session takes no provider connection, a
    seek finishes the old session (ffmpeg dead, directory gone) before the new ffmpeg starts,
    the output's timestamps are positions in the recording, the end of the file is named and
    is not the end of the session, and every other way it ends says why."""

    def setUp(self):
        super().setUp()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.file = os.path.join(self.dvr, 'Race day.ts')
        shutil.copyfile(self.clip, self.file)

    def _rec(self, path=None, status='COMPLETED', acodec='aac', duration=None, vcodec='h264'):
        rec = make_recording(status=status, name='Race day', output_path=path or self.file,
                             recorded_video_codec=vcodec, recorded_audio_codec=acodec,
                             recorded_duration_seconds=(self.CLIP_SECONDS if duration is None
                                                        else duration or None))
        db.session.commit()
        return rec

    def _play(self, rec, offset=0):
        return self.client.post(f'/api/recordings/{rec.id}/playback', json={'offset': offset})

    def _file_session(self, offset=0, path=None):
        return preview.start_file_playback(1, 'Race day', path or self.file, offset,
                                           transcode_audio=False)

    def test_plays_from_the_offset_and_takes_no_connection(self):
        rec = self._rec()
        resp = self._play(rec, 20)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        data = resp.get_json()
        sid = data['session_id']
        self.assertEqual((data['source'], data['recording_id'], data['offset']), ('file', rec.id, 20))
        self.assertIsNone(data['max_seconds'])
        self.assertNotIn(self.file, resp.get_data(as_text=True))
        self.assertEqual(connlim._holders.get(self.account.id, []), [])
        self.assertFalse(any(h for hs in connlim._holders.values() for h in hs if h[1] == sid))
        self._wait_state(sid, preview.STATE_READY)

        body = self.client.get(data['playlist_url']).get_data(as_text=True)
        seg_name = re.search(r'^(seg\d{5}\.ts)$', body, re.M).group(1)
        seg = self.client.get(f'/api/preview/{sid}/{seg_name}').get_data()
        # The output's timestamps ARE positions in the recording: the first segment starts
        # where the seek landed - the keyframe at 20 s in this 1 s-GOP clip, audio a little
        # before it. A mux delay left in would put it 1.4 s later.
        first = _first_pts(seg, self.tmp)
        self.assertGreater(first, 19.0)
        self.assertLess(first, 20.6)
        self.client.post(data['stop_url'])

    def test_a_seek_finishes_the_old_session_before_the_new_ffmpeg_starts(self):
        first = self._file_session(0)
        self._wait_state(first.id, preview.STATE_READY)
        seen = {}
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            seen['old_ffmpeg_exit'] = first.proc.poll()
            seen['old_dir_exists'] = os.path.exists(first.dir)
            seen['old_state'] = first.state
            return real_popen(*args, **kwargs)
        with mock.patch.object(preview.subprocess, 'Popen', side_effect=popen):
            second = self._file_session(25)
        self.assertIsNotNone(seen['old_ffmpeg_exit'], 'two ffmpegs would read the file at once')
        self.assertFalse(seen['old_dir_exists'])
        self.assertEqual(seen['old_state'], preview.STATE_STOPPED)
        self.assertEqual(first.reason, preview.REASON_REPLACED)
        self.assertIn('another playback', first.reason_text())
        self.assertEqual(preview.live_session().id, second.id)

    def test_the_end_of_the_file_is_named_and_the_player_can_finish(self):
        with self._config_with(idle_timeout_seconds=1):
            session = self._file_session(self.CLIP_SECONDS - 3)
        self.assertTrue(_wait(lambda: session.source_done, 15.0), session.to_dict())
        # ffmpeg is done; the session is not - the player still has segments to fetch.
        self.assertTrue(session.live)
        self.assertEqual(session.state, preview.STATE_READY)
        with open(session.playlist_path, encoding='utf-8') as fh:
            self.assertIn('#EXT-X-ENDLIST', fh.read())
        self.assertTrue(_wait(lambda: not session.live, 8.0))
        self.assertEqual(session.reason, preview.REASON_FILE_END)
        self.assertFalse(os.path.exists(session.dir))

    def test_an_offset_past_the_end_says_so(self):
        session = self._file_session(self.CLIP_SECONDS + 60)
        self.assertTrue(_wait(lambda: not session.live, 15.0))
        self.assertEqual(session.reason, preview.REASON_PAST_END)
        self.assertIn('past the end', session.reason_text())

    def test_a_file_that_cannot_be_read_shows_ffmpegs_error(self):
        session = self._file_session(0, path=os.path.join(self.dvr, 'gone.ts'))
        self.assertTrue(_wait(lambda: not session.live, 15.0))
        self.assertEqual(session.reason, preview.REASON_READ_FAILED)
        self.assertIn('No such file or directory', session.detail)
        self.assertIn('gone.ts', session.detail)

    def test_no_hard_cap(self):
        """preview.max_seconds bounds a provider connection; a file session holds none and
        ends at the end of the file by itself."""
        with self._config_with(max_seconds=1):
            session = self._file_session(0)
        self._wait_state(session.id, preview.STATE_READY)
        for _ in range(6):
            self.assertIsNotNone(preview.touch_playlist(session.id))
            time.sleep(0.5)
        self.assertTrue(session.live, f'{session.reason} {session.detail}')

    def test_nobody_watching_stops_it_by_name(self):
        with self._config_with(idle_timeout_seconds=1):
            session = self._file_session(0)
        self.assertTrue(_wait(lambda: not session.live, 15.0))
        self.assertEqual(session.reason, preview.REASON_IDLE)
        self.assertIn('this recording', session.reason_text())
        self.assertIsNotNone(session.proc.poll())

    def test_a_recording_cannot_preempt_it_and_it_holds_nothing_to_release(self):
        session = self._file_session(0)
        self.assertFalse(preview.preempt_for_account(self.account.id))
        self.assertTrue(session.live)
        with mock.patch.object(preview.connlim, 'release') as release:
            preview.stop_preview(session.id)
        release.assert_not_called()

    def test_shutdown_kills_it(self):
        session = self._file_session(0)
        preview.kill_all_previews()
        self.assertEqual(session.reason, preview.REASON_SHUTDOWN)
        self.assertIsNotNone(session.proc.poll())
        self.assertFalse(os.path.exists(session.dir))

    def test_one_watched_session_across_both_sources(self):
        channel_sid = self._start().get_json()['session_id']
        file_session = self._file_session(0)
        self.assertEqual(preview.get_session(channel_sid).reason, preview.REASON_REPLACED)
        self.assertEqual(self._holders(), [])
        again = self._start().get_json()['session_id']
        self.assertEqual(file_session.reason, preview.REASON_REPLACED)
        self.client.post(f'/api/preview/{again}/stop')

    def test_audio_a_browser_lacks_is_re_encoded(self):
        data = self._play(self._rec(acodec='ac3')).get_json()
        self.assertTrue(data['transcode_audio'])
        self.client.post(data['stop_url'])
        data = self._play(self._rec(acodec='aac')).get_json()
        self.assertFalse(data['transcode_audio'])
        self.client.post(data['stop_url'])

    def test_an_mkv_plays_too(self):
        mkv = os.path.join(self.dvr, 'Race day.mkv')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', self.clip, '-c', 'copy', mkv],
                       check=True, timeout=60)
        data = self._play(self._rec(path=mkv), 5).get_json()
        self._wait_state(data['session_id'], preview.STATE_READY)
        self.assertIn('#EXTINF', self.client.get(data['playlist_url']).get_data(as_text=True))
        self.client.post(data['stop_url'])

    def test_a_kept_ts_on_a_failed_recording_plays(self):
        data = self._play(self._rec(status='FAILED')).get_json()
        self.assertEqual(data.get('source'), 'file', data)
        self.client.post(data['stop_url'])


class _FakeCapture:
    """The one thing a follower reads from the recorder: its live state and segment number."""

    def __init__(self, segment):
        self.current_segment_num = segment


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class FollowerTests(_Live):
    """A session following the segment the recorder is writing (dev/changelog/1316), with a
    real ffmpeg writing a real .ts at real time beside it. What these prove: the follower
    plays a file that is still growing, ends by name the moment the recorder moves to another
    segment or stops, dies at once rather than after the terminate grace, and a new follower
    on the next segment plays - the player's way across a segment change."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A capture's ffmpeg writes in 256 KiB blocks, and a follower needs a few before its
        # first segment; at the shared clip's ~270 kbps that is 20 s. 2 Mbps is a block a
        # second, closer to a real channel.
        cls.dense = os.path.join(cls.tmp, 'dense.ts')
        subprocess.run(
            ['ffmpeg', '-v', 'error', '-y',
             '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30',
             '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
             '-t', '30', '-c:v', 'libx264', '-preset', 'ultrafast', '-b:v', '2M',
             '-g', '30', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-f', 'mpegts', cls.dense],
            check=True, timeout=180)

    def setUp(self):
        super().setUp()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.state = _FakeCapture(1)
        self.live_state = self.state
        patcher = mock.patch('app.recorder.get_state', side_effect=lambda rid: self.live_state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _writer(self, name, seconds=30):
        path = os.path.join(self.dvr, name)
        proc = subprocess.Popen(['ffmpeg', '-v', 'error', '-re', '-i', self.dense, '-t', str(seconds),
                                 '-c', 'copy', '-f', 'mpegts', '-y', path],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.assertTrue(_wait(lambda: os.path.exists(path) and os.path.getsize(path) > 0, 15))
        return path, proc

    def _follow(self, path, segment, base=0.0, offset=0):
        place = preview.CapturePlace(segment_number=segment, base=base,
                                     follow_state=self.state, live=True)
        return preview.start_file_playback(1, 'Race day', path, offset, transcode_audio=False,
                                           capture=place)

    def test_follows_a_growing_file_and_moves_with_the_segment(self):
        seg1, _ = self._writer('race_1_seg_001.ts')
        first = self._follow(seg1, 1)
        self.assertTrue(first.following)
        self._wait_state(first.id, preview.STATE_READY)
        listed = preview._listed_segments(first.playlist_path)
        self.assertTrue(_wait(lambda: len(preview._listed_segments(first.playlist_path))
                              > len(listed), 10), 'the window must grow as the file does')
        with open(first.playlist_path, encoding='utf-8') as fh:
            self.assertNotIn('#EXT-X-ENDLIST', fh.read())

        # The recorder moves on: segment 2 starts, and the follower on 1 ends by name.
        seg2, _ = self._writer('race_1_seg_002.ts')
        moved = time.monotonic()
        self.state.current_segment_num = 2
        self.assertTrue(_wait(lambda: not first.live, 5))
        self.assertEqual(first.reason, preview.REASON_SEGMENT_CHANGED)
        self.assertIn('segment 2', first.detail)
        self.assertIsNotNone(first.proc.poll())
        self.assertFalse(os.path.exists(first.dir))
        # Killed outright: a follower ignores the one SIGTERM, which would cost the 5 s grace.
        self.assertLess(time.monotonic() - moved, 3.0)

        second = self._follow(seg2, 2, base=12.0)
        self._wait_state(second.id, preview.STATE_READY)
        self.assertEqual(second.to_dict()['capture'],
                         {'segment_number': 2, 'base': 12.0, 'span_end': None, 'live': True})

        # The capture stops (stop, pause, retry, failure: the recorder drops its state).
        self.live_state = None
        self.assertTrue(_wait(lambda: not second.live, 5))
        self.assertEqual(second.reason, preview.REASON_CAPTURE_STOPPED)
        self.assertIsNotNone(second.proc.poll())

    def test_a_capture_that_writes_nothing_is_named_not_waited_on_forever(self):
        stalled = os.path.join(self.dvr, 'race_1_seg_001.ts')
        with open(stalled, 'wb') as fh:
            fh.write(b'\0' * 188)
        with self._config_with(connect_timeout_seconds=1), \
                mock.patch.object(preview, 'FOLLOW_START_SECONDS', 2):
            session = self._follow(stalled, 1)
        self.assertTrue(_wait(lambda: not session.live, 10))
        self.assertEqual(session.reason, preview.REASON_CONNECT_TIMEOUT)
        self.assertIn('the capture wrote nothing', session.reason_text())

    def test_a_follower_gets_longer_than_a_channel_to_start(self):
        seg, _ = self._writer('race_1_seg_001.ts')
        with self._config_with(connect_timeout_seconds=20):
            session = self._follow(seg, 1)
        self.assertEqual(session.connect_timeout, preview.FOLLOW_START_SECONDS)

    def test_a_resumed_recording_is_a_new_capture(self):
        """Pause then Resume makes a new live state; a follower on the old one has nothing
        left to follow even though the segment number may read the same."""
        seg, _ = self._writer('race_1_seg_001.ts')
        session = self._follow(seg, 1)
        self.live_state = _FakeCapture(1)
        self.assertTrue(_wait(lambda: not session.live, 5))
        self.assertEqual(session.reason, preview.REASON_CAPTURE_STOPPED)

    def test_a_follower_waiting_at_the_end_of_a_finished_file_stops_at_once(self):
        seg, writer = self._writer('race_1_seg_001.ts', seconds=6)
        session = self._follow(seg, 1)
        self._wait_state(session.id, preview.STATE_READY)
        writer.wait(timeout=15)
        time.sleep(1)                      # at the end of the file, waiting for more
        began = time.monotonic()
        preview.stop_preview(session.id)
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertIsNotNone(session.proc.poll())

    def test_shutdown_kills_a_follower(self):
        seg, _ = self._writer('race_1_seg_001.ts')
        session = self._follow(seg, 1)
        preview.kill_all_previews()
        self.assertEqual(session.reason, preview.REASON_SHUTDOWN)
        self.assertIsNotNone(session.proc.poll())
        self.assertFalse(os.path.exists(session.dir))


class FilePlaybackRouteRefusalTests(_Live):
    """What POST /api/recordings/<id>/playback refuses, without starting anything."""

    def setUp(self):
        super().setUp()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)

    def _rec(self, name='a.ts', status='COMPLETED', duration=40.0, folder=None):
        path = os.path.join(folder or self.dvr, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(b'\0' * 188)
        rec = make_recording(status=status, name='Race day', output_path=path,
                             recorded_video_codec='h264', recorded_audio_codec='aac',
                             recorded_duration_seconds=duration)
        db.session.commit()
        return rec

    def _play(self, rec, body):
        resp = self.client.post(f'/api/recordings/{rec.id}/playback', json=body)
        return resp.status_code, resp.get_json()

    def test_refusals_start_nothing(self):
        mp4 = self._rec('a.mp4')
        ts = self._rec('a.ts')
        outside = self._rec('b.ts', folder=os.path.join(self.t._tmpdir, 'not-dvr'))
        cases = [
            (mp4, {'offset': 0}, 409, "browser's own player"),
            (ts, {'offset': -1}, 400, '0 or more'),
            (ts, {'offset': 'soon'}, 400, 'number of seconds'),
            (ts, {'offset': float('inf')}, 400, '0 or more'),
            (ts, {'offset': 40}, 400, 'past the end of the recording (40s)'),
            (outside, {'offset': 0}, 403, 'outside the recording folder'),
        ]
        for rec, body, code, words in cases:
            with self.subTest(body=body, path=rec.output_path):
                got, data = self._play(rec, body)
                self.assertEqual(got, code, data)
                self.assertIn(words, data['error'])
        self.assertIsNone(preview.live_session())
        self.assertEqual(self.client.post('/api/recordings/987654/playback', json={}).status_code,
                         404)

    def test_the_check_tells_the_player_which_route_and_how_long(self):
        rec = self._rec('a.ts', duration=7236.9)
        data = self.client.get(f'/recordings/{rec.id}/file/check').get_json()
        self.assertTrue(data['pipeline'])
        self.assertFalse(data['native'])
        self.assertEqual(data['duration'], 7236.9)
        self.assertEqual(data['playback_url'], f'/api/recordings/{rec.id}/playback')


if __name__ == '__main__':
    unittest.main()


# ── The GPU re-encode a viewer asks for after a decode failure (dev/changelog/1461) ─────────
_DEVICE = '/dev/dri/renderD128'


class BuildReencodeCmdTests(unittest.TestCase):
    """The command half of a re-encode: the device is opened before the input, the video is
    decoded on the CPU and encoded on the GPU, and the audio is AAC whatever it was."""

    def _check(self, cmd):
        joined = ' '.join(cmd)
        i = cmd.index('-i')
        self.assertLess(cmd.index('-vaapi_device'), i)
        self.assertEqual(cmd[cmd.index('-vaapi_device') + 1], _DEVICE)
        self.assertLess(cmd.index('-reinit_filter:v'), i)
        self.assertNotIn('-c:v copy', joined)
        self.assertNotIn('-hwaccel', joined)                 # decoding stays on the CPU
        self.assertEqual(cmd[cmd.index('-c:v') + 1], 'h264_vaapi')
        vf = cmd[cmd.index('-vf') + 1]
        # Fitted (field by field) to one fixed size for the GPU's pool, THEN deinterlaced.
        self.assertTrue(vf.startswith('scale='), vf)
        self.assertIn(':interl=-1,', vf)
        self.assertLess(vf.index('pad=w=1920:h=1080'), vf.index(',bwdif='))
        self.assertTrue(vf.endswith('format=nv12,hwupload'), vf)
        self.assertEqual(cmd[cmd.index('-fps_mode:v') + 1], 'passthrough')
        self.assertEqual(cmd[cmd.index('-force_key_frames') + 1], 'expr:gte(t,n_forced*2)')
        self.assertIn('-c:a aac', joined)
        self.assertNotIn('libx264', joined)                  # never a CPU encode
        return joined

    def test_a_channel_preview_reencodes_on_the_device(self):
        self._check(build_preview_cmd(_CFG, 'http://h/live/u/p/1', '/x', reencode_device=_DEVICE))

    def test_a_file_reencode_keeps_the_recordings_timestamps(self):
        joined = self._check(build_file_playback_cmd(_CFG, '/a.ts', '/x', offset=3600,
                                                     reencode_device=_DEVICE))
        self.assertIn('-copyts -start_at_zero -muxdelay 0 -muxpreload 0', joined)

    def test_without_a_device_nothing_changes(self):
        self.assertEqual(build_preview_cmd(_CFG, 'http://h/1', '/x'),
                         build_preview_cmd(_CFG, 'http://h/1', '/x', reencode_device=None))
        self.assertNotIn('-vaapi_device', build_file_playback_cmd(_CFG, '/a.ts', '/x'))


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class ReencodeTimingTests(unittest.TestCase):
    """The re-encode's filter, frame rate and keyframes, run for real with libx264 standing in
    for h264_vaapi (no GPU in the suite; the GPU half is the lab job
    dev/tools/gpu-lab/jobs/preview-reencode.sh). A progressive 60 fps file played from 20 s in:
    every segment two seconds, starting on its one keyframe, at the source's 60 fps. Measured
    before the fix: the HLS muxer's constant rate doubled it to 120."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cb-reencode-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _clip(self, name, size, seconds, rate=60):
        path = os.path.join(self.tmp, name)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                        '-i', f'testsrc=size={size}:rate={rate}', '-t', str(seconds),
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', str(rate),
                        '-pix_fmt', 'yuv420p', path], check=True, timeout=120)
        return path

    def _run(self, clip, offset, seconds):
        """The app's re-encode command with the GPU's three pieces swapped for the CPU's:
        no device, no upload, libx264 for h264_vaapi. Everything else - -reinit_filter:v 0, the
        filter chain, the rate and keyframe options, the window - is the app's own."""
        out = os.path.join(self.tmp, f'out-{offset}')
        os.makedirs(out)
        cmd = build_file_playback_cmd(_CFG, clip, out, offset=offset, reencode_device=_DEVICE)
        i = cmd.index('-vaapi_device')
        del cmd[i:i + 2]
        r = cmd.index('-readrate')
        del cmd[r:r + 4]
        cmd = [c.replace(',format=nv12,hwupload', '') for c in cmd]
        e = cmd.index('h264_vaapi')
        cmd[e:e + 3] = ['libx264', '-preset', 'ultrafast', '-qp', '24']
        cmd[cmd.index('-i'):cmd.index('-i')] = ['-t', str(seconds)]
        return out, subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    def test_a_source_that_changes_size_part_way_keeps_playing(self):
        """Measured in the GPU lab: with the deinterlacer ahead of the fit, a 720p stretch
        turning 1080p killed ffmpeg ("Failed to reallocate frame", exit 255)."""
        small = self._clip('small.ts', '1280x720', 4)
        big = self._clip('big.ts', '1920x1080', 4)
        listing = os.path.join(self.tmp, 'list.txt')
        with open(listing, 'w') as fh:
            fh.write(f"file '{small}'\nfile '{big}'\n")
        joined = os.path.join(self.tmp, 'joined.ts')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', listing,
                        '-c', 'copy', joined], check=True, timeout=60)
        out, proc = self._run(joined, 0, 8)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        segs = sorted(f for f in os.listdir(out) if f.endswith('.ts'))
        last = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                               'stream=width,height', '-of', 'csv=p=0',
                               os.path.join(out, segs[-1])],
                              capture_output=True, text=True, timeout=30).stdout.split()
        self.assertEqual(last[0], '1920,1080')

    def test_segments_start_on_their_keyframe_at_the_sources_rate(self):
        clip = self._clip('p60.ts', '192x108', 30)
        out, proc = self._run(clip, 20, 6)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        segs = sorted(f for f in os.listdir(out) if f.endswith('.ts'))
        self.assertGreaterEqual(len(segs), 3)
        for name in segs[:3]:
            frames = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                 'frame=pts_time,key_frame', '-of', 'csv=p=0', os.path.join(out, name)],
                capture_output=True, text=True, timeout=30).stdout.split()
            keys = [f for f in frames if f.startswith('1,')]
            with self.subTest(segment=name):
                self.assertEqual(len(frames), 120)
                self.assertEqual(len(keys), 1)
                self.assertEqual(frames[0], keys[0])
                self.assertGreaterEqual(float(keys[0].split(',')[1]), 20.0)


class ReencodeDeviceTests(unittest.TestCase):
    """When a re-encode may start, and the words when it may not (preview.reencode_device)."""

    def setUp(self):
        self.trial = mock.Mock(return_value=mock.Mock(ok=True, error=''))
        p = mock.patch('app.toolchain.check_gpu_encoder', self.trial)
        p.start()
        self.addCleanup(p.stop)

    def _cfg(self, device=_DEVICE, encoder='software'):
        return {'ffmpeg': {'path': 'ffmpeg'},
                'recording': {'post_process': {'vaapi_device': device, 'video_encoder': encoder}}}

    def _ask(self, cfg, exists=True, converting=()):
        import app.postprocessor as pp
        with mock.patch('app.preview.os.path.exists', return_value=exists), \
                mock.patch.dict(pp._active_conversions, {rid: None for rid in converting}):
            return preview.reencode_device(cfg)

    def test_no_device_node_says_so_and_never_runs_the_trial(self):
        device, why = self._ask(self._cfg(), exists=False)
        self.assertEqual(device, '')
        self.assertIn(f'no GPU device at {_DEVICE}', why)
        self.trial.assert_not_called()

    def test_a_conversion_comes_first_and_the_gpu_is_not_touched(self):
        device, why = self._ask(self._cfg(), converting=(42,))
        self.assertEqual((device, why), ('', preview.CONVERSION_FIRST))
        self.trial.assert_not_called()

    def test_a_failed_trial_quotes_ffmpeg(self):
        self.trial.return_value = mock.Mock(ok=False, error='Failed to initialise VAAPI connection')
        device, why = self._ask(self._cfg())
        self.assertEqual(device, '')
        self.assertIn('Failed to initialise VAAPI connection', why)

    def test_the_trial_never_reports_to_the_alert_or_readiness(self):
        """Both are per device for the conversions' codec: an H.264 pass here would clear the
        alert an HEVC failure raised (dev/changelog/1184)."""
        for encoder in ('software', 'vaapi'):
            self.assertEqual(self._ask(self._cfg(encoder=encoder)), (_DEVICE, ''))
            self.assertFalse(self.trial.call_args.kwargs['report'])
            self.assertEqual(self.trial.call_args.args[1:], (_DEVICE, 'h264'))


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class ReencodeSessionTests(_Live):
    """A re-encoding session end to end. The suite has no GPU, so the command builder is
    wrapped to run the stream copy while the session is still the re-encode the route asked
    for - everything here is about the session, not the encoder."""

    def setUp(self):
        super().setUp()
        self.built = []
        real_live, real_file = preview.build_preview_cmd, preview.build_file_playback_cmd

        def live(*a, **kw):
            self.built.append(kw.get('reencode_device'))
            return real_live(*a, **{**kw, 'reencode_device': None})

        def file(*a, **kw):
            self.built.append(kw.get('reencode_device'))
            return real_file(*a, **{**kw, 'reencode_device': None})
        for name, fn in (('build_preview_cmd', live), ('build_file_playback_cmd', file)):
            p = mock.patch.object(preview, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        self.allow = mock.patch.object(preview, 'reencode_device', return_value=(_DEVICE, ''))
        import app.postprocessor as pp
        self.conversions = pp._active_conversions
        self.addCleanup(self.conversions.clear)

    def _reencode(self):
        return self.client.post(f'/api/channels/{self.channel.id}/preview', json={'reencode': True})

    def test_a_refused_reencode_says_why_and_leaves_the_copy_playing(self):
        copy_sid = self._start().get_json()['session_id']
        with mock.patch.object(preview, 'reencode_device', return_value=('', 'No GPU here.')):
            resp = self._reencode()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json(), {'error': 'No GPU here.'})
        self.assertNotEqual(self._status(copy_sid)['state'], preview.STATE_STOPPED)
        self.assertEqual(preview.live_session().id, copy_sid)

    def test_a_reencode_runs_on_the_device_and_says_so(self):
        with self.allow:
            data = self._reencode().get_json()
        self.assertTrue(data['reencode'])
        self.assertEqual(self.built, [_DEVICE])
        self.assertTrue(self._status(data['session_id'])['reencode'])
        self.assertFalse(self._start().get_json()['reencode'])
        self.assertEqual(self.built, [_DEVICE, None])

    def test_a_conversion_starting_stops_a_reencode_by_name(self):
        with self.allow:
            sid = self._reencode().get_json()['session_id']
        self._wait_state(sid, preview.STATE_READY)
        self.conversions[99] = None
        self._wait_state(sid, preview.STATE_STOPPED, timeout=5)
        st = self._status(sid)
        self.assertEqual(st['reason'], preview.REASON_CONVERSION_STARTED)
        self.assertIn('conversion comes before', st['reason_text'])
        self.assertIn('Recording 99', st['detail'])
        self.assertEqual(self._holders(), [])

    def test_a_copy_keeps_playing_beside_a_conversion(self):
        sid = self._start().get_json()['session_id']
        self._wait_state(sid, preview.STATE_READY)
        self.conversions[99] = None
        time.sleep(1.5)
        self.assertEqual(self._status(sid)['state'], preview.STATE_READY)

    def test_the_availability_route_answers_in_the_servers_words(self):
        with mock.patch.object(preview, 'reencode_device', return_value=('', 'No GPU here.')):
            self.assertEqual(self.client.get('/api/preview/reencode').get_json(),
                             {'success': True, 'available': False, 'reason': 'No GPU here.'})
        with self.allow:
            self.assertEqual(self.client.get('/api/preview/reencode').get_json(),
                             {'success': True, 'available': True, 'reason': ''})

    def test_a_native_file_plays_reencoded_through_the_pipeline_only_when_asked(self):
        dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(dvr, exist_ok=True)
        path = os.path.join(dvr, 'Race day.mp4')
        shutil.copyfile(self.clip, path)
        rec = make_recording(status='COMPLETED', name='Race day', output_path=path,
                             recorded_video_codec='hevc', recorded_audio_codec='aac',
                             recorded_duration_seconds=self.CLIP_SECONDS)
        db.session.commit()
        url = f'/api/recordings/{rec.id}/playback'
        self.assertEqual(self.client.post(url, json={'offset': 0}).status_code, 409)
        with self.allow:
            resp = self.client.post(url, json={'offset': 0, 'reencode': True})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue(resp.get_json()['reencode'])
        self.assertEqual(self.built, [_DEVICE])
        self.client.post(resp.get_json()['stop_url'])
