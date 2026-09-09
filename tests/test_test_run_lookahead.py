"""Tier 2 - test-run lookahead for imminent recordings (DESIGN-concurrency.md §5.5).

New config key `channel_testing.skip_if_recording_within_minutes` (default 10; 0 = off).
Checked at run start in `run_on_demand_test_job` for ALL runs (system and custom) - unlike
the pre-existing `skip_if_recording_active` guard, which only applies to the system 'TV
Guide Channels' job. Mirrors sync's `skip_sync_if_recording_within_minutes` guard, not its
`skip_sync_if_recording_active` one - this is about a recording ABOUT to start, not one
already IN_PROGRESS (that case is unaffected and stays is_system-only).

Two call shapes, same helper (`app.channel_tester.imminent_recording_conflict`):
- Scheduled fires (APScheduler -> run_on_demand_test_job directly, no `force`): skip with
  the existing JOB_SKIPPED alert pattern, self-heals at the job's next fire.
- Manual "Run Now" routes (start/resume/test-selected/restart): warn + explicit `force`
  override, same 409 envelope as manual sync (§5.4) - refuse with the reason before
  spawning the thread; passing force=true both bypasses the route's own check and threads
  `force=True` through so run_on_demand_test_job doesn't re-skip what the user just forced.

No real ffmpeg/test runs: `_run_channel_loop` is patched everywhere so "did it run" is
asserted by call, not by timing out waiting for a fake test to finish.
"""
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, channel_tester  # noqa: E402


def _cfg(within_minutes=10):
    """Only the keys the guard reads. load_config() is patched rather than passed through
    make_test_app overrides, which are invisible to a runtime load_config (CLAUDE.md)."""
    return {
        'channel_testing': {'skip_if_recording_within_minutes': within_minutes},
        'notifications': {'routing': {}, 'base_url': ''},
    }


def _join_test_threads():
    for t in threading.enumerate():
        if t.name.startswith('od-test-job-'):
            t.join(timeout=5)


