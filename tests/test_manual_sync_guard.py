"""Tier 2 - manual sync warn + explicit override (DESIGN-concurrency.md §5.4).

**G-manual.** The scheduled sync job (`scheduler._account_sync_job`) hard-skips three
conditions - a recording IN_PROGRESS, a recording starting within
`sync.skip_sync_if_recording_within_minutes`, an active channel test run - because nobody
is present to decide. Both *manual* entry points bypassed every one of them: the accounts
page "Sync Now" button and `/api/jobs/account_sync_<id>/run-now` went straight to
`threading.Thread(target=sync_account)`.

§5.4's decision: manual means the user is present, so the user decides - but never
silently. Refuse with the reasons, and let an explicit `force` override.

The load-bearing structural assertion is `_shared_helper` in each route class: §5.4 says
"one shared helper, two call sites - do not write it twice", so both routes are asserted to
go through `app.accounts.sync_conflicts` **by patching it and observing the refusal**,
rather than by re-asserting the conflict logic at each route (which would pass just as
happily against two copies).

No real syncs: `sync_account` is patched everywhere and any thread it would have run in is
joined before asserting, so "did it start" is deterministic rather than timing-dependent.
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
from app import db, accounts  # noqa: E402


def _cfg(skip_if_active=True, within_minutes=5):
    """Only the keys the guard reads. load_config() is patched rather than passed through
    make_test_app overrides, which are invisible to a runtime load_config (CLAUDE.md)."""
    return {
        'sync': {
            'skip_sync_if_recording_active': skip_if_active,
            'skip_sync_if_recording_within_minutes': within_minutes,
        },
        'notifications': {'routing': {}, 'base_url': ''},
    }


def _join_sync_threads():
    """Join any thread a sync route spawned, so assertions don't race the request."""
    for t in threading.enumerate():
        if t.name.startswith('account-sync-'):
            t.join(timeout=5)


class SyncConflictsHelperTests(unittest.TestCase):
    """The helper itself: which conditions are conflicts, and which config turns each off."""

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Conflicted')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _conflicts(self, tester_running=False, **cfg_kw):
        with mock.patch('app.channel_tester.is_running', return_value=tester_running), \
             mock.patch('app.config.load_config', return_value=_cfg(**cfg_kw)):
            return accounts.sync_conflicts(self.account.id)

    def test_idle_system_has_no_conflicts(self):
        self.assertEqual(self._conflicts(), [])

    def test_in_progress_recording_is_a_conflict(self):
        seed.make_recording(status='IN_PROGRESS', name='Big Game')
        db.session.commit()
        conflicts = self._conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertIn('Big Game', conflicts[0],
                      'the reason must name the recording, not just say "a recording"')

    def test_imminent_recording_is_a_conflict(self):
        start = datetime.utcnow() + timedelta(minutes=2)
        seed.make_recording(status='SCHEDULED', name='Starts Soon',
                            start_time=start, stop_time=start + timedelta(hours=1))
        db.session.commit()
        conflicts = self._conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertIn('Starts Soon', conflicts[0])

    def test_distant_scheduled_recording_is_not_a_conflict(self):
        start = datetime.utcnow() + timedelta(hours=6)
        seed.make_recording(status='SCHEDULED', name='Later',
                            start_time=start, stop_time=start + timedelta(hours=1))
        db.session.commit()
        self.assertEqual(self._conflicts(), [])

    def test_active_test_run_is_a_conflict(self):
        conflicts = self._conflicts(tester_running=True)
        self.assertEqual(len(conflicts), 1)
        self.assertIn('test run', conflicts[0].lower())

    def test_skip_if_recording_active_false_disables_that_check(self):
        """Manual must not be stricter than scheduled: a guard turned off in config is off
        for both."""
        seed.make_recording(status='IN_PROGRESS', name='Big Game')
        db.session.commit()
        self.assertEqual(self._conflicts(skip_if_active=False), [])

    def test_within_minutes_zero_disables_that_check(self):
        start = datetime.utcnow() + timedelta(minutes=2)
        seed.make_recording(status='SCHEDULED', name='Starts Soon',
                            start_time=start, stop_time=start + timedelta(hours=1))
        db.session.commit()
        self.assertEqual(self._conflicts(within_minutes=0), [])

    def test_every_conflict_is_reported_not_just_the_first(self):
        """The user is deciding, so they get the whole picture - a first-match-wins check
        would hide the recording behind the test run or vice versa."""
        seed.make_recording(status='IN_PROGRESS', name='Big Game')
        db.session.commit()
        self.assertEqual(len(self._conflicts(tester_running=True)), 2)


