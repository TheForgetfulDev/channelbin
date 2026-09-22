"""The group page's status/action bar: one primary action, every state named.

Guards `dev/docs/BUGS.md` 2026-09-21 @ 05:41:12 PM ET. The bar's buttons were chosen by a
Jinja `{% if %}` chain over `job.status` that named RUNNING, stuck RUNNING, *non-recurring*
SCHEDULED, CANCELLED and QUEUED, and let everything else fall into a trailing `{% else %}`
rendering "Test again" wired to `POST .../restart`. A recurring SCHEDULED check - the
ordinary state of a monitored group - matched no branch, landed in the catch-all, and got
the one button whose route refuses it, so the page's most prominent control answered every
click with `Job is SCHEDULED - only CANCELLED or COMPLETED jobs can be restarted`.

What is asserted, and why each case is not redundant:

  * **Every status in `OD_JOB_STATUSES` reaches a named branch.** The regression is a state
    nobody enumerated, so the test enumerates from the constant rather than from a list
    retyped here - a sixth status added later fails this file instead of silently
    inheriting the unknown-state branch.
  * **The primary button is always "Test now".** Not "Test again", not "Start now", not
    "Start over": a label that changes with the check's history describes the history, and
    the user is pressing it to do one thing (`CLAUDE.md` "one verb per action, app-wide").
  * **The unknown-state branch renders no action at all.** An `else` that offers a button
    is an `else` that can be wrong about what the button does; naming the status and
    offering nothing is the honest answer.
  * **`POST .../start` is the one run route** and accepts every non-RUNNING status, so the
    two-routes-four-statuses split that created the gap cannot come back.
  * **`/restart` is gone**, client and server, rather than kept "in case".

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_group_action_bar
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_test_job  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Channel, OD_JOB_STATUSES, OD_JOB_IDLE_STATUSES, OD_JOB_STATUS_QUEUED,
    OD_JOB_STATUS_SCHEDULED, OD_JOB_STATUS_RUNNING,
    OD_JOB_STATUS_COMPLETED, OD_JOB_STATUS_CANCELLED,
)
from app.routes.channel_groups import (  # noqa: E402
    _action_bar_state, BAR_STATES, BAR_RUNNING, BAR_STUCK, BAR_UNKNOWN,
    BAR_SCHEDULED_ONCE, BAR_SCHEDULED_RECUR,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The action group, which is the region the buttons live in. Matched rather than the whole
# page so a "Test now" elsewhere on the page cannot make a missing one look present.
_ACTIONS_RE = re.compile(r'<div class="gd-ab-actions" id="gd-inline-actions">(.*?)</div>',
                         re.S)


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


class ActionBarStateTests(unittest.TestCase):
    """_action_bar_state() is the one derivation, so it answers for every status."""

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.ctx = self.app.app_context()
        self.ctx.push()
        acct = make_account()
        self.ch = make_channel(acct, name='FS1 A')
        self.job = make_test_job(name='FS1', channels=[self.ch])
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _state(self, status, recurring=False, live=False):
        self.job.status = status
        self.job.recurring = recurring
        db.session.commit()
        tester = {'is_running': live,
                  'current_job_id': self.job.id if live else None}
        return _action_bar_state(self.job, tester)

    def test_every_status_reaches_a_named_state(self):
        """Enumerated from the constant, so a new status cannot inherit the catch-all."""
        for status in OD_JOB_STATUSES:
            with self.subTest(status=status):
                state = self._state(status, live=(status == OD_JOB_STATUS_RUNNING))
                self.assertIn(state, BAR_STATES)
                self.assertNotEqual(state, BAR_UNKNOWN,
                                    f'{status} is a real status and must be named')

    def test_a_status_outside_the_vocabulary_is_unknown(self):
        self.assertEqual(self._state('SOMETHING_NEW'), BAR_UNKNOWN)

    def test_running_splits_on_whether_the_tester_is_actually_on_this_job(self):
        self.assertEqual(self._state(OD_JOB_STATUS_RUNNING, live=True), BAR_RUNNING)
        self.assertEqual(self._state(OD_JOB_STATUS_RUNNING, live=False), BAR_STUCK)

    def test_scheduled_splits_on_recurring(self):
        self.assertEqual(
            self._state(OD_JOB_STATUS_SCHEDULED, recurring=True), BAR_SCHEDULED_RECUR)
        self.assertEqual(
            self._state(OD_JOB_STATUS_SCHEDULED, recurring=False), BAR_SCHEDULED_ONCE)

    def test_no_check_has_no_bar_state(self):
        self.assertIsNone(_action_bar_state(None, {'is_running': False}))


class ActionBarMarkupTests(unittest.TestCase):
    """What the rendered page actually offers, per status.

    Each case gets its OWN group and check, seeded up front, rather than one group whose
    status is rewritten between requests. The test client reuses one scoped session across
    requests, so a job mutated in a separate app context is still served from the request
    session's identity map - a page asserted that way renders the FIRST status for the rest
    of the test, which passes every "does it say Test now" assertion without ever having
    rendered the state it names (the same trap `test_group_detail_page_conformance.py`
    documents on its `_lock_to` helper)."""

    # The five real statuses plus the two SCHEDULED shapes and one value outside the
    # vocabulary. `key` is what a case asks for; the recurring SCHEDULED row is the
    # regression itself.
    CASES = {
        'queued': (OD_JOB_STATUS_QUEUED, False),
        'scheduled_once': (OD_JOB_STATUS_SCHEDULED, False),
        'scheduled_recurring': (OD_JOB_STATUS_SCHEDULED, True),
        'completed': (OD_JOB_STATUS_COMPLETED, False),
        'cancelled': (OD_JOB_STATUS_CANCELLED, False),
        'running': (OD_JOB_STATUS_RUNNING, False),
        'bogus': ('SOMETHING_NEW', False),
    }

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        self.gids = {}
        with self.app.app_context():
            acct = make_account()
            for key, (status, recurring) in self.CASES.items():
                ch = make_channel(acct, name=f'FS1 {key}')
                job = make_test_job(name=f'FS1 {key}', channels=[ch], status=status)
                job.recurring = recurring
                self.gids[key] = job.group_id
            db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _actions(self, key):
        r = self.client.get(f'/channel-groups/{self.gids[key]}')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        block = _ACTIONS_RE.search(html)
        self.assertIsNotNone(block, 'action group not found on the group page')
        return block.group(1), html

    def test_every_idle_status_offers_exactly_one_test_now(self):
        """The regression: `scheduled_recurring` is in this list, and it is the case that
        used to reach the trailing else and render "Test again"."""
        idle_keys = [k for k, (status, _) in self.CASES.items()
                     if status in OD_JOB_IDLE_STATUSES]
        self.assertIn('scheduled_recurring', idle_keys)
        for key in idle_keys:
            with self.subTest(case=key):
                actions, _ = self._actions(key)
                self.assertEqual(actions.count('Test now'), 1,
                                 f'{key} did not offer exactly one Test now')
                for dead in ('Test again', 'Start now', 'Start over', 'Run now'):
                    self.assertNotIn(dead, actions, f'{key} still offers "{dead}"')

    def test_the_badge_and_the_message_name_each_state_too(self):
        """All three chains, not just the buttons: the badge fell through to "Completed"
        and the message to "Last run ...", so an unnamed state was mislabeled twice over
        before anyone reached for a button."""
        expected = {
            'queued': 'No schedule',
            'scheduled_once': 'Scheduled',
            'scheduled_recurring': 'Scheduled',
            'completed': 'Completed',
            'cancelled': 'Cancelled',
            'running': 'Stuck',     # no tester behind the row in a test app
        }
        for key, label in expected.items():
            with self.subTest(case=key):
                _, html = self._actions(key)
                badge = re.search(r'id="gd-status-badge">(.*?)</span>\s*</span>', html, re.S)
                self.assertIsNotNone(badge)
                self.assertIn(label, badge.group(1))

    def test_a_stuck_run_offers_force_cancel_and_no_run(self):
        actions, _ = self._actions('running')
        self.assertIn('Force Cancel', actions)
        self.assertNotIn('Test now', actions)

    def test_an_unknown_status_names_itself_and_offers_no_action(self):
        """The `else` may report, never render a state it cannot name."""
        actions, html = self._actions('bogus')
        self.assertIn('Unknown state: SOMETHING_NEW', html)
        self.assertNotIn('Test now', actions)

    def test_the_kebab_offers_resume_only_on_a_cancelled_check(self):
        _, html = self._actions('cancelled')
        self.assertIn('Resume where it stopped', html)
        self.assertNotIn('Start over', html)

        _, html = self._actions('completed')
        self.assertNotIn('Resume where it stopped', html)

    def test_the_template_never_branches_on_job_status_itself(self):
        """One derivation, in Python. Three Jinja chains each re-deriving it is what let
        them disagree about which states exist."""
        tpl = _read('templates/channels/group_detail.html')
        self.assertNotIn("job.status ==", tpl)
        self.assertNotIn("job.status !=", tpl)


class OneRunRouteTests(unittest.TestCase):
    """POST .../start is the only way to run a check, from any status but RUNNING."""

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.app.app_context():
            acct = make_account()
            self.ch_id = make_channel(acct, name='FS1 A').id
            db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _job(self, status):
        with self.app.app_context():
            job = make_test_job(name=f'J-{status}',
                                channels=[db.session.get(Channel, self.ch_id)],
                                status=status)
            db.session.commit()
            return job.id

    def test_start_accepts_every_idle_status(self):
        """COMPLETED and CANCELLED used to belong to /restart alone, and a recurring
        SCHEDULED check belonged to neither route's accepted set in the UI's eyes."""
        import app.routes.channel_tests as ct
        started = []
        real = ct._start_job_run
        ct._start_job_run = lambda job_id, **kw: started.append(job_id)
        try:
            for status in OD_JOB_IDLE_STATUSES:
                with self.subTest(status=status):
                    job_id = self._job(status)
                    r = self.client.post(
                        f'/api/channel-tests/on-demand/{job_id}/start')
                    self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                    self.assertIn(job_id, started)
        finally:
            ct._start_job_run = real

    def test_start_still_refuses_a_running_job(self):
        job_id = self._job(OD_JOB_STATUS_RUNNING)
        r = self.client.post(f'/api/channel-tests/on-demand/{job_id}/start')
        self.assertEqual(r.status_code, 409)

    def test_the_restart_route_is_gone(self):
        job_id = self._job(OD_JOB_STATUS_COMPLETED)
        r = self.client.post(f'/api/channel-tests/on-demand/{job_id}/restart')
        self.assertEqual(r.status_code, 404)

    def test_no_client_code_still_calls_restart(self):
        for rel in ('static/js/group-detail.js', 'static/js/groups.js',
                    'templates/channels/group_detail.html'):
            with self.subTest(rel=rel):
                self.assertNotIn("jobApi('restart')", _read(rel))
                self.assertNotIn('/restart', _read(rel))


if __name__ == '__main__':
    unittest.main()
