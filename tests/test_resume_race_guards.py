"""Guards dev/docs/BUGS.md 2026-08-14 01:42:40 PM "resume_recording() can steal a segment
from a process that is still actively writing to it".

Two independent, non-exclusive guards:
  * app/recorder.py::resume_recording() - refuses to close the open segment or launch a
    second capture when that segment's file is still growing on disk (another process is
    almost certainly recording it already).
  * app/scheduler.py::init_scheduler() - refuses to start its own scheduler/startup-recovery
    pass when a pidfile shows another live process already owns this database.

No real ffmpeg and no second real OS process: the growth probe is exercised by patching the
two os.path.getsize samples it takes, and the pidfile guard's liveness check is exercised by
patching os.kill.
  python3 -m unittest tests.test_resume_race_guards
"""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

from app import db  # noqa: E402
from app import recorder  # noqa: E402
from app import scheduler  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingSegment, RecordingEvent, Alert, RECORDING_RESUME_REFUSED,
)


class SegmentFileIsGrowingTests(unittest.TestCase):
    """Pure unit tests for the read-only two-sample probe."""

    def test_missing_path_is_not_growing(self):
        self.assertFalse(recorder._segment_file_is_growing(None))
        self.assertFalse(recorder._segment_file_is_growing('/nonexistent/path/x.ts'))

    def test_growth_between_samples_is_detected(self):
        with mock.patch('app.recorder.os.path.getsize', side_effect=[100, 500]), \
             mock.patch('app.recorder.time.sleep') as sleep_mock:
            self.assertTrue(recorder._segment_file_is_growing('/fake/path.ts'))
        sleep_mock.assert_called_once()

    def test_no_growth_between_samples_is_not_growing(self):
        with mock.patch('app.recorder.os.path.getsize', side_effect=[500, 500]), \
             mock.patch('app.recorder.time.sleep'):
            self.assertFalse(recorder._segment_file_is_growing('/fake/path.ts'))


class ResumeRecordingLivenessGuardTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)

    def _seed_open_segment(self, status='IN_PROGRESS'):
        rec = seed.make_recording(status=status, name='Live One')
        db.session.flush()
        seg = RecordingSegment(
            recording_id=rec.id, segment_number=1,
            file_path=f'/fake/{rec.id}_seg_001.ts',
            started_at=datetime.utcnow() - timedelta(minutes=5), ended_at=None)
        db.session.add(seg)
        db.session.commit()
        return rec.id, seg.segment_number

    def test_refuses_to_resume_when_the_open_segment_is_still_growing(self):
        """The incident case: a second live process's resume must not steal the segment."""
        rid, segnum = self._seed_open_segment()

        with mock.patch.object(recorder, '_segment_file_is_growing', return_value=True), \
             mock.patch.object(recorder, '_launch_segment') as launch_mock:
            recorder.resume_recording(self.t.app, rid)

        launch_mock.assert_not_called()
        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        seg = RecordingSegment.query.filter_by(recording_id=rid, segment_number=segnum).one()
        self.assertEqual(rec.status, 'IN_PROGRESS')
        self.assertIsNone(seg.ended_at, 'the open segment must not be closed when a live writer is detected')
        self.assertGreaterEqual(
            RecordingEvent.query.filter_by(
                recording_id=rid, event_type=RECORDING_RESUME_REFUSED).count(), 1)
        self.assertGreaterEqual(
            Alert.query.filter_by(alert_type='RECORDING_RESUME_REFUSED').count(), 1)

    def test_refuses_before_flipping_status_from_paused(self):
        """Proves the check runs before the PAUSED/RETRYING status transition, not just
        before the segment-close step - a growing segment must block the whole resume."""
        rid, _ = self._seed_open_segment(status='PAUSED')

        with mock.patch.object(recorder, '_segment_file_is_growing', return_value=True), \
             mock.patch.object(recorder, '_launch_segment') as launch_mock:
            recorder.resume_recording(self.t.app, rid)

        launch_mock.assert_not_called()
        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.status, 'PAUSED', 'status must not flip until liveness is confirmed')

    def test_resumes_normally_when_the_open_segment_is_not_growing(self):
        """Control case: the restructuring must not break the ordinary post-crash resume."""
        rid, segnum = self._seed_open_segment()

        with mock.patch.object(recorder, '_segment_file_is_growing', return_value=False), \
             mock.patch.object(recorder, '_launch_segment') as launch_mock:
            recorder.resume_recording(self.t.app, rid)

        launch_mock.assert_called_once()
        db.session.expire_all()
        seg = RecordingSegment.query.filter_by(recording_id=rid, segment_number=segnum).one()
        self.assertIsNotNone(seg.ended_at, 'a genuinely stale open segment must still be closed')


class ClaimSingletonPidfileTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='dvr_test_pidfile_')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.pidfile = os.path.join(self.tmpdir, 'nested', 'channelbin.pid')

    def test_claims_when_no_file_exists(self):
        result = scheduler._claim_singleton_pidfile(self.pidfile)
        self.assertIsNone(result)
        with open(self.pidfile) as f:
            self.assertEqual(int(f.read().strip()), os.getpid())

    def test_reclaims_a_stale_pidfile_from_a_dead_pid(self):
        os.makedirs(os.path.dirname(self.pidfile), exist_ok=True)
        with open(self.pidfile, 'w') as f:
            f.write('999999')

        with mock.patch('app.scheduler.os.kill', side_effect=ProcessLookupError):
            result = scheduler._claim_singleton_pidfile(self.pidfile)

        self.assertIsNone(result)
        with open(self.pidfile) as f:
            self.assertEqual(int(f.read().strip()), os.getpid())

    def test_refuses_when_another_pid_is_alive(self):
        os.makedirs(os.path.dirname(self.pidfile), exist_ok=True)
        with open(self.pidfile, 'w') as f:
            f.write('424242')

        with mock.patch('app.scheduler.os.kill'):  # no exception raised = alive
            result = scheduler._claim_singleton_pidfile(self.pidfile)

        self.assertEqual(result, 424242)
        with open(self.pidfile) as f:
            self.assertEqual(f.read().strip(), '424242', 'must not overwrite a still-live pid')

    def test_own_pid_already_in_the_file_is_not_a_collision(self):
        os.makedirs(os.path.dirname(self.pidfile), exist_ok=True)
        with open(self.pidfile, 'w') as f:
            f.write(str(os.getpid()))

        result = scheduler._claim_singleton_pidfile(self.pidfile)
        self.assertIsNone(result)


class PidIsAliveTests(unittest.TestCase):
    def test_self_pid_is_alive(self):
        self.assertTrue(scheduler._pid_is_alive(os.getpid()))

    def test_dead_pid_is_not_alive(self):
        with mock.patch('app.scheduler.os.kill', side_effect=ProcessLookupError):
            self.assertFalse(scheduler._pid_is_alive(999999))


class InitSchedulerSecondInstanceGuardTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=False)
        self.addCleanup(self.t.cleanup)

    def test_refuses_to_start_when_pidfile_shows_another_live_instance(self):
        with mock.patch('app.scheduler._claim_singleton_pidfile', return_value=424242) as claim_mock, \
             mock.patch('app.scheduler.resume_in_progress_recordings') as resume_mock, \
             mock.patch('app.scheduler.BackgroundScheduler') as bg_mock:
            scheduler.init_scheduler(self.t.app)

        claim_mock.assert_called_once_with(self.t.app.config['PIDFILE_PATH'])
        resume_mock.assert_not_called()
        bg_mock.assert_not_called()
        alert = Alert.query.filter_by(alert_type='SECOND_INSTANCE_DETECTED').one()
        self.assertIn('424242', alert.body)


if __name__ == '__main__':
    unittest.main()
