"""Guards dev/docs/BUGS.md 2026-08-17 "A recording whose stop time passed while the service
was down keeps an open segment forever, and never says the outage cost it content."

The host VM was power-cycled 1h34m into a 4h recording. When the app came back up the
recording's stop time had already passed, so startup recovery took the past-stop-time
branch: straight to concatenation, no resume_recording(), and therefore none of
resume_recording()'s open-segment bookkeeping. Two defects fell out of that:

  1. The segment row kept ended_at IS NULL forever. recording_detail.html renders a
     segment's duration as `(seg.ended_at or now) - seg.started_at`, so the finished
     recording showed a segment counting up in real time, days later.
  2. The only trace of a 53-minute outage was a "Resuming for concatenation (past stop
     time)" line that reads as routine. The recording reported "missing 26%" with nothing
     anywhere explaining why - Product Principle 1's exact failure mode.

No real ffmpeg anywhere: do_concatenation is monkeypatched (same pattern as
tests/test_concat_startup_recovery.py) and the liveness probe is patched so no test
waits on its two-sample sleep.
"""
import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import recorder  # noqa: E402
from app.database import (  # noqa: E402
    RecordingSegment, RecordingEvent, Alert,
    CAPTURE_LOST_TO_OUTAGE, RECORDING_RESUME_REFUSED, SEGMENT_ENDED,
)


class PastStopTimeRecoveryTests(unittest.TestCase):
    def setUp(self):
        # resume_in_progress_recordings() sweeps orphaned APScheduler on-demand jobs
        # unconditionally at the end, which needs a live scheduler.
        self.t = make_test_app(start_scheduler=True)
        self.addCleanup(self.t.cleanup)

    def _seed_crashed_recording(self, *, stopped_before_end=timedelta(minutes=60),
                                window=timedelta(hours=4), write_file=True):
        """An IN_PROGRESS recording whose stop_time has passed, with a segment row still
        open. The segment file's mtime is the only record of when capture really stopped,
        exactly as it is after a host power-cycle."""
        now = datetime.utcnow()
        stop = now - timedelta(minutes=30)
        start = stop - window
        rec = seed.make_recording(status='IN_PROGRESS', name='Cook Out 400',
                                  start_time=start, stop_time=stop)
        db.session.flush()

        path = os.path.join(self.t._tmpdir, f'rec_{rec.id}_seg_002.ts')
        capture_stopped = stop - stopped_before_end
        if write_file:
            with open(path, 'wb') as fh:
                fh.write(b'0' * 4096)
            # Naive-UTC -> POSIX, the exact inverse of _capture_stopped_at's
            # utcfromtimestamp(). Going via a naive .timestamp() would read it as local.
            ts = capture_stopped.replace(tzinfo=timezone.utc).timestamp()
            os.utime(path, (ts, ts))

        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=2, file_path=path,
            started_at=start, ended_at=None))
        db.session.commit()
        return rec.id, capture_stopped, stop

    def _run_recovery(self, growing=False):
        from app.scheduler import resume_in_progress_recordings
        with mock.patch('app.concatenator.do_concatenation') as fake_concat, \
             mock.patch.object(recorder, '_segment_file_is_growing', return_value=growing):
            resume_in_progress_recordings(self.t.app)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not fake_concat.called:
                time.sleep(0.01)
        db.session.expire_all()
        return fake_concat

    def _event(self, rid, event_type):
        return RecordingEvent.query.filter_by(
            recording_id=rid, event_type=event_type).first()

    def test_open_segment_row_is_closed_out(self):
        """Defect 1: the row must not stay open just because the window already ended."""
        rid, _, _ = self._seed_crashed_recording()

        self._run_recovery()

        seg = RecordingSegment.query.filter_by(recording_id=rid, segment_number=2).one()
        self.assertIsNotNone(seg.ended_at,
                             'a segment left open by an unclean stop must be closed even '
                             'when the recording is already past its stop time')
        self.assertEqual(seg.exit_reason, 'SERVICE_RESTART')
        self.assertEqual(seg.bytes_recorded, 4096)
        self.assertIsNotNone(self._event(rid, SEGMENT_ENDED))

    def test_ended_at_is_when_capture_stopped_not_when_the_app_noticed(self):
        """ended_at = utcnow() would credit the segment with the whole outage as captured
        content. The file's last write is when ffmpeg actually stopped."""
        rid, capture_stopped, _ = self._seed_crashed_recording()

        self._run_recovery()

        seg = RecordingSegment.query.filter_by(recording_id=rid, segment_number=2).one()
        drift = abs((seg.ended_at - capture_stopped).total_seconds())
        self.assertLess(drift, 2.0,
                        f'ended_at {seg.ended_at} should track the file mtime '
                        f'{capture_stopped}, not the time of the restart')
        self.assertLess(seg.ended_at, datetime.utcnow() - timedelta(minutes=59),
                        'ended_at must not be dragged forward to "when we noticed"')

    def test_missing_file_falls_back_to_now_rather_than_guessing(self):
        """No file, no evidence: close the row rather than leave it open, but do not
        invent a stop time from nothing."""
        rid, _, _ = self._seed_crashed_recording(write_file=False)

        self._run_recovery()

        seg = RecordingSegment.query.filter_by(recording_id=rid, segment_number=2).one()
        self.assertIsNotNone(seg.ended_at)
        self.assertGreater(seg.ended_at, datetime.utcnow() - timedelta(minutes=1))
        self.assertIsNone(seg.bytes_recorded,
                          'no file means no honest byte count to record')

    def test_outage_event_names_the_lost_content(self):
        """Defect 2: the shortfall gets its own event, not a routine-sounding resume line."""
        rid, capture_stopped, stop = self._seed_crashed_recording()

        self._run_recovery()

        evt = self._event(rid, CAPTURE_LOST_TO_OUTAGE)
        self.assertIsNotNone(evt, 'the outage must get its own event, not just a log line')
        self.assertIn('never recorded', evt.detail)
        extra = json.loads(evt.extra_data)
        self.assertAlmostEqual(extra['uncaptured_seconds'],
                               (stop - capture_stopped).total_seconds(), delta=2.0)
        self.assertIn('capture_stopped_at', extra)

    def test_outage_raises_an_alert(self):
        rid, _, _ = self._seed_crashed_recording()

        self._run_recovery()

        alerts = Alert.query.filter_by(alert_type='CAPTURE_LOST_TO_OUTAGE').all()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].recording_id, rid)

    def test_sub_minute_shortfall_is_logged_but_does_not_alert(self):
        """Restarting the service seconds before a stop time is ordinary operation."""
        rid, _, _ = self._seed_crashed_recording(stopped_before_end=timedelta(seconds=10))

        self._run_recovery()

        self.assertIsNotNone(self._event(rid, CAPTURE_LOST_TO_OUTAGE),
                             'the event is written for any shortfall')
        self.assertEqual(Alert.query.filter_by(alert_type='CAPTURE_LOST_TO_OUTAGE').count(), 0)

    def test_no_outage_claimed_when_capture_reached_the_stop_time(self):
        """Nothing was lost, so nothing may be reported as lost."""
        now = datetime.utcnow()
        stop = now - timedelta(minutes=30)
        rec = seed.make_recording(status='IN_PROGRESS', name='Full run',
                                  start_time=stop - timedelta(hours=1), stop_time=stop)
        db.session.flush()
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=1,
            file_path=os.path.join(self.t._tmpdir, 'done.ts'),
            started_at=stop - timedelta(hours=1), ended_at=stop + timedelta(seconds=1),
            exit_reason='STOP_TIME_REACHED'))
        db.session.commit()
        rid = rec.id

        self._run_recovery()

        self.assertIsNone(self._event(rid, CAPTURE_LOST_TO_OUTAGE))
        self.assertEqual(Alert.query.filter_by(alert_type='CAPTURE_LOST_TO_OUTAGE').count(), 0)

    def test_still_growing_segment_blocks_the_close_and_the_concatenation(self):
        """The liveness guard survives the refactor: a second live process owns this
        recording, so neither its rows nor its files may be touched here."""
        rid, _, _ = self._seed_crashed_recording()

        fake_concat = self._run_recovery(growing=True)

        self.assertFalse(fake_concat.called,
                         'concatenating a file another process is writing truncates it')
        seg = RecordingSegment.query.filter_by(recording_id=rid, segment_number=2).one()
        self.assertIsNone(seg.ended_at)
        self.assertIsNotNone(self._event(rid, RECORDING_RESUME_REFUSED))
        self.assertIsNone(self._event(rid, CAPTURE_LOST_TO_OUTAGE))

    def test_concatenation_still_runs_in_the_ordinary_case(self):
        """Control: the recovery still finalizes the recording it came here to finalize."""
        rid, _, _ = self._seed_crashed_recording()

        fake_concat = self._run_recovery()

        self.assertTrue(fake_concat.called)
        self.assertEqual(fake_concat.call_args.args[1], rid)