class AccountsPageSyncGuardTests(unittest.TestCase):
    """`routes/accounts.py::sync_account_api` - 409 + the reasons + force-to-resubmit.

    Both Accounts surfaces (the list row's kebab and the account page's action bar) drive
    this one endpoint since dev/changelog/456; the form-POST route this class used to test,
    with its `?sync_conflict=<id>` redirect and its rendered `Sync Anyway` button, is gone.
    What §5.4 requires survived the move and is what is asserted here: the reasons come back
    as a LIST the caller can name one by one, not as a bare "conflict" flag - a caller that
    only knows something clashed can only refuse, and refusing is what §5.4 forbids.
    """

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.account = seed.make_account(name='Conflicted')
        db.session.commit()
        self.account_id = self.account.id

    def tearDown(self):
        _join_sync_threads()
        self.t.cleanup()

    def _post(self, json_body=None):
        spy = mock.Mock()
        with mock.patch('app.accounts.sync_account', spy):
            resp = self.t.client.post(f'/api/accounts/{self.account_id}/sync', json=json_body)
            _join_sync_threads()
        return resp, spy

    def _conflicted(self):
        return mock.patch('app.accounts.sync_conflicts', return_value=['Reactor is melting.'])

    def test_conflicted_sync_does_not_start(self):
        with self._conflicted():
            _, spy = self._post()
        spy.assert_not_called()

    def test_conflicted_sync_answers_409(self):
        with self._conflicted():
            resp, _ = self._post()
        self.assertEqual(resp.status_code, 409)

    def test_409_body_lists_the_reasons(self):
        """The override confirm names each conflict, so each has to arrive separately."""
        with self._conflicted():
            resp, _ = self._post()
        data = resp.get_json()
        self.assertIn('Reactor is melting.', data['error'])
        self.assertEqual(data['conflicts'], ['Reactor is melting.'],
                         'the UI needs the reasons as a list to build its confirm dialog')
        self.assertNotIn('success', data)

    def test_a_non_conflict_refusal_is_not_offered_as_overridable(self):
        """Already syncing is not a warning to override - force cannot fix it - so it must
        not come back looking like one, or the UI offers a Sync anyway button that fails."""
        self.account.status = 'SYNCING'
        db.session.commit()
        resp, spy = self._post()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['conflicts'], [])
        spy.assert_not_called()

    def test_force_starts_the_sync_despite_conflicts(self):
        with self._conflicted():
            resp, spy = self._post({'force': True})
        self.assertEqual(resp.status_code, 200)
        spy.assert_called_once()

    def test_unconflicted_sync_starts_normally(self):
        with mock.patch('app.accounts.sync_conflicts', return_value=[]):
            resp, spy = self._post()
        self.assertEqual(resp.status_code, 200)
        spy.assert_called_once()

    def test_missing_body_is_not_an_error(self):
        """A bodyless POST is what the kebab sends when nothing needs forcing."""
        with self._conflicted():
            resp, _ = self._post(None)
        self.assertEqual(resp.status_code, 409)

    def test_shared_helper(self):
        """§5.4: one helper, two call sites. Patching the helper must be enough to change
        this route's behavior - if it has its own copy of the logic, this passes nothing."""
        with mock.patch('app.accounts.sync_conflicts', return_value=['injected']) as helper:
            _, spy = self._post()
        helper.assert_called_once_with(self.account_id)
        spy.assert_not_called()


class JobsRunNowSyncGuardTests(unittest.TestCase):
    """`routes/jobs.py::run_job_now`, account_sync branch - 409 + force in the JSON body.

    The scheduler is faked: the route only needs get_job() to answer non-None, and a live
    jobstore would add nothing to what §5.4 is about.
    """

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.account = seed.make_account(name='Conflicted')
        db.session.commit()
        self.account_id = self.account.id
        self.url = f'/api/jobs/account_sync_{self.account_id}/run-now'

    def tearDown(self):
        _join_sync_threads()
        self.t.cleanup()

    def _post(self, json_body=None, conflicts=('Reactor is melting.',)):
        spy = mock.Mock()
        scheduler = mock.Mock()
        scheduler.get_job.return_value = mock.Mock()
        with mock.patch('app.scheduler.get_scheduler', return_value=scheduler), \
             mock.patch('app.accounts.sync_conflicts', return_value=list(conflicts)), \
             mock.patch('app.accounts.sync_account', spy):
            resp = self.t.client.post(self.url, json=json_body)
            _join_sync_threads()
        return resp, spy

    def test_conflicted_run_now_answers_409(self):
        resp, spy = self._post()
        self.assertEqual(resp.status_code, 409)
        spy.assert_not_called()

    def test_409_body_follows_the_json_envelope_and_lists_the_reasons(self):
        resp, _ = self._post()
        data = resp.get_json()
        self.assertIn('error', data)
        self.assertIn('Reactor is melting.', data['error'])
        self.assertEqual(data['conflicts'], ['Reactor is melting.'],
                         'the UI needs the reasons as a list to build its confirm dialog')
        self.assertNotIn('success', data)

    def test_force_in_the_body_starts_the_sync(self):
        resp, spy = self._post(json_body={'force': True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        spy.assert_called_once()

    def test_unconflicted_run_now_starts_normally(self):
        resp, spy = self._post(conflicts=())
        self.assertEqual(resp.status_code, 200)
        spy.assert_called_once()

    def test_missing_body_is_not_an_error(self):
        """A bodyless POST is the common case from the Run Now button; it must read as
        "no force", not blow up on request.get_json()."""
        resp, _ = self._post(json_body=None)
        self.assertEqual(resp.status_code, 409)

    def test_shared_helper(self):
        """The twin of the accounts-page assertion: same helper, second call site."""
        scheduler = mock.Mock()
        scheduler.get_job.return_value = mock.Mock()
        spy = mock.Mock()
        with mock.patch('app.scheduler.get_scheduler', return_value=scheduler), \
             mock.patch('app.accounts.sync_conflicts', return_value=['injected']) as helper, \
             mock.patch('app.accounts.sync_account', spy):
            resp = self.t.client.post(self.url)
            _join_sync_threads()
        helper.assert_called_once_with(self.account_id)
        self.assertEqual(resp.status_code, 409)
        spy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
