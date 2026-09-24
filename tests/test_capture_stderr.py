"""Tier 2 - capture ffmpeg's exit code and a bounded stderr tail per segment.

Guards dev/docs/BUGS.md 2026-08-01 22:xx "RecordingSegment.ffmpeg_exit_code has no writer;
capture stderr goes to /dev/null". Design and reasoning: dev/changelog/430.

The defect was structural rather than a wrong branch: the column had existed since the first
schema and nothing anywhere assigned to it, and _launch_segment spawned ffmpeg with
stderr=subprocess.DEVNULL, so every capture failure in the app's history was unattributable.
Recording 71 stalled 75 times in 91 minutes with no recorded cause for any of them.

No network and no provider host: the child processes here are `sys.executable -c ...`, which
is a local argv with no URL in it, so tests/support/netguard.py permits the spawn. Spools are
written under make_test_app's temp dir via the recording.capture_log_dir override - never /dvr
(a soft CIFS mount) and never the repo.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import recorder  # noqa: E402
from app.database import DIAGNOSTICS, RecordingEvent, RecordingSegment  # noqa: E402
from app.proc_utils import read_stderr_tail  # noqa: E402


def _spawn_writing(text, exit_code, stderr_fh):
    """A local child that writes `text` to stderr and exits `exit_code`. Deliberately not
    ffmpeg: this asserts on the plumbing, and a real capture would need a stream URL."""
    proc = subprocess.Popen(
        [sys.executable, '-c',
         f'import sys; sys.stderr.write({text!r}); sys.stderr.flush(); sys.exit({exit_code})'],
        stdout=subprocess.DEVNULL, stderr=stderr_fh)
    proc.wait(timeout=30)
    return proc


class ReadStderrTailTests(unittest.TestCase):
    """The twin cap. Pure function of a path, so no app context is needed."""

    def setUp(self):
        self.t = make_test_app()
        self.path = os.path.join(self.t._tmpdir, 'tail.log')

    def tearDown(self):
        self.t.cleanup()

    def _write(self, data):
        with open(self.path, 'wb') as fh:
            fh.write(data.encode() if isinstance(data, str) else data)

    def test_byte_cap_bounds_the_output_when_the_line_cap_cannot(self):
        """Deliberately shaped so the LINE cap is no help: 20 lines is already within it, so
        only the bounded read keeps the result small. A file of many short lines would pass
        this on the line cap alone and prove nothing about the byte cap."""
        self._write('\n'.join('x' * 100_000 for _ in range(20)))
        self.assertLess(len(read_stderr_tail(self.path).encode()), 4096 + 512)

    def test_line_cap_keeps_only_the_last_lines(self):
        self._write('\n'.join(f'line {i}' for i in range(500)))
        lines = read_stderr_tail(self.path).split('\n')
        self.assertEqual(len(lines), 20)
        self.assertEqual(lines[-1], 'line 499')

    def test_carriage_returns_do_not_defeat_the_line_cap(self):
        """Defensive, not the primary bound - measured on this box, ffmpeg redirected to a
        file emits newlines and essentially no \\r (dev/changelog/430). But a \\r-heavy
        stream is one str.replace away from collapsing the line cap entirely, and the flag
        set differs per call site, so the handling is asserted rather than assumed away."""
        self._write('\r'.join(f'frame={i} fps=25' for i in range(2000)) + '\rreal error here')
        out = read_stderr_tail(self.path)
        self.assertEqual(len(out.split('\n')), 20)
        self.assertTrue(out.endswith('real error here'))
        self.assertNotIn('\r', out)

    def test_short_file_is_returned_whole(self):
        """Seeking -4096 from the end of a 12-byte file would land before byte 0."""
        self._write('boom\nsplat\n')
        self.assertEqual(read_stderr_tail(self.path), 'boom\nsplat')

    def test_blank_lines_are_dropped(self):
        self._write('\n\n  \n\nonly line\n\n')
        self.assertEqual(read_stderr_tail(self.path), 'only line')

    def test_missing_and_empty_files_return_empty_string(self):
        """'' rather than a placeholder, so callers can stay quiet when there is nothing
        to say instead of emitting an event that says 'no output'."""
        self.assertEqual(read_stderr_tail(os.path.join(self.t._tmpdir, 'nope.log')), '')
        self._write('')
        self.assertEqual(read_stderr_tail(self.path), '')

    def test_invalid_utf8_does_not_raise(self):
        self._write(b'\xff\xfe bad bytes then\nreal message')
        self.assertIn('real message', read_stderr_tail(self.path))


class SpoolLifecycleTests(unittest.TestCase):
    """Open a spool, run a real local child into it, collect, and prove it is released."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.rec = seed.make_recording(status='IN_PROGRESS')
        db.session.commit()
        self.state = recorder.RecordingState()
        with recorder._lock:
            recorder._active[self.rec.id] = self.state

    def tearDown(self):
        with recorder._lock:
            recorder._active.pop(self.rec.id, None)
        self.ctx.pop()
        self.t.cleanup()

    def _spool(self, seg_num=1):
        return recorder._open_segment_stderr_spool(self.t.app, self.rec.id, seg_num)

    def test_spool_is_created_inside_the_sandbox(self):
        """A runtime load_config() here would resolve the REAL capture_log_dir and spool a
        test's stderr into the production tree (the class of BUGS.md 2026-07-18)."""
        path, fh = self._spool()
        self.addCleanup(recorder._discard_stderr_spool, path, fh)
        self.assertIsNotNone(path)
        self.assertTrue(path.startswith(self.t._tmpdir), path)
        self.assertTrue(os.path.exists(path))

    def test_spool_filename_is_unique_per_attempt(self):
        """A fixed name lets a shutdown-orphaned spool be read as the next attempt's own
        (the conversion path's BUGS.md 2026-07-23 defect)."""
        p1, f1 = self._spool()
        self.addCleanup(recorder._discard_stderr_spool, p1, f1)
        p2, f2 = recorder._open_segment_stderr_spool(self.t.app, self.rec.id, 1)
        self.addCleanup(recorder._discard_stderr_spool, p2, f2)
        self.assertNotEqual(p1, p2)

    def test_collect_returns_child_exit_code_and_stderr_tail(self):
        path, fh = self._spool()
        self.state.stderr_path, self.state.stderr_fh = path, fh
        self.state.process = _spawn_writing('Connection reset by peer\n', 3, fh)

        code, tail, _, _missing = recorder.collect_segment_diagnostics(self.rec.id)
        self.assertEqual(code, 3)
        self.assertIn('Connection reset by peer', tail)

    def test_collect_unlinks_the_spool_and_clears_the_state(self):
        """Teardown releases everything the create path acquired - otherwise every segment
        of every recording leaves a file behind."""
        path, fh = self._spool()
        self.state.stderr_path, self.state.stderr_fh = path, fh
        self.state.process = _spawn_writing('bye\n', 0, fh)

        recorder.collect_segment_diagnostics(self.rec.id)
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(self.state.stderr_path)
        self.assertIsNone(self.state.stderr_fh)

    def test_collect_is_idempotent(self):
        """Several terminal paths can run over the same segment; a second call must not
        raise on the already-unlinked spool."""
        path, fh = self._spool()
        self.state.stderr_path, self.state.stderr_fh = path, fh
        self.state.process = _spawn_writing('once\n', 1, fh)

        self.assertEqual(recorder.collect_segment_diagnostics(self.rec.id)[0], 1)
        self.assertEqual(recorder.collect_segment_diagnostics(self.rec.id),
                         (1, '', (0, True), False))

    def test_credentials_in_the_tail_are_masked(self):
        """ffmpeg echoes its input URL on error, and a provider stream URL carries the
        account username and password in its path.

        Written straight into the spool rather than through a child: netguard refuses to
        spawn any argv containing a URL, which is exactly the protection it should give -
        masking is collect_segment_diagnostics' job and it reads the FILE, so this exercises
        the real code path either way.
        """
        path, fh = self._spool()
        self.state.stderr_path, self.state.stderr_fh = path, fh
        fh.write(b'http://host.test/someuser/s3cr3tpass/12345: Server returned 403\n')
        fh.flush()
        self.state.process = _spawn_writing('', 1, subprocess.DEVNULL)

        _, tail, _reconnects, _missing = recorder.collect_segment_diagnostics(self.rec.id)
        self.assertNotIn('s3cr3tpass', tail)
        self.assertIn('403', tail)

    def test_negative_code_reports_the_signal_that_killed_it(self):
        """The distinction the whole feature turns on: a negative code is OUR SIGTERM, a
        positive one means ffmpeg gave up before we ever declared a stall."""
        path, fh = self._spool()
        self.state.stderr_path, self.state.stderr_fh = path, fh
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                stdout=subprocess.DEVNULL, stderr=fh)
        self.state.process = proc
        proc.kill()
        proc.wait(timeout=30)

        code, _, _reconnects, _missing = recorder.collect_segment_diagnostics(self.rec.id)
        self.assertEqual(code, -9)

    def test_collect_without_live_state_is_a_no_op(self):
        with recorder._lock:
            recorder._active.pop(self.rec.id, None)
        self.assertEqual(recorder.collect_segment_diagnostics(self.rec.id),
                         (None, '', (0, True), False))

    def test_a_deleted_spool_is_reported_as_missing_not_as_silence(self):
        """dev/docs/BUGS.md 2026-09-14 @ 10:42 - read_stderr_tail returns '' both when
        ffmpeg said nothing and when its spool was destroyed, so the two were
        indistinguishable and the destroyed case produced no event at all."""
        path, fh = self._spool()
        self.state.stderr_path, self.state.stderr_fh = path, fh
        self.state.process = _spawn_writing('a lot of context\n', 0, fh)
        os.unlink(path)  # what a second app build's sweep did to a live capture

        code, tail, reconnects, spool_missing = recorder.collect_segment_diagnostics(self.rec.id)
        self.assertTrue(spool_missing)
        self.assertEqual(code, 0)
        self.assertEqual(tail, '')
        self.assertEqual(reconnects, (0, True))

    def test_a_present_but_empty_spool_is_not_reported_as_missing(self):
        """The two states this separates are only useful if silence still reads as silence."""
        path, fh = self._spool()
        self.state.stderr_path, self.state.stderr_fh = path, fh
        self.state.process = _spawn_writing('', 0, fh)

        _code, tail, _reconnects, spool_missing = recorder.collect_segment_diagnostics(self.rec.id)
        self.assertFalse(spool_missing)
        self.assertEqual(tail, '')


