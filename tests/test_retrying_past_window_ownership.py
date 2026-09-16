"""Tier 2 - the startup sweep's two resume gaps: a RETRYING row past its window, and a
CONVERTING resume outside the live-chain claim.

Guards dev/docs/BUGS.md 2026-09-16 "A RETRYING recording whose window ended is joined or
failed by whichever misfired job runs first" and "The startup CONVERTING resume launches
do_postprocess outside the chain claim, so a Retry during its wait starts a duplicate chain".
Design and reasoning: dev/changelog/988.

(a) A RETRYING recording carries two persisted jobs. When its stop time passes - while the
    service is down, or at runtime when the retry lands after stop_time but before the stop
    job's join has flipped the status - both fire. stop_<id> joined what was captured;
    retry_<id> marked the row FAILED with no join. The first commit decided whether hours of
    captured video were delivered or stranded. Both now land in stop_recording(), which
    joins whenever anything was captured and gives up as a dead stream only when nothing
    was, so either order gives the same answer.

(b) Case 1c of the sweep started a bare do_postprocess() thread. The Retry-conversion route
    refuses only on the live-chain claim or a spawned ffmpeg, and a resumed chain parked in
    the conversion collision wait held neither, so a Retry started a second chain.

No network and no real ffmpeg: do_concatenation / _run_concatenation / do_postprocess are
replaced with spies, and the health blend and final thumbnail are stubbed because neither is
asserted on here and both reach real config defaults.
  python3 -m unittest tests.test_retrying_past_window_ownership
"""
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.recorder as recorder  # noqa: E402
import app.scheduler as scheduler  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    RECORDING_FAILED_DEAD_STREAM, Recording, RecordingEvent, RecordingSegment,
    REC_STATUS_CONCATENATING, REC_STATUS_FAILED, REC_STATUS_RETRYING,
)
from tests.support import make_test_app, seed  # noqa: E402


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _RetryingHarness(unittest.TestCase):
    start_scheduler = False

    def setUp(self):
        self.t = make_test_app(start_scheduler=self.start_scheduler)
        self._stubs = [
            mock.patch('app.health_score.apply_recording_health_observation'),
            mock.patch.object(recorder, 'persist_final_thumbnail'),
        ]
        for s in self._stubs:
            s.start()

    def tearDown(self):
        for s in self._stubs:
            s.stop()
        self.t.cleanup()

    def _make_retrying(self, *, with_data, stop_delta=timedelta(seconds=-5)):
        now = datetime.utcnow()
        rec = seed.make_recording(
            status=REC_STATUS_RETRYING, name='retrying-past-window',
            start_time=now - timedelta(hours=2), stop_time=now + stop_delta,
            dead_stream_retry_count=2, next_retry_at=now + timedelta(minutes=10))
        db.session.flush()
        for n in (1, 2):
            path = os.path.join(self.t._tmpdir, f'retrying_seg_{rec.id}_{n}.ts')
            size = 4096 if with_data else 0
            with open(path, 'wb') as fh:
                fh.write(b'\0' * size)
            db.session.add(RecordingSegment(
                recording_id=rec.id, segment_number=n, file_path=path,
                started_at=now - timedelta(hours=2), ended_at=now - timedelta(hours=1),
                exit_reason='STREAM_DEAD', bytes_recorded=size))
        db.session.commit()
        return rec.id

    def _dead_stream_events(self, rid):
        return RecordingEvent.query.filter_by(
            recording_id=rid, event_type=RECORDING_FAILED_DEAD_STREAM).count()

    def _status(self, rid):
        db.session.expire_all()
        return db.session.get(Recording, rid).status


