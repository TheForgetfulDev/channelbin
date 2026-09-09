"""Tier 2 - Pre-record checks C: pre-recording health check scheduling + runner.

Guards DESIGN-prerecord-checks.md §3-7 (changelog/243, "Pre-record checks C"): a precheck_<recording_id> DateTrigger job fires channel_tester.run_pre_check() a
lead time before a recording's start_time, which - after an enablement check, a margin
guard, and a single-run-globally check - tests the recording's channel (or, for a
group-backed recording, whichever member record-start would pick right now) through the
normal single-channel test path, then logs PRE_CHECK_PASSED/FAILED/SKIPPED on the
recording.

Covers, in order:
  - SchedulingTests: schedule_recording registers precheck_<id>; unschedule_recording
    removes it (needs the real jobstore).
  - DisabledTests: disabled config (global, or a profile override) is a silent no-op -
    run_channel_test is never called and nothing is logged.
  - MarginGuardTests: not enough time before start_time skips observably
    (PRE_CHECK_SKIPPED event + JOB_SKIPPED alert), without ever calling run_channel_test.
  - BusyRetryTests: tester busy with another run re-registers one collapsing retry job
    when the retry would still clear the margin guard, else skips observably.
  - ChannelResolutionTests: group-backed recording resolves to the member record-start
    would pick right now, honoring the busy-member skip rule.
  - OutcomeTests: PASSED/FAILED/SKIPPED(CANCELLED)/SKIPPED(no slot) outcome handling,
    including ChannelTest.pre_check_recording_id provenance - run_channel_test itself is
    mocked here (its own pipeline is exercised by test_tester_preemption.py and
    test_prerecord_checks_b.py) so these tests isolate run_pre_check's own outcome logic.
  - ProfileOverrideTests: RecordingProfile.pre_check_enabled tri-state wins over the
    global flag in both directions.

No real ffmpeg, no network - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_prerecord_checks_c
"""
import copy
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, channel_tester  # noqa: E402
from app import scheduler as sched  # noqa: E402
from app.config import _DEFAULTS  # noqa: E402
from app.database import (  # noqa: E402
    Alert, ChannelTest, RecordingEvent, RecordingProfile,
    PRE_CHECK_PASSED, PRE_CHECK_FAILED, PRE_CHECK_SKIPPED,
)
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402


def _cfg(**pre_check_overrides):
    """A full config dict (channel_testing.pre_check enabled, tiny worst-case numbers so
    margin math is easy to reason about: worst_case = (1+0)*1 + 0*0 + 1 + 60 = 62s)."""
    cfg = copy.deepcopy(_DEFAULTS)
    ct = cfg['channel_testing']
    ct['connect_retries'] = 0
    ct['connect_timeout_seconds'] = 1
    ct['connect_retry_delay_seconds'] = 0
    ct['test_duration_seconds'] = 1
    ct['pre_check']['enabled'] = True
    ct['pre_check']['lead_minutes'] = 15
    ct['pre_check']['retry_minutes'] = 5
    ct['pre_check']['min_margin_seconds'] = 5
    ct['pre_check'].update(pre_check_overrides)
    return cfg


def _skipped_events(recording_id):
    return RecordingEvent.query.filter_by(recording_id=recording_id, event_type=PRE_CHECK_SKIPPED).all()


def _job_skipped_alerts(source):
    return Alert.query.filter_by(alert_type='JOB_SKIPPED', source=source).all()