class StartupSweepTests(unittest.TestCase):
    """Builds a second app, so it pushes no context of its own - make_test_app pushes one.

    start_scheduler=True throughout, deliberately: since dev/changelog/967 the sweep runs
    from init_scheduler() behind the singleton pidfile claim, because a process that does
    not own startup recovery has no business deleting spools it cannot tell apart from a
    live capture's.
    """

    def _dir(self):
        log_dir = tempfile.mkdtemp(prefix='dvr_test_caplog_')
        self.addCleanup(shutil.rmtree, log_dir, True)
        return log_dir

    @staticmethod
    def _spool_in(log_dir, name='.cap-stderr-999-1-deadbeef.log'):
        path = os.path.join(log_dir, name)
        with open(path, 'wb') as fh:
            fh.write(b'left over from a killed process')
        return path

    def test_startup_sweep_removes_orphaned_spools(self):
        """kill_all_active() runs in a signal handler and deliberately does no cleanup, so a
        spool orphaned by a hard shutdown is only ever collected here. Without this they
        accumulate forever for any recording that never resumes.

        The dir is standalone rather than a previous TestApp's, whose cleanup() rmtrees it.
        """
        log_dir = self._dir()
        orphan = self._spool_in(log_dir)

        t = make_test_app(extra_overrides={'recording': {'capture_log_dir': log_dir}},
                          start_scheduler=True)
        self.addCleanup(t.cleanup)
        self.assertFalse(os.path.exists(orphan))

    def test_an_app_that_owns_no_startup_recovery_sweeps_nothing(self):
        """dev/docs/BUGS.md 2026-09-14 @ 10:42 - the defect itself. Building an app object
        against the real config (an ad-hoc read-only check while the service is up) deleted
        the running recording's spool, and that segment lost its diagnostics for good."""
        log_dir = self._dir()
        live = self._spool_in(log_dir, '.cap-stderr-18-1-abcd1234.log')

        t = make_test_app(extra_overrides={'recording': {'capture_log_dir': log_dir}})
        self.addCleanup(t.cleanup)
        self.assertTrue(os.path.exists(live))

    def test_sweep_leaves_unrelated_files_alone(self):
        """The glob is anchored on the .cap-stderr- prefix: capture_log_dir is a
        user-configurable path and may not be exclusively ours."""
        log_dir = self._dir()
        keep = os.path.join(log_dir, 'notes.txt')
        with open(keep, 'wb') as fh:
            fh.write(b'not mine')

        t = make_test_app(extra_overrides={'recording': {'capture_log_dir': log_dir}},
                          start_scheduler=True)
        self.addCleanup(t.cleanup)
        self.assertTrue(os.path.exists(keep))

    def test_the_sweep_runs_before_anything_resumes(self):
        """Ordering, not merely placement: resume_in_progress_recordings() opens the very
        spools this deletes by directory listing, so a sweep after it would destroy the
        spool of the segment it just launched."""
        import inspect
        from app import scheduler as sched
        src = inspect.getsource(sched.init_scheduler)
        self.assertLess(src.index('sweep_stale_stderr_spools(app)'),
                        src.index('resume_in_progress_recordings(app)'))


