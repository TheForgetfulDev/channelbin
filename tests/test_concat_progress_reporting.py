"""Tier 2 - the join reports how far it has got, and counts the right segments
(dev/changelog/959).

Two defects in one sentence on the recording detail page. While a recording was
CONCATENATING the strip read "Capture finished - 19 segments being joined into the final
file. This usually takes a few minutes." and never moved: on recording 19, 2026-09-13, that
was a 42.6 GB join that ran 9m36s looking identical to a hung process. And "19" was
`rec.segments | length` - every segment row, including the zero-byte ones and the placeholder
clips the app had deliberately discarded - while the join ffmpeg was actually handed 16.

Both numbers were already measured and simply not published: supervise_ffmpeg() polls the
output file every CONCAT_POLL_INTERVAL_SECONDS with progress_signal='size' and offers an
on_progress hook the concat did not pass, and the joinable list is concatenator
.joinable_segments(), the one definition the join itself builds from.

Covers, in order:
  - JoinProgressArithmeticTests: the registry's own maths - joined x of y off the cumulative
    segment sizes, the percent clamp, elapsed derived at read time.
  - JoinProgressLifecycleTests: an entry exists while the join runs and on NO path outlives
    it - success, ffmpeg failure, an exception, and a concat that never got as far as
    spawning one.
  - JoinCountTests: which count each status is asking for, through
    routes/recordings.py::_join_strip_context.
  - JoinSurfaceTests: what the three surfaces actually render - the detail strip, the
    recordings list relative line, the Dashboard background-task chip.

No real ffmpeg: supervise_ffmpeg is faked and calls the hook the way the real one does.
See CLAUDE.md section Testing. Run standalone:
  python3 -m unittest tests.test_concat_progress_reporting
"""
import os
import sys
import time
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.concatenator as catmod  # noqa: E402
import app.proc_utils as pumod  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingSegment, SEGMENT_EXCLUDED_PLACEHOLDER,
)
from app.routes.recordings import _join_strip_context  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402


class JoinProgressArithmeticTests(unittest.TestCase):
    """The registry's maths, with no app and no ffmpeg anywhere near it."""

    def setUp(self):
        self.rid = 4242
        catmod._clear_concat_progress(self.rid)

    def tearDown(self):
        catmod._clear_concat_progress(self.rid)

    def _seed(self, sizes):
        catmod._start_concat_progress(self.rid, total_bytes=sum(sizes), sizes=sizes)

    def test_nothing_is_reported_for_a_recording_that_is_not_joining(self):
        """Absent is a real state - a row can sit at CONCATENATING for as long as
        serialize_concat makes it queue, and reporting 0% for that is a fabrication."""
        self.assertIsNone(catmod.concat_progress(self.rid))

    def test_the_entry_exists_before_the_first_byte(self):
        """Seeded at spawn rather than on the first poll, so the strip reads `0 of 3` the
        moment the status flips instead of sitting blank for a poll interval."""
        self._seed([100, 100, 100])

        prog = catmod.concat_progress(self.rid)

        self.assertIsNotNone(prog)
        self.assertEqual(prog['joined'], 0)
        self.assertEqual(prog['of'], 3)
        self.assertEqual(prog['bytes'], 0)

    def test_joined_counts_only_segments_the_join_has_fully_consumed(self):
        """The concat demuxer reads its inputs in order, so bytes written against the
        running total of input sizes is an exact answer, not an estimate."""
        self._seed([100, 200, 300])  # cumulative: 100, 300, 600

        catmod._publish_concat_progress(self.rid, written=150, eta_seconds=None)

        self.assertEqual(catmod.concat_progress(self.rid)['joined'], 1)

    def test_a_segment_counts_the_moment_its_last_byte_lands(self):
        """Boundary: exactly at the cumulative offset is done, not almost done."""
        self._seed([100, 200, 300])

        catmod._publish_concat_progress(self.rid, written=300, eta_seconds=None)

        self.assertEqual(catmod.concat_progress(self.rid)['joined'], 2)

    def test_joined_never_exceeds_the_segment_count(self):
        """A `-c copy` join writes its inputs plus muxing overhead, so the output passes the
        last cumulative offset before ffmpeg exits - a strip reading "4 of 3" would be worse
        than one lingering on 3.

        Characterization, not a regression guard: the count is derived from the same list
        the offsets are, so bisect over it cannot return more. It is here to say that the
        bound is structural, so nobody re-adds a clamp that can never fire."""
        self._seed([100, 200, 300])

        catmod._publish_concat_progress(self.rid, written=999999, eta_seconds=None)

        self.assertEqual(catmod.concat_progress(self.rid)['joined'], 3)

    def test_percent_is_bytes_written_against_bytes_to_read(self):
        self._seed([100, 100, 200])  # 400 total

        catmod._publish_concat_progress(self.rid, written=100, eta_seconds=None)

        self.assertAlmostEqual(catmod.concat_progress(self.rid)['pct'], 25.0)

    def test_percent_is_clamped_below_a_hundred(self):
        """The same overhead as above. The status leaving CONCATENATING is what reports
        completion - a bar that reaches 100 and then sits there through the analysis pass
        that follows is the kind of number nobody can explain."""
        self._seed([100, 100, 200])

        catmod._publish_concat_progress(self.rid, written=440, eta_seconds=None)

        self.assertEqual(catmod.concat_progress(self.rid)['pct'], 99.0)

    def test_percent_is_unknown_rather_than_zero_when_there_is_no_total(self):
        """Nullable-guarded: zero-byte inputs would divide by zero, and 0% would be a
        claim the app cannot support."""
        catmod._start_concat_progress(self.rid, total_bytes=0, sizes=[0, 0])

        catmod._publish_concat_progress(self.rid, written=0, eta_seconds=None)

        self.assertIsNone(catmod.concat_progress(self.rid)['pct'])

    def test_elapsed_is_current_at_the_moment_it_is_read(self):
        """Derived from the monotonic start on read, not stamped on the last tick - a join
        whose poll interval is 5s would otherwise show an elapsed that jumps in 5s steps and
        freezes entirely if ffmpeg stopped reporting."""
        self._seed([100])
        time.sleep(0.05)

        first = catmod.concat_progress(self.rid)['elapsed_seconds']
        time.sleep(0.05)
        second = catmod.concat_progress(self.rid)['elapsed_seconds']

        self.assertGreater(second, first)

    def test_the_read_does_not_hand_out_the_live_entry(self):
        """The recordings list calls this per row while a join thread is writing to it; a
        caller holding the real dict would read a half-updated tick."""
        self._seed([100, 100])
        prog = catmod.concat_progress(self.rid)
        prog['joined'] = 99

        self.assertEqual(catmod.concat_progress(self.rid)['joined'], 0)

    def test_a_tick_for_a_join_that_has_ended_is_dropped(self):
        """supervise_ffmpeg publishes a final tick on the way out, and a Retry may have
        cleared the entry by then. Re-creating it would leave a phantom join on the page
        forever."""
        catmod._publish_concat_progress(self.rid, written=500, eta_seconds=None)

        self.assertIsNone(catmod.concat_progress(self.rid))


