"""A recording's start or stop job that raises says so, and is tried again.

Guards dev/docs/BUGS.md 2026-11-16 (start job) and dev/changelog/1590. On prod fourteen
recordings sat SCHEDULED for hours after their start jobs raised: nothing was written to the
recording, the log handler's alert was refused for want of an app context on the scheduler's
thread, and the startup sweep later called each one "missed entirely".

Covers:
  - StartJobTests: a raise leaves an event and one alert, re-arms the start, and fails the
    recording with the real reason once its window has closed.
  - StopJobTests: a raise leaves an event and re-arms the stop, a bounded number of times.
  - StartupSweepTests: the sweep names a recorded start error instead of "missed".
  - AlertHandlerContextTests: an ERROR logged on a thread with no app context is alerted.

No scheduler thread, no ffmpeg, no network.
  python3 -m unittest tests.test_recording_job_errors
"""
import json
import logging
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import alert_handler, seed  # noqa: E402
from app import db, scheduler  # noqa: E402
from app.database import (  # noqa: E402
    Alert, Recording, RecordingEvent, FAILURE_MISSED_AT_STARTUP, FAILURE_START_ERROR,
    REC_STATUS_FAILED, REC_STATUS_IN_PROGRESS, REC_STATUS_SCHEDULED, RECORDING_FAILED,
    RECORDING_JOB_ERROR,
)


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        alert_handler.attach()
        self.addCleanup(alert_handler.detach)
        patcher = mock.patch.object(scheduler, '_app', self.t.app)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(scheduler._job_errors.clear)
        self.start_rearm = self._patch('reschedule_recording_start')
        self.stop_rearm = self._patch('reschedule_recording_stop')
        self._patch('unschedule_recording')

    def _patch(self, name):
        patcher = mock.patch.object(scheduler, name)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _rec(self, *, stop_in=timedelta(hours=1), status=REC_STATUS_SCHEDULED):
        now = datetime.utcnow()
        rec = seed.make_recording(status=status, name='job error',
                                  start_time=now - timedelta(minutes=1), stop_time=now + stop_in)
        db.session.commit()
        return rec.id

    def _events(self, rid, event_type=RECORDING_JOB_ERROR):
        db.session.expire_all()
        return RecordingEvent.query.filter_by(
            recording_id=rid, event_type=event_type).order_by(RecordingEvent.id).all()

    def _alerts(self, rid):
        return Alert.query.filter_by(recording_id=rid).all()

    def _raise(self, job, rid, exc):
        target = 'app.recorder.start_recording' if job == 'start' else 'app.recorder.stop_recording'
        with mock.patch(target, side_effect=exc):
            (scheduler._start_job if job == 'start' else scheduler._stop_job)(rid)


