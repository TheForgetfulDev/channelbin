"""Editing a SCHEDULED recording re-baselines its scheduled_* window.

Guards dev/docs/BUGS.md 2026-08-05 - after editing a SCHEDULED recording's stop time,
the recordings list kept reporting the ORIGINAL scheduled duration ("1h15m" on a
recording just extended to 1h25m), because _apply_edit_and_reschedule wrote
start_time/stop_time and never touched the scheduled_* pair.

The semantics these tests pin down: scheduled_* is an audit trail of *execution*
drift (started late / stopped early / aborted), not of user intent. A user edit
happens before anything has run - it is a new plan, not a deviation from the old
one - so the pair follows it, and the RECORDING_EDITED event carries the previous
times. Runtime drift must still be preserved.
"""

import unittest
from datetime import datetime
from unittest.mock import patch

from app.database import db, Recording, RecordingEvent, RECORDING_EDITED
from app.routes.recordings import _apply_edit_and_reschedule, _index_row
from app.tz_utils import get_display_tz
from tests.support import make_test_app
from tests.support.seed import make_recording


class ScheduledWindowRebaselineTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.now = datetime(2026, 8, 2, 6, 0, 0)
        self.start = datetime(2026, 8, 2, 8, 0, 0)
        self.stop = datetime(2026, 8, 2, 9, 15, 0)      # 1h15m, as originally scheduled

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _make_scheduled(self):
        rec = make_recording(status='SCHEDULED', name='Game',
                             start_time=self.start, stop_time=self.stop)
        db.session.commit()
        return rec.id

    def _edit(self, rid, start, stop):
        """Drive the real edit path. The APScheduler re-registration is patched out:
        this app has no running scheduler, and the defect is in the DB write, which
        _apply_edit_and_reschedule deliberately commits before any job side effect."""
        rec = db.session.get(Recording, rid)
        with patch('app.routes.recordings.schedule_recording'), \
             patch('app.routes.recordings.unschedule_recording'):
            _apply_edit_and_reschedule(rid, rec.name, rec.url, start, stop)
        db.session.expire_all()
        return db.session.get(Recording, rid)

    def test_extending_a_scheduled_recording_moves_scheduled_stop_time(self):
        """The exact reproduction: recording 76, 09:15 -> 09:25 (dev/changelog/471)."""
        rid = self._make_scheduled()
        new_stop = datetime(2026, 8, 2, 9, 25, 0)

        rec = self._edit(rid, self.start, new_stop)

        self.assertEqual(rec.stop_time, new_stop)
        self.assertEqual(rec.scheduled_stop_time, new_stop)

    def test_moving_the_start_moves_scheduled_start_time(self):
        rid = self._make_scheduled()
        new_start = datetime(2026, 8, 2, 7, 30, 0)

        rec = self._edit(rid, new_start, self.stop)

        self.assertEqual(rec.start_time, new_start)
        self.assertEqual(rec.scheduled_start_time, new_start)

    def test_scheduled_duration_follows_the_edit(self):
        rid = self._make_scheduled()

        rec = self._edit(rid, self.start, datetime(2026, 8, 2, 9, 25, 0))

        self.assertEqual(rec.scheduled_duration_seconds, 85 * 60)

    def test_recordings_list_shows_the_new_duration_for_a_scheduled_row(self):
        """The surface actually seen: _index_row renders scheduled_duration_seconds
        for a SCHEDULED row, so a stale pair showed '1h 15m' after extending to 1h25m."""
        rid = self._make_scheduled()

        self._edit(rid, self.start, datetime(2026, 8, 2, 9, 25, 0))
        rec = db.session.get(Recording, rid)
        row = _index_row(rec, self.now, get_display_tz(), set())

        self.assertEqual(row['dur_str'], '1h 25m')

    def test_shortening_an_edit_does_not_leave_the_row_flagged_partial(self):
        """recordings.py's 'partial' flag is actual < scheduled * 0.98. A recording
        shortened by an edit and then captured in full must not read as partial."""
        rid = self._make_scheduled()
        short_stop = datetime(2026, 8, 2, 8, 30, 0)     # 1h15m -> 30m

        self._edit(rid, self.start, short_stop)

        @db.session.no_autoflush
        def _complete():
            rec = db.session.get(Recording, rid)
            rec.status = 'COMPLETED'
            rec.completed_at = short_stop
            rec.recorded_duration_seconds = 30 * 60
            db.session.commit()
        _complete()

        rec = db.session.get(Recording, rid)
        row = _index_row(rec, datetime(2026, 8, 2, 10, 0, 0), get_display_tz(), set())

        self.assertIsNone(row['dur_flag'])

    def test_the_edit_event_still_records_the_previous_times(self):
        """Re-baselining is only safe because the audit trail moved here, not vanished."""
        rid = self._make_scheduled()
        new_stop = datetime(2026, 8, 2, 9, 25, 0)

        self._edit(rid, self.start, new_stop)

        ev = (RecordingEvent.query
              .filter_by(recording_id=rid, event_type=RECORDING_EDITED).one())
        self.assertIn('09:15:00', ev.detail)
        self.assertIn('09:25:00', ev.detail)

    def test_an_edit_that_changes_nothing_leaves_the_window_alone(self):
        rid = self._make_scheduled()

        rec = self._edit(rid, self.start, self.stop)

        self.assertEqual(rec.scheduled_start_time, self.start)
        self.assertEqual(rec.scheduled_stop_time, self.stop)


class RuntimeDriftIsStillPreservedTests(unittest.TestCase):
    """The other half of the semantics: the fix must not turn scheduled_* into a
    second copy of start_time/stop_time. Runtime adjustments (app/recorder.py:
    started late, stopped early, aborted) still leave the original window standing.
    """

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_stopping_early_keeps_the_original_scheduled_window(self):
        start = datetime(2026, 7, 26, 18, 0, 0)
        stop = datetime(2026, 7, 26, 22, 30, 0)
        rec = make_recording(status='IN_PROGRESS', start_time=start, stop_time=stop)
        rid = rec.id
        db.session.commit()

        # what recorder.py does when the user stops early
        rec = db.session.get(Recording, rid)
        rec.stop_time = datetime(2026, 7, 26, 21, 55, 0)
        rec.status = 'COMPLETED'
        db.session.commit()

        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.scheduled_stop_time, stop)
        self.assertEqual(rec.scheduled_duration_seconds, 4.5 * 3600)
        self.assertLess(rec.duration_seconds, rec.scheduled_duration_seconds)


if __name__ == '__main__':
    unittest.main()
