"""A manual sync can stand in for the account's next scheduled sync.

Sync now used to leave the automatic schedule where it was, so a manual sync could be
followed minutes later by a scheduled one doing the same work again. The Sync now dialog now
asks, the route validates the answer (falling back to `sync.manual_sync_restarts_schedule`
when the caller did not ask), and a manual sync that SUCCEEDED moves the account's interval
to one period after it, spaced from the other accounts like every sync job, and drops any
deferred retry still queued for the old slot (dev/changelog/1134).

No real syncs: `sync_account` / `run_manual_sync` are patched, and any thread a route spawns
is joined before asserting. The scheduler is make_test_app's in-memory one.

  python3 -m unittest tests.test_manual_sync_restarts_schedule
"""
import json
import os
import re
import sys
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import accounts as accounts_mod  # noqa: E402
from app import scheduler as sched  # noqa: E402
from app.database import Account  # noqa: E402


def _join_sync_threads():
    for t in threading.enumerate():
        if t.name.startswith('account-sync-'):
            t.join(timeout=5)


def _cfg(**sync):
    base = {'sync_interval_hours': 12, 'skip_sync_if_recording_active': True,
            'skip_sync_if_recording_within_minutes': 5}
    base.update(sync)
    return {'sync': base, 'notifications': {'routing': {}, 'base_url': ''}}


class DefaultSettingTests(unittest.TestCase):

    def test_the_default_is_on(self):
        self.assertTrue(accounts_mod.manual_sync_restarts_schedule({}))

    def test_the_setting_turns_it_off(self):
        self.assertFalse(accounts_mod.manual_sync_restarts_schedule(
            {'sync': {'manual_sync_restarts_schedule': False}}))


class _SchedulerCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.account = seed.make_account(name='Manual', sync_interval_hours=12,
                                         sync_enabled=True)
        db.session.commit()
        self.account_id = self.account.id
        # Scheduled soon, so "did it move" is unambiguous.
        self.account.next_sync_at = datetime.utcnow() + timedelta(minutes=40)
        db.session.commit()
        sched.schedule_account_sync(self.t.app, self.account_id)

    def tearDown(self):
        _join_sync_threads()
        self.t.cleanup()

    def _interval_next(self, account_id=None):
        job = sched.get_scheduler().get_job(f'account_sync_{account_id or self.account_id}')
        return sched.to_naive_utc(job.next_run_time)

    def _mark_synced(self, when=None):
        acc = db.session.get(Account, self.account_id)
        acc.last_sync_at = when or datetime.utcnow()
        db.session.commit()

    def _queue_retry(self):
        """A deferred retry hours out, so the live test scheduler never fires it."""
        sched._add_job(func=sched._account_sync_job, trigger='date',
                       run_date=datetime.utcnow() + timedelta(hours=3),
                       id=sched.sync_retry_job_id(self.account_id), replace_existing=True,
                       kwargs={'account_id': self.account_id, 'retry': True})

    def _retry_pending(self):
        return sched.get_scheduler().get_job(sched.sync_retry_job_id(self.account_id)) is not None

    def _restart(self, ran_at):
        with mock.patch('app.config.load_config', return_value=_cfg()):
            return sched.restart_schedule_after_manual_sync(self.account_id, ran_at)