class StartJobTests(_Case):
    def test_a_raise_leaves_an_event_an_alert_and_a_retry(self):
        rid = self._rec()
        before = datetime.utcnow()
        self._raise('start', rid, RuntimeError('pool ran dry'))

        self.assertEqual(REC_STATUS_SCHEDULED, db.session.get(Recording, rid).status)
        events = self._events(rid)
        self.assertEqual(1, len(events))
        self.assertIn('RuntimeError: pool ran dry', events[0].detail)
        extra = json.loads(events[0].extra_data)
        self.assertEqual(('start', 1), (extra['job'], extra['tries']))
        self.assertEqual(1, len(self._alerts(rid)))
        self.assertIn('could not start', self._alerts(rid)[0].title)

        (called_id, run_date), _ = self.start_rearm.call_args
        self.assertEqual(rid, called_id)
        delay = (run_date - before).total_seconds()
        self.assertTrue(scheduler.JOB_ERROR_RETRY_SECONDS - 1 <= delay
                        <= scheduler.JOB_ERROR_RETRY_SECONDS + 5, delay)

    def test_the_same_error_again_is_one_event_and_one_alert(self):
        rid = self._rec()
        for _ in range(3):
            self._raise('start', rid, RuntimeError('pool ran dry'))
        self.assertEqual(1, len(self._events(rid)))
        self.assertEqual(1, len(self._alerts(rid)))
        self.assertEqual(3, self.start_rearm.call_count)

    def test_a_different_error_is_a_second_event(self):
        rid = self._rec()
        self._raise('start', rid, RuntimeError('pool ran dry'))
        self._raise('start', rid, KeyError('recording'))
        self.assertEqual(2, len(self._events(rid)))

    def test_window_closed_fails_with_the_error_named(self):
        rid = self._rec()
        self._raise('start', rid, RuntimeError('pool ran dry'))
        rec = db.session.get(Recording, rid)
        rec.stop_time = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.start_rearm.reset_mock()
        self._raise('start', rid, RuntimeError('pool ran dry'))

        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(REC_STATUS_FAILED, rec.status)
        self.assertEqual(FAILURE_START_ERROR, rec.failure_reason)
        failed = self._events(rid, RECORDING_FAILED)
        self.assertEqual(1, len(failed))
        self.assertIn('2 tries', failed[0].detail)
        self.assertIn('RuntimeError: pool ran dry', failed[0].detail)
        self.start_rearm.assert_not_called()
        self.assertNotIn(('start', rid), scheduler._job_errors)

    def test_a_failed_event_write_does_not_end_the_retry(self):
        rid = self._rec()
        with mock.patch.object(scheduler, '_write_job_error_event', return_value=False):
            self._raise('start', rid, RuntimeError('pool ran dry'))
        self.start_rearm.assert_called_once()
        # The event is still owed, and the next try writes it.
        self._raise('start', rid, RuntimeError('pool ran dry'))
        self.assertEqual(1, len(self._events(rid)))

    def test_a_raise_past_the_claim_is_not_started_again(self):
        rid = self._rec(status=REC_STATUS_IN_PROGRESS)
        self._raise('start', rid, RuntimeError('after the claim'))
        self.start_rearm.assert_not_called()
        self.assertEqual(1, len(self._events(rid)))
        self.assertEqual(REC_STATUS_IN_PROGRESS, db.session.get(Recording, rid).status)

    def test_a_clean_start_after_a_raise_arms_the_stop_again(self):
        rid = self._rec()
        self._raise('start', rid, RuntimeError('pool ran dry'))

        def _started(app, recording_id):
            rec = db.session.get(Recording, recording_id)
            rec.status = REC_STATUS_IN_PROGRESS
            db.session.commit()

        with mock.patch('app.recorder.start_recording', side_effect=_started):
            scheduler._start_job(rid)
        self.assertNotIn(('start', rid), scheduler._job_errors)
        (called_id, run_date), _ = self.stop_rearm.call_args
        self.assertEqual(rid, called_id)
        self.assertEqual(db.session.get(Recording, rid).stop_time, run_date)

    def test_a_clean_first_start_arms_nothing_extra(self):
        rid = self._rec()
        with mock.patch('app.recorder.start_recording'):
            scheduler._start_job(rid)
        self.stop_rearm.assert_not_called()
        self.assertEqual([], self._events(rid))


class _SeatCase(_Case):
    """A scheduled recording on a one-seat account, with a folder to record into."""

    def setUp(self):
        super().setUp()
        from app import connection_limits as connlim
        self.connlim = connlim
        self.addCleanup(connlim._holders.clear)
        self.account = seed.make_account(name='Solo', max_connections=1)
        self.channel = seed.make_channel(self.account, name='One')
        now = datetime.utcnow()
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='claim raises',
                                  channel_id=self.channel.id,
                                  start_time=now - timedelta(seconds=5),
                                  stop_time=now + timedelta(hours=1))
        db.session.commit()
        self.rid, self.account_id = rec.id, self.account.id
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': os.path.join(self.t._tmpdir, 'dvr'),
            'live_thumbnail': {'enabled': False}, 'gather_health_data': False}})
        os.makedirs(os.path.join(self.t._tmpdir, 'dvr'), exist_ok=True)