class RecordDiagnosticsTests(unittest.TestCase):
    """The column/extra_data partition, and when an event is worth emitting at all."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.rec = seed.make_recording(status='IN_PROGRESS', with_segment=True)
        db.session.commit()
        self.seg = RecordingSegment.query.filter_by(recording_id=self.rec.id).first()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _record(self, code, tail):
        recorder.record_segment_diagnostics(self.rec.id, self.seg, code, tail)
        db.session.commit()

    def _events(self):
        return RecordingEvent.query.filter_by(
            recording_id=self.rec.id, event_type=DIAGNOSTICS).all()

    def test_exit_code_lands_on_the_column(self):
        """The headline defect: this column had no writer anywhere in app/."""
        self._record(1, '')
        db.session.refresh(self.seg)
        self.assertEqual(self.seg.ffmpeg_exit_code, 1)

    def test_zero_is_stored_and_stays_distinct_from_null(self):
        self._record(0, '')
        db.session.refresh(self.seg)
        self.assertEqual(self.seg.ffmpeg_exit_code, 0)
        self.assertIsNotNone(self.seg.ffmpeg_exit_code)

    def test_exit_code_is_not_duplicated_into_extra_data(self):
        """CLAUDE.md measurements rule: a stat with a column does NOT also live in
        extra_data - two sources of truth for one fact is the defect, not the fix."""
        self._record(7, 'Server returned 500\n')
        extra = json.loads(self._events()[0].extra_data)
        self.assertEqual(extra, {'kind': 'capture_stderr', 'stderr_tail': 'Server returned 500\n'})

    def test_exit_code_is_carried_in_the_detail_string(self):
        """Headline numbers belong in detail, which is where the event log shows them."""
        self._record(7, 'Server returned 500\n')
        self.assertIn('7', self._events()[0].detail)

    def test_stderr_tail_is_the_only_extra_payload(self):
        self._record(7, 'Server returned 500\n')
        extra = json.loads(self._events()[0].extra_data)
        self.assertEqual(extra['kind'], 'capture_stderr')
        self.assertIn('Server returned 500', extra['stderr_tail'])

    def test_event_carries_the_segment_number(self):
        self._record(7, 'boom')
        self.assertEqual(self._events()[0].segment_number, self.seg.segment_number)

    def test_no_event_for_an_uninformative_signal_exit(self):
        """A negative code is a signal WE sent, which seg.exit_reason already names. With
        no stderr there is nothing an event would add - and a stall-loop recording would
        otherwise gain 75 empty events."""
        self._record(-15, '')
        self.assertEqual(self._events(), [])
        db.session.refresh(self.seg)
        self.assertEqual(self.seg.ffmpeg_exit_code, -15)

    def test_signal_exit_with_stderr_does_emit(self):
        self._record(-15, 'http error 502 from upstream')
        self.assertEqual(len(self._events()), 1)

    def test_clean_exit_with_no_output_stays_quiet(self):
        self._record(0, '')
        self.assertEqual(self._events(), [])

    def test_positive_code_emits_even_with_no_stderr(self):
        """ffmpeg dying on its own is always worth a line, whether or not it explained."""
        self._record(4, '')
        self.assertEqual(len(self._events()), 1)

    def test_null_segment_does_not_raise(self):
        """The give-up path may find no open segment row to attach to."""
        recorder.record_segment_diagnostics(self.rec.id, None, 2, 'gone')
        db.session.commit()
        self.assertEqual(len(self._events()), 1)
        self.assertIsNone(self._events()[0].segment_number)

    def _record_missing(self, code):
        recorder.record_segment_diagnostics(self.rec.id, self.seg, code, '', (0, True),
                                            spool_missing=True)
        db.session.commit()

    def test_a_lost_spool_emits_even_on_an_uninformative_signal_exit(self):
        """dev/docs/BUGS.md 2026-09-14 @ 10:42 - recording 18's segment 1 (46 minutes,
        exit -9) fell into the quiet branch and left the one segment worth explaining with
        nothing on its timeline at all."""
        self._record_missing(-9)
        self.assertEqual(len(self._events()), 1)

    def test_a_lost_spool_says_so_in_the_detail(self):
        """Principle 1: the user is told the output was destroyed, not left to infer it
        from an absence."""
        self._record_missing(-9)
        detail = self._events()[0].detail
        self.assertIn('ffmpeg killed by signal 9', detail)
        self.assertIn('not captured', detail)

    def test_a_lost_spool_adds_no_extra_payload(self):
        """There is no tail to carry, so extra_data stays the bare kind marker."""
        self._record_missing(-9)
        extra = json.loads(self._events()[0].extra_data)
        self.assertEqual(set(extra), {'kind'})

    def test_a_present_spool_never_mentions_a_missing_one(self):
        self._record(-15, 'http error 502 from upstream')
        self.assertNotIn('not captured', self._events()[0].detail)


class CloseActiveSegmentTests(unittest.TestCase):
    """End-to-end over the real terminal path used by stop/abort/pause/handoff."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.rec = seed.make_recording(status='IN_PROGRESS')
        seg = RecordingSegment(recording_id=self.rec.id, segment_number=1,
                               file_path=os.path.join(self.t._tmpdir, 'seg1.ts'),
                               started_at=self.rec.start_time)
        db.session.add(seg)
        db.session.commit()
        self.state = recorder.RecordingState()
        with recorder._lock:
            recorder._active[self.rec.id] = self.state

    def tearDown(self):
        with recorder._lock:
            recorder._active.pop(self.rec.id, None)
        self.ctx.pop()
        self.t.cleanup()

    def test_close_writes_exit_code_and_emits_the_diagnostic(self):
        path, fh = recorder._open_segment_stderr_spool(self.t.app, self.rec.id, 1)
        self.state.stderr_path, self.state.stderr_fh = path, fh
        self.state.process = _spawn_writing('Input/output error\n', 5, fh)

        recorder._close_active_segment(self.t.app, self.rec.id, 'STOP_TIME_REACHED')

        db.session.expire_all()
        seg = RecordingSegment.query.filter_by(recording_id=self.rec.id).first()
        self.assertEqual(seg.ffmpeg_exit_code, 5)
        self.assertEqual(seg.exit_reason, 'STOP_TIME_REACHED')
        self.assertIsNotNone(seg.ended_at)
        evts = RecordingEvent.query.filter_by(
            recording_id=self.rec.id, event_type=DIAGNOSTICS).all()
        self.assertEqual(len(evts), 1)
        self.assertIn('Input/output error', json.loads(evts[0].extra_data)['stderr_tail'])
        self.assertFalse(os.path.exists(path))

    def test_close_still_works_without_a_spool(self):
        """Capture outranks its own diagnostics: if the spool could not be opened the
        segment must still close normally."""
        self.state.process = _spawn_writing('x', 0, subprocess.DEVNULL)
        recorder._close_active_segment(self.t.app, self.rec.id, 'MANUAL_STOP')

        db.session.expire_all()
        seg = RecordingSegment.query.filter_by(recording_id=self.rec.id).first()
        self.assertEqual(seg.exit_reason, 'MANUAL_STOP')
        self.assertEqual(seg.ffmpeg_exit_code, 0)


