"""Guards dev/docs/BUGS.md 2026-08-14 11:11 "Two recordings with the same name collide on
segment and output paths".

Recording.name is not unique and nothing at create time makes it so, but three capture-side
paths were derived from `_safe_name(rec.name)` alone:

  * the segment path, so two same-named recordings capturing at once were handed the
    identical file and ran two ffmpegs writing into it;
  * resume_recording's next-segment number, taken from a directory listing of
    `{safe_name}_seg_*`, which a same-named sibling's files also matched;
  * the concat output, which overwrote whatever already sat on that name.

The concat half is the one that survives a casual fix. Under the shipped defaults
(post_process enabled, format mp4, delete_source on) a finished recording leaves only
`X.mp4` behind - its `X.ts` is deleted - so checking `.ts` alone finds the stem free, hands
it to the next same-named recording, and lets that recording's conversion silently overwrite
the first one's file while the first one's row still points at it.

No real ffmpeg and no network: subprocess.Popen and do_postprocess are patched, and every
file here is written under the test's own temp dir. Runtime config is written through
TestApp.sandbox_config() because these code paths call load_config() themselves, which
make_test_app()'s overrides do not reach (CLAUDE.md, Testing).
  python3 -m unittest tests.test_recording_name_collision
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

from app import db  # noqa: E402
from app import recorder  # noqa: E402
from app.concatenator import do_concatenation  # noqa: E402
from app.database import Recording, RecordingSegment  # noqa: E402
from app.postprocessor import (  # noqa: E402
    output_extension_family, reserve_concat_output_path,
)


def _fake_proc():
    """A child that has already exited, so _launch_segment returns right after spawning
    without starting a watchdog thread."""
    proc = mock.MagicMock()
    proc.pid = 4242
    proc.poll.return_value = 0
    return proc


class _DvrTestCase(unittest.TestCase):
    """Shared sandbox: a temp dvr_output_dir that every runtime load_config() call sees."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            # Off rather than redirected: persist_final_thumbnail spawns ffmpeg, and none
            # of these tests assert anything about thumbnails.
            'live_thumbnail': {'enabled': False},
            'post_process': {'enabled': True, 'format': 'mp4'},
        }})

    def tearDown(self):
        self.t.cleanup()

    def _write(self, path, blob=b'x'):
        with open(path, 'wb') as fh:
            fh.write(blob)
        return path

    def _add_segment(self, recording_id, number, path):
        now = datetime.utcnow()
        db.session.add(RecordingSegment(
            recording_id=recording_id, segment_number=number, file_path=path,
            started_at=now - timedelta(minutes=5), ended_at=now,
            exit_reason='STOP_TIME_REACHED', bytes_recorded=os.path.getsize(path)))


class SegmentPathCollisionTests(_DvrTestCase):
    def test_same_named_recordings_capture_to_different_files(self):
        """The corruption case: two ffmpegs writing one file for a whole segment."""
        a = seed.make_recording(status='IN_PROGRESS', name='Match Of The Day')
        b = seed.make_recording(status='IN_PROGRESS', name='Match Of The Day')
        db.session.commit()
        a_id, b_id = a.id, b.id

        with mock.patch('app.recorder.subprocess.Popen',
                        side_effect=lambda *args, **kw: _fake_proc()):
            recorder._launch_segment(self.t.app, a_id, seg_num=1)
            recorder._launch_segment(self.t.app, b_id, seg_num=1)

        db.session.expire_all()
        path_a = RecordingSegment.query.filter_by(recording_id=a_id).one().file_path
        path_b = RecordingSegment.query.filter_by(recording_id=b_id).one().file_path
        self.assertNotEqual(
            path_a, path_b,
            'two same-named recordings were handed the same segment path')

    def test_segment_path_still_carries_the_recording_name(self):
        """Uniquifying must not cost the operator the ability to tell whose file this is."""
        rec = seed.make_recording(status='IN_PROGRESS', name='Match Of The Day')
        db.session.commit()
        rid = rec.id

        with mock.patch('app.recorder.subprocess.Popen',
                        side_effect=lambda *args, **kw: _fake_proc()):
            recorder._launch_segment(self.t.app, rid, seg_num=1)

        db.session.expire_all()
        name = os.path.basename(
            RecordingSegment.query.filter_by(recording_id=rid).one().file_path)
        self.assertTrue(name.startswith('Match_Of_The_Day'), name)
        self.assertIn(str(rid), name)


