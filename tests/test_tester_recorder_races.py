"""Tier 2 - four independent tester/recorder races, each a window between two state changes.

Guards four dev/docs/BUGS.md entries dated 2026-08-18, one class per defect. They live in one
file because they share the fake-capture scaffolding, not because they are one defect - each
class states its own invariant and fails for its own reason.

1. PreemptDuringFinalizeTailTests - a preempt landing in a test's finalize tail set a flag with
   no consumer left, and the NEXT channel aborted at its connect loop and was recorded CANCELLED
   for a recording that started before that test existed.
2. BusySkipRevertsJobStatusTests - a manual start that lost the race for the tester returned
   before the try/finally that saves a final status, leaving the job row RUNNING forever with no
   thread behind it.
3. ResumeUsesLatestTestPerChannelTests - resume built its already-completed set from all history,
   so once a job had completed once, no later cancelled re-run could be resumed.
4. RacedSegmentIsClosedTests - a segment row committed just after a concurrent teardown kept
   ended_at NULL forever, next to a partial .ts the abort's delete pass had already walked past.

No real ffmpeg and no network: subprocess.Popen is monkeypatched throughout, and the
interleaves are driven from inside patched calls so they are deterministic rather than timed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import config as cfgmod  # noqa: E402
from app.database import (  # noqa: E402
    ChannelTest, OnDemandTestJob, RecordingSegment, RecordingEvent,
    SEGMENT_ENDED, REC_STATUS_IN_PROGRESS,
)
from app import channel_tester, connection_limits as connlim, recorder  # noqa: E402
from app.routes import channel_tests as ct_routes  # noqa: E402


class FakeProc:
    """Stands in for a Popen'd ffmpeg. Mirrors tests/test_tester_preemption.py::FakeProc -
    duplicated from tests/test_tester_preemption.py - that file's copy is scoped to the
    slotless-preemption guards and neither should have to change when the other does."""

    def __init__(self):
        self.terminated = False
        self.killed = False
        self.stderr = None
        self.pid = 4242
        self._returncode = None
        self.signals = []

    def send_signal(self, sig):
        # terminate_or_kill() continues a possibly-suspended child before terminating it
        # (dev/changelog/952), so a stand-in that cannot take a signal is not a faithful one.
        self.signals.append(sig)

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = -15

    def kill(self):
        self.killed = True
        self._returncode = -9

    def wait(self, timeout=None):
        self._returncode = self._returncode if self._returncode is not None else 0
        return self._returncode

    @property
    def signalled(self):
        return self.terminated or self.killed


class PreemptDuringFinalizeTailTests(unittest.TestCase):
    """BUGS.md 2026-08-18 - the preemption flag leaked past its only consumer.

    `active_test_account_id` stayed set from slot-acquire until run_channel_test's finally, i.e.
    through the whole finalize tail that runs AFTER the end-of-test block consumes
    `preempted_by_recording`. A recording preempting in that window set the flag with nothing
    left to read it, and only _reset_run_state (start of the next RUN, not the next channel)
    cleared it - so the next channel of the same run aborted at its connect loop and was
    recorded CANCELLED with "Interrupted by recording start".
    """

    def setUp(self):
        self.t = make_test_app()
        with self.t.app.app_context():
            acct = seed.make_account(name='Tail Race', max_connections=1)
            first = seed.make_channel(acct, name='First Channel')
            second = seed.make_channel(acct, stream_id='2002', name='Second Channel')
            rec = seed.make_recording(status='SCHEDULED')
            db.session.commit()
            self.account_id = acct.id
            self.first_id = first.id
            self.second_id = second.id
            self.recording_id = rec.id
        connlim._holders.clear()

    def tearDown(self):
        # _end_run(), never _reset_run_state(): the latter takes a KIND_TESTER admission ticket
        # and leaks it into the next module (dev/changelog/723).
        channel_tester._end_run()
        connlim._holders.clear()
        self.t.cleanup()

    def _fake_capture(self):
        """Patches that carry a channel test all the way to its finalize tail without ffmpeg.
        The connect attempt reports no data, so the test ends FAILED - which is the point: it
        was never preempted while it was running."""
        return [
            mock.patch.object(channel_tester.subprocess, 'Popen', lambda *a, **kw: FakeProc()),
            mock.patch.object(channel_tester, '_drain_stderr', lambda *a, **kw: None),
            mock.patch.object(channel_tester, 'wait_for_file_data', lambda *a, **kw: False),
            mock.patch.object(channel_tester, '_interruptible_sleep', lambda _d: None),
        ]

    def _run_with_preempt_in_tail(self, channel_id, handled):
        real_finalize = channel_tester._finalize_test

        def _preempt_then_finalize(*a, **kw):
            # _finalize_test runs after the end-of-test block has already consumed the flag,
            # so this is squarely inside the window the defect lived in.
            handled.append(channel_tester.kill_active_test_for_account(self.t.app, self.account_id))
            return real_finalize(*a, **kw)

        patches = self._fake_capture()
        patches.append(mock.patch.object(channel_tester, '_finalize_test', _preempt_then_finalize))
        for p in patches:
            p.start()
        try:
            channel_tester.run_channel_test(self.t.app, channel_id)
        finally:
            for p in reversed(patches):
                p.stop()

    def _run_plain(self, channel_id):
        patches = self._fake_capture()
        for p in patches:
            p.start()
        try:
            channel_tester.run_channel_test(self.t.app, channel_id)
        finally:
            for p in reversed(patches):
                p.stop()

    def _latest_test_row(self, channel_id):
        with self.t.app.app_context():
            db.session.expire_all()
            return (ChannelTest.query.filter_by(channel_id=channel_id)
                    .order_by(ChannelTest.id.desc()).first())

    def test_kill_helper_does_not_match_a_test_past_its_consumption_point(self):
        """The helper must report False once the test can no longer act on a preemption -
        answering True there is what left an unconsumable flag behind."""
        handled = []
        self._run_with_preempt_in_tail(self.first_id, handled)
        self.assertEqual(handled, [False],
                         'a test in its finalize tail can no longer be preempted, so the kill '
                         'helper must not claim it handled one')

    def test_flag_is_not_left_set_after_a_tail_preempt(self):
        """Nothing consumes the flag between the end-of-test block and the next RUN, so a flag
        set in the tail survives every remaining channel of this run."""
        self._run_with_preempt_in_tail(self.first_id, [])
        self.assertFalse(channel_tester._is_preempted(),
                         'preemption flag outlived the test that was its only consumer')

    def test_the_preempted_test_itself_is_not_retro_cancelled(self):
        """The test had already measured its result when the preempt landed; it must keep it."""
        self._run_with_preempt_in_tail(self.first_id, [])
        row = self._latest_test_row(self.first_id)
        self.assertEqual(row.status, 'FAILED')
        self.assertNotIn('Interrupted by recording start', row.error_detail or '')

    def test_next_channel_is_not_cancelled_by_the_previous_channel_tail(self):
        """The defect as the user saw it: the next channel of the same run recorded CANCELLED
        with "Interrupted by recording start" though it was not running when that recording
        started."""
        self._run_with_preempt_in_tail(self.first_id, [])
        self._run_plain(self.second_id)

        row = self._latest_test_row(self.second_id)
        self.assertIsNotNone(row, 'the second channel must still have been tested')
        self.assertEqual(row.status, 'FAILED',
                         'the second channel was cancelled by a preemption aimed at the first')
        self.assertNotIn('Interrupted by recording start', row.error_detail or '')

    def test_a_preempt_before_the_consumption_point_still_cancels(self):
        """The narrowing must not cost the real preemption its effect - the whole reason the
        flag exists. Characterization of the behavior the fix has to preserve."""
        def _wait_then_preempt(*a, **kw):
            channel_tester.kill_active_test_for_account(self.t.app, self.account_id)
            return False

        patches = self._fake_capture()
        patches[2] = mock.patch.object(channel_tester, 'wait_for_file_data', _wait_then_preempt)
        for p in patches:
            p.start()
        try:
            channel_tester.run_channel_test(self.t.app, self.first_id)
        finally:
            for p in reversed(patches):
                p.stop()

        row = self._latest_test_row(self.first_id)
        self.assertEqual(row.status, 'CANCELLED')
        self.assertIn('Interrupted by recording start', row.error_detail or '')


class BusySkipRevertsJobStatusTests(unittest.TestCase):
    """BUGS.md 2026-08-18 - a manual start that lost the race stranded the job in RUNNING.

    routes/channel_tests.py::_start_job_run commits status='RUNNING' and then spawns the thread.
    If that thread finds the tester busy, run_on_demand_test_job's busy branch alerts JOB_SKIPPED
    and returns before the try/finally that saves a final status, so nothing ever takes the row
    back out of RUNNING.
    """

    def setUp(self):
        self.t = make_test_app()
        with self.t.app.app_context():
            acct = seed.make_account(name='Job Race')
            ch = seed.make_channel(acct, name='Job Channel')
            job = seed.make_test_job(name='Skipped Job', channels=[ch], status='RUNNING')
            other = seed.make_test_job(name='Busy Job', channels=[ch], status='RUNNING')
            db.session.commit()
            self.job_id = job.id
            self.other_job_id = other.id

    def tearDown(self):
        channel_tester._end_run()
        self.t.cleanup()

    def _make_tester_busy(self, running_job_id):
        """The state a run in progress leaves behind, without taking an admission ticket -
        _reset_run_state() would, and would leak KIND_TESTER out of this module."""
        with channel_tester._lock:
            channel_tester._state.is_running = True
            channel_tester._state.run_kind = 'job'
            channel_tester._state.current_job_id = running_job_id

    def _status(self, job_id):
        with self.t.app.app_context():
            db.session.expire_all()
            return db.session.get(OnDemandTestJob, job_id).status

    def _set(self, job_id, **fields):
        with self.t.app.app_context():
            job = db.session.get(OnDemandTestJob, job_id)
            for k, v in fields.items():
                setattr(job, k, v)
            db.session.commit()

    def test_one_off_job_reverts_out_of_running(self):
        self._make_tester_busy(self.other_job_id)
        channel_tester.run_on_demand_test_job(self.t.app, self.job_id)
        self.assertEqual(self._status(self.job_id), 'CANCELLED',
                         'a skipped manual start must not leave the row RUNNING with no thread')

    def test_recurring_job_reverts_to_scheduled_not_cancelled(self):
        """A recurring job never goes terminal - its trigger keeps firing - so the resting
        status is SCHEDULED. finalize_on_demand_job_status owns that decision tree; the busy
        branch must not re-implement a second one."""
        self._set(self.job_id, recurring=True, recur_day=0, recur_time='03:00')
        self._make_tester_busy(self.other_job_id)
        channel_tester.run_on_demand_test_job(self.t.app, self.job_id)
        self.assertEqual(self._status(self.job_id), 'SCHEDULED')

    def test_the_busy_run_own_row_is_left_alone(self):
        """A recurring job's own trigger re-firing mid-run lands in the busy branch with
        running_job_id == job_id. Reverting there would take the LIVE run's row out of RUNNING
        while it is still testing channels."""
        self._make_tester_busy(self.job_id)
        channel_tester.run_on_demand_test_job(self.t.app, self.job_id)
        self.assertEqual(self._status(self.job_id), 'RUNNING',
                         'the row of the run that is actually executing must not be reverted')

    def test_a_scheduled_fire_row_is_not_stamped(self):
        """A scheduled fire never went through _start_job_run, so its row was never RUNNING and
        there is nothing to revert - it must not be stamped completed_at for a run that never
        happened."""
        self._set(self.job_id, status='SCHEDULED', completed_at=None)
        self._make_tester_busy(self.other_job_id)
        channel_tester.run_on_demand_test_job(self.t.app, self.job_id)
        with self.t.app.app_context():
            db.session.expire_all()
            job = db.session.get(OnDemandTestJob, self.job_id)
            self.assertEqual(job.status, 'SCHEDULED')
            self.assertIsNone(job.completed_at)


class ResumeUsesLatestTestPerChannelTests(unittest.TestCase):
    """BUGS.md 2026-08-18 - resume was broken on any job that had ever completed once.

    resume_on_demand_job built completed_ids from every ChannelTest row for the job across all
    history. Restart deliberately keeps prior rows, so after one completed run, a later
    restarted-then-cancelled run resumed to an empty subset and the route answered "All channels
    are already completed" for a run that had barely started.
    """

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        acct = seed.make_account(name='Resume Acct')
        self.chans = [seed.make_channel(acct, stream_id=str(3000 + i), name=f'Ch {i}')
                      for i in range(3)]
        self.job = seed.make_test_job(name='Resumable', channels=self.chans, status='CANCELLED')
        db.session.commit()
        self.job_id = self.job.id
        self.t.app.config['WTF_CSRF_ENABLED'] = False

        # Run 1: every channel passed. Run 2 (a restart) was cancelled after channel 0 only,
        # and that one test was aborted rather than measured.
        for ch in self.chans:
            seed.make_channel_test(ch, status='COMPLETED', job_id=self.job_id)
        seed.make_channel_test(self.chans[0], status='CANCELLED', job_id=self.job_id)
        db.session.commit()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _resume(self):
        with mock.patch.object(ct_routes, '_start_job_run') as spawn:
            resp = self.client.post(f'/api/channel-tests/on-demand/{self.job_id}/resume',
                                    json={'force': True})
        return resp, spawn

    def test_resume_is_not_refused_after_an_earlier_completed_run(self):
        resp, _ = self._resume()
        self.assertEqual(resp.status_code, 200, resp.get_json())

    def test_resume_retests_only_the_channel_whose_latest_test_was_cancelled(self):
        resp, spawn = self._resume()
        self.assertEqual(resp.get_json().get('channels_to_test'), 1)
        subset = spawn.call_args.kwargs['subset']
        self.assertEqual(subset, [self.chans[0].id],
                         'only the channel whose CURRENT-run test was aborted may be re-tested')

    def test_a_channel_whose_latest_test_completed_is_still_skipped(self):
        """The rule resume exists for must survive the fix: a channel that passed most recently
        is not re-tested."""
        _, spawn = self._resume()
        subset = spawn.call_args.kwargs['subset']
        for ch in self.chans[1:]:
            self.assertNotIn(ch.id, subset)

    def test_a_completed_test_from_another_job_does_not_count(self):
        """_latest_tests_by_channel is scoped for_job_id - a pass under a different health check
        must not make this job think its own channel is done."""
        other_job = seed.make_test_job(name='Other', channels=self.chans, status='COMPLETED')
        db.session.commit()
        seed.make_channel_test(self.chans[0], status='COMPLETED', job_id=other_job.id)
        db.session.commit()

        _, spawn = self._resume()
        self.assertIn(self.chans[0].id, spawn.call_args.kwargs['subset'])


class RacedSegmentIsClosedTests(unittest.TestCase):
    """BUGS.md 2026-08-18 - a segment row created after teardown stayed open forever.

    _launch_segment commits the RecordingSegment row before checking get_state(). A concurrent
    abort/stop closes every open segment and then pops _active, so a row committed in between is
    invisible to that close pass and nothing else ever closes it: ended_at stays NULL, the detail
    page's `(seg.ended_at or now)` fallback renders an ever-growing duration, and the partial .ts
    stays on disk because the abort's delete pass already ran.
    """

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        # channel_id None: _reresolve_channel_url returns immediately for a URL-only recording,
        # which keeps this test on the segment lifecycle rather than URL drift.
        rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='Raced')
        db.session.commit()
        self.rid = rec.id
        self.seg_dir = os.path.join(self.t._tmpdir, 'segments')
        os.makedirs(self.seg_dir, exist_ok=True)
        self.proc = FakeProc()
        with recorder._lock:
            recorder._active.pop(self.rid, None)   # the teardown already ran

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _cfg(self):
        return cfgmod._deep_merge(cfgmod.load_config(),
                                  {'recording': {'dvr_output_dir': self.seg_dir,
                                                 'segment_duration_seconds': 0}})

    def _launch(self):
        """Run _launch_segment against a recording whose live state is already gone. The fake
        Popen writes the file ffmpeg would have started, so the unlink has something to remove."""
        def _popen(cmd, *a, **kw):
            for arg in cmd:
                if str(arg).endswith('.ts'):
                    with open(arg, 'wb') as fh:
                        fh.write(b'\x47' * 188)
            return self.proc

        with mock.patch.object(recorder, 'load_config', self._cfg), \
             mock.patch.object(recorder.subprocess, 'Popen', _popen):
            recorder._launch_segment(self.t.app, self.rid, seg_num=1)

        db.session.expire_all()
        return RecordingSegment.query.filter_by(recording_id=self.rid, segment_number=1).one()

    def test_the_raced_row_is_closed(self):
        seg = self._launch()
        self.assertIsNotNone(seg.ended_at,
                             'a segment row created after teardown is never closed by anything '
                             'else, so it must be closed here')

    def test_the_raced_row_names_why_it_ended(self):
        seg = self._launch()
        self.assertEqual(seg.exit_reason, 'TEARDOWN_RACE')

    def test_the_partial_file_is_deleted(self):
        seg = self._launch()
        self.assertFalse(os.path.exists(seg.file_path),
                         'the abort delete pass already ran, so this file has no other owner')

    def test_the_discard_is_on_the_event_timeline(self):
        """Product principle 1 - a segment that appears and vanishes must say why it did."""
        self._launch()
        ended = RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=SEGMENT_ENDED).all()
        self.assertTrue(ended, 'no SEGMENT_ENDED event explains the discarded segment')
        self.assertTrue(any('discarded' in (e.detail or '') for e in ended),
                        [e.detail for e in ended])

    def test_the_process_is_still_killed(self):
        """The pre-existing half of this branch must survive the addition."""
        self._launch()
        self.assertTrue(self.proc.signalled)


if __name__ == '__main__':
    unittest.main()
