"""The health-check signature the Groups list and the Channel page follow a run on
(dev/changelog/1158).

channel_tester.health_check_signature() rides on /api/nav-status, and both pages render
the one they were read at; each swaps its test-result regions when the two differ. Before
this, /channel-groups showed Running long after a check finished (or never showed it), and
/channels/<id> kept "Testing now", its score and its history until reloaded unless its own
Test now button had started the test.

So the signature has to move for every way a result lands - a part that misses one is a
result the open page never shows - and stay still otherwise, or both pages re-render on
every nav poll. The run cases drive the tester's own start and end functions rather than
poking fields.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_health_check_signature
"""
import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (  # noqa: E402
    make_account, make_channel, make_channel_test, make_group,
)
from app import channel_tester, db  # noqa: E402
from app.database import (  # noqa: E402
    Channel, OnDemandTestJob, OD_JOB_STATUS_COMPLETED, OD_JOB_STATUS_RUNNING,
)


class HealthCheckSignatureTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        acct = make_account()
        self.ch = make_channel(acct, name='One')
        self.ch2 = make_channel(acct, name='Two')
        self.grp = make_group(name='Pair', members=[self.ch, self.ch2])
        self.job_id = self.grp.check.id
        db.session.commit()
        self.ch_id = self.ch.id

    def tearDown(self):
        # Before cleanup(), which fails any test that leaves a run registered.
        channel_tester._end_run()
        db.session.remove()
        self.ctx.pop()
        self.t.cleanup()

    def _sig(self):
        db.session.expire_all()
        return channel_tester.health_check_signature()['sig']

    def _moves(self, change):
        before = self._sig()
        change()
        self.assertNotEqual(before, self._sig())

    def _still(self, change):
        before = self._sig()
        change()
        self.assertEqual(before, self._sig())

    def _start_run(self):
        with channel_tester._lock:
            channel_tester._reset_run_state(run_kind='one_off', label='test')

    def _set_state(self, **fields):
        with channel_tester._lock:
            for k, v in fields.items():
                setattr(channel_tester._state, k, v)

    def _set_job(self, **cols):
        job = db.session.get(OnDemandTestJob, self.job_id)
        for k, v in cols.items():
            setattr(job, k, v)
        db.session.commit()

    # ── what the pages show ──────────────────────────────────────────────

    def test_a_run_starting_moves_it(self):
        """A Test now, a group's check and the nightly one all start through
        _reset_run_state - the Running chip and "Testing now" appear."""
        self._moves(self._start_run)

    def test_a_run_ending_moves_it(self):
        """The defect on the Groups list: a finished check kept reading Running."""
        self._start_run()
        self._moves(channel_tester._end_run)

    def test_a_run_replaced_between_two_polls_moves_it(self):
        """One run ending and the next starting inside one poll interval - a one-off test
        whose channel never connected, then another - looks running at both polls; the
        run's own start time is what tells them apart."""
        self._start_run()

        def next_run():
            channel_tester._end_run()
            self._start_run()
            with channel_tester._lock:
                channel_tester._state.run_started_at += timedelta(seconds=1)
        self._moves(next_run)

    def test_a_channel_finishing_mid_run_moves_it(self):
        """completed_channels moves after that channel's row is written, so each result
        reaches an open page during the run rather than at its end."""
        self._start_run()
        self._moves(lambda: self._set_state(completed_channels=1))

    def test_the_next_channel_starting_moves_it(self):
        """Before its row exists, the channel page for the channel now being tested
        should start saying so."""
        self._start_run()
        self._moves(lambda: self._set_state(current_channel_id=self.ch_id))

    def test_a_test_row_being_written_moves_it(self):
        def add():
            make_channel_test(db.session.get(Channel, self.ch_id), test_ended_at=None)
            db.session.commit()
        self._moves(add)

    def test_a_check_marked_running_moves_it(self):
        """The job's status is written just after the tester starts and just before it
        stops, so a render in either gap would otherwise draw the wrong chip for good."""
        self._moves(lambda: self._set_job(status=OD_JOB_STATUS_RUNNING))
        self._moves(lambda: self._set_job(status=OD_JOB_STATUS_COMPLETED))

    # ── what it must not move on ─────────────────────────────────────────

    def test_nothing_happening_leaves_it_still(self):
        self._still(lambda: None)

    def test_live_progress_inside_a_test_leaves_it_still(self):
        """Bytes and connect attempts move every second while a channel is under test;
        a signature that moved with them would re-render both pages on every poll."""
        self._start_run()
        self._set_state(current_channel_id=self.ch_id)
        self._still(lambda: self._set_state(current_live_bytes=123456,
                                            current_connect_attempt=2,
                                            current_phase='waiting'))

    def test_an_unrelated_edit_leaves_it_still(self):
        def rename():
            db.session.get(Channel, self.ch_id).name = 'One, renamed'
            self._set_job(name='Pair, renamed')
        self._still(rename)

    # ── the wire ─────────────────────────────────────────────────────────

    def test_busy_says_whether_the_tester_is_running(self):
        self.assertFalse(channel_tester.health_check_signature()['busy'])
        self._start_run()
        self.assertTrue(channel_tester.health_check_signature()['busy'])

    def test_nav_status_carries_it_and_both_pages_render_the_same_string(self):
        sig = self._sig()
        nav = self.client.get('/api/nav-status').get_json()
        self.assertEqual(nav['health_check'], {'sig': sig, 'busy': False})
        groups = self.client.get('/channel-groups').get_data(as_text=True)
        self.assertIn(f'id="grp-list-groups" data-section="groups" data-hc-sig="{sig}"', groups)
        detail = self.client.get(f'/channels/{self.ch_id}').get_data(as_text=True)
        self.assertIn(f'id="cd-statusbar" data-hc-sig="{sig}"', detail)


if __name__ == '__main__':
    unittest.main()
