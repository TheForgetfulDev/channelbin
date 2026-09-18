"""Tier 2 - teardown completeness for the artifacts a CONVERSION leaves behind.

Guards dev/docs/BUGS.md 2026-09-09 @ 04:24 AM "A failed conversion's partial output is
never enumerated for teardown". recorder.recording_disk_paths() used to expand a
recording's stem in one direction only - it added the .ts sibling when output_path did NOT
end in .ts - but Recording.output_path is repointed at the converted file only on SUCCESS.
A conversion that gave up therefore left the row naming its .ts with a multi-GB partial
.mp4 beside it, and deleting that FAILED recording removed the .ts, the segments and the
thumbnail while walking straight past the partial. Nothing referenced it afterward.

Sibling concern in the same function: the .conv-progress-* / .conv-stderr-* scratch files
run_conversion_supervised writes alongside the output. It unlinks them in a finally, so
they only survive a shutdown mid-conversion - after which a delete is the last thing that
will ever look at them.

Every file here is written under make_test_app's own temp dir, never /dvr. The thumbnail
and output dirs are resolved inside recording_disk_paths from a runtime load_config(),
which make_test_app's overrides cannot reach, so that one lookup is patched - the same
treatment test_teardown.py's abort test uses and for the same reason (dev/changelog/520).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.recorder import recording_disk_paths  # noqa: E402
from app.database import Recording, RecordingSegment  # noqa: E402
from datetime import datetime  # noqa: E402


class _ConversionArtifactCase(unittest.TestCase):
    """A FAILED recording whose conversion gave up: row still names the .ts, a partial
    .mp4 and both flavors of conversion scratch sit beside it on disk."""

    fmt = 'mp4'

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()

        self.dvr_dir = os.path.join(self.t._tmpdir, 'dvr')
        self.thumb_dir = os.path.join(self.t._tmpdir, 'images', 'thumbnails')
        os.makedirs(self.dvr_dir, exist_ok=True)
        os.makedirs(self.thumb_dir, exist_ok=True)
        self.cfg = load_config()
        self.cfg['recording']['dvr_output_dir'] = self.dvr_dir
        self.cfg['recording']['images_dir'] = os.path.dirname(self.thumb_dir)
        self.cfg['recording']['post_process']['enabled'] = True
        self.cfg['recording']['post_process']['format'] = self.fmt

        # output_path still names the .ts, exactly as the give-up path leaves it.
        rec = seed.make_recording(status='FAILED', name='Movie Night',
                                  channel_id=self.ch.id)
        db.session.flush()
        self.rid = rec.id
        self.ts_path = os.path.join(self.dvr_dir, 'Movie Night.ts')
        rec.output_path = self.ts_path
        self.partial_out = os.path.join(self.dvr_dir, f'Movie Night.{self.fmt}')

        self.seg_path = os.path.join(self.dvr_dir, f'seg_{self.rid}_0.ts')
        db.session.add(RecordingSegment(
            recording_id=self.rid, segment_number=0, file_path=self.seg_path,
            started_at=datetime.utcnow(), exit_reason='STOP_TIME_REACHED',
            bytes_recorded=32))
        db.session.commit()

        # Both scratch spellings: the per-attempt token form in use today, and the fixed
        # name older attempts wrote (run_conversion_supervised still reaps both).
        self.scratch = [
            os.path.join(self.dvr_dir, f'.conv-progress-{self.rid}-abc12345.txt'),
            os.path.join(self.dvr_dir, f'.conv-stderr-{self.rid}-abc12345.log'),
            os.path.join(self.dvr_dir, f'.conv-progress-{self.rid}.txt'),
            os.path.join(self.dvr_dir, f'.conv-stderr-{self.rid}.log'),
        ]
        self.thumb_path = os.path.join(self.thumb_dir, f'{self.rid}.jpg')
        for path in ([self.ts_path, self.partial_out, self.seg_path, self.thumb_path]
                     + self.scratch):
            with open(path, 'wb') as fh:
                fh.write(b'\x00' * 16)

    def tearDown(self):
        self.t.cleanup()

    def _paths(self):
        with mock.patch('app.recorder.load_config', return_value=self.cfg):
            return recording_disk_paths(self.rid)


class ConversionArtifactEnumerationTests(_ConversionArtifactCase):

    def test_partial_conversion_output_is_enumerated(self):
        """The whole point: the .mp4 the give-up path stranded is listed even though the
        row's output_path still ends in .ts."""
        self.assertIn(self.partial_out, self._paths())

    def test_ts_source_sibling_is_still_enumerated_after_a_successful_conversion(self):
        """The direction that already worked must keep working: a converted recording's
        row names the .mp4, and its kept .ts source is listed."""
        rec = db.session.get(Recording, self.rid)
        rec.output_path = self.partial_out
        db.session.commit()
        paths = self._paths()
        self.assertIn(self.ts_path, paths)
        self.assertIn(self.partial_out, paths)

    def test_conversion_scratch_files_are_enumerated(self):
        paths = self._paths()
        for path in self.scratch:
            self.assertIn(path, paths, f'conversion scratch not enumerated: {path}')

    def test_segments_and_thumbnail_are_still_enumerated(self):
        paths = self._paths()
        self.assertIn(self.seg_path, paths)
        self.assertIn(self.thumb_path, paths)

    def test_another_recordings_suffixed_stem_is_not_enumerated(self):
        """reserve_concat_output_path hands a colliding name the `_2` stem, so expanding
        this recording's stem across the extension family must never reach the neighbor's
        files - deleting one recording would take another's output with it."""
        neighbor_ts = os.path.join(self.dvr_dir, 'Movie Night_2.ts')
        neighbor_out = os.path.join(self.dvr_dir, f'Movie Night_2.{self.fmt}')
        paths = self._paths()
        self.assertNotIn(neighbor_ts, paths)
        self.assertNotIn(neighbor_out, paths)

    def test_another_recordings_scratch_id_prefix_is_not_enumerated(self):
        """The scratch globs are listed per id rather than as f'{id}*': recording 6 must
        not match recording 64's files."""
        decoy = os.path.join(self.dvr_dir, f'.conv-progress-{self.rid}9-abc12345.txt')
        with open(decoy, 'wb') as fh:
            fh.write(b'x')
        self.assertNotIn(decoy, self._paths())