class SchedulingTests(unittest.TestCase):
    """Needs the real jobstore (start_scheduler=True) to assert scheduler jobs."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.acct = seed.make_account()
        self.channel = seed.make_channel(self.acct, name='Sched Channel')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_schedule_recording_registers_precheck_job(self):
        future = datetime.utcnow() + timedelta(days=3650)
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.channel.id,
                                  start_time=future, stop_time=future + timedelta(hours=1))
        db.session.commit()

        sched.schedule_recording(self.t.app, rec.id, rec.start_time, rec.stop_time)

        self.assertIsNotNone(sched.get_scheduler().get_job(f'precheck_{rec.id}'))

    def test_unschedule_recording_removes_precheck_job(self):
        future = datetime.utcnow() + timedelta(days=3650)
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.channel.id,
                                  start_time=future, stop_time=future + timedelta(hours=1))
        db.session.commit()
        sched.schedule_recording(self.t.app, rec.id, rec.start_time, rec.stop_time)
        self.assertIsNotNone(sched.get_scheduler().get_job(f'precheck_{rec.id}'))

        sched.unschedule_recording(rec.id)

        self.assertIsNone(sched.get_scheduler().get_job(f'precheck_{rec.id}'))


class DisabledTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account()
        self.channel = seed.make_channel(self.acct, name='Disabled Channel')
        db.session.commit()

    def tearDown(self):
        with channel_tester._lock:
            channel_tester._state = channel_tester.RunState()
        self.t.cleanup()

    def test_globally_disabled_is_a_silent_noop(self):
        rec = seed.make_recording(
            status='SCHEDULED', channel_id=self.channel.id,
            start_time=datetime.utcnow() + timedelta(minutes=30),
            stop_time=datetime.utcnow() + timedelta(hours=1))
        db.session.commit()

        cfg = _cfg(enabled=False)
        with mock.patch('app.config.load_config', return_value=cfg), \
             mock.patch.object(channel_tester, 'run_channel_test') as spy:
            channel_tester.run_pre_check(self.t.app, rec.id)

        spy.assert_not_called()
        self.assertEqual(RecordingEvent.query.filter_by(recording_id=rec.id).count(), 0)
        self.assertEqual(Alert.query.count(), 0)

    def test_missing_or_non_scheduled_recording_is_a_noop(self):
        rec = seed.make_recording(status='COMPLETED', channel_id=self.channel.id)
        db.session.commit()

        cfg = _cfg()
        with mock.patch('app.config.load_config', return_value=cfg), \
             mock.patch.object(channel_tester, 'run_channel_test') as spy:
            channel_tester.run_pre_check(self.t.app, rec.id)
            channel_tester.run_pre_check(self.t.app, rec.id + 9999)  # doesn't exist

        spy.assert_not_called()


class MarginGuardTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account()
        self.channel = seed.make_channel(self.acct, name='Margin Channel')
        db.session.commit()

    def tearDown(self):
        with channel_tester._lock:
            channel_tester._state = channel_tester.RunState()
        self.t.cleanup()

    def test_margin_exhausted_skips_without_running(self):
        # worst_case+min_margin = 67s; 30s to start_time is not enough.
        rec = seed.make_recording(
            status='SCHEDULED', channel_id=self.channel.id,
            start_time=datetime.utcnow() + timedelta(seconds=30),
            stop_time=datetime.utcnow() + timedelta(hours=1))
        db.session.commit()

        cfg = _cfg()
        with mock.patch('app.config.load_config', return_value=cfg), \
             mock.patch.object(channel_tester, 'run_channel_test') as spy:
            channel_tester.run_pre_check(self.t.app, rec.id)

        spy.assert_not_called()
        events = _skipped_events(rec.id)
        self.assertEqual(len(events), 1)
        alerts = _job_skipped_alerts(f'precheck_{rec.id}')
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].recording_id, rec.id)


class BusyRetryTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.acct = seed.make_account()
        self.channel = seed.make_channel(self.acct, name='Busy Channel')
        db.session.commit()

    def tearDown(self):
        with channel_tester._lock:
            channel_tester._state = channel_tester.RunState()
        self.t.cleanup()

    def _make_busy(self):
        with channel_tester._lock:
            channel_tester._state = channel_tester.RunState(is_running=True)

    def test_busy_with_viable_retry_reregisters_one_job(self):
        rec = seed.make_recording(
            status='SCHEDULED', channel_id=self.channel.id,
            start_time=datetime.utcnow() + timedelta(seconds=300),
            stop_time=datetime.utcnow() + timedelta(hours=1))
        db.session.commit()
        self._make_busy()

        cfg = _cfg(retry_minutes=1)  # retry_at ~60s out; margin after that is ~240s >= 67s
        with mock.patch('app.config.load_config', return_value=cfg), \
             mock.patch.object(channel_tester, 'run_channel_test') as spy:
            channel_tester.run_pre_check(self.t.app, rec.id)

        spy.assert_not_called()
        self.assertIsNotNone(sched.get_scheduler().get_job(f'precheck_{rec.id}'))
        self.assertEqual(_skipped_events(rec.id), [], 'a viable retry is not a skip')

    def test_repeated_busy_retries_collapse_into_one_job(self):
        rec = seed.make_recording(
            status='SCHEDULED', channel_id=self.channel.id,
            start_time=datetime.utcnow() + timedelta(seconds=300),
            stop_time=datetime.utcnow() + timedelta(hours=1))
        db.session.commit()
        self._make_busy()
        cfg = _cfg(retry_minutes=1)

        with mock.patch('app.config.load_config', return_value=cfg), \
             mock.patch.object(channel_tester, 'run_channel_test'):
            for _ in range(3):
                channel_tester.run_pre_check(self.t.app, rec.id)

        matching = [j for j in sched.get_scheduler().get_jobs() if j.id == f'precheck_{rec.id}']
        self.assertEqual(len(matching), 1,
                         'replace_existing must collapse repeated deferrals into one pending retry')

    def test_busy_with_no_viable_retry_skips_observably(self):
        # Not enough runway left for even one retry_minutes-delayed attempt.
        rec = seed.make_recording(
            status='SCHEDULED', channel_id=self.channel.id,
            start_time=datetime.utcnow() + timedelta(seconds=100),
            stop_time=datetime.utcnow() + timedelta(hours=1))
        db.session.commit()
        self._make_busy()

        cfg = _cfg(retry_minutes=5)  # retry_at ~300s out, already past start_time
        with mock.patch('app.config.load_config', return_value=cfg), \
             mock.patch.object(channel_tester, 'run_channel_test') as spy:
            channel_tester.run_pre_check(self.t.app, rec.id)

        spy.assert_not_called()
        self.assertEqual(len(_skipped_events(rec.id)), 1)
        self.assertEqual(len(_job_skipped_alerts(f'precheck_{rec.id}')), 1)

    def test_zero_retry_minutes_skips_with_no_retry_job(self):
        rec = seed.make_recording(
            status='SCHEDULED', channel_id=self.channel.id,
            start_time=datetime.utcnow() + timedelta(seconds=300),
            stop_time=datetime.utcnow() + timedelta(hours=1))
        db.session.commit()
        self._make_busy()

        cfg = _cfg(retry_minutes=0)
        with mock.patch('app.config.load_config', return_value=cfg), \
             mock.patch.object(channel_tester, 'run_channel_test') as spy:
            channel_tester.run_pre_check(self.t.app, rec.id)

        spy.assert_not_called()
        self.assertIsNone(sched.get_scheduler().get_job(f'precheck_{rec.id}'))
        self.assertEqual(len(_skipped_events(rec.id)), 1)


class ChannelResolutionTests(unittest.TestCase):
    """Group-backed recordings resolve to whichever member record-start would pick right
    now, honoring the busy-member skip rule (app/recorder.py::start_recording mirrors
    this exact shape)."""

    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account()

    def tearDown(self):
        with channel_tester._lock:
            channel_tester._state = channel_tester.RunState()
        self.t.cleanup()

    def test_busy_member_is_skipped_for_the_free_alternative(self):
        busy_member = seed.make_channel(self.acct, name='Busy Member', health_score=90)
        free_member = seed.make_channel(self.acct, name='Free Member', health_score=50)
        group = seed.make_group(name='G', members=[busy_member, free_member])
        db.session.commit()
        # Another IN_PROGRESS recording occupies busy_member.
        seed.make_recording(status='IN_PROGRESS', channel_id=busy_member.id)
        rec = seed.make_recording(
            status='SCHEDULED', group_id=group.id,
            start_time=datetime.utcnow() + timedelta(seconds=300),
            stop_time=datetime.utcnow() + timedelta(hours=1))
        db.session.commit()

        cfg = _cfg()
        with mock.patch('app.config.load_config', return_value=cfg), \
             mock.patch.object(channel_tester, 'run_channel_test', return_value=None) as spy:
            channel_tester.run_pre_check(self.t.app, rec.id)

        spy.assert_called_once()
        called_channel_id = spy.call_args[0][1]
        self.assertEqual(called_channel_id, free_member.id,
                         'the higher-scoring member is busy, so the free member must be picked')


class OutcomeTests(unittest.TestCase):
    """run_channel_test is mocked so these tests isolate run_pre_check's own
    outcome-recording logic (its pipeline is exercised elsewhere)."""

    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account()
        self.channel = seed.make_channel(self.acct, name='Outcome Channel', health_score=80)
        db.session.commit()
        self.rec = seed.make_recording(
            status='SCHEDULED', channel_id=self.channel.id,
            start_time=datetime.utcnow() + timedelta(seconds=300),
            stop_time=datetime.utcnow() + timedelta(hours=1))
        db.session.commit()

    def tearDown(self):
        with channel_tester._lock:
            channel_tester._state = channel_tester.RunState()
        self.t.cleanup()

    def _run(self, test_id):
        cfg = _cfg()
        with mock.patch('app.config.load_config', return_value=cfg), \
             mock.patch.object(channel_tester, 'run_channel_test', return_value=test_id):
            channel_tester.run_pre_check(self.t.app, self.rec.id)

    def test_passed_run_sets_provenance_and_event(self):
        test = seed.make_channel_test(self.channel, all_null=False, status='COMPLETED')
        db.session.commit()

        self._run(test.id)

        db.session.expire_all()
        self.assertEqual(db.session.get(ChannelTest, test.id).pre_check_recording_id, self.rec.id)
        events = RecordingEvent.query.filter_by(recording_id=self.rec.id, event_type=PRE_CHECK_PASSED).all()
        self.assertEqual(len(events), 1)

    def test_failed_run_sets_provenance_and_event(self):
        test = seed.make_channel_test(self.channel, all_null=False, status='FAILED',
                                      error_detail='no data received')
        db.session.commit()

        self._run(test.id)

        db.session.expire_all()
        self.assertEqual(db.session.get(ChannelTest, test.id).pre_check_recording_id, self.rec.id)
        events = RecordingEvent.query.filter_by(recording_id=self.rec.id, event_type=PRE_CHECK_FAILED).all()
        self.assertEqual(len(events), 1)
        self.assertIn('no data received', events[0].detail or '')

    def test_cancelled_run_is_a_skip_not_a_failure(self):
        test = seed.make_channel_test(self.channel, all_null=False, status='CANCELLED',
                                      error_detail='Interrupted by recording start')
        db.session.commit()

        self._run(test.id)

        db.session.expire_all()
        self.assertEqual(db.session.get(ChannelTest, test.id).pre_check_recording_id, self.rec.id)
        self.assertEqual(len(_skipped_events(self.rec.id)), 1)
        self.assertEqual(
            RecordingEvent.query.filter_by(recording_id=self.rec.id, event_type=PRE_CHECK_FAILED).count(), 0)
        self.assertEqual(len(_job_skipped_alerts(f'precheck_{self.rec.id}')), 1)

    def test_no_slot_available_is_a_skip(self):
        self._run(None)

        self.assertEqual(len(_skipped_events(self.rec.id)), 1)
        self.assertEqual(len(_job_skipped_alerts(f'precheck_{self.rec.id}')), 1)


class ProfileOverrideTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account()
        self.channel = seed.make_channel(self.acct, name='Profile Channel')
        db.session.commit()

    def tearDown(self):
        with channel_tester._lock:
            channel_tester._state = channel_tester.RunState()
        self.t.cleanup()

    def _make_recording_with_profile(self, pre_check_enabled):
        profile = RecordingProfile(name='P', pre_check_enabled=pre_check_enabled)
        db.session.add(profile)
        db.session.flush()
        rec = seed.make_recording(
            status='SCHEDULED', channel_id=self.channel.id, profile_id=profile.id,
            start_time=datetime.utcnow() + timedelta(seconds=300),
            stop_time=datetime.utcnow() + timedelta(hours=1))
        db.session.commit()
        return rec

    def test_profile_enabled_true_overrides_global_disabled(self):
        rec = self._make_recording_with_profile(pre_check_enabled=True)
        cfg = _cfg(enabled=False)

        with mock.patch('app.config.load_config', return_value=cfg), \
             mock.patch.object(channel_tester, 'run_channel_test', return_value=None) as spy:
            channel_tester.run_pre_check(self.t.app, rec.id)

        spy.assert_called_once()

    def test_profile_enabled_false_overrides_global_enabled(self):
        rec = self._make_recording_with_profile(pre_check_enabled=False)
        cfg = _cfg(enabled=True)

        with mock.patch('app.config.load_config', return_value=cfg), \
             mock.patch.object(channel_tester, 'run_channel_test') as spy:
            channel_tester.run_pre_check(self.t.app, rec.id)

        spy.assert_not_called()
        self.assertEqual(RecordingEvent.query.filter_by(recording_id=rec.id).count(), 0)


if __name__ == '__main__':
    unittest.main()
