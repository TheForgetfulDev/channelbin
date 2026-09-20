"""Tier 2 - maintenance window for recurring health checks (dev/changelog/497).
A recurring OnDemandTestJob with recur_use_window=True has no APScheduler job of
its own; the dispatcher (app.check_window.dispatch_tick) starts the next due one, one at a
time, and window_close() hard-stops whatever is left at the window's end time and reports
any leftover work.

All config (display timezone, channel_testing.window.*) is supplied by patching
app.config.load_config, per CLAUDE.md's testing rules (make_test_app overrides are invisible
to a runtime load_config call, and every function under test re-imports load_config locally
for exactly this reason). No real ffmpeg/test runs: run_on_demand_test_job is always stubbed.
"""
import os
import sys
import threading
import unittest
import yaml
from datetime import datetime, timedelta, time, timezone
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app
from tests.support.config_sandbox import ConfigSandbox  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, check_window, channel_tester  # noqa: E402
from app.database import OnDemandTestJob, HealthCheckProfile, Alert  # noqa: E402


def _cfg(start='02:00', end='06:00', dispatch_interval_minutes=5, tz='America/New_York'):
    return {
        'display': {'timezone': tz, 'time_format': '12h'},
        'channel_testing': {
            'test_duration_seconds': 120,
            'wait_between_channels_seconds': 180,
            'window': {'start': start, 'end': end,
                      'dispatch_interval_minutes': dispatch_interval_minutes},
        },
        'notifications': {'routing': {}, 'base_url': ''},
    }


def _patch_cfg(**kw):
    return mock.patch('app.config.load_config', return_value=_cfg(**kw))


def _join_dispatch_threads():
    for t in threading.enumerate():
        if t.name.startswith('od-window-job-') or t.name == 'check-window-dispatch-kick':
            t.join(timeout=5)


# A Thursday (recur_day 5), on standard time, so no DST transition falls inside either
# pinned window. recur_day=0 jobs match any day regardless.
_WINDOW_DAY = '2026-01-15'
# The end of the default 02:00-06:00 window - the moment production's cron fires
# window_close() - and a moment inside it, for the dispatcher.
_WINDOW_CLOSE_LOCAL = f'{_WINDOW_DAY}T06:00:00'
_INSIDE_WINDOW_LOCAL = f'{_WINDOW_DAY}T03:00:00'


def _naive_utc(local_iso, tz='America/New_York'):
    """A wall-clock moment in `tz` as the naive UTC datetime the app stores."""
    return (datetime.fromisoformat(local_iso)
            .replace(tzinfo=ZoneInfo(tz))
            .astimezone(timezone.utc)
            .replace(tzinfo=None))


def _pin_clock(local_iso, tz='America/New_York'):
    """Pin check_window's view of "now" to a wall-clock moment in `tz`.

    window_close(), dispatch_tick() and due_jobs() each read datetime.utcnow() through the
    module's own datetime, so one patch pins all three. Without it these tests answered a
    different question depending on what time of night the suite ran: between 00:00 and the
    02:00 window start, the occurrence window_close() closes begins later tonight, so the
    alert raised by the first call predates occurrence_start_utc, the dedupe query misses it
    and the second call raises a duplicate (dev/changelog/1051, dev/docs/BUGS.md
    2026-09-19).

    A datetime subclass rather than a Mock: the module also calls datetime.combine(), which
    has to keep working.
    """
    pinned = _naive_utc(local_iso, tz)

    class _PinnedDatetime(datetime):
        @classmethod
        def utcnow(cls):
            return pinned

    return mock.patch('app.check_window.datetime', _PinnedDatetime)


class WindowBoundsTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_parses_configured_start_end(self):
        start, end = check_window.window_bounds({'window': {'start': '01:30', 'end': '05:45'}})
        self.assertEqual(start, time(1, 30))
        self.assertEqual(end, time(5, 45))

    def test_garbage_falls_back_to_defaults_and_never_raises(self):
        start, end = check_window.window_bounds({'window': {'start': 'nonsense', 'end': '06:00'}})
        self.assertEqual(start, time(2, 0))
        self.assertEqual(end, time(6, 0))

    def test_missing_window_key_falls_back_to_defaults(self):
        start, end = check_window.window_bounds({})
        self.assertEqual(start, time(2, 0))
        self.assertEqual(end, time(6, 0))


