"""Tier 2 - a terminal recording's displayed duration is the ACTUAL recorded length,
never the scheduled window.

Guards BUGS.md 2026-07-18: FAILED/ABORTED recordings never concatenate or ffprobe, so
they have no recorded_duration_seconds; the UI fell through to Recording.duration_seconds
(stop_time - start_time = the scheduled window), showing e.g. 3h4m for a recording that
only captured ~1h6m. Recording.actual_duration_seconds now resolves the real length:
ffprobe of the final file when present, else the captured-segment span.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import RecordingSegment  # noqa: E402


class ActualDurationTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_failed_uses_captured_segment_span_not_window(self):
        # 3h scheduled window, but only ~1h of data captured in seg 0; segs 1-2 are
        # 0-byte stall retries that must not count.
        start = datetime(2026, 7, 17, 2, 0, 0)
        stop = start + timedelta(hours=3)
        rec = seed.make_recording(status='FAILED', channel_id=self.ch.id,
                                  start_time=start, stop_time=stop)
        seg_end = start + timedelta(hours=1, minutes=6, seconds=56)
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=0,
            file_path='/tmp/seed_seg_000.ts', started_at=start, ended_at=seg_end,
            exit_reason='STALL_KILLED', bytes_recorded=3_800_000_000))
        # Two 0-byte retries - captured nothing, must be excluded.
        for n, off in ((1, 70), (2, 95)):
            db.session.add(RecordingSegment(
                recording_id=rec.id, segment_number=n,
                file_path=f'/tmp/seed_seg_00{n}.ts',
                started_at=seg_end + timedelta(seconds=off),
                ended_at=seg_end + timedelta(seconds=off + 21),
                exit_reason='STALL_KILLED', bytes_recorded=0))
        db.session.commit()

        expected = (seg_end - start).total_seconds()
        self.assertEqual(rec.actual_duration_seconds, expected)
        self.assertEqual(rec.actual_duration_source, 'segments')
        # Sanity: it is NOT the 3h scheduled window.
        self.assertNotEqual(rec.actual_duration_seconds, rec.duration_seconds)

    def test_ffprobe_file_duration_wins_over_segments(self):
        start = datetime(2026, 7, 17, 2, 0, 0)
        stop = start + timedelta(hours=3)
        rec = seed.make_recording(status='COMPLETED', channel_id=self.ch.id,
                                  start_time=start, stop_time=stop,
                                  recorded_duration_seconds=3661.0)
        # A captured segment exists, but the ffprobed final-file duration must win.
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=0,
            file_path='/tmp/seed_seg_000.ts', started_at=start,
            ended_at=start + timedelta(hours=1), exit_reason='STOP_TIME_REACHED',
            bytes_recorded=1_000_000))
        db.session.commit()

        self.assertEqual(rec.actual_duration_seconds, 3661.0)
        self.assertEqual(rec.actual_duration_source, 'file')

    def test_non_terminal_has_no_actual_duration(self):
        for status in ('SCHEDULED', 'IN_PROGRESS'):
            rec = seed.make_recording(status=status, channel_id=self.ch.id)
            db.session.commit()
            self.assertIsNone(rec.actual_duration_seconds, status)
            self.assertIsNone(rec.actual_duration_source, status)


if __name__ == '__main__':
    unittest.main()