class SegmentTableRenderTests(unittest.TestCase):
    """The recording detail page surface."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.rec = seed.make_recording(status='COMPLETED')
        self.seg = RecordingSegment(
            recording_id=self.rec.id, segment_number=1,
            file_path='/dvr/x_seg_001.ts', started_at=self.rec.start_time,
            ended_at=self.rec.stop_time, exit_reason='STALL_KILLED', bytes_recorded=2048)
        db.session.add(self.seg)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _page(self):
        resp = self.t.client.get(f'/recordings/{self.rec.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _add_tail(self, tail, seg_number=1):
        db.session.add(RecordingEvent(
            recording_id=self.rec.id, event_type=DIAGNOSTICS, segment_number=seg_number,
            detail='Segment 1 capture ended: ffmpeg exited 5',
            extra_data=json.dumps({'kind': 'capture_stderr', 'stderr_tail': tail})))
        db.session.commit()

    def test_positive_exit_code_renders_as_a_bad_chip(self):
        self.seg.ffmpeg_exit_code = 5
        db.session.commit()
        self.assertIn('rc 5', self._page())

    def test_negative_exit_code_renders_as_the_signal_number(self):
        self.seg.ffmpeg_exit_code = -15
        db.session.commit()
        html = self._page()
        self.assertIn('sig 15', html)
        self.assertNotIn('sig -15', html)

    def test_null_exit_code_renders_no_chip(self):
        """Every segment recorded before the exit-code column existed has NULL here."""
        self.assertNotIn('exit-code', self._page())

    def test_stderr_tail_renders_in_an_inline_disclosure(self):
        self._add_tail('Server returned 503 Service Unavailable')
        html = self._page()
        self.assertIn('class="seg-why"', html)
        self.assertIn('Server returned 503 Service Unavailable', html)

    def test_disclosure_is_keyed_on_the_segment_number(self):
        """#panel-segments-slot is swapped wholesale every 15s; without a stable key an
        open disclosure snaps shut under the reader."""
        self._add_tail('boom')
        self.assertIn('data-seg-no="1"', self._page())

    def test_tail_for_another_segment_does_not_leak_onto_this_row(self):
        self._add_tail('other segment problem', seg_number=9)
        self.assertNotIn('class="seg-why"', self._page())

    def test_malformed_extra_data_does_not_500(self):
        db.session.add(RecordingEvent(
            recording_id=self.rec.id, event_type=DIAGNOSTICS, segment_number=1,
            detail='bad blob', extra_data='{not json'))
        db.session.commit()
        self.assertNotIn('class="seg-why"', self._page())


if __name__ == '__main__':
    unittest.main()
