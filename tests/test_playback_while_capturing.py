"""Play on a recording that is still capturing (app/playback.py, app/routes/recordings.py,
dev/changelog/1316).

The promises the page makes, each a test here:

  * a capture's segments are one timeline laid end to end, as the join will lay them, and a
    position names one segment file and a place in it - or the live edge, or nothing
  * IN_PROGRESS, PAUSED and RETRYING each offer Play on purpose; there is nothing to download
    until the recording has finished, and video a browser cannot decode is refused by name
  * the start route follows the segment the recorder is writing only when the position is in
    it, reads which segment that is from the recorder's state rather than the newest file, and
    says why the recording moved in the recording's own words
  * a player that outlives the capture is told what became of it, never a bare refusal

ffmpeg is never started here: preview.start_file_playback is replaced by a recorder of its
arguments. tests/test_preview.py::FollowerTests runs the real follower.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_recording  # noqa: E402
from app import db, playback, preview  # noqa: E402
from app.database import (GROUP_FAILOVER, RecordingEvent, RecordingSegment,  # noqa: E402
                          SEGMENT_ENDED, STALL_DETECTED)

T0 = datetime(2026, 10, 27, 18, 0, 0)


def _seg(n, start, end, path='/dvr/x.ts'):
    return SimpleNamespace(segment_number=n, file_path=path, started_at=start, ended_at=end)


class TimelineTests(unittest.TestCase):
    """capture_timeline / resolve_capture_position are pure: no app, no file."""

    def setUp(self):
        # Two finished segments with a 30 s gap between them, then one being written.
        self.segs = [_seg(0, T0, T0 + timedelta(seconds=100)),
                     _seg(1, T0 + timedelta(seconds=130), T0 + timedelta(seconds=230)),
                     _seg(2, T0 + timedelta(seconds=240), None)]
        self.now = T0 + timedelta(seconds=300)
        self.spans = playback.capture_timeline(self.segs, self.now, growing_segment=2)

    def test_segments_lie_end_to_end_without_the_gaps(self):
        """The join runs the segments back to back, so the gap between them is not on the
        timeline - a position here is the same spot in the joined file."""
        self.assertEqual([(s.base, s.length) for s in self.spans],
                         [(0.0, 100.0), (100.0, 100.0), (200.0, 60.0)])
        self.assertEqual([s.growing for s in self.spans], [False, False, True])

    def test_a_position_names_its_segment_and_the_place_in_it(self):
        span, at, live = playback.resolve_capture_position(self.spans, 150)
        self.assertEqual((span.segment_number, at, live), (1, 50.0, False))
        span, at, live = playback.resolve_capture_position(self.spans, 100)
        self.assertEqual((span.segment_number, at), (1, 0.0))

    def test_near_or_past_the_live_edge_is_live(self):
        for pos in (255, 259.9, 260, 5000):
            with self.subTest(pos=pos):
                span, at, live = playback.resolve_capture_position(self.spans, pos)
                self.assertEqual((span.segment_number, at, live), (2, 0.0, True))
        span, at, live = playback.resolve_capture_position(self.spans, 220)
        self.assertEqual((span.segment_number, at, live), (2, 20.0, False))

    def test_past_everything_with_nothing_being_written_is_nothing(self):
        paused = playback.capture_timeline(self.segs[:2], self.now, growing_segment=None)
        self.assertIsNone(playback.resolve_capture_position(paused, 200))
        self.assertIsNone(playback.resolve_capture_position([], 0))

    def test_which_segment_is_growing_is_the_recorders_answer_not_the_newest_row(self):
        """A row still open that the recorder is not writing (left by an unclean stop) is not
        followed: the recorder's current segment number decides."""
        spans = playback.capture_timeline(self.segs, self.now, growing_segment=None)
        self.assertFalse(any(s.growing for s in spans))