class OccurrenceContainingTests(unittest.TestCase):
    """America/New_York is UTC-5 (EST) outside DST, so a 02:00-06:00 local window is
    07:00-11:00 UTC on a non-DST date. occurrence_containing() resolves the display
    timezone via tz_utils (a runtime load_config() call), so every test here patches
    app.config.load_config rather than depending on this machine's real config.yaml."""

    def setUp(self):
        self.t = make_test_app()
        patcher = _patch_cfg()
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.t.cleanup()

    def test_normal_window_contains_moment_inside(self):
        ct_cfg = _cfg()['channel_testing']
        now = datetime(2026, 1, 15, 8, 0)  # 03:00 EST - inside 02:00-06:00
        occ = check_window.occurrence_containing(ct_cfg, now)
        self.assertIsNotNone(occ)
        start_utc, end_utc = occ
        self.assertEqual(start_utc, datetime(2026, 1, 15, 7, 0))
        self.assertEqual(end_utc, datetime(2026, 1, 15, 11, 0))

    def test_normal_window_excludes_moment_outside(self):
        ct_cfg = _cfg()['channel_testing']
        now = datetime(2026, 1, 15, 15, 0)  # 10:00 EST - well outside the window
        self.assertIsNone(check_window.occurrence_containing(ct_cfg, now))

    def test_midnight_crossing_window_tail_of_yesterday(self):
        """23:00-03:00: a moment at 01:00 local belongs to the occurrence that started
        the previous local day at 23:00."""
        ct_cfg = _cfg(start='23:00', end='03:00')['channel_testing']
        now = datetime(2026, 1, 16, 6, 0)  # 01:00 EST on Jan 16
        occ = check_window.occurrence_containing(ct_cfg, now)
        self.assertIsNotNone(occ)
        start_utc, end_utc = occ
        # Started 23:00 EST on Jan 15 (UTC 2026-01-16 04:00), ends 03:00 EST Jan 16 (UTC 08:00).
        self.assertEqual(start_utc, datetime(2026, 1, 16, 4, 0))
        self.assertEqual(end_utc, datetime(2026, 1, 16, 8, 0))

    def test_midnight_crossing_window_start_of_tonight(self):
        ct_cfg = _cfg(start='23:00', end='03:00')['channel_testing']
        now = datetime(2026, 1, 17, 4, 30)  # 23:30 EST on Jan 16 (just entered tonight's occurrence)
        occ = check_window.occurrence_containing(ct_cfg, now)
        self.assertIsNotNone(occ)
        start_utc, end_utc = occ
        self.assertEqual(start_utc, datetime(2026, 1, 17, 4, 0))
        self.assertEqual(end_utc, datetime(2026, 1, 17, 8, 0))

    def test_midnight_crossing_window_dead_zone(self):
        ct_cfg = _cfg(start='23:00', end='03:00')['channel_testing']
        now = datetime(2026, 1, 16, 20, 0)  # 15:00 EST - well outside 23:00-03:00
        self.assertIsNone(check_window.occurrence_containing(ct_cfg, now))

    def test_dst_transition_shifts_utc_offset_by_one_hour(self):
        """2026-03-08 is the US spring-forward date (America/New_York EST->EDT at 2 AM
        local). A fixed local wall-clock moment either side of the transition must convert
        to a UTC instant that differs by exactly one hour - proof this goes through
        zoneinfo rather than a fixed offset."""
        ct_cfg = _cfg()['channel_testing']
        # 04:00 local (inside the window on both sides, avoiding the nonexistent 02:00-03:00
        # local hour on the transition day itself).
        before = check_window.occurrence_containing(ct_cfg, datetime(2026, 3, 6, 9, 0))   # 04:00 EST
        after = check_window.occurrence_containing(ct_cfg, datetime(2026, 3, 10, 8, 0))    # 04:00 EDT
        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        self.assertEqual(before[0], datetime(2026, 3, 6, 7, 0))    # 02:00 EST = UTC 07:00
        self.assertEqual(after[0], datetime(2026, 3, 10, 6, 0))    # 02:00 EDT = UTC 06:00


class EstimateJobSecondsTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account()
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _job(self, n_channels, **kw):
        channels = [seed.make_channel(self.account, name=f'Ch{i}') for i in range(n_channels)]
        db.session.commit()
        job = seed.make_test_job(name='Estimate Job', channels=channels,
                                 recurring=True, recur_use_window=True, **kw)
        db.session.commit()
        return job

    def test_waits_fall_between_channels_not_after_the_last(self):
        job = self._job(3)
        ct_cfg = _cfg()['channel_testing']
        # n=3, duration=120, wait=180 -> 3*120 + 2*180 = 720
        self.assertEqual(check_window.estimate_job_seconds(job, ct_cfg), 720)

    def test_single_channel_has_no_wait(self):
        job = self._job(1)
        ct_cfg = _cfg()['channel_testing']
        self.assertEqual(check_window.estimate_job_seconds(job, ct_cfg), 120)

    def test_profile_override_changes_the_answer(self):
        profile = HealthCheckProfile(name='Fast', test_duration_seconds=10,
                                     wait_between_channels_seconds=5)
        db.session.add(profile)
        db.session.flush()
        job = self._job(3, profile_id=profile.id)
        ct_cfg = _cfg()['channel_testing']
        # 3*10 + 2*5 = 40, not the global 720
        self.assertEqual(check_window.estimate_job_seconds(job, ct_cfg), 40)

    def test_no_group_is_zero(self):
        job = OnDemandTestJob(name='No Group', status='SCHEDULED', recurring=True,
                              recur_use_window=True)
        db.session.add(job)
        db.session.commit()
        ct_cfg = _cfg()['channel_testing']
        self.assertEqual(check_window.estimate_job_seconds(job, ct_cfg), 0)


