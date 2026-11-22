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
    Recording, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING)
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


if __name__ == '__main__':
    unittest.main()
