"""Guards dev/docs/BUGS.md 2026-08-14 "A crash during concatenation strands the row in
CONCATENATING forever, and it blocks restarts."

resume_in_progress_recordings() had explicit startup recovery for IN_PROGRESS, PAUSED,
CONVERTING and SCHEDULED rows, but nothing looked at CONCATENATING. A crash or a
restart-kill mid-concat left the row CONCATENATING with its segments intact on disk;
at startup it was silently skipped, so it rendered as concatenating forever with no
alert - and CONCATENATING sits in RESTART_BLOCKING_STATUSES, so every future restart
refused until an operator noticed and manually retried the concat.

No real ffmpeg: do_concatenation is monkeypatched, following the pattern used for the
sibling CONVERTING case in tests/test_restart_guard.py.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402


class ConcatenatingStartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        # resume_in_progress_recordings() also sweeps orphaned APScheduler on-demand
        # jobs unconditionally at the end, which needs a live scheduler.
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        self.t.cleanup()

    def _wait_for(self, fake, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if fake.called:
                return
            time.sleep(0.01)

    def test_concatenating_row_relaunches_concatenation(self):
        from app.scheduler import resume_in_progress_recordings

        rec = seed.make_recording(status='CONCATENATING', name='stranded concat')
        db.session.commit()
        rid = rec.id

        with mock.patch('app.concatenator.do_concatenation') as fake:
            resume_in_progress_recordings(self.t.app)
            self._wait_for(fake)

        self.assertTrue(fake.called,
                        'a CONCATENATING row at restart must relaunch do_concatenation')
        args, kwargs = fake.call_args
        self.assertEqual(args[0], self.t.app)
        self.assertEqual(args[1], rid)
        self.assertIn('restart', kwargs.get('reason', '').lower())

    def test_non_concatenating_rows_are_left_alone(self):
        from app.scheduler import resume_in_progress_recordings

        seed.make_recording(status='COMPLETED')
        seed.make_recording(status='FAILED')
        db.session.commit()

        with mock.patch('app.concatenator.do_concatenation') as fake:
            resume_in_progress_recordings(self.t.app)
            time.sleep(0.05)

        self.assertFalse(fake.called)

    def test_multiple_concatenating_rows_all_relaunch(self):
        from app.scheduler import resume_in_progress_recordings

        recs = [seed.make_recording(status='CONCATENATING', name=f'stranded {i}')
                for i in range(3)]
        db.session.commit()
        rids = {r.id for r in recs}

        with mock.patch('app.concatenator.do_concatenation') as fake:
            resume_in_progress_recordings(self.t.app)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and fake.call_count < 3:
                time.sleep(0.01)

        seen_ids = {c.args[1] for c in fake.call_args_list}
        self.assertEqual(seen_ids, rids)


if __name__ == '__main__':
    unittest.main()
