"""Tier 2 - deleting a recording removes every file it ever left, on every delete path.

Guards dev/docs/BUGS.md 2026-09-19 @ 11:35:07 AM "A moved recording's kept .ts source is
never enumerated for teardown" and 2026-09-19 @ 11:35:30 AM "Deleting a recording but
keeping its files strands its thumbnail forever".

The inventory below is built from the paths that CREATE files - capture (segments), join
(the .ts output and its scratch), conversion (the converted file, resumable parts, their
.clean re-mux siblings, scratch for all four supervised prefixes), move on completion (the
destination, with a kept source and scratch left in the recording folder) and the thumbnail
(the image plus the temp file a failed capture can leave) - not from the delete path, which
is what an audit that only reads the delete path cannot see (CLAUDE.md, teardown releases
everything the create path acquired). Every file lives under the test's temp dir; the
runtime config is pointed there with sandbox_config(), never at /dvr.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import scheduler  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingSegment, FILE_MOVED, add_recording_event)
from app.postprocessor import clean_part_path, part_path  # noqa: E402

SCRATCH_PREFIXES = ('conv', 'concat', 'part', 'join')


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(b'\x00' * 16)


class _EveryArtifactCase(unittest.TestCase):
    """A COMPLETED recording that was converted to .mp4 and moved on completion, with
    delete_source off - so the .ts source stays in the recording folder while the row
    names the destination - and every other file kind seeded beside it."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        tmp = self.t._tmpdir
        self.dvr = os.path.join(tmp, 'dvr')
        self.dest = os.path.join(tmp, 'dvr-complete')
        self.images = os.path.join(tmp, 'images')
        self.thumbs = os.path.join(self.images, 'thumbnails')
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'images_dir': self.images,
            'post_process': {'enabled': True, 'format': 'mp4', 'delete_source': False},
            'move_on_complete': {'enabled': True, 'destination': self.dest},
        }})

        finished = datetime.utcnow() - timedelta(days=40)
        rec = seed.make_recording(status='COMPLETED', name='Movie Night',
                                  completed_at=finished, stop_time=finished)
        self.rid = rid = rec.id
        moved_from = os.path.join(self.dvr, 'Movie Night.mp4')
        rec.output_path = os.path.join(self.dest, 'Movie Night.mp4')
        add_recording_event(rid, FILE_MOVED, detail='Moved',
                            extra={'from_path': moved_from})

        self.media = [
            rec.output_path,                               # the finished, moved file
            os.path.join(self.dvr, 'Movie Night.ts'),      # the kept joined source
        ]
        for n in (1, 2):
            seg = os.path.join(self.dvr, f'Movie Night_seg_{n:03d}.ts')
            db.session.add(RecordingSegment(
                recording_id=rid, segment_number=n, file_path=seg,
                started_at=finished, ended_at=finished,
                exit_reason='STOP_TIME_REACHED', bytes_recorded=16))
            self.media.append(seg)
        part = part_path(moved_from, 1)
        self.media += [part, clean_part_path(part)]
        for prefix in SCRATCH_PREFIXES:
            self.media += [
                os.path.join(self.dvr, f'.{prefix}-progress-{rid}-abc12345.txt'),
                os.path.join(self.dvr, f'.{prefix}-stderr-{rid}-abc12345.log'),
                os.path.join(self.dvr, f'.{prefix}-progress-{rid}.txt'),
                os.path.join(self.dvr, f'.{prefix}-stderr-{rid}.log'),
            ]
        self.images_owned = [
            os.path.join(self.thumbs, f'{rid}.jpg'),
            os.path.join(self.thumbs, f'{rid}.0123456789abcdef.tmp.jpg'),
        ]
        # Another recording's files: a _2-suffixed stem and an id that starts with ours.
        self.neighbours = [
            os.path.join(self.dvr, 'Movie Night_2.ts'),
            os.path.join(self.dest, 'Movie Night_2.mp4'),
            os.path.join(self.thumbs, f'{rid}9.jpg'),
            os.path.join(self.thumbs, f'{rid}9.0123456789abcdef.tmp.jpg'),
            os.path.join(self.dvr, f'.conv-progress-{rid}9-abc12345.txt'),
        ]
        db.session.commit()
        for path in self.media + self.images_owned + self.neighbours:
            _touch(path)

    def tearDown(self):
        self.t.cleanup()

    def _left(self, paths):
        return [p for p in paths if os.path.exists(p)]

    def _assert_row_gone(self):
        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, self.rid))

    def _with_retention(self, delete_file):
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'images_dir': self.images,
            'retention_days': 30,
            'retention_delete_file': delete_file,
            'post_process': {'enabled': True, 'format': 'mp4', 'delete_source': False},
            'move_on_complete': {'enabled': True, 'destination': self.dest},
        }})

    def _assert_neighbours_untouched(self):
        self.assertEqual([], [p for p in self.neighbours if not os.path.exists(p)],
                         "another recording's files were deleted")