class ImminentRecordingConflictHelperTests(unittest.TestCase):
    """The shared helper itself."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _conflict(self, **cfg_kw):
        with mock.patch('app.config.load_config', return_value=_cfg(**cfg_kw)):
            return channel_tester.imminent_recording_conflict()

    def test_no_recordings_no_conflict(self):
        self.assertIsNone(self._conflict())

    def test_imminent_scheduled_recording_is_a_conflict(self):
        start = datetime.utcnow() + timedelta(minutes=2)
        seed.make_recording(status='SCHEDULED', name='Starts Soon',
                             start_time=start, stop_time=start + timedelta(hours=1))
        db.session.commit()
        reason = self._conflict()
        self.assertIsNotNone(reason)
        self.assertIn('Starts Soon', reason,
                       'the reason must name the recording, not just say "a recording"')

    def test_distant_scheduled_recording_is_not_a_conflict(self):
        start = datetime.utcnow() + timedelta(hours=6)
        seed.make_recording(status='SCHEDULED', name='Later',
                             start_time=start, stop_time=start + timedelta(hours=1))
        db.session.commit()
        self.assertIsNone(self._conflict())

    def test_in_progress_recording_alone_is_not_a_conflict(self):
        """Distinct from sync's skip_if_active check - an already-IN_PROGRESS recording is
        the pre-existing is_system-only guard's job, not this one's."""
        seed.make_recording(status='IN_PROGRESS', name='Already Running')
        db.session.commit()
        self.assertIsNone(self._conflict())

    def test_within_minutes_zero_disables_the_check(self):
        start = datetime.utcnow() + timedelta(minutes=2)
        seed.make_recording(status='SCHEDULED', name='Starts Soon',
                             start_time=start, stop_time=start + timedelta(hours=1))
        db.session.commit()
        self.assertIsNone(self._conflict(within_minutes=0))


class RunOnDemandTestJobMissingRowSelfHealTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-08-10: a jobstore od_job_<id> entry can outlive its
    OnDemandTestJob DB row (found live as od_job_1, from a since-fixed test isolation bug
    that wrote real jobstore rows into production). Firing run_on_demand_test_job for a
    job_id with no DB row must remove that job's own scheduler entry, or it fires (and
    logs the same error) forever."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        self.t.cleanup()

    def test_missing_job_removes_its_own_scheduler_entry(self):
        from app.scheduler import _add_job, _on_demand_job_trigger, get_scheduler

        nonexistent_job_id = 999999
        aps_job_id = f'od_job_{nonexistent_job_id}'
        _add_job(func=_on_demand_job_trigger, trigger='cron', hour=6, minute=0,
                 args=[nonexistent_job_id], id=aps_job_id, replace_existing=True)
        self.assertIsNotNone(get_scheduler().get_job(aps_job_id))

        channel_tester.run_on_demand_test_job(self.t.app, nonexistent_job_id)

        self.assertIsNone(get_scheduler().get_job(aps_job_id),
                          'a run for a nonexistent job must clean up its own stray entry')


class RunOnDemandTestJobSkipTests(unittest.TestCase):
    """The scheduled-fire path: run_on_demand_test_job checks the helper itself unless
    `force`, for both system and custom jobs."""

    def setUp(self):
        self.t = make_test_app()
        self.channel = seed.make_channel(seed.make_account())
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _run(self, force=False, conflict='A recording starts within 10 minutes ("X").'):
        job = seed.make_test_job(name='Custom Job', channels=[self.channel], status='RUNNING')
        db.session.commit()
        loop_spy = mock.Mock(return_value=True)
        with mock.patch('app.channel_tester.imminent_recording_conflict', return_value=conflict), \
             mock.patch('app.channel_tester._run_channel_loop', loop_spy):
            channel_tester.run_on_demand_test_job(self.t.app, job.id, force=force)
        return job.id, loop_spy

    def test_conflict_skips_the_run(self):
        job_id, loop_spy = self._run(force=False)
        loop_spy.assert_not_called()

    def test_conflict_raises_a_job_skipped_alert(self):
        with mock.patch('app.alerts.create_alert') as alert_spy:
            self._run(force=False)
        alert_spy.assert_called_once()
        self.assertEqual(alert_spy.call_args.args[0], 'JOB_SKIPPED')

    def test_force_bypasses_the_check(self):
        job_id, loop_spy = self._run(force=True)
        loop_spy.assert_called_once()

    def test_no_conflict_runs_normally(self):
        job_id, loop_spy = self._run(force=False, conflict=None)
        loop_spy.assert_called_once()

    def test_custom_job_is_covered_not_just_system(self):
        """The pre-existing skip_if_recording_active guard is is_system-only; this one
        applies to ALL jobs - the fixture job here is a plain custom (non-system) job."""
        with mock.patch('app.config.load_config', return_value=_cfg(within_minutes=10)):
            start = datetime.utcnow() + timedelta(minutes=2)
            seed.make_recording(status='SCHEDULED', name='Starts Soon',
                                 start_time=start, stop_time=start + timedelta(hours=1))
            db.session.commit()
            job = seed.make_test_job(name='Custom Job 2', channels=[self.channel], status='RUNNING')
            db.session.commit()
            loop_spy = mock.Mock(return_value=True)
            with mock.patch('app.channel_tester._run_channel_loop', loop_spy):
                channel_tester.run_on_demand_test_job(self.t.app, job.id)
            loop_spy.assert_not_called()


class RunOnDemandTestJobBusyTests(unittest.TestCase):
    """The _state.is_running early return (channel_tester.py:438) - previously silent
    (dev/docs/BUGS.md 2026-08-07), now raises a JOB_SKIPPED alert naming the check that
    is already running, for every run_kind that can hold the slot."""

    def setUp(self):
        self.t = make_test_app()
        self.channel = seed.make_channel(seed.make_account())
        db.session.commit()

    def tearDown(self):
        with channel_tester._lock:
            channel_tester._state.clear()
        self.t.cleanup()

    def _mark_busy(self, job_id=None, run_kind='job', pre_check_recording_id=None):
        with channel_tester._lock:
            channel_tester._reset_run_state(job_id=job_id, run_kind=run_kind,
                                             pre_check_recording_id=pre_check_recording_id)

    def test_busy_job_skip_names_both_jobs_and_does_not_run(self):
        running_job = seed.make_test_job(name='Running Job', channels=[self.channel],
                                          status='RUNNING')
        skipped_job = seed.make_test_job(name='Skipped Job', channels=[self.channel],
                                          status='SCHEDULED')
        db.session.commit()
        self._mark_busy(job_id=running_job.id)

        loop_spy = mock.Mock(return_value=True)
        with mock.patch('app.channel_tester._run_channel_loop', loop_spy), \
             mock.patch('app.alerts.create_alert') as alert_spy:
            channel_tester.run_on_demand_test_job(self.t.app, skipped_job.id)

        loop_spy.assert_not_called()
        alert_spy.assert_called_once()
        self.assertEqual(alert_spy.call_args.args[0], 'JOB_SKIPPED')
        kwargs = alert_spy.call_args.kwargs
        self.assertIn('Skipped Job', kwargs['title'],
                       'title must name the job that was skipped')
        self.assertIn('Running Job', kwargs['body'],
                       'body must name the job that was already running - the actionable half')

    def test_busy_skip_does_not_corrupt_the_live_run_state(self):
        """last_skip_reason and current_job_id belong to the run that is currently going -
        the skipped job's alert must never write into them (one-flag-one-meaning)."""
        running_job = seed.make_test_job(name='Running Job', channels=[self.channel],
                                          status='RUNNING')
        skipped_job = seed.make_test_job(name='Skipped Job', channels=[self.channel],
                                          status='SCHEDULED')
        db.session.commit()
        self._mark_busy(job_id=running_job.id)

        with mock.patch('app.channel_tester._run_channel_loop'), \
             mock.patch('app.alerts.create_alert'):
            channel_tester.run_on_demand_test_job(self.t.app, skipped_job.id)

        self.assertEqual(channel_tester._state.current_job_id, running_job.id)
        self.assertIsNone(channel_tester._state.last_skip_reason)

    def test_busy_pre_check_skip_names_the_recording(self):
        rec = seed.make_recording(status='IN_PROGRESS', name='Protected Recording')
        skipped_job = seed.make_test_job(name='Skipped Job', channels=[self.channel],
                                          status='SCHEDULED')
        db.session.commit()
        self._mark_busy(run_kind='pre_check', pre_check_recording_id=rec.id)

        with mock.patch('app.channel_tester._run_channel_loop'), \
             mock.patch('app.alerts.create_alert') as alert_spy:
            channel_tester.run_on_demand_test_job(self.t.app, skipped_job.id)

        alert_spy.assert_called_once()
        self.assertIn('Protected Recording', alert_spy.call_args.kwargs['body'])

    def test_busy_one_off_skip_uses_a_generic_description(self):
        skipped_job = seed.make_test_job(name='Skipped Job', channels=[self.channel],
                                          status='SCHEDULED')
        db.session.commit()
        self._mark_busy(run_kind='one_off')

        with mock.patch('app.channel_tester._run_channel_loop'), \
             mock.patch('app.alerts.create_alert') as alert_spy:
            channel_tester.run_on_demand_test_job(self.t.app, skipped_job.id)

        alert_spy.assert_called_once()
        self.assertIn('manual channel test', alert_spy.call_args.kwargs['body'])