class _ConcatCase(unittest.TestCase):
    """A recording with three joinable segments plus two that must not be counted."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.rec = seed.make_recording(status='IN_PROGRESS', name='Long Race')
        db.session.commit()
        self.rid = self.rec.id
        self.sizes = [4096, 8192, 4096]
        for n, size in enumerate(self.sizes, start=1):
            path = os.path.join(self.dvr, f'seg_{self.rid}_{n}.ts')
            with open(path, 'wb') as fh:
                fh.write(b'\x00' * size)
            db.session.add(RecordingSegment(
                recording_id=self.rid, segment_number=n, file_path=path,
                started_at=datetime.utcnow(), exit_reason='STOP_TIME_REACHED',
                bytes_recorded=size))
        # A discarded placeholder: its file is on disk and has bytes, so only the exclusion
        # keeps it out of the join (dev/changelog/957).
        ph_path = os.path.join(self.dvr, f'seg_{self.rid}_ph.ts')
        with open(ph_path, 'wb') as fh:
            fh.write(b'\x00' * 4096)
        db.session.add(RecordingSegment(
            recording_id=self.rid, segment_number=4, file_path=ph_path,
            started_at=datetime.utcnow(), exit_reason='PROCESS_EXITED',
            bytes_recorded=4096, excluded_reason=SEGMENT_EXCLUDED_PLACEHOLDER))
        # A segment whose capture produced no file at all.
        db.session.add(RecordingSegment(
            recording_id=self.rid, segment_number=5,
            file_path=os.path.join(self.dvr, f'seg_{self.rid}_missing.ts'),
            started_at=datetime.utcnow(), exit_reason='PROCESS_EXITED',
            bytes_recorded=0))
        db.session.commit()

        self.cfg = load_config()
        self.cfg['recording']['dvr_output_dir'] = self.dvr
        self.cfg['recording']['post_process']['enabled'] = False
        self.cfg['recording']['move_on_complete']['enabled'] = False

    def tearDown(self):
        catmod._clear_concat_progress(self.rid)
        self.t.cleanup()

    def _run_concat(self, supervise):
        with mock.patch('app.config.load_config', return_value=self.cfg), \
             mock.patch.object(catmod, 'supervise_ffmpeg', side_effect=supervise), \
             mock.patch('app.recorder.persist_final_thumbnail'), \
             mock.patch('app.recorder.persist_poster_frame'), \
             mock.patch('app.health_score.apply_capture_phase_health_observation'), \
             mock.patch.object(catmod, '_measure_segment_content_durations'):
            catmod._run_concatenation(self.t.app, self.rid, reason='test')


class JoinProgressLifecycleTests(_ConcatCase):
    """An entry while the join runs, and nothing left behind on any way out."""

    def _supervise_capturing(self, seen, *, result=None, raises=None, written=None):
        """A fake supervise_ffmpeg that drives the caller's on_progress hook the way the
        real one does, recording what the registry said at each tick."""
        def _fake(cmd, output_path, **kwargs):
            with open(output_path, 'wb') as fh:
                fh.write(b'\x00' * (written or 0))
            hook = kwargs.get('on_progress')
            if hook is not None:
                hook(1.0, 0.0, written or 0)
            seen.append(catmod.concat_progress(self.rid))
            if raises is not None:
                raise raises
            return result or pumod.SupervisedRun('success', returncode=0)
        return _fake

    def test_the_join_publishes_its_progress_while_it_runs(self):
        """The defect: the hook exists and the concat did not pass one, so the strip had
        nothing to show for a join that ran nine and a half minutes."""
        seen = []
        self._run_concat(self._supervise_capturing(seen, written=4096))

        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0], 'the join published no progress at all')
        self.assertEqual(seen[0]['bytes'], 4096)
        self.assertEqual(seen[0]['joined'], 1)

    def test_the_count_is_the_joinable_count_not_the_row_count(self):
        """Five segment rows, three of them joinable. The strip used to say five."""
        seen = []
        self._run_concat(self._supervise_capturing(seen, written=4096))

        self.assertEqual(seen[0]['of'], 3)

    def test_the_total_is_the_joinable_bytes(self):
        seen = []
        self._run_concat(self._supervise_capturing(seen, written=4096))

        self.assertEqual(seen[0]['total_bytes'], sum(self.sizes))

    def test_nothing_is_left_behind_after_a_successful_join(self):
        self._run_concat(self._supervise_capturing([], written=4096))

        self.assertIsNone(catmod.concat_progress(self.rid),
                          'a finished join still claims to be running')

    def test_nothing_is_left_behind_after_a_failed_join(self):
        seen = []
        self._run_concat(self._supervise_capturing(
            seen, result=pumod.SupervisedRun('stalled', error_msg='No concat progress'),
            written=4096))

        self.assertIsNone(catmod.concat_progress(self.rid))

    def test_nothing_is_left_behind_when_the_join_raises(self):
        """The path a `finally` exists for. A leaked entry outlives every attempt to clear
        it and pins a phantom percentage to the page for the life of the process."""
        self._run_concat(self._supervise_capturing(
            [], raises=OSError('boom'), written=0))

        self.assertIsNone(catmod.concat_progress(self.rid))

    def test_a_retry_reports_its_own_attempt_from_zero(self):
        """Retry concatenation re-enters _run_concatenation on a recording whose first join
        failed. The second attempt must not inherit the first one's bytes."""
        self._run_concat(self._supervise_capturing(
            [], result=pumod.SupervisedRun('stalled', error_msg='No concat progress'),
            written=8192))
        seen = []
        self._run_concat(self._supervise_capturing(seen, written=0))

        self.assertEqual(seen[0]['bytes'], 0)
        self.assertEqual(seen[0]['joined'], 0)

    def test_a_concat_that_never_reaches_ffmpeg_reports_nothing(self):
        """Out of disk space, judged before anything is spawned. There is no join to report
        on, and an entry here would describe work that never started."""
        with mock.patch.object(catmod.shutil, 'disk_usage') as du:
            du.return_value = mock.Mock(free=1)
            self._run_concat(self._supervise_capturing([]))

        self.assertIsNone(catmod.concat_progress(self.rid))
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).status, 'FAILED')


