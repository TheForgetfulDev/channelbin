"""Tier 2 - a recording's picture and sound are compared where they end. Guards dev/docs/BUGS.md
2026-11-03: recording 32 played with its sound 1:52 ahead of its picture, passed every check,
scored 99 and had its source deleted as clean.

Real ffmpeg on small synthesized local files; the read is the same tail read a capture gets.
"""
import json
import os
from datetime import datetime
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.concatenator import _note_segment_track_ends  # noqa: E402
from app.database import (Alert, CAPTURE_VIDEO_RESUMED, CAPTURE_VIDEO_STOPPED,  # noqa: E402
                          DIAGNOSTICS, Recording, RecordingEvent, RecordingSegment,
                          SEGMENT_EXIT_VIDEO_STOPPED, SOURCE_DELETED, SOURCE_KEPT,
                          add_recording_event)
from app.health_score import score_capture_quality_correction  # noqa: E402
from app.postprocessor import SOURCE_KEPT_OUT_OF_STEP, _dispose_of_source  # noqa: E402
from app.track_ends import note_track_ends, read_track_ends  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))
_CLEAN_CHECK = {'lost_seconds': 0.0}


def _make_ts(path, video_seconds, audio_seconds):
    subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
         '-f', 'lavfi', '-i', f'testsrc2=s=160x90:r=10:d={video_seconds}',
         '-f', 'lavfi', '-i', f'sine=f=440:r=48000:d={audio_seconds}',
         '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '10', '-c:a', 'aac', '-f', 'mpegts',
         path], check=True, timeout=120)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not on PATH')
class TrackEndsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.apart = os.path.join(cls.dir, 'apart.ts')
        cls.together = os.path.join(cls.dir, 'together.ts')
        _make_ts(cls.apart, 20, 12)
        _make_ts(cls.together, 12, 12)
        cls.sound_on = os.path.join(cls.dir, 'sound_on.ts')
        _make_ts(cls.sound_on, 12, 20)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.rec = seed.make_recording(status='COMPLETED', name='Race')
        db.session.commit()
        self.rid = self.rec.id

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _row(self):
        db.session.expire_all()
        return db.session.get(Recording, self.rid)

    def _alerts(self):
        return Alert.query.filter_by(alert_type='RECORDING_OUT_OF_STEP').all()

    def _track_events(self):
        rows = RecordingEvent.query.filter_by(recording_id=self.rid, event_type=DIAGNOSTICS).all()
        return [r for r in rows if json.loads(r.extra_data or '{}').get('kind') == 'track_ends']

    def test_a_file_whose_tracks_end_apart_reads_as_apart(self):
        video, audio = read_track_ends(self.apart)
        self.assertGreater(video - audio, 7)

    def test_out_of_step_is_recorded_said_and_alerted(self):
        note_track_ends(self.rid, [(1, self.together), (2, self.apart)], 'segments')
        rec = self._row()
        self.assertGreater(rec.track_end_gap_seconds, 7)
        self.assertEqual(rec.picture_sound_apart_seconds, rec.track_end_gap_seconds)
        events = self._track_events()
        self.assertEqual(len(events), 1)
        self.assertIn('segment 2', events[0].detail)
        self.assertIn('the video runs on', events[0].detail)
        self.assertEqual(len(self._alerts()), 1)

    def test_a_clean_recording_gets_its_answer_and_no_alert(self):
        note_track_ends(self.rid, [(None, self.together)], 'converted file')
        rec = self._row()
        self.assertIsNotNone(rec.track_end_gap_seconds)
        self.assertLess(rec.track_end_gap_seconds, 2)
        self.assertIsNone(rec.picture_sound_apart_seconds)
        self.assertIn('end together', self._track_events()[0].detail)
        self.assertEqual(self._alerts(), [])

    def test_the_alert_fires_once_and_a_later_clean_stage_does_not_undo_it(self):
        note_track_ends(self.rid, [(1, self.apart)], 'capture')
        worst = self._row().track_end_gap_seconds
        note_track_ends(self.rid, [(None, self.apart)], 'converted file')
        note_track_ends(self.rid, [(None, self.together)], 'converted file')
        self.assertEqual(self._row().track_end_gap_seconds, worst)
        self.assertEqual(len(self._alerts()), 1)

    def _segment(self, number, exit_reason=None):
        db.session.add(RecordingSegment(recording_id=self.rid, segment_number=number,
                                        file_path=f'/nowhere/{number}.ts',
                                        started_at=datetime.utcnow(),
                                        exit_reason=exit_reason))
        db.session.commit()

    def _video_event(self, number, event_type):
        add_recording_event(self.rid, event_type, detail='x', segment_number=number)
        db.session.commit()

    def test_a_restart_for_a_stopped_picture_is_not_out_of_step(self):
        """dev/docs/BUGS.md 2026-11-16: the watchdog closes a segment once its picture has been
        gone about 35 s under its sound, so that segment's tracks end apart by construction,
        and the recording was alerted on, kept its source and cost its channel 80 points."""
        self._segment(1, SEGMENT_EXIT_VIDEO_STOPPED)
        self._segment(2)
        note_track_ends(self.rid, [(1, self.sound_on), (2, self.together)], 'segments')
        rec = self._row()
        self.assertLess(rec.track_end_gap_seconds, 2)
        self.assertIsNone(rec.picture_sound_apart_seconds)
        self.assertEqual(self._alerts(), [])
        event = self._track_events()[0]
        self.assertIn('Segment 1 ends with', event.detail)
        self.assertIn('of sound and no picture', event.detail)
        self.assertIn('Everywhere else the picture and sound end together', event.detail)
        self.assertNotIn('Expect them out of step', event.detail)
        ends = json.loads(event.extra_data)['ends']
        self.assertEqual([e.get('sound_only', False) for e in ends], [True, False])
        self.assertGreater(ends[0]['audio'] - ends[0]['video'], 7)
        quality, _ = score_capture_quality_correction(
            0, 0, 3600, {}, picture_sound_apart_seconds=rec.picture_sound_apart_seconds)
        self.assertEqual(quality, 100)

    def test_a_capture_left_on_its_sound_is_not_out_of_step_in_any_file(self):
        """dev/docs/BUGS.md 2026-11-16: with no restart left the capture stays for the sound,
        and its segment, the joined file and the converted file all end sound past picture."""
        self._segment(1)
        self._video_event(1, CAPTURE_VIDEO_STOPPED)
        note_track_ends(self.rid, [(1, self.sound_on)], 'capture')
        note_track_ends(self.rid, [(None, self.sound_on)], 'converted file')
        self.assertIsNone(self._row().picture_sound_apart_seconds)
        self.assertEqual(self._alerts(), [])
        self.assertIn('The converted file ends with', self._track_events()[1].detail)

    def test_a_picture_that_came_back_on_the_segment_explains_nothing(self):
        self._segment(1)
        self._video_event(1, CAPTURE_VIDEO_STOPPED)
        self._video_event(1, CAPTURE_VIDEO_RESUMED)
        note_track_ends(self.rid, [(1, self.sound_on)], 'capture')
        self.assertGreater(self._row().picture_sound_apart_seconds, 7)
        self.assertEqual(len(self._alerts()), 1)

    def test_a_picture_running_past_its_sound_is_never_explained_by_a_video_stop(self):
        self._segment(1, SEGMENT_EXIT_VIDEO_STOPPED)
        note_track_ends(self.rid, [(1, self.apart)], 'capture')
        self.assertGreater(self._row().picture_sound_apart_seconds, 7)
        self.assertEqual(len(self._alerts()), 1)

    def test_another_segment_out_of_step_is_still_said_beside_a_video_stop(self):
        self._segment(1, SEGMENT_EXIT_VIDEO_STOPPED)
        self._segment(2)
        note_track_ends(self.rid, [(1, self.sound_on), (2, self.apart)], 'segments')
        self.assertGreater(self._row().picture_sound_apart_seconds, 7)
        detail = self._track_events()[0].detail
        self.assertIn('Segment 1 ends with', detail)
        self.assertIn('apart in segment 2', detail)
        self.assertEqual(len(self._alerts()), 1)

    def test_an_unreadable_file_says_so_and_leaves_the_measure_empty(self):
        note_track_ends(self.rid, [(1, os.path.join(self.dir, 'missing.ts'))], 'capture')
        self.assertIsNone(self._row().track_end_gap_seconds)
        self.assertIn('Could not compare', self._track_events()[0].detail)

    def test_the_join_survives_the_measurement_failing(self):
        seg = mock.Mock(segment_number=1, file_path=self.apart)
        with mock.patch('app.track_ends.note_track_ends', side_effect=RuntimeError('boom')):
            _note_segment_track_ends(self.rid, [seg])

    def _source(self):
        fd, path = tempfile.mkstemp(suffix='.ts', dir=self.dir)
        os.close(fd)
        return path

    def test_the_source_is_kept_when_the_picture_and_sound_are_apart(self):
        self._row().track_end_gap_seconds = 112.05
        db.session.commit()
        path = self._source()
        _dispose_of_source(self.rid, path, _CLEAN_CHECK, True)
        self.assertTrue(os.path.exists(path))
        ev = RecordingEvent.query.filter_by(recording_id=self.rid, event_type=SOURCE_KEPT).one()
        self.assertEqual(json.loads(ev.extra_data)['reason'], SOURCE_KEPT_OUT_OF_STEP)
        self.assertIn('apart', ev.detail)

    def test_the_source_still_goes_when_they_end_together(self):
        self._row().track_end_gap_seconds = 0.14
        db.session.commit()
        path = self._source()
        _dispose_of_source(self.rid, path, _CLEAN_CHECK, True)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=SOURCE_DELETED).count(), 1)

    def test_the_recording_page_says_it(self):
        self._row().track_end_gap_seconds = 112.05
        db.session.commit()
        html = self.t.client.get(f'/recordings/{self.rid}').get_data(as_text=True)
        self.assertIn('the picture and sound end', html)

    def test_the_page_stays_quiet_on_a_clean_recording(self):
        self._row().track_end_gap_seconds = 0.14
        db.session.commit()
        html = self.t.client.get(f'/recordings/{self.rid}').get_data(as_text=True)
        self.assertNotIn('the picture and sound end', html)


if __name__ == '__main__':
    unittest.main()