class _Rig(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.client = self.t.client
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)

    def _rec(self, status='IN_PROGRESS', segments=(), vcodec='h264', acodec='aac'):
        """`segments`: (number, seconds_ago_started, seconds_ago_ended or None)."""
        rec = make_recording(status=status, name='Race day')
        now = datetime.utcnow()
        for n, began, ended in segments:
            path = os.path.join(self.dvr, f'race_{rec.id}_seg_{n:03d}.ts')
            with open(path, 'wb') as fh:
                fh.write(b'\0' * 188 * 10)
            db.session.add(RecordingSegment(
                recording_id=rec.id, segment_number=n, file_path=path,
                started_at=now - timedelta(seconds=began),
                ended_at=None if ended is None else now - timedelta(seconds=ended),
                probe_video_codec=vcodec, probe_audio_codec=acodec))
        db.session.commit()
        return rec


class OfferTests(_Rig):
    def test_every_capture_window_status_offers_play_and_no_download(self):
        for status in ('IN_PROGRESS', 'PAUSED', 'RETRYING'):
            with self.subTest(status=status):
                o = playback.offer(self._rec(status=status), capture_video_codec='h264')
                self.assertTrue(o.play and o.capture)
                self.assertFalse(o.download or o.native or o.pipeline)

    def test_video_a_browser_cannot_decode_is_refused_by_name(self):
        o = playback.offer(self._rec(), capture_video_codec='mpeg2video')
        self.assertTrue(o.play)
        self.assertFalse(o.capture)
        self.assertIn('mpeg2video', o.reason)

    def test_hevc_carries_its_caveat(self):
        self.assertIn('HEVC', playback.offer(self._rec(), capture_video_codec='hevc').caveat)

    def test_joining_and_converting_still_offer_nothing(self):
        for status in ('CONCATENATING', 'ANALYZING', 'CONVERTING', 'SCHEDULED'):
            with self.subTest(status=status):
                self.assertEqual(playback.offer(self._rec(status=status)), playback.Offer())