class RetryFiringAfterTheWindowJoinsWhatWasCapturedTests(_RetryingHarness):

    def test_retry_job_past_the_window_joins_a_capture_instead_of_failing_it(self):
        """The item's own reproduction: two data-bearing segments, stop_time past, through
        fire_dead_stream_retry. Before the fix this finalized straight to FAILED."""
        rid = self._make_retrying(with_data=True)
        with mock.patch('app.concatenator.do_concatenation') as join:
            recorder.fire_dead_stream_retry(self.t.app, rid)
            self.assertTrue(_wait_until(lambda: join.called),
                            'a RETRYING recording with captured data was never joined')
        self.assertEqual(join.call_args.args[1], rid)
        self.assertNotEqual(self._status(rid), REC_STATUS_FAILED)
        self.assertEqual(self._dead_stream_events(rid), 0)

    def _race_both_jobs(self, rid, retry_first):
        """Runs the real do_concatenation (so its claim is live) around a fake join body
        that blocks BEFORE writing CONCATENATING - the window the runtime race lives in."""
        gate = threading.Event()
        entered = threading.Event()
        runs = []

        def _fake_run(app, recording_id, *, reason):
            runs.append(recording_id)
            entered.set()
            gate.wait(5)
            with app.app_context():
                r = db.session.get(Recording, recording_id)
                if r.status == REC_STATUS_RETRYING:
                    r.status = REC_STATUS_CONCATENATING
                    db.session.commit()

        with mock.patch('app.concatenator._run_concatenation', side_effect=_fake_run):
            try:
                if retry_first:
                    recorder.fire_dead_stream_retry(self.t.app, rid)
                    entered.wait(3)
                    recorder.stop_recording(self.t.app, rid)
                else:
                    recorder.stop_recording(self.t.app, rid)
                    entered.wait(3)
                    recorder.fire_dead_stream_retry(self.t.app, rid)
                time.sleep(0.2)
            finally:
                gate.set()
            _wait_until(lambda: self._status(rid) != REC_STATUS_RETRYING)
            time.sleep(0.1)
        return runs

    def test_stop_job_then_retry_job_is_one_join_and_never_failed(self):
        rid = self._make_retrying(with_data=True)
        runs = self._race_both_jobs(rid, retry_first=False)
        self.assertEqual(runs, [rid], 'expected exactly one join')
        self.assertEqual(self._dead_stream_events(rid), 0,
                         'the retry job failed a recording the stop job was already joining')
        self.assertEqual(self._status(rid), REC_STATUS_CONCATENATING)

    def test_retry_job_then_stop_job_is_one_join_and_never_failed(self):
        rid = self._make_retrying(with_data=True)
        runs = self._race_both_jobs(rid, retry_first=True)
        self.assertEqual(runs, [rid], 'expected exactly one join')
        self.assertEqual(self._dead_stream_events(rid), 0)
        self.assertEqual(self._status(rid), REC_STATUS_CONCATENATING)


class NothingCapturedStillGivesUpAsADeadStreamTests(_RetryingHarness):

    def _assert_dead_stream_once(self, rid, join):
        self.assertFalse(join.called, 'a capture with no data was sent to the join')
        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.status, REC_STATUS_FAILED)
        self.assertEqual(rec.failure_reason, 'DEAD_STREAM_DETECTED')
        self.assertEqual(self._dead_stream_events(rid), 1)

    def test_stop_job_then_retry_job(self):
        rid = self._make_retrying(with_data=False)
        with mock.patch('app.concatenator.do_concatenation') as join:
            recorder.stop_recording(self.t.app, rid)
            recorder.fire_dead_stream_retry(self.t.app, rid)
            time.sleep(0.1)
        self._assert_dead_stream_once(rid, join)

    def test_retry_job_then_stop_job(self):
        rid = self._make_retrying(with_data=False)
        with mock.patch('app.concatenator.do_concatenation') as join:
            recorder.fire_dead_stream_retry(self.t.app, rid)
            recorder.stop_recording(self.t.app, rid)
            time.sleep(0.1)
        self._assert_dead_stream_once(rid, join)

    def test_bytes_recorded_counts_as_captured_even_with_the_files_gone(self):
        """The join is what names "the files went missing after capture"; the dead-stream
        give-up would blame the stream for a local loss."""
        rid = self._make_retrying(with_data=True)
        for seg in RecordingSegment.query.filter_by(recording_id=rid).all():
            os.unlink(seg.file_path)
        with mock.patch('app.concatenator.do_concatenation') as join:
            recorder.stop_recording(self.t.app, rid)
            self.assertTrue(_wait_until(lambda: join.called))
        self.assertEqual(self._dead_stream_events(rid), 0)

    def test_manual_stop_inside_the_window_still_goes_to_the_join(self):
        """Characterization: the give-up is only for a window that ENDED. A user's Stop on
        an empty RETRYING row keeps the join's own no-valid-segments answer."""
        rid = self._make_retrying(with_data=False, stop_delta=timedelta(hours=1))
        with mock.patch('app.concatenator.do_concatenation') as join, \
             mock.patch.object(recorder, '_log_manual_stop_and_commit'):
            recorder.stop_recording(self.t.app, rid, reason='MANUAL_STOP')
            self.assertTrue(_wait_until(lambda: join.called))
        self.assertEqual(self._dead_stream_events(rid), 0)


