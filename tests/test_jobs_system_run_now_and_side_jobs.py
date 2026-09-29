"""/jobs: Run Now on the system jobs, and a recording's side jobs named on its own row
(dev/changelog/1159).

Before this, precheck_<id>, retry_<id> and resume_<id> fell through _build_job_list's
catch-all: a raw id, a Recurring badge, and a Skip next run the route answered 400 to. Every
scheduled recording has a precheck_<id>, so the page carried one such row per recording.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
    python3 -m unittest tests.test_jobs_system_run_now_and_side_jobs
"""
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402


class RecordingSideJobTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-09-29 07:53: a recording's precheck/retry/resume job is a line on
    that recording's row, or a named one-off row - never a raw id offered as recurring."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        from app import db
        self.db = db
        now = datetime.utcnow()
        self.start = now + timedelta(hours=3)
        self.stop = now + timedelta(hours=4)

    def tearDown(self):
        self.t.cleanup()

    def _items(self):
        from app.routes.jobs import _build_job_list
        return {i['id']: i for i in _build_job_list()}

    def test_precheck_is_a_line_on_the_scheduled_recordings_row(self):
        from app import scheduler as sched
        rec = seed.make_recording(status='SCHEDULED', name='Match',
                                  start_time=self.start, stop_time=self.stop)
        self.db.session.commit()
        sched.schedule_recording(self.t.app, rec.id, self.start, self.stop)
        self.assertIsNotNone(sched.get_scheduler().get_job(f'precheck_{rec.id}'))

        items = self._items()
        self.assertNotIn(f'precheck_{rec.id}', items)
        lines = items[f'start_{rec.id}'].get('pending_lines', [])
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('pre-recording check '))

    def test_retry_and_resume_are_lines_on_the_active_recordings_row(self):
        from app import scheduler as sched
        rec = seed.make_recording(status='RETRYING', name='Live',
                                  start_time=datetime.utcnow() - timedelta(minutes=10),
                                  stop_time=self.stop)
        self.db.session.commit()
        sched._register_stop_job(rec)
        sched.schedule_dead_stream_retry(rec.id, self.start)
        sched.reschedule_recording_resume(rec.id, self.start)

        items = self._items()
        self.assertNotIn(f'retry_{rec.id}', items)
        self.assertNotIn(f'resume_{rec.id}', items)
        lines = items[f'active_{rec.id}'].get('pending_lines', [])
        self.assertEqual(sorted(line.split(' ')[0] for line in lines), ['dead-stream', 'resumes'])

    def test_a_side_job_without_its_recordings_row_is_a_named_one_off(self):
        from app import scheduler as sched
        rec = seed.make_recording(status='RETRYING', name='Orphaned stop',
                                  start_time=datetime.utcnow() - timedelta(minutes=10),
                                  stop_time=self.stop)
        self.db.session.commit()
        sched.schedule_dead_stream_retry(rec.id, self.start)  # no stop job registered

        item = self._items()[f'retry_{rec.id}']
        self.assertEqual(item['display_name'], 'Dead-stream retry: Orphaned stop')
        self.assertEqual(item['type'], 'one_off')
        self.assertNotIn('skip_url', item)
        self.assertNotIn('run_url', item)
        self.assertEqual(item['edit_url'], f'/recordings/{rec.id}')

    def test_no_date_triggered_job_is_offered_as_recurring(self):
        """The catch-all shape the defect had: whatever lands there must not be a one-shot."""
        from app import scheduler as sched
        rec = seed.make_recording(status='SCHEDULED', start_time=self.start,
                                  stop_time=self.stop)
        self.db.session.commit()
        sched.schedule_recording(self.t.app, rec.id, self.start, self.stop)
        date_ids = {j.id for j in sched.get_scheduler().get_jobs()
                    if type(j.trigger).__name__ == 'DateTrigger'}
        for job_id, item in self._items().items():
            if job_id in date_ids:
                self.assertNotEqual(item['type'], 'recurring', job_id)
                self.assertNotIn('skip_url', item, job_id)


class SystemJobRunNowTests(unittest.TestCase):
    """Run Now works for the cheap system jobs, and the two heavy ones ask admission in the
    request - a refusal comes back as a 409 naming the blocker, never a silent deferral."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_listed_system_jobs_offer_run_now(self):
        from app.routes.jobs import _build_job_list
        items = {i['id']: i for i in _build_job_list()}
        for job_id in ('recording_retention_daily', 'db_maintenance_daily',
                       'storage_dirs_check', 'account_stats_fold'):
            self.assertEqual(items[job_id].get('run_url'), f'/api/jobs/{job_id}/run-now')
            self.assertNotIn('run_disabled_reason', items[job_id])

    def test_cheap_job_runs_on_a_thread(self):
        from app import scheduler as sched
        ran = threading.Event()
        with mock.patch.dict(sched._RUN_NOW_PLAIN, {'storage_dirs_check': ran.set}):
            resp = self.client.post('/api/jobs/storage_dirs_check/run-now')
            self.assertEqual(resp.status_code, 200, resp.get_json())
            self.assertTrue(resp.get_json()['success'])
            self.assertTrue(ran.wait(5))

    def test_heavy_job_refused_by_admission_answers_409_and_does_not_run(self):
        from app import admission
        from app import scheduler as sched
        sweep = mock.Mock()
        blocker = admission.try_start(admission.KIND_SYNC, 'account 1')
        try:
            with mock.patch.dict(sched._RUN_NOW_ADMITTED,
                                 {'db_maintenance_daily': ('database maintenance', sweep)}):
                resp = self.client.post('/api/jobs/db_maintenance_daily/run-now')
        finally:
            admission.release(blocker)
        self.assertEqual(resp.status_code, 409)
        self.assertIn('an account sync', resp.get_json()['error'])
        sweep.assert_not_called()

    def test_heavy_job_admitted_runs_and_releases_its_ticket(self):
        from app import admission
        from app import scheduler as sched
        ran = threading.Event()
        seen_kinds = []

        def sweep():
            seen_kinds.extend(admission.active_kinds())
            ran.set()

        with mock.patch.dict(sched._RUN_NOW_ADMITTED,
                             {'recording_retention_daily': ('recording retention', sweep)}):
            resp = self.client.post('/api/jobs/recording_retention_daily/run-now')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue(ran.wait(5))
        self.assertIn(admission.KIND_MAINTENANCE, seen_kinds)
        for _ in range(50):
            if admission.KIND_MAINTENANCE not in admission.active_kinds():
                break
            threading.Event().wait(0.05)
        self.assertNotIn(admission.KIND_MAINTENANCE, admission.active_kinds())


if __name__ == '__main__':
    unittest.main()
