"""A scheduled job blocked by a recording is deferred to the first usable gap, not dropped.

Guards dev/docs/BUGS.md 2026-09-12 @ 06:24:35 PM ET. A scheduled account sync that hit either
recording guard in `scheduler.py::_account_sync_job` returned early, so the occurrence was
discarded and the next attempt was a whole `sync_interval_hours` away. On 2026-09-10 one
30-minute recording overlapped all four accounts' staggered sync slots, all four were dropped,
and the next attempt was 24h later - 48h between syncs on a 24h interval. Meanwhile every
"next sync" on the dashboard, accounts list, account page and channel page read from
`Account.next_sync_at`, which is written only when a sync SUCCEEDS, so all four showed a time
that had already passed.

The scheduled health-check job had the identical shape and is covered here too, because both
now go through the one set of slot-finding helpers.

No network and no real scheduler work beyond the in-memory jobstore `make_test_app` provides
(CLAUDE.md §Testing).

Run standalone:
  python3 -m unittest tests.test_sync_catchup_deferral
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import accounts as accounts_mod  # noqa: E402
from app import scheduler as sched  # noqa: E402
from app.database import Alert  # noqa: E402


def _sync_cfg(**over):
    cfg = {
        'skip_sync_if_recording_active': True,
        'skip_sync_if_recording_within_minutes': 5,
        'sync_interval_hours': 24,
    }
    cfg.update(over)
    return cfg


def _full_cfg(sync=None):
    return {'sync': sync or _sync_cfg(), 'notifications': {'routing': {}, 'base_url': ''}}


class FirstFreeSlotTests(unittest.TestCase):
    """The slot arithmetic on its own - no app, no scheduler."""

    def setUp(self):
        self.now = datetime(2026, 9, 12, 12, 0, 0)

    def test_now_is_returned_when_nothing_is_in_the_way(self):
        self.assertEqual(sched.first_free_slot(600, [], now=self.now), self.now)

    def test_a_gap_shorter_than_the_job_is_not_a_slot(self):
        """The defect the old 5-minute lookahead had: a 4-minute hole is not room for a
        4-minute sync plus the guard window around the next recording."""
        windows = [
            (self.now, self.now + timedelta(minutes=30)),
            (self.now + timedelta(minutes=34), self.now + timedelta(minutes=90)),
        ]
        slot = sched.first_free_slot(600, windows, now=self.now)
        self.assertEqual(slot, self.now + timedelta(minutes=90))

    def test_the_same_gap_is_a_slot_for_a_shorter_job(self):
        windows = [
            (self.now, self.now + timedelta(minutes=30)),
            (self.now + timedelta(minutes=34), self.now + timedelta(minutes=90)),
        ]
        slot = sched.first_free_slot(120, windows, now=self.now)
        self.assertEqual(slot, self.now + timedelta(minutes=30))

    def test_overlapping_windows_merge(self):
        windows = [
            (self.now, self.now + timedelta(minutes=30)),
            (self.now + timedelta(minutes=20), self.now + timedelta(minutes=50)),
        ]
        self.assertEqual(sched.first_free_slot(600, windows, now=self.now),
                         self.now + timedelta(minutes=50))

    def test_nothing_within_the_horizon_is_none_not_a_bad_guess(self):
        windows = [(self.now, self.now + timedelta(days=5))]
        self.assertIsNone(sched.first_free_slot(600, windows, now=self.now))

    def test_an_unmeasured_job_is_not_treated_as_instantaneous(self):
        """A zero-length window makes every instant look free, which is the whole bug."""
        self.assertEqual(sched.occurrence_seconds(None), float(sched.DEFAULT_OCCURRENCE_SECONDS))
        self.assertEqual(sched.occurrence_seconds(0), float(sched.DEFAULT_OCCURRENCE_SECONDS))
        self.assertGreater(sched.occurrence_seconds(120), 120)


class RecordingWindowTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.account = seed.make_account(name='Windows')
        self.channel = seed.make_channel(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_a_scheduled_recording_blocks_from_before_its_start(self):
        now = datetime.utcnow()
        start = now + timedelta(hours=2)
        seed.make_recording(status='SCHEDULED', channel_id=self.channel.id, name='Later',
                            start_time=start, stop_time=start + timedelta(hours=1))
        db.session.commit()
        windows = sched.recording_windows(lead_minutes=5, now=now)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0][0], start - timedelta(minutes=5))

    def test_an_overrunning_recording_still_blocks_the_present(self):
        """stop_time already past, still IN_PROGRESS - the window must not be empty."""
        now = datetime.utcnow()
        seed.make_recording(status='IN_PROGRESS', channel_id=self.channel.id, name='Overrun',
                            start_time=now - timedelta(hours=3),
                            stop_time=now - timedelta(minutes=10))
        db.session.commit()
        windows = sched.recording_windows(now=now)
        self.assertEqual(windows, [(now, now)])

    def test_the_active_guard_being_off_removes_its_windows(self):
        now = datetime.utcnow()
        seed.make_recording(status='IN_PROGRESS', channel_id=self.channel.id, name='Live',
                            start_time=now - timedelta(hours=1),
                            stop_time=now + timedelta(hours=1))
        db.session.commit()
        self.assertEqual(sched.recording_windows(include_active=False, now=now), [])


class SyncIsDeferredNotDroppedTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.account = seed.make_account(name='Deferred Account')
        self.channel = seed.make_channel(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _run_job(self, sync=None):
        spy = mock.Mock()
        with mock.patch('app.channel_tester.is_running', return_value=False), \
             mock.patch('app.accounts.sync_account', spy), \
             mock.patch('app.config.load_config', return_value=_full_cfg(sync)):
            sched._account_sync_job(self.account.id)
        return spy

    def _retry_job(self):
        return sched.get_scheduler().get_job(sched.sync_retry_job_id(self.account.id))

    def test_an_active_recording_queues_a_retry_after_it_ends(self):
        now = datetime.utcnow()
        stop = now + timedelta(minutes=40)
        seed.make_recording(status='IN_PROGRESS', channel_id=self.channel.id, name='Live',
                            start_time=now - timedelta(minutes=10), stop_time=stop)
        db.session.commit()

        spy = self._run_job()
        spy.assert_not_called()

        job = self._retry_job()
        self.assertIsNotNone(job, 'the blocked sync was dropped instead of deferred')
        self.assertGreaterEqual(sched.to_naive_utc(job.next_run_time), stop)

    def test_an_upcoming_recording_queues_a_retry_after_it_ends(self):
        now = datetime.utcnow()
        start = now + timedelta(minutes=2)
        stop = start + timedelta(minutes=30)
        seed.make_recording(status='SCHEDULED', channel_id=self.channel.id, name='Soon',
                            start_time=start, stop_time=stop)
        db.session.commit()

        self._run_job().assert_not_called()
        job = self._retry_job()
        self.assertIsNotNone(job, 'the blocked sync was dropped instead of deferred')
        self.assertGreaterEqual(sched.to_naive_utc(job.next_run_time), stop)

    def test_the_regular_interval_job_is_not_moved(self):
        """The retry is a separate one-shot. Nudging the interval trigger would permanently
        drift the account's schedule (APScheduler 3.x recomputes from next_run_time)."""
        sched.schedule_account_sync(self.t.app, self.account.id)
        before = sched.get_scheduler().get_job(f'account_sync_{self.account.id}').next_run_time

        now = datetime.utcnow()
        seed.make_recording(status='IN_PROGRESS', channel_id=self.channel.id, name='Live',
                            start_time=now - timedelta(minutes=10),
                            stop_time=now + timedelta(minutes=40))
        db.session.commit()
        self._run_job()

        after = sched.get_scheduler().get_job(f'account_sync_{self.account.id}').next_run_time
        self.assertEqual(before, after)

    def test_the_skip_is_recorded_on_the_account_with_the_retry_time(self):
        from app.database import AccountSyncLog

        now = datetime.utcnow()
        seed.make_recording(status='IN_PROGRESS', channel_id=self.channel.id, name='Live',
                            start_time=now - timedelta(minutes=10),
                            stop_time=now + timedelta(minutes=40))
        db.session.commit()
        self._run_job()

        db.session.expire_all()
        row = AccountSyncLog.query.filter_by(account_id=self.account.id,
                                             status='SKIPPED').one()
        self.assertIn('deferred', row.error_message)
        self.assertNotIn('next regular time', row.error_message)

    def test_a_recording_wall_with_no_gap_says_so_instead_of_queueing(self):
        now = datetime.utcnow()
        seed.make_recording(status='IN_PROGRESS', channel_id=self.channel.id, name='Endless',
                            start_time=now - timedelta(hours=1),
                            stop_time=now + timedelta(days=4))
        db.session.commit()
        self._run_job()

        self.assertIsNone(self._retry_job())
        from app.database import AccountSyncLog
        row = AccountSyncLog.query.filter_by(account_id=self.account.id,
                                             status='SKIPPED').one()
        self.assertIn('No gap long enough', row.error_message)