class RaisedStartUnwindTests(_SeatCase):
    """What a start that raised leaves behind must not make the next try a silent no-op."""

    def test_a_claim_that_raises_gives_back_the_live_state_and_the_seat(self):
        from app import recorder
        with mock.patch.object(recorder, '_claim_status_row',
                               side_effect=RuntimeError('database is locked')), \
                self.assertRaises(RuntimeError):
            recorder.start_recording(self.t.app, self.rid)
        self.assertNotIn(self.rid, recorder._active)
        self.assertEqual({}, self.connlim.holder_counts())

    def test_the_next_try_really_starts_it(self):
        from app import recorder
        with mock.patch.object(recorder, '_claim_status_row',
                               side_effect=RuntimeError('database is locked')):
            scheduler._start_job(self.rid)
        self.assertEqual(1, self.start_rearm.call_count)
        with mock.patch.object(recorder, '_launch_segment') as launch:
            scheduler._start_job(self.rid)
        self.addCleanup(recorder._active.pop, self.rid, None)
        db.session.expire_all()
        self.assertEqual(REC_STATUS_IN_PROGRESS, db.session.get(Recording, self.rid).status)
        launch.assert_called_once()

    def test_failing_it_gives_back_a_seat_an_earlier_try_took(self):
        self.assertTrue(self.connlim.try_acquire(self.account_id, 'recording', self.rid))
        self._raise('start', self.rid, RuntimeError('pool ran dry'))
        rec = db.session.get(Recording, self.rid)
        rec.stop_time = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self._raise('start', self.rid, RuntimeError('pool ran dry'))
        db.session.expire_all()
        self.assertEqual(REC_STATUS_FAILED, db.session.get(Recording, self.rid).status)
        self.assertEqual({}, self.connlim.holder_counts())