class RestartAfterManualSyncTests(_SchedulerCase):

    def test_a_successful_manual_sync_moves_the_schedule_one_interval_after_it(self):
        before = self._interval_next()
        ran_at = datetime.utcnow()
        self._mark_synced()
        self.assertTrue(self._restart(ran_at))
        after = self._interval_next()
        self.assertNotEqual(after, before)
        self.assertGreaterEqual(after, ran_at + timedelta(hours=12))
        self.assertLess(after, ran_at + timedelta(hours=12, minutes=30))
        db.session.expire_all()
        self.assertEqual(db.session.get(Account, self.account_id).next_sync_at, after,
                         'the displayed next sync must be the job that will fire')

    def test_a_failed_manual_sync_leaves_the_schedule_alone(self):
        """No success commit since the manual sync started: the scheduled sync is still
        wanted."""
        before = self._interval_next()
        ran_at = datetime.utcnow()
        self._mark_synced(ran_at - timedelta(hours=5))
        self.assertFalse(self._restart(ran_at))
        self.assertEqual(self._interval_next(), before)

    def test_a_never_synced_account_that_failed_is_left_alone(self):
        before = self._interval_next()
        self.assertFalse(self._restart(datetime.utcnow()))
        self.assertEqual(self._interval_next(), before)

    def test_a_pending_retry_is_dropped_with_the_old_slot(self):
        self._queue_retry()
        ran_at = datetime.utcnow()
        self._mark_synced()
        self._restart(ran_at)
        self.assertFalse(self._retry_pending(),
                         'the retry would run a second full sync right after the manual one')

    def test_a_failed_manual_sync_keeps_the_pending_retry(self):
        self._queue_retry()
        self._restart(datetime.utcnow())
        self.assertTrue(self._retry_pending())

    def test_sync_disabled_moves_nothing(self):
        acc = db.session.get(Account, self.account_id)
        acc.sync_enabled = False
        db.session.commit()
        ran_at = datetime.utcnow()
        self._mark_synced()
        self.assertFalse(self._restart(ran_at))

    def test_the_new_slot_keeps_its_distance_from_another_accounts_sync(self):
        """dev/changelog/31: two accounts' syncs never land together."""
        ran_at = datetime.utcnow()
        other = seed.make_account(name='Other', sync_interval_hours=12, sync_enabled=True)
        other.next_sync_at = ran_at + timedelta(hours=12)
        db.session.commit()
        sched.schedule_account_sync(self.t.app, other.id)
        other_at = self._interval_next(other.id)
        self._mark_synced()
        self._restart(ran_at)
        gap = abs((self._interval_next() - other_at).total_seconds())
        self.assertGreaterEqual(gap, 5 * 60)


class RunManualSyncTests(_SchedulerCase):
    """The thread body: the schedule moves after the sync, only when asked."""

    def _run(self, restart_schedule, succeed=True):
        def fake_sync(app, account_id, **kwargs):
            if succeed:
                with app.app_context():
                    acc = db.session.get(Account, account_id)
                    acc.last_sync_at = datetime.utcnow()
                    db.session.commit()

        with mock.patch('app.accounts.sync_account', side_effect=fake_sync) as spy, \
             mock.patch('app.config.load_config', return_value=_cfg()):
            accounts_mod.run_manual_sync(self.t.app, self.account_id,
                                         restart_schedule=restart_schedule)
        db.session.expire_all()
        return spy

    def test_asked_and_succeeded_moves_the_schedule(self):
        before = self._interval_next()
        self._run(True)
        self.assertGreater(self._interval_next(), before + timedelta(hours=11))

    def test_not_asked_leaves_the_schedule(self):
        before = self._interval_next()
        self._run(False)
        self.assertEqual(self._interval_next(), before)

    def test_asked_but_failed_leaves_the_schedule(self):
        before = self._interval_next()
        self._run(True, succeed=False)
        self.assertEqual(self._interval_next(), before)

    def test_the_sync_itself_is_forced_past_admission(self):
        spy = self._run(False)
        self.assertTrue(spy.call_args.kwargs['force_admission'])