class DeleteWithFilesRemovesEverythingTests(_EveryArtifactCase):

    def test_json_delete_leaves_nothing(self):
        resp = self.t.app.test_client().post(
            f'/recordings/{self.rid}/delete-json', json={'delete_files': True})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self._assert_row_gone()
        self.assertEqual([], self._left(self.media + self.images_owned))
        self._assert_neighbours_untouched()

    def test_form_delete_leaves_nothing(self):
        resp = self.t.app.test_client().post(
            f'/recordings/{self.rid}/delete', data={'delete_files': 'on'})
        self.assertEqual(resp.status_code, 302)
        self._assert_row_gone()
        self.assertEqual([], self._left(self.media + self.images_owned))
        self._assert_neighbours_untouched()

    def test_retention_delete_leaves_nothing(self):
        self._with_retention(delete_file=True)
        scheduler._recording_retention_sweep()
        self._assert_row_gone()
        self.assertEqual([], self._left(self.media + self.images_owned))
        self._assert_neighbours_untouched()

    def test_the_kept_source_in_the_recording_folder_is_removed(self):
        """The specific strand: output_path names the destination, and only the FILE_MOVED
        event remembers the folder the .ts was left in."""
        source = os.path.join(self.dvr, 'Movie Night.ts')
        self.t.app.test_client().post(
            f'/recordings/{self.rid}/delete-json', json={'delete_files': True})
        self.assertFalse(os.path.exists(source), 'kept .ts source stranded after a move')


class DeleteKeepingFilesTests(_EveryArtifactCase):
    """Keeping the files keeps the media, and still removes ChannelBin's own thumbnail -
    once the row is gone nothing will ever show or delete that image again."""

    def _assert_kept(self):
        self._assert_row_gone()
        self.assertEqual([], [p for p in self.media if not os.path.exists(p)],
                         'a kept recording file was deleted')
        self.assertEqual([], self._left(self.images_owned),
                         'thumbnail stranded after its recording was deleted')
        self._assert_neighbours_untouched()

    def test_json_delete_keeps_media_but_not_the_thumbnail(self):
        resp = self.t.app.test_client().post(
            f'/recordings/{self.rid}/delete-json', json={'delete_files': False})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self._assert_kept()

    def test_form_delete_keeps_media_but_not_the_thumbnail(self):
        resp = self.t.app.test_client().post(
            f'/recordings/{self.rid}/delete', data={'delete_files': 'false'})
        self.assertEqual(resp.status_code, 302)
        self._assert_kept()

    def test_retention_keeps_media_but_not_the_thumbnail(self):
        self._with_retention(delete_file=False)
        scheduler._recording_retention_sweep()
        self._assert_kept()


class MoveRecordsWhereTheFileCameFromTests(unittest.TestCase):
    """The real move step writes the from_path teardown depends on."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        self.dest = os.path.join(self.t._tmpdir, 'dvr-complete')
        self.ts_path = os.path.join(self.dvr, 'show.ts')
        _touch(self.ts_path)
        os.makedirs(self.dest, exist_ok=True)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'gather_health_data': False,
            'serialize_concat': False,
            'live_thumbnail': {'enabled': False},
            'post_process': {'enabled': False},
            'post_script': {'enabled': False},
            'move_on_complete': {'enabled': True, 'destination': self.dest},
        }})

    def tearDown(self):
        self.t.cleanup()

    def test_file_moved_event_carries_the_source_path(self):
        from app.database import RecordingEvent
        from app.postprocessor import do_postprocess
        rec = seed.make_recording(status='CONCATENATING', name='show')
        db.session.commit()
        rid = rec.id
        do_postprocess(self.t.app, rid, self.ts_path)
        db.session.expire_all()
        events = RecordingEvent.query.filter_by(recording_id=rid, event_type=FILE_MOVED).all()
        self.assertEqual(1, len(events))
        extra = json.loads(events[0].extra_data or '{}')
        self.assertEqual(self.ts_path, extra.get('from_path'))
        self.assertEqual(os.path.join(self.dest, 'show.ts'),
                         db.session.get(Recording, rid).output_path)


if __name__ == '__main__':
    unittest.main()