class ResumeSegmentNumberingTests(_DvrTestCase):
    def test_numbering_ignores_a_same_named_siblings_files(self):
        """Numbering comes from this recording's own segment rows, not from whatever
        `{safe_name}_seg_*` happens to match in dvr_output_dir."""
        target = seed.make_recording(status='IN_PROGRESS', name='Twin')
        db.session.commit()
        rid = target.id

        # A same-named sibling's four segments, in the pre-dev/changelog/643 filename shape
        # that the old directory listing counted.
        for n in range(1, 5):
            self._write(os.path.join(self.dvr, f'Twin_seg_{n:03d}.ts'))

        own = self._write(os.path.join(self.dvr, f'Twin_{rid}_seg_001.ts'))
        self._add_segment(rid, 1, own)
        db.session.commit()

        with mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.resume_recording(self.t.app, rid)

        self.assertTrue(launch.called, 'resume_recording did not relaunch a segment')
        self.assertEqual(
            launch.call_args.kwargs['seg_num'], 2,
            'resume numbered its next segment from a sibling recording\'s files')

    def test_first_segment_of_a_recording_with_no_rows_is_one(self):
        rec = seed.make_recording(status='IN_PROGRESS', name='Fresh')
        db.session.commit()

        with mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.resume_recording(self.t.app, rec.id)

        self.assertEqual(launch.call_args.kwargs['seg_num'], 1)


class ConcatOutputCollisionTests(_DvrTestCase):
    def _recording_with_one_segment(self, name, blob=b'SECOND'):
        rec = seed.make_recording(status='IN_PROGRESS', name=name)
        db.session.commit()
        seg = self._write(
            os.path.join(self.dvr, f'{name}_{rec.id}_seg_001.ts'), blob)
        self._add_segment(rec.id, 1, seg)
        db.session.commit()
        return rec.id

    def test_concat_does_not_target_a_stem_whose_converted_sibling_exists(self):
        """The default-config clobber. Recording A finished and left only Twin.mp4 behind;
        B's conversion must not be pointed at that same file."""
        survivor = self._write(os.path.join(self.dvr, 'Twin.mp4'), b'FIRST RECORDING')
        rid = self._recording_with_one_segment('Twin')

        with mock.patch('app.postprocessor.do_postprocess') as postprocess:
            do_concatenation(self.t.app, rid)

        self.assertTrue(postprocess.called, 'concat did not reach post-processing')
        ts_handed_over = postprocess.call_args[0][2]
        would_convert_to = os.path.splitext(ts_handed_over)[0] + '.mp4'
        self.assertNotEqual(
            would_convert_to, survivor,
            'conversion was aimed at an earlier same-named recording\'s finished file')
        with open(survivor, 'rb') as fh:
            self.assertEqual(fh.read(), b'FIRST RECORDING')

    def test_concat_does_not_overwrite_an_existing_ts_of_the_same_name(self):
        keeper = self._write(os.path.join(self.dvr, 'Rerun.ts'), b'FIRST RECORDING')
        rid = self._recording_with_one_segment('Rerun')

        with mock.patch('app.postprocessor.do_postprocess'):
            do_concatenation(self.t.app, rid)

        with open(keeper, 'rb') as fh:
            self.assertEqual(fh.read(), b'FIRST RECORDING')
        db.session.expire_all()
        self.assertNotEqual(db.session.get(Recording, rid).output_path, keeper)

    def test_uncontested_name_is_used_unchanged(self):
        """The filename designer promises `_safe_name(name)` is what lands in /dvr
        (app/routes/settings.py::template_preview_api), so a collision suffix must appear
        only when there is an actual collision."""
        rid = self._recording_with_one_segment('Solo')

        with mock.patch('app.postprocessor.do_postprocess'):
            do_concatenation(self.t.app, rid)

        db.session.expire_all()
        self.assertEqual(
            os.path.basename(db.session.get(Recording, rid).output_path), 'Solo.ts')

    def test_rename_is_explained_in_an_event(self):
        """A file that landed under a different name than the recording is called has to be
        explainable afterwards (Product Principle 1)."""
        self._write(os.path.join(self.dvr, 'Twin.mp4'), b'FIRST RECORDING')
        rid = self._recording_with_one_segment('Twin')

        with mock.patch('app.postprocessor.do_postprocess'):
            do_concatenation(self.t.app, rid)

        db.session.expire_all()
        from app.database import RecordingEvent, CONCATENATION_DONE
        details = ' '.join(
            e.detail or '' for e in RecordingEvent.query.filter_by(
                recording_id=rid, event_type=CONCATENATION_DONE).all())
        self.assertIn('renamed', details.lower(), details)