class DueJobsTests(unittest.TestCase):
    """due_jobs() resolves the occurrence's local day via tz_utils (a runtime
    load_config() call), so every test here patches app.config.load_config rather than
    depending on this machine's real config.yaml."""

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account)
        db.session.commit()
        patcher = _patch_cfg()
        patcher.start()
        self.addCleanup(patcher.stop)
        # 2026-01-15 is a Thursday -> recur_day 5 (1=Sun...7=Sat).
        self.occurrence_start = datetime(2026, 1, 15, 7, 0)

    def tearDown(self):
        self.t.cleanup()

    def _job(self, **kw):
        job = seed.make_test_job(name=kw.pop('name', 'Job'), channels=[self.channel],
                                 status=kw.pop('status', 'SCHEDULED'),
                                 recurring=True, recur_use_window=True, **kw)
        db.session.commit()
        return job

    def test_recur_day_zero_is_every_day(self):
        job = self._job(recur_day=0)
        self.assertIn(job.id, [j.id for j in check_window.due_jobs(self.occurrence_start)])

    def test_matching_specific_day_is_eligible(self):
        job = self._job(recur_day=5)  # Thursday
        self.assertIn(job.id, [j.id for j in check_window.due_jobs(self.occurrence_start)])

    def test_non_matching_specific_day_is_excluded(self):
        job = self._job(recur_day=2)  # Monday
        self.assertNotIn(job.id, [j.id for j in check_window.due_jobs(self.occurrence_start)])

    def test_midnight_crossing_occurrence_belongs_to_the_day_it_opened(self):
        """23:00-03:00 starting Thursday night (recur_day=5) - the occurrence start is
        still Thursday even though most of the window falls on Friday's calendar date."""
        job = self._job(recur_day=5)
        occurrence_start = datetime(2026, 1, 16, 4, 0)  # 23:00 EST Jan 15 (Thursday) in UTC
        self.assertIn(job.id, [j.id for j in check_window.due_jobs(occurrence_start)])

    def test_already_ran_this_occurrence_is_excluded(self):
        job = self._job(completed_at=self.occurrence_start + timedelta(minutes=5))
        self.assertNotIn(job.id, [j.id for j in check_window.due_jobs(self.occurrence_start)])

    def test_completed_before_this_occurrence_is_still_eligible(self):
        job = self._job(completed_at=self.occurrence_start - timedelta(days=1))
        self.assertIn(job.id, [j.id for j in check_window.due_jobs(self.occurrence_start)])

    def test_paused_is_excluded(self):
        job = self._job(recur_paused=True)
        self.assertNotIn(job.id, [j.id for j in check_window.due_jobs(self.occurrence_start)])

    def test_window_skip_until_in_the_future_is_excluded(self):
        job = self._job(window_skip_until=datetime.utcnow() + timedelta(hours=1))
        self.assertNotIn(job.id, [j.id for j in check_window.due_jobs(self.occurrence_start)])

    def test_window_skip_until_in_the_past_is_eligible(self):
        job = self._job(window_skip_until=datetime.utcnow() - timedelta(hours=1))
        self.assertIn(job.id, [j.id for j in check_window.due_jobs(self.occurrence_start)])

    def test_non_window_recurring_job_is_excluded(self):
        job = seed.make_test_job(name='Exact time', channels=[self.channel], status='SCHEDULED',
                                 recurring=True, recur_use_window=False, recur_day=0,
                                 recur_hour=3, recur_minute=0)
        db.session.commit()
        self.assertNotIn(job.id, [j.id for j in check_window.due_jobs(self.occurrence_start)])

    def test_ordering_is_system_first_then_least_recently_fully_run(self):
        old = self._job(name='Ran long ago', last_full_run_at=datetime(2020, 1, 1))
        never_run = self._job(name='Never run')
        recent = self._job(name='Ran recently', last_full_run_at=datetime.utcnow())
        system = self._job(name='TV Guide Channels', is_system=True,
                           last_full_run_at=datetime.utcnow())
        order = [j.id for j in check_window.due_jobs(self.occurrence_start)]
        self.assertEqual(order.index(system.id), 0, 'is_system must sort first')
        # Among non-system jobs: NULL last_full_run_at first, then oldest first.
        self.assertLess(order.index(never_run.id), order.index(old.id))
        self.assertLess(order.index(old.id), order.index(recent.id))

    def test_truncated_check_sorts_ahead_of_a_completed_one(self):
        """A hard-stopped run leaves last_full_run_at stale (never set) while completed_at
        is fresh - it must still sort ahead of a check that finished cleanly tonight."""
        truncated = self._job(name='Truncated', last_full_run_at=datetime(2020, 1, 1),
                              completed_at=datetime.utcnow())
        completed = self._job(name='Completed cleanly', last_full_run_at=datetime.utcnow())
        # Both eligible for a LATER occurrence (completed_at from tonight excludes them
        # from tonight's own due_jobs, so check ordering via the eligibility-agnostic
        # cohort helper instead).
        cohort = check_window._window_eligible_for_day(self.occurrence_start, ('SCHEDULED',))
        order = [j.id for j in cohort]
        self.assertLess(order.index(truncated.id), order.index(completed.id))