class SyncRouteTests(unittest.TestCase):
    """POST /api/accounts/<id>/sync decides the flag server-side."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.account = seed.make_account(name='Routed', sync_enabled=True)
        db.session.commit()
        self.account_id = self.account.id

    def tearDown(self):
        _join_sync_threads()
        self.t.cleanup()

    def _post(self, body, default=True):
        spy = mock.Mock()
        with mock.patch('app.accounts.run_manual_sync', spy), \
             mock.patch('app.accounts.sync_conflicts', return_value=[]), \
             mock.patch('app.routes.accounts.manual_sync_restarts_schedule',
                        return_value=default):
            resp = self.t.client.post(f'/api/accounts/{self.account_id}/sync', json=body)
            _join_sync_threads()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        spy.assert_called_once()
        return spy.call_args.kwargs['restart_schedule'], resp.get_json()['message']

    def test_the_users_answer_wins_over_the_default(self):
        self.assertTrue(self._post({'restart_schedule': True}, default=False)[0])
        self.assertFalse(self._post({'restart_schedule': False}, default=True)[0])

    def test_a_caller_that_did_not_ask_gets_the_setting(self):
        self.assertTrue(self._post({}, default=True)[0])
        self.assertFalse(self._post({}, default=False)[0])

    def test_junk_reads_as_no(self):
        self.assertFalse(self._post({'restart_schedule': 'maybe'}, default=True)[0])

    def test_sync_disabled_never_restarts(self):
        acc = db.session.get(Account, self.account_id)
        acc.sync_enabled = False
        db.session.commit()
        self.assertFalse(self._post({'restart_schedule': True})[0])

    def test_the_message_says_the_scheduled_sync_is_replaced(self):
        _, on = self._post({'restart_schedule': True})
        _, off = self._post({'restart_schedule': False})
        self.assertIn('replaces the next scheduled sync', on)
        self.assertNotIn('replaces', off)


class JobsRunNowTests(unittest.TestCase):
    """The Jobs page's Run now on an account's sync job is a manual sync too."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.account = seed.make_account(name='Jobbed', sync_enabled=True)
        self.account.next_sync_at = datetime.utcnow() + timedelta(hours=2)
        db.session.commit()
        self.account_id = self.account.id
        sched.schedule_account_sync(self.t.app, self.account_id)

    def tearDown(self):
        _join_sync_threads()
        self.t.cleanup()

    def _run_now(self, default):
        spy = mock.Mock()
        with mock.patch('app.accounts.run_manual_sync', spy), \
             mock.patch('app.accounts.sync_conflicts', return_value=[]), \
             mock.patch('app.accounts.manual_sync_restarts_schedule', return_value=default):
            resp = self.t.client.post(f'/api/jobs/account_sync_{self.account_id}/run-now',
                                      json={})
            _join_sync_threads()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return spy.call_args.kwargs['restart_schedule']

    def test_it_follows_the_setting(self):
        self.assertTrue(self._run_now(True))
        self.assertFalse(self._run_now(False))


class PagesCarryThePromptTests(unittest.TestCase):
    """Both Accounts pages hand the dialog the same reading of the account."""

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Paged', sync_interval_hours=6,
                                         sync_enabled=True)
        db.session.commit()
        self.account_id = self.account.id
        self.next_at = datetime(2030, 1, 2, 3, 4, 5)

    def tearDown(self):
        self.t.cleanup()

    def _get(self, url):
        with mock.patch.object(sched, 'next_sync_attempts',
                               return_value={self.account_id: self.next_at}):
            resp = self.t.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _expected(self):
        return {'auto': True, 'next_at': self.next_at.isoformat(), 'interval_hours': 6,
                'restart_default': True}

    def test_the_list_row_carries_it(self):
        import html as html_mod
        page = self._get('/accounts')
        m = re.search(r'data-sync-prompt="([^"]*)"', page)
        self.assertIsNotNone(m)
        self.assertEqual(json.loads(html_mod.unescape(m.group(1))), self._expected())

    def test_the_account_page_carries_it(self):
        page = self._get(f'/accounts/{self.account_id}')
        m = re.search(r'syncPrompt: (\{.*?\}),\n', page)
        self.assertIsNotNone(m)
        self.assertEqual(json.loads(m.group(1)), self._expected())


if __name__ == '__main__':
    unittest.main()
