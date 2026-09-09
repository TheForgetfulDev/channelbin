"""Guards dev/docs/BUGS.md 2026-08-24 "A restart during post-processing re-runs the
concat and fails a finished recording" and its sibling entries.

do_postprocess() runs inside do_concatenation()'s chain while the row still reads
CONCATENATING, so CONCATENATING covers two phases: the concat itself, and a
post-processing tail that can run for minutes on a large capture. The startup resume
path read the status as "the concat has not run yet", relaunched it, found no segment
files - a successful concat deletes them - and marked a complete 17.9 GB recording
FAILED, then blamed the channel's health score for a failure that was local.

No real ffmpeg anywhere: do_postprocess is monkeypatched, and the "output on disk" is a
real file under the test's temp dir so the existence checks are genuine.
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
from app.database import Channel, Recording, RecordingSegment  # noqa: E402


class ConcatAlreadyCommittedTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _finished_concat(self, size=4096):
        """A recording whose concat already committed: output_path on the row, the file
        on disk, and its segment rows pointing at files the concat consumed."""
        out = os.path.join(self.t._tmpdir, 'finished_capture.ts')
        with open(out, 'wb') as fh:
            fh.write(b'\0' * size)
        rec = seed.make_recording(status='CONCATENATING', name='finished capture',
                                  output_path=out, final_file_size=size)
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=1,
            file_path=os.path.join(self.t._tmpdir, 'gone_seg_001.ts'),
            started_at=rec.start_time, ended_at=rec.stop_time,
            exit_reason='STOP_TIME_REACHED', bytes_recorded=size))
        db.session.commit()
        return rec, out

    def test_committed_concat_output_reads_the_row_not_the_status(self):
        from app.concatenator import committed_concat_output

        rec, out = self._finished_concat()
        self.assertEqual(committed_concat_output(rec), out)

    def test_committed_concat_output_is_none_when_the_file_is_gone(self):
        """A Retry after the output was deleted genuinely does need a fresh concat."""
        from app.concatenator import committed_concat_output

        rec, out = self._finished_concat()
        os.unlink(out)
        self.assertIsNone(committed_concat_output(rec))

    def test_committed_concat_output_is_none_before_any_concat(self):
        from app.concatenator import committed_concat_output

        rec = seed.make_recording(status='IN_PROGRESS')
        db.session.commit()
        self.assertIsNone(committed_concat_output(rec))

    def test_rerun_resumes_postprocessing_instead_of_failing(self):
        from app.concatenator import do_concatenation

        rec, out = self._finished_concat()
        rid = rec.id

        with mock.patch('app.postprocessor.do_postprocess') as fake_pp:
            do_concatenation(self.t.app, rid, reason='Resuming concatenation after service restart')

        db.session.expire_all()
        again = db.session.get(Recording, rid)
        self.assertNotEqual(again.status, 'FAILED',
                            'a concat that already committed its output must never be re-run '
                            'into a no-valid-segments failure')
        self.assertTrue(fake_pp.called,
                        'the chain must pick back up in post-processing, which is re-runnable')
        self.assertEqual(fake_pp.call_args[0][2], out,
                         'post-processing must resume from the .ts the concat already produced')

    def test_rerun_says_out_loud_that_concat_was_already_done(self):
        """Principle 1: a resume that skips a phase names the phase it skipped."""
        from app.concatenator import do_concatenation
        from app.database import RecordingEvent

        rec, out = self._finished_concat()
        rid = rec.id
        with mock.patch('app.postprocessor.do_postprocess'):
            do_concatenation(self.t.app, rid, reason='Resuming concatenation after service restart')

        details = [e.detail or '' for e in
                   RecordingEvent.query.filter_by(recording_id=rid).all()]
        self.assertTrue(any('already complete' in d for d in details),
                        f'no event explains the skipped concat: {details}')


class StartupResumeTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        self.t.cleanup()

    def test_restart_does_not_reconcatenate_a_finished_capture(self):
        """The end-to-end shape of the incident: a CONCATENATING row whose concat had
        already committed, swept at startup."""
        from app.scheduler import resume_in_progress_recordings

        out = os.path.join(self.t._tmpdir, 'restart_capture.ts')
        with open(out, 'wb') as fh:
            fh.write(b'\0' * 2048)
        rec = seed.make_recording(status='CONCATENATING', name='crashed in postprocess',
                                  output_path=out, final_file_size=2048)
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=1,
            file_path=os.path.join(self.t._tmpdir, 'consumed_seg_001.ts'),
            started_at=rec.start_time, ended_at=rec.stop_time,
            exit_reason='STOP_TIME_REACHED', bytes_recorded=2048))
        db.session.commit()
        rid = rec.id

        with mock.patch('app.postprocessor.do_postprocess') as fake_pp:
            resume_in_progress_recordings(self.t.app)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not fake_pp.called:
                time.sleep(0.01)

        db.session.expire_all()
        again = db.session.get(Recording, rid)
        self.assertNotEqual(again.status, 'FAILED',
                            'the restart sweep must not fail a recording whose file is on disk')
        self.assertTrue(fake_pp.called)


class NoSegmentsHealthAttributionTests(unittest.TestCase):
    """apply_recording_health_observation's own contract: only outcomes that reflect
    stream quality may reach it. "No segment files on disk" is two different failures
    and only one of them is the stream's fault."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _run_with_missing_segments(self, bytes_recorded):
        acct = seed.make_account()
        ch = seed.make_channel(acct, name='healthy feed')
        db.session.flush()
        ch.health_score = 100.0
        ch.health_score_sample_count = 5
        rec = seed.make_recording(status='CONCATENATING', channel_id=ch.id,
                                  name='segments vanished')
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=1,
            file_path=os.path.join(self.t._tmpdir, 'never_written_seg_001.ts'),
            started_at=rec.start_time, ended_at=rec.stop_time,
            exit_reason='STOP_TIME_REACHED', bytes_recorded=bytes_recorded))
        db.session.commit()
        rid, cid = rec.id, ch.id

        from app.concatenator import do_concatenation
        do_concatenation(self.t.app, rid)
        db.session.expire_all()
        return db.session.get(Recording, rid), db.session.get(Channel, cid)

    def test_capture_that_recorded_bytes_does_not_blame_the_channel(self):
        rec, ch = self._run_with_missing_segments(bytes_recorded=19_231_703_084)
        self.assertEqual(rec.status, 'FAILED')
        self.assertEqual(ch.health_score, 100.0,
                         'files going missing after a capture that pulled 17.9 GB is local '
                         'infrastructure, not stream signal')

    def test_capture_that_recorded_nothing_still_blames_the_channel(self):
        rec, ch = self._run_with_missing_segments(bytes_recorded=0)
        self.assertEqual(rec.status, 'FAILED')
        self.assertLess(ch.health_score, 100.0,
                        'a capture that never recorded a byte is exactly the failure the '
                        'health score exists to record')

    def test_failure_detail_says_which_of_the_two_it_was(self):
        rec, _ = self._run_with_missing_segments(bytes_recorded=4096)
        from app.database import RecordingEvent
        details = [e.detail or '' for e in
                   RecordingEvent.query.filter_by(recording_id=rec.id).all()]
        self.assertTrue(any('not a stream fault' in d for d in details),
                        f'the event log must name the reason: {details}')


if __name__ == '__main__':
    unittest.main()