class DispatchTickTests(unittest.TestCase):
    """Pinned inside the default 02:00-06:00 window. These tests used to reshape the
    configured window instead - a 00:00-23:59 one to guarantee "now" was inside it, a
    02:00-02:01 one to guarantee it was not - which left the second genuinely failing for
    the 60 seconds a day it was wrong about."""

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account)
        db.session.commit()
        clock = _pin_clock(_INSIDE_WINDOW_LOCAL)
        clock.start()
        self.addCleanup(clock.stop)

    def tearDown(self):
        _join_dispatch_threads()
        with channel_tester._lock:
            channel_tester._state.clear()
        self.t.cleanup()

    def test_noop_when_tester_is_running(self):
        seed.make_test_job(name='Due', channels=[self.channel], status='SCHEDULED',
                           recurring=True, recur_use_window=True, recur_day=0)
        db.session.commit()
        with channel_tester._lock:
            channel_tester._reset_run_state()
        spy = mock.Mock()
        with _patch_cfg(), mock.patch('app.channel_tester.run_on_demand_test_job', spy):
            check_window.dispatch_tick(self.t.app)
        _join_dispatch_threads()
        spy.assert_not_called()

    def test_noop_outside_the_window(self):
        seed.make_test_job(name='Due', channels=[self.channel], status='SCHEDULED',
                           recurring=True, recur_use_window=True, recur_day=0)
        db.session.commit()
        spy = mock.Mock()
        # 04:00-05:00 does not contain the pinned 03:00.
        with _patch_cfg(start='04:00', end='05:00'), \
             mock.patch('app.channel_tester.run_on_demand_test_job', spy):
            check_window.dispatch_tick(self.t.app)
        _join_dispatch_threads()
        spy.assert_not_called()

    def test_starts_exactly_one_due_job_inside_the_window(self):
        job = seed.make_test_job(name='Due', channels=[self.channel], status='SCHEDULED',
                                 recurring=True, recur_use_window=True, recur_day=0)
        db.session.commit()
        spy = mock.Mock()
        with _patch_cfg(), \
             mock.patch('app.channel_tester.run_on_demand_test_job', spy):
            check_window.dispatch_tick(self.t.app)
            _join_dispatch_threads()
        spy.assert_called_once()
        self.assertEqual(spy.call_args.args[1], job.id)