class ReserveConcatOutputPathTests(unittest.TestCase):
    """The naming helper on its own - no app, no DB."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='dvr_reserve_test_')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _touch(self, name):
        with open(os.path.join(self.dir, name), 'wb') as fh:
            fh.write(b'x')

    def test_free_family_keeps_the_plain_name(self):
        got = reserve_concat_output_path(self.dir, 'Show', ['.ts', '.mp4'])
        self.assertEqual(os.path.basename(got), 'Show.ts')

    def test_stem_is_skipped_when_only_the_converted_sibling_exists(self):
        self._touch('Show.mp4')
        got = reserve_concat_output_path(self.dir, 'Show', ['.ts', '.mp4'])
        self.assertEqual(os.path.basename(got), 'Show_2.ts')

    def test_suffix_climbs_past_every_taken_stem(self):
        self._touch('Show.ts')
        self._touch('Show_2.mp4')
        got = reserve_concat_output_path(self.dir, 'Show', ['.ts', '.mp4'])
        self.assertEqual(os.path.basename(got), 'Show_3.ts')

    def test_reservation_blocks_a_concurrent_caller_from_the_same_name(self):
        """Two same-named recordings finishing together must not both clear the existence
        check and pick one name - the winner stakes it with O_CREAT|O_EXCL."""
        first = reserve_concat_output_path(self.dir, 'Show', ['.ts', '.mp4'])
        second = reserve_concat_output_path(self.dir, 'Show', ['.ts', '.mp4'])
        self.assertNotEqual(first, second)

    def test_returned_path_carries_the_first_extension(self):
        got = reserve_concat_output_path(self.dir, 'Show', ['.ts', '.mp4'])
        self.assertTrue(got.endswith('.ts'))


class OutputExtensionFamilyTests(unittest.TestCase):
    def test_family_includes_the_configured_conversion_format(self):
        cfg = {'recording': {'post_process': {'enabled': True, 'format': 'mkv'}}}
        self.assertEqual(output_extension_family(cfg), ['.ts', '.mkv'])

    def test_family_is_ts_only_when_post_processing_is_off(self):
        cfg = {'recording': {'post_process': {'enabled': False, 'format': 'mp4'}}}
        self.assertEqual(output_extension_family(cfg), ['.ts'])

    def test_ts_is_not_duplicated_when_the_format_is_also_ts(self):
        cfg = {'recording': {'post_process': {'enabled': True, 'format': 'ts'}}}
        self.assertEqual(output_extension_family(cfg), ['.ts'])


if __name__ == '__main__':
    unittest.main()