class StartupSweepRetryingCaseTests(_RetryingHarness):
    start_scheduler = True

    def _add_future_job(self, func, job_id, rid):
        scheduler._add_job(func=func, trigger='date',
                           run_date=datetime.utcnow() + timedelta(hours=3),
                           args=[rid], id=job_id, replace_existing=True)

    def test_past_window_is_joined_and_its_jobs_are_removed(self):
        rid = self._make_retrying(with_data=True)
        self._add_future_job(scheduler._dead_stream_retry_job, f'retry_{rid}', rid)
        self._add_future_job(scheduler._stop_job, f'stop_{rid}', rid)

        with mock.patch('app.concatenator.do_concatenation') as join:
            scheduler.resume_in_progress_recordings(self.t.app)
            self.assertTrue(_wait_until(lambda: join.called),
                            'the sweep left a RETRYING row past its window to the misfired jobs')

        sched = scheduler.get_scheduler()
        self.assertIsNone(sched.get_job(f'retry_{rid}'))
        self.assertIsNone(sched.get_job(f'stop_{rid}'))
        self.assertEqual(self._dead_stream_events(rid), 0)

    def test_open_window_stays_retrying_with_a_retry_job_registered(self):
        rid = self._make_retrying(with_data=True, stop_delta=timedelta(hours=1))
        with mock.patch('app.concatenator.do_concatenation') as join:
            scheduler.resume_in_progress_recordings(self.t.app)
            time.sleep(0.1)
        self.assertFalse(join.called)
        self.assertEqual(self._status(rid), REC_STATUS_RETRYING)
        self.assertIsNotNone(scheduler.get_scheduler().get_job(f'retry_{rid}'),
                             'a RETRYING row came out of the sweep with nothing to retry it')


class ConvertingResumeHoldsTheChainClaimTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.t.app.config['WTF_CSRF_ENABLED'] = False

    def tearDown(self):
        self.t.cleanup()

    def test_retry_during_a_resumed_conversion_wait_is_refused(self):
        """The item's reproduction: the resumed chain parks (here, inside a fake
        do_postprocess standing in for _wait_for_conversion_clear); a Retry must not start
        a second one."""
        ts_path = os.path.join(self.t._tmpdir, 'resumed.ts')
        with open(ts_path, 'wb') as fh:
            fh.write(b'\0' * 2048)
        rec = seed.make_recording(status='CONVERTING', name='resumed conversion',
                                  output_path=ts_path, conversion_attempts=0)
        db.session.commit()
        rid = rec.id

        gate = threading.Event()
        calls = []

        def _parked_postprocess(app, recording_id, path):
            calls.append(recording_id)
            gate.wait(5)

        with mock.patch('app.postprocessor.do_postprocess', side_effect=_parked_postprocess):
            try:
                scheduler.resume_in_progress_recordings(self.t.app)
                self.assertTrue(_wait_until(lambda: len(calls) == 1))
                resp = self.t.client.post(f'/recordings/{rid}/retry-convert')
                self.assertEqual(resp.status_code, 302)
                time.sleep(0.3)
                self.assertEqual(calls, [rid],
                                 'Retry started a second chain over the resumed one')
                db.session.expire_all()
                self.assertEqual(db.session.get(Recording, rid).conversion_attempts, 1,
                                 'the refused Retry reset the resumed run\'s attempt budget')
            finally:
                gate.set()
            time.sleep(0.1)


if __name__ == '__main__':
    unittest.main()
