"""Tier 2 - a new end on a running recording moves its stop job and nothing else.

dev/docs/BUGS.md 2026-11-21 @ 07:28:32 PM: the TV Guide's change-the-end and the recording
page's +30 min both post to /recordings/<id>/adjust-stop, which unscheduled every job the
recording had and then ran schedule_recording(). That re-armed start_<id> and a pre-check at a
start time already passed (the start fired at once and logged "already IN_PROGRESS,
skipping"), and the unschedule also dropped a RETRYING recording's pending retry_<id> and a
deferred resume_<id>, so a dead stream waiting out its backoff was never tried again before
the stop. Same shape as the resume fix in dev/changelog/1242.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.scheduler as schedmod  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, RECORDING_STOP_TIME_ADJUSTED, REC_STATUS_COMPLETED,
    REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING)
from app.tz_utils import local_input_value  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402


class AdjustStopReArmsTheStopJobOnlyTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)

    def tearDown(self):
        self.t.cleanup()

    def _adjust(self, status):
        rec = seed.make_recording(status=status, name='running')
        db.session.commit()
        rid = rec.id
        new_stop = (datetime.utcnow() + timedelta(hours=3)).replace(second=0, microsecond=0)
        added, removed = [], []

        def _add_job(**kwargs):
            added.append((kwargs['id'], kwargs.get('run_date')))

        def _remove(job_id):
            removed.append(job_id)
            return True

        with mock.patch.object(schedmod, '_add_job', _add_job), \
             mock.patch.object(schedmod, 'remove_job_if_exists', _remove):
            resp = self.t.client.post(f'/recordings/{rid}/adjust-stop', json={
                'stop_time': local_input_value(new_stop),
                'stop_time_utc': new_stop.strftime('%Y-%m-%dT%H:%M:%SZ')})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).stop_time, new_stop)
        return rid, new_stop, added, removed

    def test_an_in_progress_recording_gets_its_stop_job_and_nothing_else(self):
        rid, new_stop, added, _ = self._adjust(REC_STATUS_IN_PROGRESS)
        self.assertEqual(added, [(f'stop_{rid}', new_stop)])

    def test_a_retrying_recording_keeps_its_pending_retry(self):
        rid, new_stop, added, removed = self._adjust(REC_STATUS_RETRYING)
        self.assertNotIn(f'retry_{rid}', removed)
        self.assertEqual(removed, [])
        self.assertEqual(added, [(f'stop_{rid}', new_stop)])

    def test_a_paused_recording_keeps_its_deferred_resume(self):
        rid, new_stop, added, removed = self._adjust(REC_STATUS_PAUSED)
        self.assertNotIn(f'resume_{rid}', removed)
        self.assertEqual(removed, [])
        self.assertEqual(added, [(f'stop_{rid}', new_stop)])



class AdjustStopWriteTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-11-26: the stop-time change's event printed the two stored UTC
    datetimes, and the write did not check again that the recording was still open. The
    locked-database case is a characterization test: the write was retried before too, by a
    decorator on the whole route, and is now retried as its own unit (dev/changelog/1759)."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.t.sandbox_config({'display': {'timezone': 'America/New_York', 'time_format': '12h'}})
        # Noon UTC on a January day: 7:00 AM Eastern, so the two clocks cannot be confused.
        self.old_stop = datetime(2099, 1, 15, 12, 0)
        self.new_stop = datetime(2099, 1, 15, 13, 30)
        rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='running',
                                  start_time=datetime.utcnow() - timedelta(minutes=5),
                                  stop_time=self.old_stop)
        db.session.commit()
        self.rid = rec.id

    def post(self):
        with mock.patch.object(schedmod, '_add_job'), \
                mock.patch.object(schedmod, 'remove_job_if_exists', return_value=True):
            return self.t.client.post(f'/recordings/{self.rid}/adjust-stop', json={
                'stop_time': local_input_value(self.new_stop),
                'stop_time_utc': self.new_stop.strftime('%Y-%m-%dT%H:%M:%SZ')})

    def events(self):
        db.session.expire_all()
        return RecordingEvent.query.filter_by(recording_id=self.rid,
                                              event_type=RECORDING_STOP_TIME_ADJUSTED).all()

    def test_a_locked_database_is_retried_and_the_change_lands_once(self):
        from sqlalchemy.exc import OperationalError
        real_commit = db.session.commit
        calls = []

        def locked_once():
            calls.append(1)
            if len(calls) == 1:
                raise OperationalError('UPDATE recordings', {}, Exception('database is locked'))
            return real_commit()

        with mock.patch.object(db.session, 'commit', locked_once), \
                mock.patch('app.db_utils.time.sleep'):
            resp = self.post()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(db.session.get(Recording, self.rid).stop_time, self.new_stop)

    def test_the_event_says_both_times_in_the_display_timezone(self):
        self.assertEqual(self.post().status_code, 200)
        (ev,) = self.events()
        self.assertIn('7:00 AM', ev.detail)
        self.assertIn('8:30 AM', ev.detail)
        self.assertNotIn('12:00:00', ev.detail)
        self.assertNotIn('13:30:00', ev.detail)

    def test_a_recording_that_ended_before_the_write_is_refused(self):
        real_get = db.session.get
        seen = []

        def get_then_end(model, ident, **kw):
            if model is Recording and ident == self.rid:
                seen.append(1)
                if len(seen) == 2:
                    db.session.execute(db.text('UPDATE recordings SET status = :s WHERE id = :i'),
                                       {'s': REC_STATUS_COMPLETED, 'i': self.rid})
                    db.session.commit()
            return real_get(model, ident, **kw)

        with mock.patch.object(db.session, 'get', get_then_end):
            resp = self.post()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.events(), [])
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).stop_time, self.old_stop)

if __name__ == '__main__':
    unittest.main()
