"""The Readiness check's "Sync N accounts now" button, and what a changed sync interval does.

dev/docs/BUGS.md 2026-11-05 @ 06:50:39 AM (the button) and 06:50:40 AM (the interval). The
button posted once per account, a sync refuses to overlap another, so the first account synced,
the second answered an error and the rest never ran. Saving a shorter sync interval put every
account past it at once while leaving their next run where the old interval had booked it, so
the check reported them overdue for hours, under a setting whose badge wrongly asked for a
restart.

Invariants:
  (a) A queue runs one account per step, pauses between them, and ends with no job left.
  (b) It never overlaps another sync: refused by admission, it holds and looks again, keeping
      every account it still owes, even when started with "Sync anyway".
  (c) "Sync anyway" passes recordings and health checks. Without it, they hold the queue and
      it says why.
  (d) A failed account does not stop the rest; a deleted or already caught-up one is skipped.
  (e) Stopping drops what is waiting and does not outlive the account syncing now.
  (f) The Readiness action is one request carrying every account.
  (g) A changed interval moves the next run to one new interval after the last sync, at once
      if that has passed, and spaces the accounts. An unchanged interval moves nothing.
  (h) The sync interval raises no restart, on the field save or the raw editor.

Real admission, real scheduler and jobstore on the test's temp database; `_do_sync` is the
stand-in, so no provider is reached.

  python3 -m unittest tests.test_sync_queue
"""
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import admission, config as cfgmod, db, readiness, sync_queue  # noqa: E402
from app import scheduler as sched  # noqa: E402
from app.database import Account, REC_STATUS_IN_PROGRESS  # noqa: E402


def _job():
    return sched.get_scheduler().get_job(sync_queue.JOB_ID)


class _QueueCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(start_scheduler=True, csrf=False)
        self.t.sandbox_config({'sync': {'sync_interval_hours': 12,
                                        'skip_sync_if_recording_active': True,
                                        'skip_sync_if_recording_within_minutes': 5}})
        self.ids = []
        for name in ('One', 'Two', 'Three'):
            self.ids.append(seed.make_account(name=name, sync_enabled=True).id)
        db.session.commit()
        self.queued_at = datetime.utcnow()
        self.synced = []
        patcher = mock.patch('app.accounts._do_sync',
                             side_effect=lambda account_id, *a, **k: self.synced.append(account_id))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        sched.remove_job_if_exists(sync_queue.JOB_ID)
        self.t.cleanup()

    def _step(self, pending=None, override=False):
        # The scheduler removes a one-shot as it fires it.
        sched.remove_job_if_exists(sync_queue.JOB_ID)
        sync_queue.run_step(pending if pending is not None else list(self.ids), len(self.ids),
                            self.queued_at, override)
        db.session.remove()


class OneAtATimeTests(_QueueCase):

    def test_a_step_syncs_one_account_and_keeps_the_rest_for_after_the_pause(self):
        self._step()
        self.assertEqual(self.synced, [self.ids[0]])
        job = _job()
        self.assertEqual(job.kwargs['pending'], self.ids[1:])
        wait = (sched.to_naive_utc(job.next_run_time) - datetime.utcnow()).total_seconds()
        self.assertGreater(wait, sync_queue.PAUSE_SECONDS - 10)
        self.assertLessEqual(wait, sync_queue.PAUSE_SECONDS)

    def test_the_last_account_leaves_nothing_behind(self):
        self._step(pending=[self.ids[2]])
        self.assertEqual(self.synced, [self.ids[2]])
        self.assertIsNone(_job())
        self.assertIsNone(sync_queue.status())

    def test_every_account_gets_its_turn(self):
        pending = list(self.ids)
        while pending:
            self._step(pending=pending)
            job = _job()
            pending = job.kwargs['pending'] if job else []
        self.assertEqual(self.synced, self.ids)

    def test_a_queue_started_for_real_runs_its_first_account_and_arms_the_next(self):
        started, message = sync_queue.start(self.ids)
        self.assertTrue(started, message)
        self.assertIn('3 accounts', message)
        deadline = time.time() + 15
        while time.time() < deadline and not self.synced:
            time.sleep(0.05)
        self.assertEqual(self.synced[:1], [self.ids[0]])
        while time.time() < deadline:
            job = _job()
            if job is not None and job.kwargs['pending'] == self.ids[1:]:
                wait = sched.to_naive_utc(job.next_run_time) - datetime.utcnow()
                if wait.total_seconds() <= sync_queue.PAUSE_SECONDS:
                    break
            time.sleep(0.05)
        self.assertEqual(self.synced, [self.ids[0]])
        self.assertEqual(sync_queue.status()['done'], 1)