class WindowCloseTests(unittest.TestCase):
    """Every test here is pinned to the window's end, which is when production's cron
    actually calls window_close(). Run against the real clock they answer a different
    question every hour of the night - see _pin_clock()."""

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account)
        db.session.commit()
        clock = _pin_clock(_WINDOW_CLOSE_LOCAL)
        clock.start()
        self.addCleanup(clock.stop)
        self.now_utc = _naive_utc(_WINDOW_CLOSE_LOCAL)

    def tearDown(self):
        with channel_tester._lock:
            channel_tester._state.clear()
        self.t.cleanup()

    def _mark_running(self, job_id, run_kind='job', tested=5, total=10):
        with channel_tester._lock:
            channel_tester._reset_run_state(job_id=job_id, run_kind=run_kind)
            channel_tester._state.completed_channels = tested
            channel_tester._state.total_channels = total

    def test_silent_when_window_drained_cleanly(self):
        ct_cfg = _cfg()['channel_testing']
        with _patch_cfg():
            occurrence_start, _end = check_window._occurrence_bounds_for_closing(
                ct_cfg, self.now_utc)
        # Ran inside tonight's own occurrence, so due_jobs() must exclude it.
        seed.make_test_job(name='Done', channels=[self.channel], status='SCHEDULED',
                          recurring=True, recur_use_window=True, recur_day=0,
                          completed_at=occurrence_start + timedelta(minutes=1),
                          last_full_run_at=occurrence_start + timedelta(minutes=1))
        db.session.commit()
        with _patch_cfg(), mock.patch('app.alerts.create_alert') as alert_spy:
            check_window.window_close(self.t.app)
        alert_spy.assert_not_called()

    def test_stops_a_running_window_job_and_reports_it(self):
        job = seed.make_test_job(name='Sports channels', channels=[self.channel],
                                 status='RUNNING', recurring=True, recur_use_window=True,
                                 recur_day=0)
        db.session.commit()
        self._mark_running(job.id, tested=22, total=41)
        with _patch_cfg(), \
             mock.patch('app.channel_tester.request_stop') as stop_spy, \
             mock.patch('app.alerts.create_alert') as alert_spy:
            check_window.window_close(self.t.app)
        stop_spy.assert_called_once()
        alert_spy.assert_called_once()
        self.assertEqual(alert_spy.call_args.args[0], 'HEALTH_CHECK_WINDOW')
        body = alert_spy.call_args.kwargs['body']
        self.assertIn('Sports channels', body)
        self.assertIn('22 of 41', body)

    def test_leaves_a_manual_run_alone(self):
        """A one-off/manual run holding the slot must never be stopped by window_close -
        only a recur_use_window job."""
        job = seed.make_test_job(name='Manual test', channels=[self.channel],
                                 status='RUNNING', recurring=False)
        db.session.commit()
        self._mark_running(job.id)
        with _patch_cfg(), mock.patch('app.channel_tester.request_stop') as stop_spy:
            check_window.window_close(self.t.app)
        stop_spy.assert_not_called()

    def test_leaves_an_exact_time_run_alone(self):
        job = seed.make_test_job(name='Exact time job', channels=[self.channel],
                                 status='RUNNING', recurring=True, recur_use_window=False,
                                 recur_day=0, recur_hour=3, recur_minute=0)
        db.session.commit()
        self._mark_running(job.id)
        with _patch_cfg(), mock.patch('app.channel_tester.request_stop') as stop_spy:
            check_window.window_close(self.t.app)
        stop_spy.assert_not_called()

    def test_reports_never_started_jobs(self):
        seed.make_test_job(name='Locals HD', channels=[self.channel],
                          status='SCHEDULED', recurring=True,
                          recur_use_window=True, recur_day=0)
        db.session.commit()
        with _patch_cfg(), mock.patch('app.alerts.create_alert') as alert_spy:
            check_window.window_close(self.t.app)
        alert_spy.assert_called_once()
        body = alert_spy.call_args.kwargs['body']
        self.assertIn('Locals HD', body)
        self.assertIn('Never started', body)

    def test_a_clean_window_clears_an_earlier_windows_warning(self):
        """dev/changelog/930: window_close returns early when nothing was stopped and
        nothing was left unstarted. Returning was all it did, so the previous window's
        warning stayed open indefinitely - describing leftover work that had since been
        done. A window that drains clean is exactly the evidence that clears it."""
        stale = Alert(alert_type='HEALTH_CHECK_WINDOW', severity='WARN',
                      title='Maintenance window closed with work left over',
                      source='check_window')
        db.session.add(stale)
        db.session.commit()
        stale_id = stale.id

        with _patch_cfg():
            check_window.window_close(self.t.app)

        db.session.expire_all()
        self.assertIsNotNone(
            db.session.get(Alert, stale_id).dismissed_at,
            'a window that stopped nothing and left nothing unstarted must clear the '
            'standing leftover-work warning')

    def test_does_not_double_emit_for_one_occurrence(self):
        """The dedupe query asks whether an alert exists that was created since the
        occurrence started, so it only holds when the first alert lands inside the
        occurrence being closed. Unpinned, this failed every night between 00:00 and the
        02:00 start (dev/docs/BUGS.md 2026-09-19)."""
        seed.make_test_job(name='Locals HD', channels=[self.channel], status='SCHEDULED',
                           recurring=True, recur_use_window=True, recur_day=0)
        db.session.commit()
        with _patch_cfg():
            check_window.window_close(self.t.app)
            raised = Alert.query.filter_by(alert_type='HEALTH_CHECK_WINDOW').all()
            self.assertEqual(len(raised), 1)
            # create_alert stamps created_at from the real clock, which the pin does not
            # reach. Restamp it to a moment inside the pinned occurrence so the dedupe
            # query is asked the question production asks it, rather than passing because
            # a pinned date in the past precedes every real timestamp.
            raised[0].created_at = self.now_utc - timedelta(minutes=5)
            db.session.commit()

            with mock.patch('app.alerts.create_alert') as alert_spy:
                check_window.window_close(self.t.app)
            alert_spy.assert_not_called()


class ScheduleOnDemandJobTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_window_job_registers_no_apscheduler_job(self):
        from app.scheduler import schedule_on_demand_job, get_scheduler
        job = seed.make_test_job(name='Window job', channels=[self.channel],
                                 status='SCHEDULED', recurring=True, recur_use_window=True,
                                 recur_day=0)
        db.session.commit()
        with _patch_cfg():
            aps_job_id, next_run = schedule_on_demand_job(job)
        self.assertIsNone(aps_job_id)
        self.assertIsNotNone(next_run)
        self.assertIsNone(get_scheduler().get_job(f'od_job_{job.id}'))

    def test_exact_time_job_still_registers_a_cron_job(self):
        from app.scheduler import schedule_on_demand_job, get_scheduler
        job = seed.make_test_job(name='Exact time job', channels=[self.channel],
                                 status='SCHEDULED', recurring=True, recur_use_window=False,
                                 recur_day=0, recur_hour=3, recur_minute=0)
        db.session.commit()
        aps_job_id, next_run = schedule_on_demand_job(job)
        self.assertIsNotNone(aps_job_id)
        self.assertIsNotNone(get_scheduler().get_job(aps_job_id))


