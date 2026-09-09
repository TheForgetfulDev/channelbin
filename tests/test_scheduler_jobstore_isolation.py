"""Tier 2 - the APScheduler jobstore must stay inside the test sandbox.

Guards BUGS.md 2026-07-18: init_scheduler() built its jobstore engine from a runtime
load_config() (the real config.yaml) instead of the app it was handed, so every test using
make_test_app(start_scheduler=True) wrote apscheduler_jobs rows into the production dvr.db.
Those rows reference test-module functions, and production then logged
"Unable to restore job ... could not import module 'test_contention'" at every startup and
dropped them.

Both directions are asserted: the jobstore engine points at the temp DB, and the real DB
gains no rows from a test that adds a job.
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app.config import load_config  # noqa: E402
from app.scheduler import _add_job, get_scheduler, remove_job_if_exists  # noqa: E402


def _noop_job():
    """Module-level so SQLAlchemyJobStore can serialize it by reference."""
    pass


def _prod_job_ids():
    """Job ids in the configured production DB, or None if it has no jobstore table."""
    path = load_config()['database']['path']
    if not path or not os.path.isfile(path):
        return None
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='apscheduler_jobs'"
        ).fetchall()
        if not rows:
            return None
        return {r[0] for r in conn.execute('SELECT id FROM apscheduler_jobs')}
    finally:
        conn.close()


class JobstoreSandboxTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        self.t.cleanup()

    def test_jobstore_engine_points_at_temp_db(self):
        url = str(get_scheduler()._lookup_jobstore('default').engine.url)
        self.assertIn(self.t._tmpdir, url)
        self.assertNotIn(load_config()['database']['path'], url)

    def test_adding_a_job_never_reaches_the_production_db(self):
        before = _prod_job_ids()
        if before is None:
            self.skipTest('no production DB with a jobstore table on this machine')

        job_id = 'sandbox_probe_job'
        try:
            _add_job(func=_noop_job, trigger='date',
                     run_date=datetime.utcnow() + timedelta(days=3650),
                     id=job_id, replace_existing=True)
            self.assertIsNotNone(get_scheduler().get_job(job_id))  # it really was written
            self.assertEqual(_prod_job_ids(), before,
                             'test scheduler wrote into the production jobstore')
        finally:
            remove_job_if_exists(job_id)


if __name__ == '__main__':
    unittest.main(verbosity=2)