class CaptureStoppedAtTests(unittest.TestCase):
    """Unit tests for the mtime clamp - an mtime outside [started_at, now] describes
    something other than this capture."""

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)

    def _seg(self, started_at, mtime_offset):
        path = os.path.join(self.t._tmpdir, 'clamp.ts')
        with open(path, 'wb') as fh:
            fh.write(b'x')
        ts = time.time() + mtime_offset
        os.utime(path, (ts, ts))
        return RecordingSegment(recording_id=1, segment_number=1, file_path=path,
                                started_at=started_at)

    def test_mtime_before_started_at_is_rejected(self):
        seg = self._seg(datetime.utcnow() - timedelta(minutes=5), mtime_offset=-3600)
        self.assertGreater(recorder._capture_stopped_at(seg),
                           datetime.utcnow() - timedelta(minutes=1))

    def test_mtime_in_the_future_is_clamped_to_now(self):
        seg = self._seg(datetime.utcnow() - timedelta(minutes=5), mtime_offset=3600)
        self.assertLessEqual(recorder._capture_stopped_at(seg),
                             datetime.utcnow() + timedelta(seconds=1))

    def test_missing_file_returns_now(self):
        seg = RecordingSegment(recording_id=1, segment_number=1,
                               file_path='/nonexistent/x.ts',
                               started_at=datetime.utcnow() - timedelta(minutes=5))
        self.assertGreater(recorder._capture_stopped_at(seg),
                           datetime.utcnow() - timedelta(minutes=1))

    def test_no_file_path_returns_now(self):
        seg = RecordingSegment(recording_id=1, segment_number=1, file_path='',
                               started_at=datetime.utcnow() - timedelta(minutes=5))
        self.assertGreater(recorder._capture_stopped_at(seg),
                           datetime.utcnow() - timedelta(minutes=1))


if __name__ == '__main__':
    unittest.main()