class ClaimedLaunchRecoveryTests(_SeatCase):
    """dev/docs/BUGS.md 2026-11-16 (a start that raises after its claim): the row was left
    IN_PROGRESS with its live state and seat held and nothing capturing, and nothing tried
    again before a restart of the service."""

    def setUp(self):
        super().setUp()
        from app import recorder
        self.recorder = recorder
        self.resume_rearm = self._patch('reschedule_recording_resume')
        self.addCleanup(recorder._active.pop, self.rid, None)

    def _status(self):
        db.session.expire_all()
        return db.session.get(Recording, self.rid).status

    def _start_with_launch_raising(self, exc=None):
        with mock.patch.object(self.recorder, '_launch_segment',
                               side_effect=exc or RuntimeError('database is locked')):
            self.recorder.start_recording(self.t.app, self.rid)

    def test_a_launch_that_raises_is_unwound_said_and_armed_again(self):
        self._start_with_launch_raising()
        self.assertEqual(REC_STATUS_IN_PROGRESS, self._status())
        self.assertNotIn(self.rid, self.recorder._active)
        self.assertEqual({}, self.connlim.holder_counts())
        events = self._events(self.rid)
        self.assertEqual(1, len(events))
        self.assertIn('after the recording was claimed', events[0].detail)
        self.assertIn('database is locked', events[0].detail)
        self.assertIn('started again every 60 seconds', events[0].detail)
        self.assertEqual({'job': 'start', 'after_claim': True},
                         {k: v for k, v in json.loads(events[0].extra_data).items()
                          if k != 'error'})
        self.assertEqual(1, self.resume_rearm.call_count)
        self.assertEqual(self.rid, self.resume_rearm.call_args[0][0])
        self.assertEqual(1, len(self._alerts(self.rid)))

    def test_the_scheduler_reads_it_as_handled_not_as_a_start_to_give_up_on(self):
        with mock.patch.object(self.recorder, '_launch_segment',
                               side_effect=RuntimeError('database is locked')):
            scheduler._start_job(self.rid)
        details = [e.detail for e in self._events(self.rid)]
        self.assertEqual(1, len(details))
        self.assertNotIn('the start is not tried again', details[0])
        self.assertEqual(0, self.start_rearm.call_count)

    def test_the_armed_resume_really_launches_it(self):
        self._start_with_launch_raising()
        with mock.patch.object(self.recorder, '_launch_segment') as launch:
            self.recorder.resume_recording(self.t.app, self.rid)
        launch.assert_called_once()
        self.assertEqual(1, launch.call_args.kwargs['seg_num'])
        self.assertIn(self.rid, self.recorder._active)
        self.assertEqual(1, sum(self.connlim.holder_counts().values()))

    def test_a_resume_that_raises_the_same_way_is_armed_again_with_one_event(self):
        self._start_with_launch_raising()
        with mock.patch.object(self.recorder, '_launch_segment',
                               side_effect=RuntimeError('database is locked')):
            self.recorder.resume_recording(self.t.app, self.rid)
        self.assertNotIn(self.rid, self.recorder._active)
        self.assertEqual({}, self.connlim.holder_counts())
        self.assertEqual(2, self.resume_rearm.call_count)
        self.assertEqual(['start'], [json.loads(e.extra_data)['job']
                                     for e in self._events(self.rid)])
        self.assertEqual(1, len(self._alerts(self.rid)))

    def test_a_window_about_to_close_is_not_armed_again_and_says_so(self):
        rec = db.session.get(Recording, self.rid)
        rec.stop_time = datetime.utcnow() + timedelta(seconds=20)
        db.session.commit()
        self._start_with_launch_raising()
        self.assertEqual(0, self.resume_rearm.call_count)
        self.assertIn('Its window ends before another try', self._events(self.rid)[0].detail)
        self.assertNotIn(self.rid, self.recorder._active)
        self.assertEqual({}, self.connlim.holder_counts())

    def test_an_ffmpeg_spawned_before_the_raise_is_killed(self):
        proc = mock.MagicMock(pid=4242)
        proc.poll.return_value = None
        real = self.recorder.add_recording_event

        def _segment_row_fails(recording_id, event_type, *args, **kwargs):
            if event_type == 'SEGMENT_STARTED':
                raise RuntimeError('database is locked')
            return real(recording_id, event_type, *args, **kwargs)

        with mock.patch.object(self.recorder.subprocess, 'Popen', return_value=proc), \
                mock.patch.object(self.recorder, 'add_recording_event', _segment_row_fails), \
                mock.patch.object(self.recorder, 'terminate_or_kill') as kill:
            self.recorder.start_recording(self.t.app, self.rid)
        kill.assert_called_once_with(proc, hard=True)
        self.assertNotIn(self.rid, self.recorder._active)
        self.assertEqual({}, self.connlim.holder_counts())
        self.assertEqual(1, self.resume_rearm.call_count)
        from app.database import RecordingSegment
        self.assertEqual(0, RecordingSegment.query.filter_by(recording_id=self.rid).count())

    def test_a_wait_note_that_cannot_be_cleared_does_not_cost_the_launch(self):
        from sqlalchemy.exc import OperationalError
        with mock.patch.object(self.recorder, 'end_capture_wait',
                               side_effect=OperationalError('x', {}, Exception('locked'))), \
                mock.patch.object(self.recorder, '_launch_segment') as launch:
            self.recorder.start_recording(self.t.app, self.rid)
        launch.assert_called_once()
        self.assertEqual([], self._events(self.rid))

    def test_a_segment_a_crash_left_open_is_left_for_the_resume_that_works(self):
        """Found by review: the recovery's teardown closed a crash-left open segment as
        ERROR at the present time, where the next resume owes it SERVICE_RESTART and the
        time its file stopped."""
        from app.database import RecordingSegment
        rec = db.session.get(Recording, self.rid)
        rec.status = REC_STATUS_IN_PROGRESS
        db.session.add(RecordingSegment(recording_id=self.rid, segment_number=1,
                                        file_path=os.path.join(self.t._tmpdir, 'gone.ts'),
                                        started_at=datetime.utcnow() - timedelta(hours=1)))
        db.session.commit()
        with mock.patch.object(self.recorder, 'end_capture_wait',
                               side_effect=RuntimeError('database is locked')):
            error = self.recorder.resume_recording(self.t.app, self.rid)
        self.assertIn('database is locked', error)
        db.session.expire_all()
        seg = RecordingSegment.query.filter_by(recording_id=self.rid).one()
        self.assertIsNone(seg.ended_at)
        self.assertIsNone(seg.exit_reason)
        self.assertNotIn(self.rid, self.recorder._active)
        self.assertEqual({}, self.connlim.holder_counts())

    def test_the_same_error_after_a_capture_ran_is_said_again(self):
        """Found by review: the one-event rule compared against the last such event ever,
        so a second failure an hour into the capture left no trace."""
        from app.database import add_recording_event
        self._start_with_launch_raising()
        add_recording_event(self.rid, 'SEGMENT_STARTED', detail='Segment 1 started',
                            segment_number=1)
        db.session.commit()
        with mock.patch.object(self.recorder, '_launch_segment',
                               side_effect=RuntimeError('database is locked')):
            self.recorder.resume_recording(self.t.app, self.rid)
        self.assertEqual(['start', 'resume'], [json.loads(e.extra_data)['job']
                                               for e in self._events(self.rid)])
        self.assertEqual(2, len(self._alerts(self.rid)))

    def test_a_recording_aborted_meanwhile_gets_no_event_and_no_resume(self):
        from app.database import REC_STATUS_ABORTED

        def _abort_then_raise(app, recording_id, seg_num):
            db.session.get(Recording, recording_id).status = REC_STATUS_ABORTED
            db.session.commit()
            raise RuntimeError('database is locked')

        with mock.patch.object(self.recorder, '_launch_segment', _abort_then_raise):
            self.recorder.start_recording(self.t.app, self.rid)
        self.assertEqual([], self._events(self.rid))
        self.assertEqual(0, self.resume_rearm.call_count)
        self.assertEqual([], self._alerts(self.rid))
        self.assertNotIn(self.rid, self.recorder._active)
        self.assertEqual({}, self.connlim.holder_counts())

    def test_a_re_arm_that_raises_does_not_leave_the_start(self):
        self.resume_rearm.side_effect = RuntimeError('jobstore is locked')
        self._start_with_launch_raising()
        self.assertEqual(1, len(self._events(self.rid)))
        self.assertNotIn(self.rid, self.recorder._active)

    def test_a_pause_takes_the_armed_resume_with_it(self):
        """Found by review: a resume armed for a minute on fired on a recording paused in
        between and resumed it, since PAUSED is a status a resume takes."""
        rec = db.session.get(Recording, self.rid)
        rec.status = REC_STATUS_IN_PROGRESS
        db.session.commit()
        with mock.patch.object(scheduler, 'remove_job_if_exists') as remove:
            self.recorder.pause_recording(self.t.app, self.rid)
        remove.assert_called_once_with(f'resume_{self.rid}')

    def test_the_resume_button_says_when_the_resume_did_not_work(self):
        """Found by review: resume_recording() no longer raises, and the routes answered
        "resumed" over a recording still paused."""
        from app.database import REC_STATUS_PAUSED
        from tests.support.csrf import csrf_token
        rec = db.session.get(Recording, self.rid)
        rec.status = REC_STATUS_PAUSED
        db.session.commit()
        with mock.patch.object(self.recorder, '_launch_segment',
                               side_effect=RuntimeError('database is locked')):
            resp = self.t.client.post(f'/recordings/{self.rid}/resume-json',
                                      headers={'X-CSRFToken': csrf_token(self.t.client)})
        self.assertEqual(500, resp.status_code)
        self.assertIn('database is locked', resp.get_json()['error'])
        self.assertIn('tried again every minute', resp.get_json()['error'])

    def test_a_paused_resume_whose_claim_raises_gives_back_what_it_took(self):
        from app.database import REC_STATUS_PAUSED
        rec = db.session.get(Recording, self.rid)
        rec.status = REC_STATUS_PAUSED
        db.session.commit()
        with mock.patch.object(self.recorder, '_claim_status_row',
                               side_effect=RuntimeError('database is locked')):
            self.recorder.resume_recording(self.t.app, self.rid)
        self.assertEqual(REC_STATUS_PAUSED, self._status())
        self.assertNotIn(self.rid, self.recorder._active)
        self.assertEqual({}, self.connlim.holder_counts())
        self.assertEqual(1, self.resume_rearm.call_count)
        self.assertEqual(['resume'], [json.loads(e.extra_data)['job']
                                      for e in self._events(self.rid)])