class YieldingTests(_QueueCase):

    def _hold_ticket(self, kind):
        ticket = admission.try_start(kind, 'held by the test', force=True)
        self.addCleanup(admission.release, ticket)
        return ticket

    def test_another_sync_holds_the_queue_and_it_keeps_every_account(self):
        self._hold_ticket(admission.KIND_SYNC)
        self._step()
        self.assertEqual(self.synced, [])
        self.assertEqual(_job().kwargs['pending'], self.ids)
        self.assertIn('already running', sync_queue.status()['waiting'])

    def test_sync_anyway_still_never_overlaps_another_sync(self):
        self._hold_ticket(admission.KIND_SYNC)
        self._step(override=True)
        self.assertEqual(self.synced, [])
        self.assertEqual(_job().kwargs['pending'], self.ids)

    def test_a_held_queue_goes_on_when_the_other_sync_ends(self):
        ticket = self._hold_ticket(admission.KIND_SYNC)
        self._step()
        admission.release(ticket)
        self._step(pending=_job().kwargs['pending'])
        self.assertEqual(self.synced, [self.ids[0]])
        self.assertIsNone(sync_queue.status()['waiting'])

    def test_a_recording_holds_the_queue_and_it_says_which(self):
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='Big Game')
        db.session.commit()
        self._step()
        self.assertEqual(self.synced, [])
        self.assertEqual(_job().kwargs['pending'], self.ids)
        self.assertIn('Big Game', sync_queue.status()['waiting'])

    def test_sync_anyway_goes_past_a_recording(self):
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='Big Game')
        db.session.commit()
        self._step(override=True)
        self.assertEqual(self.synced, [self.ids[0]])

    def test_a_health_check_run_holds_the_queue(self):
        self._hold_ticket(admission.KIND_TESTER)
        self._step()
        self.assertEqual(self.synced, [])
        self.assertEqual(_job().kwargs['pending'], self.ids)

    def test_sync_anyway_goes_past_a_health_check_run(self):
        self._hold_ticket(admission.KIND_TESTER)
        self._step(override=True)
        self.assertEqual(self.synced, [self.ids[0]])


class SkippingAndFailingTests(_QueueCase):

    def test_a_failed_account_does_not_stop_the_rest(self):
        with mock.patch('app.accounts.sync_account', side_effect=RuntimeError('provider down')):
            self._step()
        self.assertEqual(_job().kwargs['pending'], self.ids[1:])
        self.assertIsNone(sync_queue.status()['current'])

    def test_a_deleted_account_is_skipped(self):
        doomed = db.session.get(Account, self.ids[0])
        db.session.delete(doomed)
        db.session.commit()
        self._step()
        self.assertEqual(self.synced, [self.ids[1]])
        self.assertEqual(_job().kwargs['pending'], self.ids[2:])

    def test_an_account_that_synced_since_the_click_is_not_synced_again(self):
        db.session.get(Account, self.ids[0]).last_sync_at = datetime.utcnow()
        db.session.commit()
        self._step()
        self.assertEqual(self.synced, [self.ids[1]])

    def test_nothing_left_to_do_ends_the_queue(self):
        doomed = db.session.get(Account, self.ids[0])
        db.session.delete(doomed)
        db.session.commit()
        self._step(pending=[self.ids[0]])
        self.assertEqual(self.synced, [])
        self.assertIsNone(sync_queue.status())

    def test_the_schedule_setting_reaches_a_queued_sync(self):
        with mock.patch('app.scheduler.restart_schedule_after_manual_sync') as restart:
            self._step(pending=[self.ids[0]])
        restart.assert_called_once()

    def test_an_account_with_automatic_sync_off_has_no_schedule_to_restart(self):
        db.session.get(Account, self.ids[0]).sync_enabled = False
        db.session.commit()
        with mock.patch('app.scheduler.restart_schedule_after_manual_sync') as restart:
            self._step(pending=[self.ids[0]])
        restart.assert_not_called()
        self.assertEqual(self.synced, [self.ids[0]])