class JoinCountTests(_ConcatCase):
    """Which segment count each status is asking about."""

    def _set_status(self, status):
        rec = db.session.get(Recording, self.rid)
        rec.status = status
        db.session.commit()
        return rec

    def test_a_running_join_reports_the_list_ffmpeg_was_handed(self):
        """Taken from the registry rather than re-derived, so the strip and the join cannot
        disagree even if a file vanished in between."""
        rec = self._set_status('CONCATENATING')
        catmod._start_concat_progress(self.rid, total_bytes=999, sizes=[1, 2, 996])

        prog, count = _join_strip_context(rec)

        self.assertIsNotNone(prog)
        self.assertEqual(count, 3)

    def test_a_queued_join_counts_the_joinable_files(self):
        """No ffmpeg yet, so nothing has been handed to anything - the question goes back to
        the one definition of joinable."""
        rec = self._set_status('CONCATENATING')

        prog, count = _join_strip_context(rec)

        self.assertIsNone(prog)
        self.assertEqual(count, 3, 'the queued strip counted discarded or empty segments')

    def test_a_finished_recording_counts_rows_because_its_files_are_gone(self):
        """A successful concat deletes the segments it consumed, so a filesystem test
        answers 0 for every COMPLETED recording - which is how the done strip would have
        read "Joined 0 segments" if it asked the same question the live one does."""
        rec = self._set_status('COMPLETED')
        for seg in rec.segments:
            if os.path.exists(seg.file_path):
                os.unlink(seg.file_path)

        prog, count = _join_strip_context(rec)

        self.assertIsNone(prog)
        self.assertEqual(count, 3)