class OverdueAccountsAreReleasedOneAtATimeTests(unittest.TestCase):
    """Admission refuses sync-while-sync, so releasing four blocked accounts into the same
    gap produces one sync and three refusals. Their slots must not overlap."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.accounts = [seed.make_account(name=f'Account {i}') for i in range(3)]
        self.channel = seed.make_channel(self.accounts[0])
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_three_blocked_accounts_get_three_distinct_slots(self):
        now = datetime.utcnow()
        seed.make_recording(status='IN_PROGRESS', channel_id=self.channel.id, name='Live',
                            start_time=now - timedelta(minutes=10),
                            stop_time=now + timedelta(minutes=30))
        db.session.commit()

        spy = mock.Mock()
        with mock.patch('app.channel_tester.is_running', return_value=False), \
             mock.patch('app.accounts.sync_account', spy), \
             mock.patch('app.config.load_config', return_value=_full_cfg()):
            for acc in self.accounts:
                sched._account_sync_job(acc.id)

        times = sorted(sched.pending_sync_retries().values())
        self.assertEqual(len(times), 3, 'every blocked account should own a pending retry')
        for earlier, later in zip(times, times[1:]):
            gap = (later - earlier).total_seconds()
            self.assertGreaterEqual(
                gap, sched.DEFAULT_OCCURRENCE_SECONDS,
                'two catch-up syncs were released into the same window')


class NextSyncDisplayTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.account = seed.make_account(name='Display')
        self.channel = seed.make_channel(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_a_pending_retry_replaces_a_stale_stored_time(self):
        """The reported defect: next_sync_at is written only on a successful sync, so after
        a skip the page said "overdue" while the real attempt was hours away."""
        self.account.next_sync_at = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()

        retry_at = datetime.utcnow() + timedelta(hours=1)
        with mock.patch.object(sched, 'next_sync_attempts',
                               return_value={self.account.id: retry_at}):
            shown = accounts_mod.next_sync_map([self.account])

        self.assertEqual(shown[self.account.id], retry_at)

    def test_the_real_deferral_is_what_the_map_reports(self):
        """End to end through the real jobstore, no patching: defer a sync past a recording
        and the displayed next attempt is the retry, not the stale column."""
        self.account.next_sync_at = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()
        now = datetime.utcnow()
        stop = now + timedelta(minutes=40)
        seed.make_recording(status='IN_PROGRESS', channel_id=self.channel.id, name='Live',
                            start_time=now - timedelta(minutes=5), stop_time=stop)
        db.session.commit()

        with mock.patch('app.channel_tester.is_running', return_value=False), \
             mock.patch('app.accounts.sync_account', mock.Mock()), \
             mock.patch('app.config.load_config', return_value=_full_cfg()):
            sched._account_sync_job(self.account.id)

        shown = accounts_mod.next_sync_map([self.account])[self.account.id]
        self.assertIsNotNone(shown)
        self.assertGreaterEqual(shown, stop)

    def test_the_stored_column_is_the_fallback_with_no_scheduler(self):
        stored = datetime.utcnow() + timedelta(hours=4)
        self.account.next_sync_at = stored
        db.session.commit()
        with mock.patch.object(sched, 'next_sync_attempts', return_value={}):
            shown = accounts_mod.next_sync_map([self.account])
        self.assertEqual(shown[self.account.id], stored)

    def test_sync_disabled_has_no_next_attempt(self):
        self.account.sync_enabled = False
        self.account.next_sync_at = datetime.utcnow() + timedelta(hours=1)
        db.session.commit()
        self.assertIsNone(accounts_mod.next_sync_map([self.account])[self.account.id])

    def test_the_dashboard_renders_the_deferred_time(self):
        self.account.next_sync_at = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()
        retry_at = datetime.utcnow() + timedelta(hours=3)
        with mock.patch.object(sched, 'next_sync_attempts',
                               return_value={self.account.id: retry_at}):
            html = self.t.client.get('/').get_data(as_text=True)
        self.assertIn('Next sync', html)
        self.assertNotIn('data-nextsync=""', html)


class OverdueAlertTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.account = seed.make_account(name='Stale Account')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _open_alerts(self):
        return Alert.query.filter(Alert.alert_type == 'SYNC_ACCOUNT_OVERDUE',
                                  Alert.dismissed_at.is_(None)).all()

    def test_one_interval_late_does_not_alert(self):
        self.account.last_sync_at = datetime.utcnow() - timedelta(hours=25)
        db.session.commit()
        accounts_mod.update_overdue_alert(self.account.id, _sync_cfg())
        self.assertEqual(self._open_alerts(), [])

    def test_a_whole_extra_interval_late_alerts(self):
        self.account.last_sync_at = datetime.utcnow() - timedelta(hours=49)
        db.session.commit()
        accounts_mod.update_overdue_alert(self.account.id, _sync_cfg())
        self.assertEqual(len(self._open_alerts()), 1)

    def test_the_alert_clears_itself(self):
        self.account.last_sync_at = datetime.utcnow() - timedelta(hours=49)
        db.session.commit()
        accounts_mod.update_overdue_alert(self.account.id, _sync_cfg())
        self.assertEqual(len(self._open_alerts()), 1)

        self.account.last_sync_at = datetime.utcnow()
        db.session.commit()
        accounts_mod.update_overdue_alert(self.account.id, _sync_cfg())
        self.assertEqual(self._open_alerts(), [])

    def test_a_never_synced_account_is_not_overdue(self):
        self.account.last_sync_at = None
        db.session.commit()
        accounts_mod.update_overdue_alert(self.account.id, _sync_cfg())
        self.assertEqual(self._open_alerts(), [])

    def test_an_account_with_sync_off_is_not_overdue(self):
        self.account.sync_enabled = False
        self.account.last_sync_at = datetime.utcnow() - timedelta(days=30)
        db.session.commit()
        accounts_mod.update_overdue_alert(self.account.id, _sync_cfg())
        self.assertEqual(self._open_alerts(), [])


class HealthCheckRunIsDeferredTests(unittest.TestCase):
    """The scheduled tester had the identical skip-and-drop shape, so it consumes the same
    slot helpers rather than a second implementation."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.account = seed.make_account(name='HC Account')
        self.channel = seed.make_channel(self.account, in_guide=True)
        self.job = seed.make_test_job(name='Nightly', channels=[self.channel],
                                      status='SCHEDULED')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_a_recording_defers_the_run_instead_of_dropping_it(self):
        now = datetime.utcnow()
        stop = now + timedelta(minutes=45)
        seed.make_recording(status='IN_PROGRESS', channel_id=self.channel.id, name='Live',
                            start_time=now - timedelta(minutes=5), stop_time=stop)
        db.session.commit()

        ct_cfg = {'skip_if_recording_active': True, 'skip_if_recording_within_minutes': 10}
        retry_at = sched.defer_health_check_past_recording(self.job.id, ct_cfg, 'a recording')

        self.assertIsNotNone(retry_at)
        self.assertGreaterEqual(retry_at, stop)
        self.assertIsNotNone(
            sched.get_scheduler().get_job(sched.health_check_retry_job_id(self.job.id)))

    def test_cancelling_the_schedule_removes_the_pending_retry(self):
        """Teardown releases everything the create path acquired - a DateTrigger left behind
        would start a run for a job that has been cancelled or paused."""
        now = datetime.utcnow()
        seed.make_recording(status='IN_PROGRESS', channel_id=self.channel.id, name='Live',
                            start_time=now - timedelta(minutes=5),
                            stop_time=now + timedelta(minutes=45))
        db.session.commit()
        ct_cfg = {'skip_if_recording_active': True, 'skip_if_recording_within_minutes': 10}
        sched.defer_health_check_past_recording(self.job.id, ct_cfg, 'a recording')

        sched.cancel_on_demand_job_schedule(self.job)
        self.assertIsNone(
            sched.get_scheduler().get_job(sched.health_check_retry_job_id(self.job.id)))


if __name__ == '__main__':
    unittest.main()