class StoppingTests(_QueueCase):

    def test_stopping_between_accounts_drops_the_rest(self):
        self._step()
        self.assertIn('normal schedule', sync_queue.stop())
        self.assertIsNone(_job())
        self.assertIsNone(sync_queue.status())

    def test_stopping_during_a_sync_does_not_re_arm_after_it(self):
        def _stop_mid_sync(account_id, *a, **k):
            self.synced.append(account_id)
            self.assertIn('finishes', sync_queue.stop())

        with mock.patch('app.accounts._do_sync', side_effect=_stop_mid_sync):
            self._step()
        self.assertEqual(self.synced, [self.ids[0]])
        self.assertIsNone(_job())
        self.assertIsNone(sync_queue.status())


class RouteTests(_QueueCase):

    def test_starting_answers_with_what_it_will_do(self):
        with mock.patch('app.sync_queue.start', return_value=(True, 'Syncing.')) as start:
            r = self.t.client.post('/api/accounts/sync-queue',
                                   json={'account_ids': self.ids, 'override': True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['message'], 'Syncing.')
        start.assert_called_once_with(self.ids, override=True)

    def test_a_second_start_is_refused_while_one_runs(self):
        sync_queue._arm(self.ids, 3, self.queued_at, False, 3600)
        r = self.t.client.post('/api/accounts/sync-queue', json={'account_ids': self.ids})
        self.assertEqual(r.status_code, 409)
        self.assertIn('already syncing', r.get_json()['error'])

    def test_bad_bodies_are_refused(self):
        for body in ({}, {'account_ids': []}, {'account_ids': 'all'}, {'account_ids': [True]},
                     {'account_ids': ['1']}):
            with self.subTest(body=body):
                r = self.t.client.post('/api/accounts/sync-queue', json=body)
                self.assertEqual(r.status_code, 400)
                self.assertIn('error', r.get_json())

    def test_an_unknown_account_is_refused(self):
        r = self.t.client.post('/api/accounts/sync-queue', json={'account_ids': [999999]})
        self.assertEqual(r.status_code, 404)

    def test_the_status_read_names_a_recording_but_not_another_sync(self):
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='Big Game')
        db.session.commit()
        ticket = admission.try_start(admission.KIND_SYNC, 'held by the test', force=True)
        self.addCleanup(admission.release, ticket)
        data = self.t.client.get('/api/accounts/sync-queue').get_json()
        self.assertEqual(data['pause_seconds'], sync_queue.PAUSE_SECONDS)
        self.assertEqual(len(data['conflicts']), 1)
        self.assertIn('Big Game', data['conflicts'][0])

    def test_the_status_read_lists_every_account_oldest_first(self):
        body = self.t.client.get('/api/accounts/sync-queue').get_json()
        self.assertEqual([a['id'] for a in body['accounts']], self.ids)
        self.assertEqual([a['name'] for a in body['accounts']], ['One', 'Two', 'Three'])
        self.assertIsNone(body['sentence'])

    def test_the_status_read_says_where_a_running_queue_stands(self):
        sched.remove_job_if_exists(sync_queue.JOB_ID)
        sync_queue._arm(self.ids[1:], 3, self.queued_at, False, 3600)
        body = self.t.client.get('/api/accounts/sync-queue').get_json()
        self.assertEqual(body['sentence'],
                         'Syncing accounts one at a time, 1 of 3 done. Still to go: Two and Three')

    def test_stop_answers_with_what_happens_next(self):
        sync_queue._arm(self.ids, 3, self.queued_at, False, 3600)
        r = self.t.client.post('/api/accounts/sync-queue/stop')
        self.assertIn('normal schedule', r.get_json()['message'])
        self.assertIsNone(_job())

    def test_the_jobs_page_names_it(self):
        sync_queue._arm(self.ids, 3, self.queued_at, False, 3600)
        html = self.t.client.get('/jobs').get_data(as_text=True)
        self.assertIn('Account Sync Queue', html)
        self.assertIn('3 accounts left to sync, one at a time', html)

    def test_a_dead_scheduler_is_said_out_loud(self):
        with mock.patch('app.scheduler.scheduler_is_live', return_value=False):
            started, message = sync_queue.start(self.ids)
        self.assertFalse(started)
        self.assertIn('scheduler is not running', message)


