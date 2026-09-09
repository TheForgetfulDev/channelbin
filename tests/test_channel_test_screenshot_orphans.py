"""Every ChannelTest bulk-delete path unlinks its screenshot files (dev/docs/BUGS.md
2026-08-30).

Four paths deleted ChannelTest rows without unlinking each row's screenshot_path first:
teardown_test_job() (reached by a standalone job delete and by the group-dissolve
cascade), remove_channel_from_job(), remove_duplicate_channels(), and _cleanup_old_tests().
Once the row is gone nothing references the file any more, so no later cleanup pass can
ever find it - it accumulates in screenshot_dir forever. All four now go through the shared
app.channel_tester.delete_tests_collecting_screenshots() helper: query the matching rows'
screenshot_path values, bulk delete, return the paths. The actual os.unlink() happens via
app.recorder.delete_files() at each call site, always AFTER that site's own
retry_on_locked() commit has durably succeeded - a bulk delete can retry on a locked
database, and unlinking is a non-idempotent side effect that must not sit inside a closure
that might re-run (tests/test_static_invariants.py::RetryOnLockedSideEffectTests).

_cleanup_old_tests() itself is covered in tests/test_retention.py, alongside its sibling
_cleanup_old_screenshots() - this file covers the shared helper plus the other three
call sites, which are reached through their routes rather than called directly.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_channel_test_screenshot_orphans
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.database import ChannelTest  # noqa: E402
from app.channel_tester import delete_tests_collecting_screenshots  # noqa: E402
from app.recorder import delete_files  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self._shots = []

    def tearDown(self):
        for p in self._shots:
            try:
                os.unlink(p)
            except OSError:
                pass
        self.t.cleanup()

    def _shot(self, name):
        path = os.path.join(self.t._tmpdir, name)
        with open(path, 'wb') as f:
            f.write(b'x')
        self._shots.append(path)
        return path


class DeleteTestsCollectingScreenshotsHelperTests(_Base):
    """Direct unit coverage of the shared DB helper plus app.recorder.delete_files(), the
    two pieces every call site composes (never a single call, per the retry-closure rule
    above)."""

    def test_returns_paths_and_deletes_rows(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        shot = self._shot('a.jpg')
        ct = seed.make_channel_test(ch, screenshot_path=shot)
        db.session.commit()

        paths = delete_tests_collecting_screenshots(ChannelTest.query.filter_by(id=ct.id))
        db.session.commit()

        self.assertEqual(paths, [shot])
        self.assertTrue(os.path.exists(shot))  # the helper itself never unlinks
        self.assertEqual(ChannelTest.query.count(), 0)

        delete_files(paths)
        self.assertFalse(os.path.exists(shot))

    def test_missing_file_on_disk_does_not_raise(self):
        """A row whose file is already gone (a prior prune, a hand-edited row) must not
        crash delete_files() - best-effort unlink only."""
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        ct = seed.make_channel_test(ch, screenshot_path='/nonexistent/dir/x.jpg')
        db.session.commit()

        paths = delete_tests_collecting_screenshots(ChannelTest.query.filter_by(id=ct.id))
        db.session.commit()

        delete_files(paths)  # must not raise
        self.assertEqual(ChannelTest.query.count(), 0)

    def test_null_screenshot_path_is_skipped(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        ct = seed.make_channel_test(ch, screenshot_path=None)
        db.session.commit()

        paths = delete_tests_collecting_screenshots(ChannelTest.query.filter_by(id=ct.id))
        db.session.commit()

        self.assertEqual(paths, [])
        self.assertEqual(ChannelTest.query.count(), 0)


class JobDeleteUnlinksScreenshotsTests(_Base):
    """teardown_test_job(), reached by DELETE /api/channel-tests/on-demand/<id>."""

    def test_deleting_a_job_unlinks_its_channel_tests_screenshots(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        job = seed.make_test_job(name='Job', channels=[ch])
        shot = self._shot('job.jpg')
        seed.make_channel_test(ch, job_id=job.id, screenshot_path=shot)
        db.session.commit()

        resp = self.client.delete(f'/api/channel-tests/on-demand/{job.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(shot))
        self.assertEqual(ChannelTest.query.count(), 0)


class GroupCascadeDeleteUnlinksScreenshotsTests(_Base):
    """teardown_test_job(), reached via the group-dissolve cascade
    (POST /api/channel-groups/<id>/delete)."""

    def test_dissolving_a_group_unlinks_its_jobs_channel_tests_screenshots(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        job = seed.make_test_job(name='Job', channels=[ch])
        shot = self._shot('cascade.jpg')
        seed.make_channel_test(ch, job_id=job.id, screenshot_path=shot)
        db.session.commit()

        resp = self.client.post(f'/api/channel-groups/{job.group_id}/delete', json={})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(shot))
        self.assertEqual(ChannelTest.query.count(), 0)


class RemoveChannelFromJobUnlinksScreenshotTests(_Base):
    """remove_channel_from_job(), DELETE .../on-demand/<job_id>/channels/<channel_id>."""

    def test_removing_one_channel_unlinks_only_its_own_screenshot(self):
        acc = seed.make_account()
        a = seed.make_channel(acc, name='A')
        b = seed.make_channel(acc, name='B')
        job = seed.make_test_job(name='Job', channels=[a, b])
        shot_a = self._shot('a.jpg')
        shot_b = self._shot('b.jpg')
        seed.make_channel_test(a, job_id=job.id, screenshot_path=shot_a)
        seed.make_channel_test(b, job_id=job.id, screenshot_path=shot_b)
        db.session.commit()

        resp = self.client.delete(f'/api/channel-tests/on-demand/{job.id}/channels/{a.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(shot_a))
        self.assertTrue(os.path.exists(shot_b))
        self.assertEqual(ChannelTest.query.filter_by(channel_id=a.id).count(), 0)


class RemoveDuplicateChannelsUnlinksScreenshotsTests(_Base):
    """remove_duplicate_channels(), POST .../on-demand/<job_id>/remove-duplicates."""

    def test_removing_a_duplicate_unlinks_its_screenshot(self):
        acc = seed.make_account()
        a = seed.make_channel(acc, name='A')
        b = seed.make_channel(acc, name='B')
        a.stream_url = b.stream_url = 'http://example.test/live/shared'
        job = seed.make_test_job(name='Job', channels=[a, b])
        shot_a = self._shot('dup.jpg')
        seed.make_channel_test(a, job_id=job.id, screenshot_path=shot_a)
        db.session.commit()

        resp = self.client.post(
            f'/api/channel-tests/on-demand/{job.id}/remove-duplicates',
            json={'removals': [{'channel_id': a.id, 'keep_channel_id': b.id}], 'transfer': False})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(shot_a))
        self.assertEqual(ChannelTest.query.filter_by(channel_id=a.id).count(), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