class LostRetryTests(_Case):
    def test_a_failure_that_cannot_be_written_is_tried_again(self):
        rid = self._rec()
        self._raise('start', rid, RuntimeError('pool ran dry'))
        rec = db.session.get(Recording, rid)
        rec.stop_time = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.start_rearm.reset_mock()
        with mock.patch.object(scheduler, '_fail_start_after_errors',
                               side_effect=RuntimeError('still locked')):
            self._raise('start', rid, RuntimeError('pool ran dry'))
        self.start_rearm.assert_called_once()
        self.assertEqual(2, scheduler._job_errors[('start', rid)]['tries'])
        # And the try after that fails it for good.
        self._raise('start', rid, RuntimeError('pool ran dry'))
        db.session.expire_all()
        self.assertEqual(REC_STATUS_FAILED, db.session.get(Recording, rid).status)

    def test_a_rollback_that_raises_does_not_end_the_retry(self):
        rid = self._rec()
        with mock.patch.object(db.session, 'rollback', side_effect=RuntimeError('gone')):
            self._raise('start', rid, RuntimeError('pool ran dry'))
        self.start_rearm.assert_called_once()


class UnscheduleClearsTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        self.addCleanup(scheduler._job_errors.clear)

    def test_unscheduling_forgets_the_count(self):
        """A cancel, delete or edit: a later raise on the id is a first raise again, with
        its own event and alert."""
        scheduler._note_job_error('start', 7, RuntimeError('x'))
        scheduler._note_job_error('stop', 7, RuntimeError('x'))
        scheduler.unschedule_recording(7)
        self.assertEqual({}, scheduler._job_errors)


