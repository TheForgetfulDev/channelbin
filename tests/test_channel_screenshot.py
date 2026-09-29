"""Channel page "Capture screenshot" (app/preview.py::capture_frame,
app/routes/preview.py::capture_channel_screenshot, dev/changelog/1160).

A screenshot is a preview that keeps one frame: it reads the preview already playing when
there is one, and otherwise opens a capture session through the preview's own start path.
The lifecycle cases run real ffmpeg against a local clip (never a URL - netguard), so what
they prove is the real teardown: the slot is free, the session is stopped and named, the
directory is gone, and nothing reached the health score.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.test_preview import _Live, _HAVE_FFMPEG  # noqa: E402

import app.connection_limits as connlim  # noqa: E402
import app.preview as preview  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import Channel, ChannelTest  # noqa: E402
from app.storage_dirs import manual_screenshot_path  # noqa: E402

JPEG_MAGIC = b'\xff\xd8\xff'


def _shot_path(channel_id):
    return manual_screenshot_path(load_config(), channel_id)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class CaptureTests(_Live):
    def setUp(self):
        super().setUp()
        # One-second segments: the clip carries a keyframe every second, so a capture has
        # its first segment in about half the time, and every case here waits for one.
        patcher = self._config_with(segment_seconds=1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _capture(self, channel=None):
        return self.client.post(f'/api/channels/{(channel or self.channel).id}/screenshot')

    def _column(self, channel=None):
        db.session.expire_all()
        return db.session.get(Channel, (channel or self.channel).id).screenshot_captured_at

    def test_capture_without_a_preview_saves_a_frame_and_releases_everything(self):
        resp = self._capture()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        data = resp.get_json()
        self.assertEqual(data['source'], 'capture')
        path = _shot_path(self.channel.id)
        with open(path, 'rb') as fh:
            self.assertEqual(fh.read(3), JPEG_MAGIC)
        self.assertIn(os.path.basename(path), data['url'])
        self.assertIsNotNone(self._column())
        # The capture session ran through the preview machinery and ended as a capture.
        sessions = [s for s in preview._sessions.values() if s.purpose == preview.PURPOSE_CAPTURE]
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session.reason, preview.REASON_CAPTURED)
        self.assertIsNotNone(session.proc.poll(), 'ffmpeg must be dead after a capture')
        self.assertFalse(os.path.exists(session.dir), 'segment directory must be removed')
        self.assertEqual(self._holders(), [])
        self.assertIsNone(preview.live_session(None))
        # The file is served where the returned URL says.
        self.assertEqual(self.client.get(data['url']).status_code, 200)

    def test_a_capture_is_not_a_health_observation(self):
        self.assertEqual(self._capture().status_code, 200)
        db.session.expire_all()
        ch = db.session.get(Channel, self.channel.id)
        self.assertEqual(ChannelTest.query.filter_by(channel_id=ch.id).count(), 0)
        self.assertIsNone(ch.health_score)
        self.assertEqual(ch.health_score_sample_count, 0)

    def test_a_playing_preview_of_the_channel_is_read_without_a_new_connection(self):
        """max_connections=1 and the preview holds it: a second connection would be refused,
        so a 200 here can only have come from the preview's own segments."""
        sid = self._start().get_json()['session_id']
        self._wait_state(sid, preview.STATE_READY)
        resp = self._capture()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()['source'], 'preview')
        self.assertTrue(preview.get_session(sid).live, 'the preview being watched keeps playing')
        self.assertEqual(self._holders(), [('preview', sid)])
        self.assertFalse(any(s.purpose == preview.PURPOSE_CAPTURE
                             for s in preview._sessions.values()))
        self.assertTrue(os.path.exists(_shot_path(self.channel.id)))

    def test_a_capture_never_stops_the_preview_being_watched_on_another_channel(self):
        self.account.max_connections = 2
        other = self._clip_channel('Other')
        db.session.commit()
        sid = self._start(other).get_json()['session_id']
        self._wait_state(sid, preview.STATE_READY)
        resp = self._capture()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()['source'], 'capture')
        self.assertTrue(preview.get_session(sid).live)
        self.assertEqual(self._holders(), [('preview', sid)])

    def test_refused_at_the_connection_limit_naming_the_holder(self):
        self.assertTrue(connlim.try_acquire(self.account.id, 'recording', 4242))
        self.addCleanup(connlim.release, self.account.id, 'recording', 4242)
        resp = self._capture()
        self.assertEqual(resp.status_code, 409)
        self.assertIn('a recording', resp.get_json()['error'])
        self.assertFalse(os.path.exists(_shot_path(self.channel.id)))
        self.assertIsNone(self._column())
        self.assertEqual(self._holders(), [('recording', 4242)])

    def test_a_failed_capture_says_why_and_keeps_the_previous_frame(self):
        path = _shot_path(self.channel.id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(b'previous frame')
        self.channel.stream_url = os.path.join(self.tmp, 'does-not-exist.ts')
        db.session.commit()
        resp = self._capture()
        self.assertEqual(resp.status_code, 502)
        self.assertIn('does-not-exist', resp.get_json()['error'])
        with open(path, 'rb') as fh:
            self.assertEqual(fh.read(), b'previous frame')
        self.assertIsNone(self._column())
        self.assertEqual(self._holders(), [])
        self.assertFalse(os.path.exists(f'{path}.part'))

    def test_a_recording_preempts_a_capture_and_takes_its_slot(self):
        from app.recorder import _try_acquire_slot_with_preemption
        real_launch = preview._launch

        def launch_then_recording_arrives(channel_id, purpose):
            session = real_launch(channel_id, purpose)
            self.assertTrue(_try_acquire_slot_with_preemption(self.t.app, 77, self.account.id))
            return session

        self.addCleanup(connlim.release, self.account.id, 'recording', 77)
        with mock.patch.object(preview, '_launch', side_effect=launch_then_recording_arrives):
            resp = self._capture()
        self.assertEqual(resp.status_code, 502)
        self.assertIn('recording', resp.get_json()['error'])
        session = next(s for s in preview._sessions.values()
                       if s.purpose == preview.PURPOSE_CAPTURE)
        self.assertEqual(session.reason, preview.REASON_PREEMPTED)
        self.assertIsNotNone(session.proc.poll())
        self.assertEqual(self._holders(), [('recording', 77)])
        self.assertIsNone(self._column())

    def test_the_slot_is_free_before_the_frame_is_decoded(self):
        """The connection is done with once a segment is on local disk."""
        from app.probe import parse_ffprobe as original
        seen = []

        def probe_and_look(*args, **kwargs):
            seen.append(list(self._holders()))
            return original(*args, **kwargs)

        # capture_frame imports it at call time, so the patch lands at its home module.
        with mock.patch('app.probe.parse_ffprobe', side_effect=probe_and_look):
            self.assertEqual(self._capture().status_code, 200)
        self.assertEqual(seen, [[]])

    def test_unknown_channel_is_404(self):
        resp = self.client.post('/api/channels/999999/screenshot')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.get_json())