class ManualRunNowGuardTests(unittest.TestCase):
    """The four manual entry points: start, resume, test-selected, restart. Each must
    refuse with 409 + conflicts when conflicted and no force, proceed (threading `force`
    through) when force=true or unconflicted, and go through the one shared helper."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.channel = seed.make_channel(seed.make_account())
        db.session.commit()

    def tearDown(self):
        _join_test_threads()
        self.t.cleanup()

    def _conflicted(self, reason='A recording starts within 10 minutes ("X").'):
        return mock.patch('app.channel_tester.imminent_recording_conflict', return_value=reason)

    def _unconflicted(self):
        return mock.patch('app.channel_tester.imminent_recording_conflict', return_value=None)

    def _post(self, url, **json_body):
        with mock.patch('app.channel_tester.run_on_demand_test_job') as run_spy:
            resp = self.t.client.post(url, json=json_body)
            _join_test_threads()
        return resp, run_spy

    # -- start --

    def test_start_refuses_when_conflicted(self):
        job = seed.make_test_job(name='J', channels=[self.channel], status='QUEUED')
        db.session.commit()
        with self._conflicted('Reactor is melting.'):
            resp, run_spy = self._post(f'/api/channel-tests/on-demand/{job.id}/start')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('Reactor is melting.', resp.get_json()['conflicts'])
        run_spy.assert_not_called()

    def test_start_force_bypasses_the_refusal(self):
        job = seed.make_test_job(name='J', channels=[self.channel], status='QUEUED')
        db.session.commit()
        with self._conflicted():
            resp, run_spy = self._post(f'/api/channel-tests/on-demand/{job.id}/start', force=True)
        self.assertEqual(resp.status_code, 200)
        run_spy.assert_called_once()
        self.assertTrue(run_spy.call_args.kwargs.get('force'))

    def test_start_unconflicted_proceeds_normally(self):
        job = seed.make_test_job(name='J', channels=[self.channel], status='QUEUED')
        db.session.commit()
        with self._unconflicted():
            resp, run_spy = self._post(f'/api/channel-tests/on-demand/{job.id}/start')
        self.assertEqual(resp.status_code, 200)
        run_spy.assert_called_once()
        self.assertFalse(run_spy.call_args.kwargs.get('force'))

    # -- resume --

    def test_resume_refuses_when_conflicted(self):
        job = seed.make_test_job(name='J', channels=[self.channel], status='CANCELLED')
        db.session.commit()
        with self._conflicted('Reactor is melting.'):
            resp, run_spy = self._post(f'/api/channel-tests/on-demand/{job.id}/resume')
        self.assertEqual(resp.status_code, 409)
        run_spy.assert_not_called()

    def test_resume_force_bypasses_the_refusal(self):
        job = seed.make_test_job(name='J', channels=[self.channel], status='CANCELLED')
        db.session.commit()
        with self._conflicted():
            resp, run_spy = self._post(f'/api/channel-tests/on-demand/{job.id}/resume', force=True)
        self.assertEqual(resp.status_code, 200)
        run_spy.assert_called_once()
        self.assertTrue(run_spy.call_args.kwargs.get('force'))

    # -- test-selected --

    def test_test_selected_refuses_when_conflicted(self):
        job = seed.make_test_job(name='J', channels=[self.channel], status='COMPLETED')
        db.session.commit()
        with self._conflicted('Reactor is melting.'):
            resp, run_spy = self._post(
                f'/api/channel-tests/on-demand/{job.id}/test-selected',
                channel_ids=[self.channel.id])
        self.assertEqual(resp.status_code, 409)
        run_spy.assert_not_called()

    def test_test_selected_force_bypasses_the_refusal(self):
        job = seed.make_test_job(name='J', channels=[self.channel], status='COMPLETED')
        db.session.commit()
        with self._conflicted():
            resp, run_spy = self._post(
                f'/api/channel-tests/on-demand/{job.id}/test-selected',
                channel_ids=[self.channel.id], force=True)
        self.assertEqual(resp.status_code, 200)
        run_spy.assert_called_once()
        self.assertTrue(run_spy.call_args.kwargs.get('force'))

    # -- restart --

    def test_restart_refuses_when_conflicted(self):
        job = seed.make_test_job(name='J', channels=[self.channel], status='COMPLETED')
        db.session.commit()
        with self._conflicted('Reactor is melting.'):
            resp, run_spy = self._post(f'/api/channel-tests/on-demand/{job.id}/restart')
        self.assertEqual(resp.status_code, 409)
        run_spy.assert_not_called()

    def test_restart_force_bypasses_the_refusal(self):
        job = seed.make_test_job(name='J', channels=[self.channel], status='COMPLETED')
        db.session.commit()
        with self._conflicted():
            resp, run_spy = self._post(f'/api/channel-tests/on-demand/{job.id}/restart', force=True)
        self.assertEqual(resp.status_code, 200)
        run_spy.assert_called_once()
        self.assertTrue(run_spy.call_args.kwargs.get('force'))

    # -- shared helper --

    def test_shared_helper_not_duplicated_logic(self):
        """Patching the module-level helper must be enough to change every route's
        behavior - if a route had its own copy of the conflict logic, this would pass
        nothing for that route."""
        jobs = {
            'start': seed.make_test_job(name='S', channels=[self.channel], status='QUEUED'),
            'resume': seed.make_test_job(name='R', channels=[self.channel], status='CANCELLED'),
            'restart': seed.make_test_job(name='X', channels=[self.channel], status='COMPLETED'),
        }
        db.session.commit()
        with self._conflicted('injected') as helper:
            for action, job in jobs.items():
                resp, run_spy = self._post(f'/api/channel-tests/on-demand/{job.id}/{action}')
                self.assertEqual(resp.status_code, 409, f'{action} did not honor the helper')
        self.assertEqual(helper.call_count, len(jobs))


if __name__ == '__main__':
    unittest.main()