class ReadinessActionTests(_QueueCase):

    def _row(self, **ctx_kw):
        ctx = readiness.Context(**ctx_kw)
        return next(r for r in readiness.evaluate(ctx=ctx)['checks']
                    if r['id'] == 'account_sync')

    def _make_stale(self):
        long_ago = datetime.utcnow() - timedelta(days=3)
        for account_id in self.ids:
            db.session.get(Account, account_id).last_sync_at = long_ago
        db.session.commit()

    def test_the_action_is_one_request_carrying_every_stale_account(self):
        self._make_stale()
        action = self._row()['action']
        self.assertEqual(action['label'], 'Sync 3 accounts now')
        self.assertEqual(action['urls'], ['/api/accounts/sync-queue'])
        self.assertEqual({a['id'] for a in action['accounts']}, set(self.ids))
        self.assertEqual({a['name'] for a in action['accounts']}, {'One', 'Two', 'Three'})

    def test_a_running_queue_replaces_the_button_with_its_progress_and_a_stop(self):
        self._make_stale()
        sync_queue._arm(self.ids[1:], 3, self.queued_at, False, 3600)
        row = self._row()
        self.assertIn('1 of 3 done', row['found'])
        self.assertIn('Still to go: Two and Three', row['found'])
        self.assertEqual(row['action']['urls'], ['/api/accounts/sync-queue/stop'])
        self.assertFalse(row['action']['accounts'])
        self.assertTrue(readiness.evaluate(ctx=readiness.Context())['sync_queue_active'])

    def test_a_held_queue_says_why(self):
        self._make_stale()
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='Big Game')
        db.session.commit()
        self._step()
        self.assertIn('Big Game', self._row()['found'])

    def test_the_support_bundle_sees_no_recording_or_account_name(self):
        self._make_stale()
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='Big Game')
        db.session.commit()
        self._step()
        found = self._row(pseudonymize=True)['found']
        self.assertNotIn('Big Game', found)
        self.assertIn('Waiting for other work', found)
        self.assertIn(f'account {self.ids[0]}', found)
        self.assertNotIn('Two', found)

    def test_no_queue_means_nothing_extra(self):
        self.assertFalse(readiness.evaluate()['sync_queue_active'])


class _IntervalCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(start_scheduler=True, csrf=False)
        self.t.sandbox_config({'sync': {'sync_interval_hours': 12}})
        self.now = datetime.utcnow()

    def tearDown(self):
        self.t.cleanup()

    def _account(self, name, last_sync_hours_ago):
        """An account on the global interval, its job booked one old interval after its sync."""
        last = self.now - timedelta(hours=last_sync_hours_ago)
        acc = seed.make_account(name=name, sync_enabled=True, last_sync_at=last,
                                next_sync_at=last + timedelta(hours=12))
        db.session.commit()
        account_id = acc.id
        sched.schedule_account_sync(self.t.app, account_id)
        return account_id

    def _next_run(self, account_id):
        return sched.to_naive_utc(
            sched.get_scheduler().get_job(f'account_sync_{account_id}').next_run_time)

    def _set_interval(self, hours):
        self.t.sandbox_config({'sync': {'sync_interval_hours': hours}})


