"""Tier 2 - Concurrency 5/5 small hardenings (DESIGN-concurrency.md §5.6).

Two independent changes, both feature/hardening (no BUGS.md entry per §7 - 5.1 was the
only defect in this design doc's numbering):

  * G7 - a recording whose account has no free connection slot. This originally shipped as
    "connect anyway, and raise a RECORDING_OVER_CONNECTION_LIMIT alert so the over-limit
    start is at least visible". The limit is now a hard ceiling instead: the acquire refuses
    and the caller waits (dev/changelog/854). The refusal itself is asserted here; the
    waiting, the fairness ordering and the terminal give-up are in
    tests/test_connection_limit_ceiling.py.
  * G9 cleanup - connection_limits.try_acquire() no longer calls load_config() while
    holding connection_limits._lock; the config-derived default_max_connections is read
    before the lock is taken. This test defends behavior (the limit is still enforced
    correctly with an account override and with the config fallback), since the lock
    hoist itself isn't independently observable from outside the module.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, recorder  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.database import Alert, RecordingEvent, DIAGNOSTICS  # noqa: E402


class SlotAcquireRefusalTests(unittest.TestCase):
    """G7, as amended: the acquire refuses rather than exceeding the account's limit."""

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Tight Account', max_connections=1)
        self.occupying_rec = seed.make_recording(status='IN_PROGRESS')
        self.new_rec = seed.make_recording(status='IN_PROGRESS')
        db.session.commit()
        self.account_id = self.account.id
        self.occupying_id = self.occupying_rec.id
        self.new_id = self.new_rec.id
        connlim._holders.clear()

    def tearDown(self):
        connlim._holders.clear()
        self.t.cleanup()

    def test_acquire_refuses_when_every_slot_is_held_by_a_recording(self):
        # Slot already held by a different recording; no channel test to preempt.
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', self.occupying_id))
        got = recorder._try_acquire_slot_with_preemption(
            self.t.app, self.new_id, self.account_id)
        self.assertFalse(got)

    def test_refusal_registers_no_holder(self):
        """The point of the ceiling: a refused acquire must leave the account's holder
        count at its limit, not one over it."""
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', self.occupying_id))
        recorder._try_acquire_slot_with_preemption(self.t.app, self.new_id, self.account_id)
        self.assertEqual(len(connlim._holders[self.account_id]), 1)
        self.assertNotIn(('recording', self.new_id), connlim._holders[self.account_id])

    def test_acquire_succeeds_and_registers_when_under_the_limit(self):
        got = recorder._try_acquire_slot_with_preemption(
            self.t.app, self.new_id, self.account_id)
        self.assertTrue(got)
        self.assertIn(('recording', self.new_id), connlim._holders[self.account_id])

    def test_refusal_writes_no_alert_and_no_event_of_its_own(self):
        """The acquire is now a pure decision - it neither connects over the limit nor
        narrates. Disclosure belongs to the caller that decides to wait, so the two are
        not written twice for one refusal (and the failover caller writes a different
        event than the two start callers do)."""
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', self.occupying_id))
        recorder._try_acquire_slot_with_preemption(self.t.app, self.new_id, self.account_id)
        self.assertEqual(Alert.query.filter_by(
            alert_type='RECORDING_OVER_CONNECTION_LIMIT').count(), 0)
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=self.new_id, event_type=DIAGNOSTICS).count(), 0)


class LimitEnforcementAfterHoistTests(unittest.TestCase):
    """G9: the config read moved out from under the lock; the limit must still be
    correctly derived from either the account override or the config fallback."""

    def setUp(self):
        self.t = make_test_app()
        self.default_account = seed.make_account(name='Default Limit Account')
        self.override_account = seed.make_account(name='Override Account', max_connections=3)
        db.session.commit()
        self.default_account_id = self.default_account.id
        self.override_account_id = self.override_account.id
        connlim._holders.clear()

    def tearDown(self):
        connlim._holders.clear()
        self.t.cleanup()

    def test_config_default_still_enforced(self):
        # accounts.default_max_connections defaults to 1 (config.py _DEFAULTS).
        self.assertTrue(connlim.try_acquire(self.default_account_id, 'recording', 1))
        self.assertFalse(connlim.try_acquire(self.default_account_id, 'recording', 2))

    def test_account_override_still_takes_precedence_over_config_default(self):
        for holder_id in (1, 2, 3):
            self.assertTrue(connlim.try_acquire(self.override_account_id, 'recording', holder_id))
        self.assertFalse(connlim.try_acquire(self.override_account_id, 'recording', 4))

    def test_idempotent_reacquire_still_returns_true_without_double_counting(self):
        self.assertTrue(connlim.try_acquire(self.default_account_id, 'recording', 1))
        self.assertTrue(connlim.try_acquire(self.default_account_id, 'recording', 1))
        self.assertEqual(len(connlim._holders[self.default_account_id]), 1)


if __name__ == '__main__':
    unittest.main()