class JoinSurfaceTests(_ConcatCase):
    """What the three surfaces render. Each asserts on the page, not on a helper."""

    def _set_status(self, status):
        rec = db.session.get(Recording, self.rid)
        rec.status = status
        db.session.commit()

    def _live_join(self, written):
        catmod._start_concat_progress(self.rid, total_bytes=sum(self.sizes),
                                      sizes=self.sizes)
        catmod._publish_concat_progress(self.rid, written=written, eta_seconds=90)

    def test_the_detail_strip_shows_how_far_the_join_has_got(self):
        """The asked-for minimum, per dev/changelog/959: if not true progress, then at
        least "joined x of y segments" and an elapsed counter."""
        self._set_status('CONCATENATING')
        self._live_join(written=4096)

        with self.t.app.test_client() as c:
            html = c.get(f'/recordings/{self.rid}').get_data(as_text=True)

        self.assertIn('1 of 3', html)
        self.assertIn('25%', html)
        self.assertIn('elapsed', html)

    def test_the_detail_strip_never_counts_discarded_or_empty_segments(self):
        """The other half of the defect: five rows, three joined, and the strip said five.

        Asserted against the strip's own sentence, not the page - the capture-health
        tooltip beside the badge says "5 segments" and is correct to: it is reporting what
        the capture produced, which is a different question from what the join was given.
        """
        self._set_status('CONCATENATING')

        with self.t.app.test_client() as c:
            html = c.get(f'/recordings/{self.rid}').get_data(as_text=True)

        self.assertIn('<b>3 segments</b> waiting to be joined', html)
        self.assertNotIn('<b>5 segments</b>', html)

    def test_a_queued_join_says_it_has_not_started(self):
        """Rather than a 0% that never moves - that reads exactly like the frozen strip
        this replaced."""
        self._set_status('CONCATENATING')

        with self.t.app.test_client() as c:
            html = c.get(f'/recordings/{self.rid}').get_data(as_text=True)

        self.assertIn('waiting to be joined', html)

    def test_the_finished_strip_counts_what_was_actually_joined(self):
        self._set_status('COMPLETED')
        for seg in db.session.get(Recording, self.rid).segments:
            if os.path.exists(seg.file_path):
                os.unlink(seg.file_path)

        with self.t.app.test_client() as c:
            html = c.get(f'/recordings/{self.rid}').get_data(as_text=True)

        self.assertIn('Joined <b>3 segments</b>', html)

    def test_the_recordings_list_row_shows_the_percent(self):
        """The list's relative line for this status was only "ended Nm ago"."""
        self._set_status('CONCATENATING')
        self._live_join(written=4096)

        with self.t.app.test_client() as c:
            html = c.get('/recordings').get_data(as_text=True)

        self.assertIn('1 of 3 segments', html)
        self.assertIn('25%', html)

    def test_the_recordings_list_row_falls_back_when_no_join_is_running(self):
        self._set_status('CONCATENATING')

        with self.t.app.test_client() as c:
            html = c.get('/recordings').get_data(as_text=True)

        self.assertIn('ended', html)

    def _joining_chip(self):
        """The Dashboard's own background-task row for this recording, read from the
        endpoint the page uses rather than from the helper - a helper nothing calls is the
        shape that let this ship unwired."""
        with self.t.app.test_client() as c:
            bg = c.get('/api/activity/status').get_json()['background']
        rows = [t for t in bg['tasks'] if t['label'] == 'Joining segments']
        self.assertEqual(len(rows), 1, f'expected one join chip, got {bg["tasks"]}')
        return rows[0]['detail']

    def test_the_dashboard_chip_shows_the_percent(self):
        self._set_status('CONCATENATING')
        self._live_join(written=4096)

        detail = self._joining_chip()

        self.assertIn('25%', detail)
        self.assertIn('1 of 3 segments', detail)

    def test_the_dashboard_chip_is_just_the_name_when_nothing_is_joining(self):
        self._set_status('CONCATENATING')

        self.assertEqual(self._joining_chip(), 'Long Race')


if __name__ == '__main__':
    unittest.main()