class StartupReconciliationTests(unittest.TestCase):
    """resume_in_progress_recordings()'s on-demand-job reconciliation loop, specifically
    the recur_use_window branch. Guards dev/docs/BUGS.md 2026-08-07 - a job whose
    recur_use_window flag is flipped on outside the normal reschedule route (a direct DB
    edit was how this was actually found, live, during this feature's own smoke test) can
    leave its old CronTrigger alive in the jobstore, firing independently of the dispatcher
    and running the check twice."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_stray_apscheduler_job_is_cancelled_for_a_window_job(self):
        from app.scheduler import _add_job, _on_demand_job_trigger, get_scheduler, \
            resume_in_progress_recordings

        job = seed.make_test_job(name='Flipped to window', channels=[self.channel],
                                 status='SCHEDULED', recurring=True, recur_use_window=True,
                                 recur_day=0, recur_hour=3, recur_minute=45)
        db.session.commit()
        aps_job_id = f'od_job_{job.id}'
        # Simulate the stray CronTrigger a direct DB flip of recur_use_window would leave
        # behind - registered exactly as schedule_on_demand_job's exact-time branch would.
        _add_job(func=_on_demand_job_trigger, trigger='cron', hour=3, minute=45,
                 args=[job.id], id=aps_job_id, replace_existing=True)
        job.scheduler_job_id = aps_job_id
        db.session.commit()
        self.assertIsNotNone(get_scheduler().get_job(aps_job_id))

        # No config patch here - resume_in_progress_recordings() touches many unrelated
        # config keys (recording.post_process, etc.) that _cfg()'s minimal shape doesn't
        # carry, and this test's assertions don't depend on the specific window times.
        resume_in_progress_recordings(self.t.app)

        self.assertIsNone(get_scheduler().get_job(aps_job_id),
                          'the stray CronTrigger must be removed, or the check runs twice')
        db.session.expire_all()
        refreshed = db.session.get(OnDemandTestJob, job.id)
        self.assertIsNone(refreshed.scheduler_job_id)

    def test_orphaned_apscheduler_job_with_no_matching_db_row_is_removed(self):
        """dev/docs/BUGS.md 2026-08-10: a jobstore od_job_<id> row can outlive its
        OnDemandTestJob DB row entirely (found live as od_job_1, from a since-fixed test
        isolation bug that wrote real jobstore rows into this exact production dvr.db).
        Left alone, its recurring CronTrigger fires forever, logging "job N not found" on
        every occurrence. The startup sweep must remove it."""
        from app.scheduler import _add_job, _on_demand_job_trigger, get_scheduler, \
            resume_in_progress_recordings

        nonexistent_job_id = 999999
        self.assertIsNone(db.session.get(OnDemandTestJob, nonexistent_job_id))
        aps_job_id = f'od_job_{nonexistent_job_id}'
        _add_job(func=_on_demand_job_trigger, trigger='cron', hour=6, minute=0,
                 args=[nonexistent_job_id], id=aps_job_id, replace_existing=True)
        self.assertIsNotNone(get_scheduler().get_job(aps_job_id))

        resume_in_progress_recordings(self.t.app)

        self.assertIsNone(get_scheduler().get_job(aps_job_id),
                          'an orphaned od_job_* entry with no matching DB row must be swept')


class OnDemandRouteWindowModeTests(unittest.TestCase):
    """create_on_demand_job/reschedule_on_demand_job's use_window handling - item 3's
    actual gap, since schedule_on_demand_job's own window-registration behavior
    (ScheduleOnDemandJobTests above) already shipped in item 2. schedule_on_demand_job is
    stubbed throughout so these tests aren't also exercising APScheduler/tz config - that's
    already covered above and in test_scheduler_*."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_create_with_use_window_sets_flag_and_leaves_hour_minute_unset(self):
        future = datetime.utcnow() + timedelta(days=1)
        with mock.patch('app.scheduler.schedule_on_demand_job', return_value=(None, future)):
            resp = self.client.post('/api/channel-tests/on-demand', json={
                'name': 'Window check', 'channel_ids': [self.channel.id],
                'action': 'schedule', 'recurring': True, 'recur_day': 0, 'use_window': True,
            })
        self.assertEqual(resp.status_code, 200)
        job = OnDemandTestJob.query.filter_by(name='Window check').one()
        self.assertTrue(job.recur_use_window)
        self.assertIsNone(job.recur_hour)
        self.assertIsNone(job.recur_minute)

    def test_create_with_use_window_does_not_require_recur_time(self):
        """The exact-time branch 400s with no recur_time; window mode must not, since
        schedule-fields.js omits it entirely in that mode."""
        future = datetime.utcnow() + timedelta(days=1)
        with mock.patch('app.scheduler.schedule_on_demand_job', return_value=(None, future)):
            resp = self.client.post('/api/channel-tests/on-demand', json={
                'name': 'No time needed', 'channel_ids': [self.channel.id],
                'action': 'schedule', 'recurring': True, 'recur_day': 0, 'use_window': True,
                'recur_time': None,
            })
        self.assertEqual(resp.status_code, 200)

    def test_reschedule_into_window_mode_preserves_existing_hour_minute(self):
        job = seed.make_test_job(name='Toggle me', channels=[self.channel], status='SCHEDULED',
                                 recurring=True, recur_use_window=False, recur_day=2,
                                 recur_hour=3, recur_minute=30)
        db.session.commit()
        future = datetime.utcnow() + timedelta(days=1)
        with mock.patch('app.scheduler.schedule_on_demand_job', return_value=(None, future)):
            resp = self.client.post(f'/api/channel-tests/on-demand/{job.id}/reschedule', json={
                'recurring': True, 'recur_day': 3, 'use_window': True,
            })
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        refreshed = db.session.get(OnDemandTestJob, job.id)
        self.assertTrue(refreshed.recur_use_window)
        self.assertEqual(refreshed.recur_day, 3)
        self.assertEqual((refreshed.recur_hour, refreshed.recur_minute), (3, 30),
                         'toggling into window mode must not null the prior exact time')

    def test_reschedule_out_of_window_mode_sets_the_new_time(self):
        job = seed.make_test_job(name='Toggle back', channels=[self.channel], status='SCHEDULED',
                                 recurring=True, recur_use_window=True, recur_day=0,
                                 recur_hour=3, recur_minute=30)
        db.session.commit()
        future = datetime.utcnow() + timedelta(days=1)
        with mock.patch('app.scheduler.schedule_on_demand_job', return_value=('aps-x', future)):
            resp = self.client.post(f'/api/channel-tests/on-demand/{job.id}/reschedule', json={
                'recurring': True, 'recur_day': 1, 'recur_time': '04:15', 'use_window': False,
            })
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        refreshed = db.session.get(OnDemandTestJob, job.id)
        self.assertFalse(refreshed.recur_use_window)
        self.assertEqual((refreshed.recur_hour, refreshed.recur_minute), (4, 15))

    def test_schedule_ctx_surfaces_use_window(self):
        from app.routes.channel_groups import _schedule_ctx
        job = seed.make_test_job(name='Ctx check', channels=[self.channel], status='SCHEDULED',
                                 recurring=True, recur_use_window=True, recur_day=0)
        db.session.commit()
        ctx = _schedule_ctx(job, {'window': {'start': '02:00', 'end': '06:00'}})
        self.assertTrue(ctx['use_window'])

    def test_schedule_ctx_use_window_false_for_exact_time_job(self):
        from app.routes.channel_groups import _schedule_ctx
        job = seed.make_test_job(name='Exact ctx', channels=[self.channel], status='SCHEDULED',
                                 recurring=True, recur_use_window=False, recur_day=0,
                                 recur_hour=3, recur_minute=0)
        db.session.commit()
        ctx = _schedule_ctx(job, {'window': {'start': '02:00', 'end': '06:00'}})
        self.assertFalse(ctx['use_window'])