class StopJobTests(_Case):
    def test_a_raise_leaves_an_event_an_alert_and_a_retry(self):
        rid = self._rec(status=REC_STATUS_IN_PROGRESS)
        self._raise('stop', rid, RuntimeError('pool ran dry'))
        events = self._events(rid)
        self.assertEqual(1, len(events))
        self.assertEqual('stop', json.loads(events[0].extra_data)['job'])
        self.assertEqual(1, len(self._alerts(rid)))
        self.stop_rearm.assert_called_once()

    def test_it_gives_up_after_the_last_try_and_says_so(self):
        rid = self._rec(status=REC_STATUS_IN_PROGRESS)
        for _ in range(scheduler.STOP_JOB_MAX_TRIES):
            self._raise('stop', rid, RuntimeError('pool ran dry'))
        self.assertEqual(scheduler.STOP_JOB_MAX_TRIES - 1, self.stop_rearm.call_count)
        events = self._events(rid)
        self.assertEqual(2, len(events))
        self.assertIn('No more are made', events[-1].detail)
        self.assertEqual(2, len(self._alerts(rid)))

    def test_a_clean_stop_forgets_the_error(self):
        rid = self._rec(status=REC_STATUS_IN_PROGRESS)
        self._raise('stop', rid, RuntimeError('pool ran dry'))
        with mock.patch('app.recorder.stop_recording'):
            scheduler._stop_job(rid)
        self.assertNotIn(('stop', rid), scheduler._job_errors)


class StartupSweepTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.addCleanup(self.t.cleanup)

    def _missed(self):
        start = datetime.utcnow() - timedelta(hours=3)
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='missed',
                                  start_time=start, stop_time=start + timedelta(hours=1))
        db.session.commit()
        return rec.id

    def _sweep(self, rid):
        scheduler.resume_in_progress_recordings(self.t.app)
        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        failed = RecordingEvent.query.filter_by(
            recording_id=rid, event_type=RECORDING_FAILED).one()
        return rec, failed

    def test_a_recorded_start_error_is_the_reason(self):
        rid = self._missed()
        from app.database import add_recording_event
        add_recording_event(rid, RECORDING_JOB_ERROR, 'first',
                            extra={'job': 'start', 'error': 'KeyError: old', 'tries': 1})
        add_recording_event(rid, RECORDING_JOB_ERROR, 'second',
                            extra={'job': 'start', 'error': 'TimeoutError: pool', 'tries': 4})
        db.session.commit()
        rec, failed = self._sweep(rid)
        self.assertEqual(FAILURE_START_ERROR, rec.failure_reason)
        self.assertIn('TimeoutError: pool', failed.detail)
        self.assertNotIn('missed entirely', failed.detail)

    def test_no_start_error_is_still_missed(self):
        rid = self._missed()
        rec, failed = self._sweep(rid)
        self.assertEqual(FAILURE_MISSED_AT_STARTUP, rec.failure_reason)

    def test_a_stop_error_is_not_read_as_a_start_error(self):
        rid = self._missed()
        from app.database import add_recording_event
        add_recording_event(rid, RECORDING_JOB_ERROR, 'stop',
                            extra={'job': 'stop', 'error': 'KeyError: x', 'tries': 1})
        db.session.commit()
        rec, _ = self._sweep(rid)
        self.assertEqual(FAILURE_MISSED_AT_STARTUP, rec.failure_reason)


class StartupLateStartTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-11-23 @ 10:40:09 PM: the startup sweep starts a recording that
    missed its start time on a thread of its own, after dropping the overdue start job. A
    raise on that thread is answered like a start job's raise."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.addCleanup(self.t.cleanup)
        self.addCleanup(scheduler._job_errors.clear)

    def test_a_late_start_that_raises_says_so_and_is_armed_again(self):
        now = datetime.utcnow()
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='late start',
                                  start_time=now - timedelta(minutes=2),
                                  stop_time=now + timedelta(hours=1))
        db.session.commit()
        rid = rec.id
        ran = threading.Event()

        def _raise(app, recording_id):
            ran.set()
            raise TimeoutError('pool')

        with mock.patch('app.recorder.start_recording', side_effect=_raise):
            scheduler.resume_in_progress_recordings(self.t.app)
            self.assertTrue(ran.wait(15), 'the sweep never started the late recording')
            deadline = datetime.utcnow() + timedelta(seconds=15)
            while (('start', rid) not in scheduler._job_errors
                   or scheduler._scheduler.get_job(f'start_{rid}') is None):
                if datetime.utcnow() > deadline:
                    break
                threading.Event().wait(0.05)
        self.assertIsNotNone(scheduler._scheduler.get_job(f'start_{rid}'),
                             'nothing is armed to start the recording again')
        db.session.expire_all()
        events = RecordingEvent.query.filter_by(
            recording_id=rid, event_type=RECORDING_JOB_ERROR).all()
        self.assertEqual(1, len(events))
        self.assertIn('TimeoutError: pool', events[0].detail)
        self.assertEqual(REC_STATUS_SCHEDULED, db.session.get(Recording, rid).status)


class AlertHandlerContextTests(unittest.TestCase):
    """The scheduler's own threads log a job's exception outside any app context."""

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        alert_handler.attach()
        self.addCleanup(alert_handler.detach)

    def _log_on_bare_thread(self, message):
        thread = threading.Thread(
            target=lambda: logging.getLogger('apscheduler.executors.default').error(message))
        thread.start()
        thread.join()

    def test_an_error_on_a_context_less_thread_is_alerted(self):
        with mock.patch.object(scheduler, '_app', self.t.app):
            self._log_on_bare_thread('Job "x" raised an exception')
        db.session.expire_all()
        titles = [a.title for a in Alert.query.filter_by(alert_type='LOG_ERROR').all()]
        self.assertIn('Job "x" raised an exception', titles)

    def test_with_no_app_it_stays_in_the_log_and_says_nothing_more(self):
        with mock.patch.object(scheduler, '_app', None), \
                self.assertNoLogs('app.alerts', level='WARNING'):
            self._log_on_bare_thread('Job "y" raised an exception')
        self.assertEqual(0, Alert.query.filter_by(alert_type='LOG_ERROR').count())


if __name__ == '__main__':
    unittest.main()
