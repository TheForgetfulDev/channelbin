"""Retention slice-boundary units for the channel-tester cleanup helpers
(app/channel_tester.py::_cleanup_old_screenshots / _cleanup_old_tests).

Pins two things:
  * the keep-newest-N slice semantics (`tests[keep_count:]`) at the 0 / 1 / N boundaries,
    including the deliberate asymmetry at keep_count=0 (screenshots prune all; tests keep
    all via the `keep_count <= 0` guard - a `[:-0]`-style no-op would silently keep all);
  * the per-(channel_id, job_id) scoping (BUGS.md 2026-07-12 retention-scoping class): one
    job's history must never be evicted by another job's runs on the same channel.

DB-backed (make_test_app temp DB); real screenshot files are created so unlink is exercised.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import ChannelTest  # noqa: E402
from app.channel_tester import _cleanup_old_screenshots, _cleanup_old_tests  # noqa: E402


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc)
        self._shots = []
        db.session.commit()

    def tearDown(self):
        for p in self._shots:
            try:
                os.unlink(p)
            except OSError:
                pass
        self.t.cleanup()

    def _make_tests(self, n, job_id=None, with_shots=True):
        """n ChannelTests for self.ch, newest first (test 0 is newest). Returns rows."""
        base = datetime(2026, 7, 17, 12, 0)
        rows = []
        for i in range(n):
            shot = None
            if with_shots:
                shot = os.path.join(self.t._tmpdir, f'shot_{job_id}_{i}.jpg')
                with open(shot, 'wb') as f:
                    f.write(b'x')
                self._shots.append(shot)
            row = ChannelTest(
                channel_id=self.ch.id, job_id=job_id,
                test_started_at=base - timedelta(minutes=i),   # i=0 newest
                status='COMPLETED', screenshot_path=shot)
            db.session.add(row)
            rows.append(row)
        db.session.commit()
        return rows

    def _shot_count_on_disk(self):
        return sum(os.path.exists(p) for p in self._shots)

    # ── screenshot slice boundaries ──────────────────────────────────────────
    def test_screenshots_keep_newest_n(self):
        self._make_tests(5)
        _cleanup_old_screenshots(self.t.app, self.ch.id, None, self.t._tmpdir, keep_count=2)
        self.assertEqual(self._shot_count_on_disk(), 2)      # 2 newest files remain
        pruned = ChannelTest.query.filter_by(screenshot_pruned=True).count()
        self.assertEqual(pruned, 3)                          # 3 oldest rows marked

    def test_screenshots_keep_count_1(self):
        self._make_tests(3)
        _cleanup_old_screenshots(self.t.app, self.ch.id, None, self.t._tmpdir, keep_count=1)
        self.assertEqual(self._shot_count_on_disk(), 1)

    def test_screenshots_keep_count_0_prunes_all(self):
        # keep_count=0 → tests[0:] → every screenshot pruned (NOT a [:-0] no-op)
        self._make_tests(3)
        _cleanup_old_screenshots(self.t.app, self.ch.id, None, self.t._tmpdir, keep_count=0)
        self.assertEqual(self._shot_count_on_disk(), 0)

    def test_screenshots_scoped_per_job(self):
        self._make_tests(3, job_id=None)   # guide-test scope
        self._make_tests(3, job_id=7)      # on-demand job scope
        _cleanup_old_screenshots(self.t.app, self.ch.id, None, self.t._tmpdir, keep_count=1)
        # only the guide-test (job_id=None) scope is pruned; job 7's 3 shots untouched
        job7_pruned = ChannelTest.query.filter_by(job_id=7, screenshot_pruned=True).count()
        self.assertEqual(job7_pruned, 0)

    # ── ChannelTest-row slice boundaries ─────────────────────────────────────
    def test_tests_keep_newest_n(self):
        self._make_tests(5, with_shots=False)
        _cleanup_old_tests(self.t.app, self.ch.id, None, keep_count=2)
        self.assertEqual(ChannelTest.query.count(), 2)

    def test_tests_keep_count_0_keeps_all(self):
        # asymmetry vs screenshots: the `keep_count <= 0` guard means 0 keeps everything
        self._make_tests(4, with_shots=False)
        _cleanup_old_tests(self.t.app, self.ch.id, None, keep_count=0)
        self.assertEqual(ChannelTest.query.count(), 4)

    def test_tests_scoped_per_job(self):
        self._make_tests(3, job_id=None, with_shots=False)
        self._make_tests(3, job_id=7, with_shots=False)
        _cleanup_old_tests(self.t.app, self.ch.id, None, keep_count=1)
        # guide scope trimmed to 1; job 7's 3 rows survive
        self.assertEqual(ChannelTest.query.filter_by(job_id=7).count(), 3)
        self.assertEqual(ChannelTest.query.filter(ChannelTest.job_id.is_(None)).count(), 1)

    def test_tests_delete_unlinks_screenshot_files(self):
        # dev/docs/BUGS.md 2026-08-30: rows pruned by _cleanup_old_tests (as opposed to
        # _cleanup_old_screenshots above) never unlinked their screenshot_path, so a
        # test_history_keep smaller than screenshots_keep_count orphaned files forever -
        # nothing referenced them once the row was gone.
        self._make_tests(5, with_shots=True)
        self.assertEqual(self._shot_count_on_disk(), 5)
        _cleanup_old_tests(self.t.app, self.ch.id, None, keep_count=2)
        self.assertEqual(ChannelTest.query.count(), 2)
        self.assertEqual(self._shot_count_on_disk(), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