class SkipNextWindowJobTests(unittest.TestCase):
    """skip_next_on_demand_job's window branch (dev/docs/BUGS.md 2026-08-07): a window job
    has no scheduler_job_id (by design - it is dispatcher-owned), so the precondition that
    used to require one must not reject it, and the skip must go through
    window_skip_until (which app.check_window.due_jobs() already checks) rather than
    scheduler.skip_next_run(), which has nothing to modify for these jobs."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account)

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _patch_all_cfg(self):
        return (mock.patch('app.config.load_config', return_value=_cfg()),
                mock.patch('app.routes.channel_tests.load_config', return_value=_cfg()))

    def test_skip_next_sets_window_skip_until_and_advances_scheduled_start(self):
        # 2026-01-20 is outside DST for America/New_York (EST, UTC-5): 02:00 local is
        # 07:00 UTC and 06:00 local (window end) is 11:00 UTC.
        occurrence_start = datetime(2026, 1, 20, 7, 0)
        job = seed.make_test_job(name='Skip me', channels=[self.channel], status='SCHEDULED',
                                 recurring=True, recur_use_window=True, recur_day=0,
                                 scheduler_job_id=None, scheduled_start_time=occurrence_start)
        db.session.commit()
        p1, p2 = self._patch_all_cfg()
        with p1, p2:
            resp = self.client.post(f'/api/channel-tests/on-demand/{job.id}/skip-next')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        db.session.expire_all()
        refreshed = db.session.get(OnDemandTestJob, job.id)
        self.assertEqual(refreshed.window_skip_until, datetime(2026, 1, 20, 11, 0))
        self.assertEqual(refreshed.scheduled_start_time, datetime(2026, 1, 21, 7, 0))

    def test_skip_next_rejects_a_paused_window_job(self):
        job = seed.make_test_job(name='Paused window', channels=[self.channel], status='SCHEDULED',
                                 recurring=True, recur_use_window=True, recur_paused=True,
                                 recur_day=0, scheduler_job_id=None,
                                 scheduled_start_time=datetime(2026, 1, 20, 7, 0))
        db.session.commit()
        resp = self.client.post(f'/api/channel-tests/on-demand/{job.id}/skip-next')
        self.assertEqual(resp.status_code, 409)


class JobsPageWindowVisibilityTests(unittest.TestCase):
    """_build_job_list() (app/routes/jobs.py) - a window job has no APScheduler row (by
    design), so the od_job_(\\d+) regex loop that lists every other recurring on-demand
    job never matches it. Without its own loop it would silently vanish from /jobs.

    _build_job_list() touches config keys well outside channel_testing (recording.
    post_script, for one), so - unlike the rest of this file - these tests do not patch
    load_config(): the real config.yaml + _DEFAULTS merge (CLAUDE.md's config-cache rule)
    already carries every key it needs, and none of the assertions below depend on the
    exact configured window bounds."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_window_job_appears_with_skip_but_no_run_or_cancel(self):
        from app.routes.jobs import _build_job_list

        job = seed.make_test_job(name='Nightly window check', channels=[self.channel],
                                 status='SCHEDULED', recurring=True, recur_use_window=True,
                                 recur_day=0, scheduler_job_id=None)
        db.session.commit()

        items = _build_job_list()

        matches = [i for i in items if i['id'] == f'od_window_{job.id}']
        self.assertEqual(len(matches), 1, 'window job must appear exactly once')
        item = matches[0]
        self.assertEqual(item['type'], 'recurring')
        self.assertEqual(item['display_name'], 'Health check: Nightly window check')
        self.assertEqual(item['edit_url'], f'/channels/health-checks/{job.id}')
        self.assertEqual(item['skip_url'], f'/api/channel-tests/on-demand/{job.id}/skip-next')
        self.assertNotIn('run_url', item)
        self.assertNotIn('cancel_url', item)
        self.assertIn('maintenance window', item['schedule_description'])

    def test_paused_window_job_is_not_listed(self):
        from app.routes.jobs import _build_job_list

        seed.make_test_job(name='Paused window check', channels=[self.channel],
                           status='SCHEDULED', recurring=True, recur_use_window=True,
                           recur_paused=True, recur_day=0, scheduler_job_id=None)
        db.session.commit()

        items = _build_job_list()

        self.assertFalse(any('Paused window check' in i['display_name'] for i in items))


class WindowSettingsValidationTests(ConfigSandbox):
    """api_settings_field()'s channel_testing.window.start|end validation (app/routes/
    settings.py). ConfigSandbox because make_test_app() does not sandbox config.yaml, so
    these writes would otherwise land on the real repo-root file."""

    def setUp(self):
        super().setUp()
        # start_scheduler=True: a start/end change calls reschedule_window_jobs()
        # (app/scheduler.py), which re-registers hc_window_close - it needs a real
        # scheduler instance to register against.
        self.t = make_test_app(start_scheduler=True)
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client

    def tearDown(self):
        self.t.cleanup()

    def _post_field(self, path, value):
        return self.client.post('/api/settings/field', json={'path': path, 'value': value})

    def test_start_equal_to_end_is_rejected(self):
        resp = self._post_field('channel_testing.window.start', '06:00')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())

    def test_malformed_time_is_rejected(self):
        resp = self._post_field('channel_testing.window.start', 'not-a-time')
        self.assertEqual(resp.status_code, 400)

    def test_valid_start_change_is_accepted_and_does_not_restart(self):
        resp = self._post_field('channel_testing.window.start', '01:00')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()['restart_required'])

    def test_end_before_start_is_valid_and_crosses_midnight(self):
        """end < start is a legitimate midnight-crossing window (app/check_window.py) -
        only start == end (zero-length) is rejected."""
        resp = self._post_field('channel_testing.window.end', '01:00')
        self.assertEqual(resp.status_code, 200)

    def test_dispatch_interval_minutes_clamps_to_at_least_one(self):
        resp = self._post_field('channel_testing.window.dispatch_interval_minutes', 0)
        self.assertEqual(resp.status_code, 200)
        with open(self._cfg_path) as f:
            saved = yaml.safe_load(f)
        self.assertEqual(saved['channel_testing']['window']['dispatch_interval_minutes'], 1)

    def test_dispatch_interval_minutes_change_requires_restart(self):
        resp = self._post_field('channel_testing.window.dispatch_interval_minutes', 10)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['restart_required'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