class PageTests(_Rig):
    def _page(self, rec):
        resp = self.client.get(f'/recordings/{rec.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    @staticmethod
    def _bar(html):
        start = html.index('<div class="mobile-actbar">')
        bar = html[start:html.index('<div id="status-strip-slot">', start)]
        menu_at = bar.index('<div class="menu-wrap">')
        return bar[:menu_at], bar[menu_at:]

    def test_live_header_offers_play_and_the_phone_kebab_leads_with_it(self):
        """The phone bar's two slots stay Pause/Resume and Stop (DESIGN.md 9.5)."""
        for status in ('IN_PROGRESS', 'PAUSED', 'RETRYING'):
            with self.subTest(status=status):
                html = self._page(self._rec(status=status, segments=[(0, 60, None)]))
                head = html[html.index('<div class="dh-actions">'):html.index('<div class="mobile-actbar">')]
                self.assertIn('data-act="play"', head)
                self.assertNotIn('data-act="download"', html)
                bar, kebab = self._bar(html)
                self.assertNotIn('data-act="play"', bar)
                self.assertLess(kebab.index('data-act="play"'), kebab.index('class="sep"'))

    def test_mpeg2_capture_still_shows_play_which_says_why(self):
        html = self._page(self._rec(segments=[(0, 60, None)], vcodec='mpeg2video'))
        self.assertIn('data-act="play"', html)


class CheckRouteTests(_Rig):
    def test_check_answers_from_the_rows_with_no_file_to_wake(self):
        rec = self._rec(segments=[(0, 60, None)])
        with mock.patch('app.playback.open_for_playback',
                        side_effect=AssertionError('a capture has no finished file')):
            data = self.client.get(f'/recordings/{rec.id}/file/check').get_json()
        self.assertTrue(data['capture'])
        self.assertFalse(data['download'] or data['native'] or data['pipeline'])
        self.assertFalse(data['live'])            # no recorder state: nothing is being written
        self.assertEqual(data['playback_url'], f'/api/recordings/{rec.id}/playback')
        with mock.patch('app.routes.recordings.get_state', return_value=SimpleNamespace()):
            self.assertTrue(self.client.get(f'/recordings/{rec.id}/file/check').get_json()['live'])

    def test_a_finished_recordings_check_says_there_is_a_download(self):
        path = os.path.join(self.dvr, 'done.mp4')
        with open(path, 'wb') as fh:
            fh.write(b'x')
        rec = make_recording(status='COMPLETED', name='Done', output_path=path,
                             recorded_video_codec='h264', recorded_audio_codec='aac')
        db.session.commit()
        data = self.client.get(f'/recordings/{rec.id}/file/check').get_json()
        self.assertTrue(data['download'])
        self.assertFalse(data['capture'])


class StartRouteTests(_Rig):
    def setUp(self):
        super().setUp()
        self.calls = []

        def fake_start(rid, name, path, offset, transcode_audio, capture=None):
            self.calls.append(dict(path=path, offset=offset, capture=capture,
                                   transcode_audio=transcode_audio))
            return preview.PreviewSession(
                id='sid', channel_id=None, channel_name='', account_id=None, dir='',
                stderr_path='', transcode_audio=transcode_audio, idle_timeout=15,
                max_seconds=None, connect_timeout=20, source=preview.SOURCE_FILE,
                recording_id=rid, offset=offset, capture=capture,
                following=bool(capture and capture.follow_state is not None))
        patcher = mock.patch.object(preview, 'start_file_playback', side_effect=fake_start)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _play(self, rec, body, state=None):
        with mock.patch('app.routes.recordings.get_state', return_value=state):
            resp = self.client.post(f'/api/recordings/{rec.id}/playback', json=body)
        return resp.status_code, resp.get_json()

    def _capturing(self):
        # Segment 0: 100 s, finished. Segment 1: written for the last 60 s.
        rec = self._rec(segments=[(0, 200, 100), (1, 60, None)])
        return rec, SimpleNamespace(current_segment_num=1)

    def test_live_follows_the_segment_the_recorder_is_writing(self):
        rec, state = self._capturing()
        code, data = self._play(rec, {'live': True}, state)
        self.assertEqual(code, 200, data)
        call = self.calls[-1]
        self.assertTrue(call['path'].endswith('_seg_001.ts'))
        self.assertIs(call['capture'].follow_state, state)
        # A few seconds before what the growing segment holds so far (60 s of it).
        self.assertAlmostEqual(call['offset'], 60 - playback.FOLLOW_FROM_END_SECONDS, delta=1)
        self.assertTrue(call['capture'].live)
        self.assertAlmostEqual(call['capture'].base, 100.0, delta=1)
        self.assertTrue(data['following'] and data['capturing'])
        self.assertAlmostEqual(data['captured_seconds'], 160.0, delta=2)
        self.assertEqual(data['capture_note'], '')

    def test_a_position_in_a_finished_segment_plays_it_and_ends_with_it(self):
        rec, state = self._capturing()
        code, data = self._play(rec, {'offset': 30}, state)
        self.assertEqual(code, 200, data)
        call = self.calls[-1]
        self.assertTrue(call['path'].endswith('_seg_000.ts'))
        self.assertEqual(call['offset'], 30.0)
        self.assertIsNone(call['capture'].follow_state)
        self.assertAlmostEqual(data['capture']['span_end'], 100.0, delta=1)
        self.assertFalse(data['following'])

    def test_behind_live_in_the_growing_segment_follows_from_the_offset(self):
        rec, state = self._capturing()
        code, data = self._play(rec, {'offset': 110}, state)
        self.assertEqual(code, 200, data)
        call = self.calls[-1]
        self.assertTrue(call['path'].endswith('_seg_001.ts'))
        self.assertIs(call['capture'].follow_state, state)
        self.assertAlmostEqual(call['offset'], 10.0, delta=1)
        self.assertFalse(call['capture'].live)

    def test_not_yet_answers_409_so_the_player_asks_again(self):
        rec = self._rec(segments=[])
        code, data = self._play(rec, {'live': True}, SimpleNamespace(current_segment_num=0))
        self.assertEqual((code, data['error']), (409, 'Nothing has been captured yet.'))
        # The recorder has moved to segment 2, which has written nothing yet.
        rec, _ = self._capturing()
        code, data = self._play(rec, {'live': True}, SimpleNamespace(current_segment_num=2))
        self.assertEqual(code, 409, data)
        self.assertIn('starting segment 2', data['error'])
        self.assertEqual(self.calls, [])

    def test_paused_plays_what_was_captured_and_says_so(self):
        rec = self._rec(status='PAUSED', segments=[(0, 200, 100), (1, 60, 30)])
        code, data = self._play(rec, {'offset': 400})
        self.assertEqual(code, 400, data)
        self.assertIn('past what has been captured so far', data['error'])
        code, data = self._play(rec, {'live': True})
        self.assertEqual(code, 200, data)
        call = self.calls[-1]
        self.assertTrue(call['path'].endswith('_seg_001.ts'))
        self.assertIsNone(call['capture'].follow_state)
        # "Live" with nothing being written is the last few seconds captured.
        self.assertAlmostEqual(call['offset'], 30 - playback.LIVE_SNAP_SECONDS, delta=1)
        self.assertIn('paused', data['capture_note'])
        rec = self._rec(status='RETRYING', segments=[(0, 200, 100)])
        self.assertIn('dropped', self._play(rec, {'offset': 0})[1]['capture_note'])

    def test_audio_a_browser_lacks_is_re_encoded_from_the_segments_probe(self):
        rec = self._rec(segments=[(0, 60, None)], acodec='ac3')
        self._play(rec, {'live': True}, SimpleNamespace(current_segment_num=0))
        self.assertTrue(self.calls[-1]['transcode_audio'])

    def test_the_move_is_explained_in_the_recordings_own_events(self):
        rec, state = self._capturing()
        seg0 = RecordingSegment.query.filter_by(recording_id=rec.id, segment_number=0).one()
        seg1 = RecordingSegment.query.filter_by(recording_id=rec.id, segment_number=1).one()
        at = seg0.ended_at
        for kind, detail, num, when in (
                (STALL_DETECTED, 'File stalled at 6316621824 bytes for 10.0s', 0, at),
                (GROUP_FAILOVER, 'Group "Cup": feed "A" died - failing over to "B"', None, at),
                (SEGMENT_ENDED, 'Segment 0 ended: STALLED', 0, at),
                # After the new segment started: not part of why segment 0 ended.
                (GROUP_FAILOVER, 'Group "Cup": later move', None, seg1.started_at + timedelta(seconds=5))):
            db.session.add(RecordingEvent(recording_id=rec.id, event_type=kind, detail=detail,
                                          segment_number=num, timestamp=when))
        db.session.commit()
        code, data = self._play(rec, {'live': True, 'moved_from': 0}, state)
        self.assertEqual(code, 200, data)
        self.assertEqual(data['moved_because'],
                         'File stalled at 6316621824 bytes for 10.0s. Group "Cup": feed "A" '
                         'died - failing over to "B". Segment 0 ended: STALLED.')
        self.assertEqual(self._play(rec, {'live': True}, state)[1]['moved_because'], '')

    def test_a_capture_that_finished_under_the_player_says_what_it_is_now(self):
        for status in ('CONCATENATING', 'ANALYZING', 'CONVERTING'):
            with self.subTest(status=status):
                code, data = self._play(self._rec(status=status), {'offset': 10})
                self.assertEqual(code, 409)
                self.assertIn('final file is being made', data['error'])

    def test_a_segment_outside_the_recording_folders_is_not_played(self):
        rec, state = self._capturing()
        seg = RecordingSegment.query.filter_by(recording_id=rec.id, segment_number=1).one()
        outside = os.path.join(self.t._tmpdir, 'elsewhere', 'x.ts')
        os.makedirs(os.path.dirname(outside))
        with open(outside, 'wb') as fh:
            fh.write(b'\0' * 188)
        seg.file_path = outside
        db.session.commit()
        code, data = self._play(rec, {'live': True}, state)
        self.assertEqual(code, 403, data)
        self.assertEqual(self.calls, [])

    def test_mpeg2_capture_is_refused(self):
        rec = self._rec(segments=[(0, 60, None)], vcodec='mpeg2video')
        code, data = self._play(rec, {'live': True}, SimpleNamespace(current_segment_num=0))
        self.assertEqual(code, 409)
        self.assertIn('mpeg2video', data['error'])


if __name__ == '__main__':
    unittest.main()