class ConversionOutputFormatFamilyTests(_ConversionArtifactCase):
    """The sibling comes from the configured post_process.format, not a hardcoded .mp4."""

    fmt = 'mkv'

    def test_partial_mkv_is_enumerated(self):
        self.assertIn(self.partial_out, self._paths())

    def test_nothing_beyond_the_family_is_listed(self):
        stray = os.path.join(self.dvr_dir, 'Movie Night.mp4')
        with open(stray, 'wb') as fh:
            fh.write(b'x')
        self.assertNotIn(stray, self._paths())


class ConversionArtifactDeletionTests(_ConversionArtifactCase):
    """End to end through the delete route, which is where the strand was actually
    observed - enumeration alone is not the user-visible half."""

    def test_delete_removes_the_partial_output_and_scratch(self):
        client = self.t.app.test_client()
        with mock.patch('app.recorder.load_config', return_value=self.cfg):
            resp = client.post(f'/recordings/{self.rid}/delete',
                               data={'delete_files': 'on'})
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(db.session.get(Recording, self.rid))
        self.assertFalse(os.path.exists(self.partial_out),
                         'partial conversion output left on disk with no row pointing at it')
        self.assertFalse(os.path.exists(self.ts_path), 'source .ts left on disk')
        self.assertFalse(os.path.exists(self.seg_path), 'segment file left on disk')
        self.assertFalse(os.path.exists(self.thumb_path), 'live thumbnail left on disk')
        for path in self.scratch:
            self.assertFalse(os.path.exists(path), f'conversion scratch left on disk: {path}')

    def test_delete_without_file_removal_leaves_the_partial_alone(self):
        client = self.t.app.test_client()
        with mock.patch('app.recorder.load_config', return_value=self.cfg):
            resp = client.post(f'/recordings/{self.rid}/delete',
                               data={'delete_files': 'false'})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(os.path.exists(self.partial_out))
        self.assertTrue(os.path.exists(self.ts_path))


if __name__ == '__main__':
    unittest.main()