class ChannelPageTileTests(unittest.TestCase):
    """The Health card's tile shows whichever screenshot is newer and says which."""

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        self.account = seed.make_account('Acct')
        self.channel = seed.make_channel(self.account, name='Chan')
        db.session.commit()

    def _page(self):
        resp = self.t.client.get(f'/channels/{self.channel.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_both_capture_buttons_render(self):
        html = self._page()
        self.assertIn('data-act="screenshot"', html)
        self.assertEqual(html.count('>Capture screenshot</button>'), 2)   # action bar + kebab
        self.assertIn('Grabs a single frame from the live stream right now', html)

    def test_a_never_tested_channel_shows_its_hand_capture(self):
        self.assertNotIn('captured by hand', self._page())
        self.channel.screenshot_captured_at = datetime.utcnow()
        db.session.commit()
        html = self._page()
        self.assertIn('captured by hand', html)
        self.assertIn(os.path.basename(manual_screenshot_path(load_config(), self.channel.id)), html)

    def test_the_newer_of_capture_and_test_wins_the_tile(self):
        now = datetime.utcnow()
        seed.make_channel_test(self.channel, status='COMPLETED',
                               test_started_at=now - timedelta(hours=1))
        self.channel.screenshot_captured_at = now
        db.session.commit()
        self.assertIn('captured by hand', self._page())
        seed.make_channel_test(self.channel, status='COMPLETED',
                               test_started_at=now + timedelta(minutes=5))
        db.session.commit()
        html = self._page()
        self.assertNotIn('captured by hand', html)
        self.assertIn('during the last test', html)


class DeleteTeardownTests(unittest.TestCase):
    """Deleting a channel, or the account that owns it, removes its screenshot files."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.addCleanup(self.t.cleanup)
        self.account = seed.make_account('Acct')
        db.session.commit()

    def _hand_capture(self, channel):
        path = manual_screenshot_path(load_config(), channel.id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(b'frame')
        channel.screenshot_captured_at = datetime.utcnow()
        return path

    def test_missing_channel_delete_removes_the_hand_capture(self):
        ch = seed.make_channel(self.account, name='Gone')
        ch.last_seen_at = datetime.utcnow() - timedelta(days=30)
        self.account.last_sync_at = datetime.utcnow() - timedelta(days=1)
        path = self._hand_capture(ch)
        db.session.commit()
        resp = self.t.client.post('/channels/missing-delete', json={'account_id': self.account.id})
        self.assertEqual(resp.get_json()['deleted_count'], 1, resp.get_json())
        self.assertFalse(os.path.exists(path))

    def test_account_delete_removes_test_screenshots_and_the_hand_capture(self):
        """dev/docs/BUGS.md 2026-09-29 08:21: the cascade took the ChannelTest rows and left
        their screenshot files on disk."""
        ch = seed.make_channel(self.account, name='Chan')
        db.session.commit()
        test_shot = os.path.join(self.t._tmpdir, 'test-shot.jpg')
        with open(test_shot, 'wb') as fh:
            fh.write(b'test frame')
        seed.make_channel_test(ch, screenshot_path=test_shot)
        hand = self._hand_capture(ch)
        db.session.commit()
        resp = self.t.client.delete(f'/api/accounts/{self.account.id}')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse(os.path.exists(test_shot), 'test screenshot left on disk')
        self.assertFalse(os.path.exists(hand), 'hand capture left on disk')


if __name__ == '__main__':
    unittest.main()
