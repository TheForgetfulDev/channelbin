"""Post-conversion move must not clobber, and deleting a recording must be able to keep files.

Two independent behaviors shipped together in dev/changelog/587:

1. `postprocessor.collision_safe_dest()` - `shutil.move()` silently overwrites an existing
   destination file, so two recordings rendering to the same filename (a re-record of the
   same program on the same day) destroyed the first one's file while its DB row went on
   pointing at a path that now held the *second* recording's content.

2. `delete_files` on the two delete routes - deleting a recording always destroyed its
   files. The toggle defaults to on, so an omitted key keeps the previous behavior for
   every existing caller (the guide's unschedule paths, any external POST).

Test-seam note: files live under the test's own temp dir and are addressed via the DB row's
own output_path / segment file_path (recording_disk_paths reads those off the row), so
nothing here touches the real /dvr.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Recording  # noqa: E402
from app.postprocessor import collision_safe_dest  # noqa: E402


def _touch(path, data=b'\x00' * 32):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


class CollisionSafeDestTests(unittest.TestCase):
    """collision_safe_dest() never returns a path that would overwrite a different file."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.src_dir = os.path.join(self._dir.name, 'src')
        self.dest_dir = os.path.join(self._dir.name, 'dest')
        os.makedirs(self.src_dir)
        os.makedirs(self.dest_dir)
        self.src = os.path.join(self.src_dir, 'Show.mp4')
        _touch(self.src)

    def tearDown(self):
        self._dir.cleanup()

    def test_free_name_is_used_as_is(self):
        self.assertEqual(collision_safe_dest(self.dest_dir, self.src),
                         os.path.join(self.dest_dir, 'Show.mp4'))

    def test_occupied_name_gets_a_suffix_and_does_not_overwrite(self):
        occupant = os.path.join(self.dest_dir, 'Show.mp4')
        _touch(occupant, b'first recording')
        dest = collision_safe_dest(self.dest_dir, self.src)
        self.assertNotEqual(dest, occupant)
        self.assertEqual(os.path.basename(dest), 'Show_2.mp4')
        # The extension is preserved, not appended after the suffix.
        self.assertTrue(dest.endswith('.mp4'))
        # And the occupant is untouched by the mere act of picking a name.
        with open(occupant, 'rb') as f:
            self.assertEqual(f.read(), b'first recording')

    def test_suffix_counts_up_past_existing_suffixed_files(self):
        _touch(os.path.join(self.dest_dir, 'Show.mp4'))
        _touch(os.path.join(self.dest_dir, 'Show_2.mp4'))
        _touch(os.path.join(self.dest_dir, 'Show_3.mp4'))
        self.assertEqual(os.path.basename(collision_safe_dest(self.dest_dir, self.src)),
                         'Show_4.mp4')

    def test_destination_equal_to_current_dir_is_a_no_op(self):
        """The file is already the occupant of its own name - renaming it to _2 would be
        wrong, so the source path comes back unchanged and the caller skips the move."""
        self.assertEqual(collision_safe_dest(self.src_dir, self.src), self.src)

    def test_extensionless_name_still_gets_a_suffix(self):
        src = os.path.join(self.src_dir, 'Show')
        _touch(src)
        _touch(os.path.join(self.dest_dir, 'Show'))
        self.assertEqual(os.path.basename(collision_safe_dest(self.dest_dir, src)), 'Show_2')


class DeleteRecordingFileToggleTests(unittest.TestCase):
    """Both delete routes honor delete_files, and default to deleting."""

    def setUp(self):
        self.t = make_test_app()
        # These tests exercise the delete routes' own logic; CSRF rejection is covered by
        # tests/test_csrf_envelope.py and would otherwise 400 every request here.
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _completed_recording_with_file(self):
        """A COMPLETED row whose output_path is a real file inside the test's temp dir."""
        rec = seed.make_recording(status='COMPLETED')
        path = os.path.join(self.t._tmpdir, f'out_{rec.id}.mp4')
        _touch(path)
        rec.output_path = path
        db.session.commit()
        return rec.id, path

    def test_json_delete_removes_files_by_default(self):
        rec_id, path = self._completed_recording_with_file()
        resp = self.t.client.post(f'/recordings/{rec_id}/delete-json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        self.assertFalse(os.path.exists(path), 'file should be gone when the flag is omitted')
        self.assertIsNone(db.session.get(Recording, rec_id))

    def test_json_delete_keeps_files_when_opted_out(self):
        rec_id, path = self._completed_recording_with_file()
        resp = self.t.client.post(f'/recordings/{rec_id}/delete-json',
                                  data=json.dumps({'delete_files': False}),
                                  content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['success'])
        self.assertTrue(body['kept_files'])
        self.assertEqual(body['files_deleted'], 0)
        self.assertTrue(os.path.exists(path), 'file must survive an explicit opt-out')
        # The row is still deleted - the toggle governs the files, not the recording.
        self.assertIsNone(db.session.get(Recording, rec_id))

    def test_json_delete_true_removes_files(self):
        rec_id, path = self._completed_recording_with_file()
        resp = self.t.client.post(f'/recordings/{rec_id}/delete-json',
                                  data=json.dumps({'delete_files': True}),
                                  content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()['kept_files'])
        self.assertFalse(os.path.exists(path))

    def test_form_delete_removes_files_by_default(self):
        rec_id, path = self._completed_recording_with_file()
        resp = self.t.client.post(f'/recordings/{rec_id}/delete', data={})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(db.session.get(Recording, rec_id))

    def test_form_delete_keeps_files_when_checkbox_unchecked(self):
        """An HTML checkbox sends nothing when unchecked, so the form path has to accept an
        explicit falsey string rather than relying on key presence."""
        rec_id, path = self._completed_recording_with_file()
        resp = self.t.client.post(f'/recordings/{rec_id}/delete',
                                  data={'delete_files': 'false'})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(os.path.exists(path))
        self.assertIsNone(db.session.get(Recording, rec_id))

    def test_active_recording_is_still_refused_regardless_of_the_flag(self):
        rec = seed.make_recording(status='IN_PROGRESS')
        db.session.commit()
        rec_id = rec.id
        resp = self.t.client.post(f'/recordings/{rec_id}/delete-json',
                                  data=json.dumps({'delete_files': False}),
                                  content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())
        self.assertIsNotNone(db.session.get(Recording, rec_id))


if __name__ == '__main__':
    unittest.main()