class IntervalChangeTests(_IntervalCase):

    def test_an_account_past_a_shorter_interval_syncs_within_the_minute(self):
        account_id = self._account('Late', last_sync_hours_ago=8)
        self.assertGreater(self._next_run(account_id), self.now + timedelta(hours=3))
        self._set_interval(6)
        self.assertEqual(sched.schedule_all_account_syncs(
            self.t.app, force_reschedule_defaults=True), 1)
        self.assertLess(self._next_run(account_id), datetime.utcnow() + timedelta(minutes=2))

    def test_the_next_run_is_stored_with_the_job(self):
        account_id = self._account('Late', last_sync_hours_ago=8)
        self._set_interval(6)
        sched.schedule_all_account_syncs(self.t.app, force_reschedule_defaults=True)
        db.session.expire_all()
        self.assertEqual(db.session.get(Account, account_id).next_sync_at,
                         self._next_run(account_id))

    def test_an_account_inside_a_shorter_interval_moves_up_to_it(self):
        account_id = self._account('Recent', last_sync_hours_ago=2)
        self._set_interval(6)
        self.assertEqual(sched.schedule_all_account_syncs(
            self.t.app, force_reschedule_defaults=True), 0)
        expected = self.now + timedelta(hours=4)
        self.assertLess(abs((self._next_run(account_id) - expected).total_seconds()), 5)

    def test_a_longer_interval_moves_the_next_run_out(self):
        account_id = self._account('Recent', last_sync_hours_ago=2)
        self._set_interval(24)
        sched.schedule_all_account_syncs(self.t.app, force_reschedule_defaults=True)
        expected = self.now + timedelta(hours=22)
        self.assertLess(abs((self._next_run(account_id) - expected).total_seconds()), 5)

    def test_several_late_accounts_start_apart(self):
        ids = [self._account(f'Late {i}', last_sync_hours_ago=8 + i) for i in range(3)]
        self._set_interval(6)
        self.assertEqual(sched.schedule_all_account_syncs(
            self.t.app, force_reschedule_defaults=True), 3)
        runs = sorted(self._next_run(i) for i in ids)
        for earlier, later in zip(runs, runs[1:]):
            self.assertGreaterEqual((later - earlier).total_seconds(), 5 * 60)

    def test_an_account_with_its_own_interval_is_left_alone(self):
        last = self.now - timedelta(hours=8)
        acc = seed.make_account(name='Own', sync_enabled=True, sync_interval_hours=12,
                                last_sync_at=last, next_sync_at=last + timedelta(hours=12))
        db.session.commit()
        account_id = acc.id
        sched.schedule_account_sync(self.t.app, account_id)
        before = self._next_run(account_id)
        self._set_interval(6)
        sched.schedule_all_account_syncs(self.t.app, force_reschedule_defaults=True)
        self.assertEqual(self._next_run(account_id), before)

    def test_an_unchanged_interval_keeps_a_next_run_someone_set(self):
        """_reanchor_account_sync sets next_sync_at and force-reschedules; that must stand."""
        account_id = self._account('Anchored', last_sync_hours_ago=8)
        wanted = self.now + timedelta(hours=11)
        db.session.get(Account, account_id).next_sync_at = wanted
        db.session.commit()
        sched.schedule_account_sync(self.t.app, account_id, force_reschedule=True)
        self.assertEqual(self._next_run(account_id), wanted)

    def test_a_changed_interval_found_at_startup_is_caught_up_too(self):
        account_id = self._account('Late', last_sync_hours_ago=8)
        self._set_interval(6)
        sched.schedule_all_account_syncs(self.t.app)
        self.assertLess(self._next_run(account_id), datetime.utcnow() + timedelta(minutes=2))

    def test_no_scheduler_is_named_rather_than_a_crash(self):
        with mock.patch('app.scheduler._scheduler', None):
            self.assertEqual(sched.schedule_all_account_syncs(self.t.app), 0)


class IntervalSaveTests(_IntervalCase):

    def setUp(self):
        super().setUp()
        cfgmod.set_restart_needed(False)

    def tearDown(self):
        cfgmod.set_restart_needed(False)
        super().tearDown()

    def test_a_field_save_applies_live_says_so_and_raises_no_restart(self):
        account_id = self._account('Late', last_sync_hours_ago=8)
        r = self.t.client.post('/api/settings/field',
                               json={'path': 'sync.sync_interval_hours', 'value': 6})
        data = r.get_json()
        self.assertFalse(data['restart_required'])
        self.assertFalse(cfgmod.is_restart_needed())
        self.assertIn('1 account is past the new interval', data['notice'])
        job = sched.get_scheduler().get_job(f'account_sync_{account_id}')
        self.assertEqual(job.trigger.interval, timedelta(hours=6))
        self.assertLess(self._next_run(account_id), datetime.utcnow() + timedelta(minutes=2))

    def test_several_accounts_get_the_plural_sentence(self):
        self._account('Late 1', last_sync_hours_ago=8)
        self._account('Late 2', last_sync_hours_ago=9)
        r = self.t.client.post('/api/settings/field',
                               json={'path': 'sync.sync_interval_hours', 'value': 6})
        self.assertIn('2 accounts are past the new interval', r.get_json()['notice'])

    def test_a_save_that_makes_nobody_late_says_nothing_extra(self):
        self._account('Recent', last_sync_hours_ago=2)
        r = self.t.client.post('/api/settings/field',
                               json={'path': 'sync.sync_interval_hours', 'value': 6})
        self.assertNotIn('notice', r.get_json())

    def test_the_raw_editor_applies_it_live_too(self):
        account_id = self._account('Late', last_sync_hours_ago=8)
        raw = yaml.dump({**cfgmod._load_config_file(), 'sync': {'sync_interval_hours': 6}})
        r = self.t.client.post('/settings', data={'config_yaml': raw}, follow_redirects=True)
        self.assertIn('1 account is past the new interval', r.get_data(as_text=True))
        job = sched.get_scheduler().get_job(f'account_sync_{account_id}')
        self.assertEqual(job.trigger.interval, timedelta(hours=6))


if __name__ == '__main__':
    unittest.main()
