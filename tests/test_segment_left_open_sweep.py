"""The startup sweep closes a segment left open on a recording whose capture is over
(app/recorder.py::close_segments_left_open_on_ended_recordings, dev/changelog/1374).

Guards dev/docs/BUGS.md 2026-10-31 08:20 PM: nothing closed such a row, so the detail page
would count it up as still capturing and the usage ledger's watermark stayed behind it.
"""
import logging
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, recorder  # noqa: E402
from app.database import RecordingEvent, RecordingSegment, SEGMENT_ENDED  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

T0 = datetime(2026, 9, 10, 15, 0, 0)


def _no_disk(*_a, **_k):
    raise AssertionError('the sweep read a finished recording\'s storage')


class _Fixture(unittest.TestCase):
    START_SCHEDULER = False

    def setUp(self):
        self.t = make_test_app(start_scheduler=self.START_SCHEDULER)
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc)
        self.ended = seed.make_recording(status='ABORTED', channel_id=self.ch.id)
        self.stuck = seed.make_segment(self.ended, self.ch, T0, None, 1)
        self.running = seed.make_recording(status='IN_PROGRESS', channel_id=self.ch.id,
                                           stop_time=datetime.utcnow() + timedelta(hours=1))
        self.live = seed.make_segment(self.running, self.ch, T0, None, 1)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()


class CloseSegmentsLeftOpenTests(_Fixture):

    def test_closes_it_at_its_own_start_without_reading_the_file(self):
        with mock.patch('app.recorder.os.path.exists', _no_disk), \
                mock.patch('app.recorder.os.path.getmtime', _no_disk), \
                mock.patch('app.recorder.os.path.getsize', _no_disk), \
                self.assertLogs('app.recorder', logging.WARNING) as logs:
            closed = recorder.close_segments_left_open_on_ended_recordings()
        self.assertEqual(closed, [(self.ended.id, 1)])
        db.session.expire_all()
        seg = db.session.get(RecordingSegment, self.stuck.id)
        self.assertEqual(seg.ended_at, seg.started_at)
        self.assertEqual(seg.exit_reason, 'LEFT_OPEN')
        event = RecordingEvent.query.filter_by(recording_id=self.ended.id,
                                               event_type=SEGMENT_ENDED).one()
        self.assertIn('When it really stopped is unknown', event.detail)
        self.assertTrue(any('left open after the recording ended' in m for m in logs.output))

    def test_leaves_a_running_recordings_segment_alone(self):
        recorder.close_segments_left_open_on_ended_recordings()
        db.session.expire_all()
        self.assertIsNone(db.session.get(RecordingSegment, self.live.id).ended_at)

    def test_the_detail_page_explains_the_pill(self):
        rid = self.ended.id
        recorder.close_segments_left_open_on_ended_recordings()
        db.session.remove()
        html = self.t.client.get(f'/recordings/{rid}').get_data(as_text=True)
        self.assertIn('LEFT_OPEN', html)
        self.assertIn('closed it at the next service restart', html)


class StartupSweepRunsItTests(_Fixture):
    """The sweep schedules jobs for the rows it resumes, so it needs the scheduler."""
    START_SCHEDULER = True

    def test_the_startup_sweep_runs_it(self):
        from app.scheduler import resume_in_progress_recordings
        with mock.patch('app.recorder.resume_recording'), \
                mock.patch('app.scheduler._register_stop_job'):
            resume_in_progress_recordings(self.t.app)
        db.session.expire_all()
        self.assertEqual(db.session.get(RecordingSegment, self.stuck.id).exit_reason,
                         'LEFT_OPEN')


if __name__ == '__main__':
    unittest.main()
