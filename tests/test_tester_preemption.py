"""Tier 2 - the slotless-test preemption race (DESIGN-concurrency.md G1/G2, §5.1).

Guards BUGS.md 2026-07-20 (channel test launched ffmpeg after a recording stripped its
connection slot). The defect: channel_tester registered `active_test_account_id` only
alongside the ffmpeg proc, deep inside the connect loop, so between
`connlim.try_acquire()` and that registration - and again across every connect-retry
sleep, where the proc is deregistered - `kill_active_test_for_account()` matched nothing,
early-returned False without setting `preempted_by_recording`, and the test went on to
open a second provider connection on a one-slot account.

The invariant every test here defends, stated in DESIGN-concurrency.md §5.1:
**a test ffmpeg process must never outlive the slot that authorized it**, and the
connection_limits holder count for the account never exceeds its limit once the recording
holds its slot.

No real ffmpeg: subprocess.Popen is monkeypatched with FakeProc. The preemption is driven
from inside the patched call, which is what makes the interleave deterministic rather than
timing-dependent.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import ChannelTest, Channel  # noqa: E402
from app.config import load_config  # noqa: E402
from app.health_score import score_test_quality, apply_test_health_observation  # noqa: E402
from app.routes.channel_tests import _test_status_label, _tally  # noqa: E402
from app import channel_tester, connection_limits as connlim, recorder  # noqa: E402


class FakeProc:
    """Stands in for a Popen'd ffmpeg. Records whether it was signalled, so a test can
    assert the tester killed its own just-spawned process."""

    def __init__(self):
        self.terminated = False
        self.killed = False
        self.stderr = None
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = -15

    def kill(self):
        self.killed = True
        self._returncode = -9

    def wait(self, timeout=None):
        self._returncode = self._returncode if self._returncode is not None else 0
        return self._returncode

    @property
    def signalled(self):
        return self.terminated or self.killed


class TesterPreemptionRaceTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        with self.t.app.app_context():
            acct = seed.make_account(name='One Slot', max_connections=1)
            ch = seed.make_channel(acct, name='Race Channel')
            db.session.commit()
            self.account_id = acct.id
            self.channel_id = ch.id
            rec = seed.make_recording(status='SCHEDULED')
            db.session.commit()
            self.recording_id = rec.id
        # Module-level tester state is global, and make_test_app() has already swapped in
        # a fresh RunState via reset_module_globals - NOT _reset_run_state(), which is a
        # start-of-run function that takes an admission ticket. Calling it as a reset left
        # KIND_TESTER held after every test here, and the next module's first try_start
        # for a kind that yields to the tester was then refused by a run that had never
        # happened (dev/changelog/723).
        connlim._holders.clear()

    def tearDown(self):
        # _end_run(), not _reset_run_state(): a test that drove a run to completion may
        # still hold the ticket, and this is the one path that gives it back.
        channel_tester._end_run()
        connlim._holders.clear()
        self.t.cleanup()

    def _holders(self):
        return list(connlim._holders.get(self.account_id, []))

    def _preempt_as_recording(self):
        """Exactly what a starting recording does when it needs the slot."""
        recorder._try_acquire_slot_with_preemption(self.t.app, self.recording_id, self.account_id)

    def _latest_test_row(self):
        with self.t.app.app_context():
            db.session.expire_all()
            return (ChannelTest.query.filter_by(channel_id=self.channel_id)
                    .order_by(ChannelTest.id.desc()).first())

    # ── G1: preemption inside the acquire → Popen window ──────────────────────

    def test_g1_preempted_before_popen_never_launches_ffmpeg(self):
        """The core regression. Preempting while the tester is between slot acquisition
        and its first Popen must abort the channel WITHOUT ever spawning ffmpeg."""
        popen_calls = []

        def _never_called(*a, **kw):
            popen_calls.append(a)
            raise AssertionError('tester launched ffmpeg after its slot was preempted')

        # mkstemp sits squarely in the acquire → Popen window; preempting from there
        # reproduces G1 without any thread timing.
        real_mkstemp = channel_tester.tempfile.mkstemp

        def _mkstemp_then_preempt(*a, **kw):
            result = real_mkstemp(*a, **kw)
            self._preempt_as_recording()
            return result

        with mock.patch.object(channel_tester.tempfile, 'mkstemp', _mkstemp_then_preempt), \
             mock.patch.object(channel_tester.subprocess, 'Popen', _never_called):
            channel_tester.run_channel_test(self.t.app, self.channel_id)

        self.assertEqual(popen_calls, [], 'no ffmpeg may be launched once the slot is gone')
        # The recording holds the account's only slot, and holds it alone.
        self.assertEqual(self._holders(), [('recording', self.recording_id)])

        row = self._latest_test_row()
        self.assertEqual(row.status, 'CANCELLED')
        self.assertIn('Interrupted by recording start', row.error_detail or '')

    def test_g1_preempted_during_popen_kills_own_proc(self):
        """The flag can be set after Popen returns but before the tester registers the
        proc. The register-then-recheck block must then kill the proc it just spawned."""
        procs = []

        def _popen_then_preempt(*a, **kw):
            proc = FakeProc()
            procs.append(proc)
            self._preempt_as_recording()  # flag set before registration lands
            return proc

        with mock.patch.object(channel_tester.subprocess, 'Popen', _popen_then_preempt), \
             mock.patch.object(channel_tester, '_drain_stderr', lambda *a, **kw: None):
            channel_tester.run_channel_test(self.t.app, self.channel_id)

        self.assertEqual(len(procs), 1, 'must abort after the first attempt, not retry')
        self.assertTrue(procs[0].signalled, 'tester must kill the proc it spawned slotless')
        self.assertEqual(self._holders(), [('recording', self.recording_id)])

        row = self._latest_test_row()
        self.assertEqual(row.status, 'CANCELLED')
        self.assertIn('Interrupted by recording start', row.error_detail or '')

    # ── G2: preemption during a connect-retry sleep ───────────────────────────

    def test_g2_preempted_during_retry_sleep_does_not_relaunch(self):
        """The proc is deregistered across the retry sleep, so a preemption landing there
        matched nothing under the old code and the loop launched a second slotless
        ffmpeg. Exactly one Popen may happen."""
        procs = []

        def _popen(*a, **kw):
            proc = FakeProc()
            proc._returncode = 1  # exited immediately: drives the retry branch
            procs.append(proc)
            return proc

        def _sleep_then_preempt(_delay):
            self._preempt_as_recording()

        with mock.patch.object(channel_tester.subprocess, 'Popen', _popen), \
             mock.patch.object(channel_tester, '_drain_stderr', lambda *a, **kw: None), \
             mock.patch.object(channel_tester, 'wait_for_file_data', lambda *a, **kw: False), \
             mock.patch.object(channel_tester, '_interruptible_sleep', _sleep_then_preempt):
            channel_tester.run_channel_test(self.t.app, self.channel_id)

        self.assertEqual(len(procs), 1,
                         'must not start another connect attempt after being preempted')
        self.assertEqual(self._holders(), [('recording', self.recording_id)])
        row = self._latest_test_row()
        self.assertEqual(row.status, 'CANCELLED')
        self.assertIn('Interrupted by recording start', row.error_detail or '')

    # ── the kill helper's own contract ────────────────────────────────────────

    def test_kill_helper_flags_account_match_with_no_proc(self):
        """kill_active_test_for_account must report handled (True) and set the flag on an
        account match even with no proc registered - the early return it used to take is
        what let the tester continue into Popen."""
        with channel_tester._lock:
            channel_tester._state.is_running = True
            channel_tester._state.active_test_account_id = self.account_id
            channel_tester._state.active_test_proc = None

        self.assertTrue(channel_tester.kill_active_test_for_account(self.t.app, self.account_id))
        self.assertTrue(channel_tester._is_preempted())

    def test_kill_helper_ignores_other_accounts(self):
        """A different account's recording must not preempt this test."""
        with channel_tester._lock:
            channel_tester._state.is_running = True
            channel_tester._state.active_test_account_id = self.account_id
            channel_tester._state.active_test_proc = None

        self.assertFalse(channel_tester.kill_active_test_for_account(self.t.app, self.account_id + 999))
        self.assertFalse(channel_tester._is_preempted())

    def test_account_registered_at_acquire_not_at_popen(self):
        """The registration must be visible before any Popen happens - this is the field
        kill_active_test_for_account() matches on."""
        seen = {}

        def _popen_records_state(*a, **kw):
            with channel_tester._lock:
                seen['account_id_at_popen'] = channel_tester._state.active_test_account_id
            raise OSError('stop the test here, the assertion is above')

        with mock.patch.object(channel_tester.subprocess, 'Popen', _popen_records_state):
            channel_tester.run_channel_test(self.t.app, self.channel_id)

        self.assertEqual(seen.get('account_id_at_popen'), self.account_id)

    # ── normal (non-window) preemption still works ────────────────────────────

    def test_registered_proc_is_killed_and_reported(self):
        """The pre-existing happy path: proc registered, then a recording preempts."""
        procs = []

        def _popen(*a, **kw):
            proc = FakeProc()
            procs.append(proc)
            return proc

        def _wait_for_file_data(*a, **kw):
            # Called after the proc is registered - preempt here, then report "no data"
            # so the connect loop takes its failure branch.
            self._preempt_as_recording()
            return False

        with mock.patch.object(channel_tester.subprocess, 'Popen', _popen), \
             mock.patch.object(channel_tester, '_drain_stderr', lambda *a, **kw: None), \
             mock.patch.object(channel_tester, '_interruptible_sleep', lambda _d: None), \
             mock.patch.object(channel_tester, 'wait_for_file_data', _wait_for_file_data):
            channel_tester.run_channel_test(self.t.app, self.channel_id)

        self.assertTrue(procs[0].signalled)
        self.assertEqual(self._holders(), [('recording', self.recording_id)])
        row = self._latest_test_row()
        self.assertEqual(row.status, 'CANCELLED')
        self.assertIn('Interrupted by recording start', row.error_detail or '')

    def test_slot_released_and_account_cleared_after_run(self):
        """The finally must clear the registration it added at acquire time, or the next
        preemption matches a test that is no longer running."""
        with mock.patch.object(channel_tester.subprocess, 'Popen',
                               mock.Mock(side_effect=OSError('nope'))):
            channel_tester.run_channel_test(self.t.app, self.channel_id)

        with channel_tester._lock:
            self.assertIsNone(channel_tester._state.active_test_account_id)
            self.assertIsNone(channel_tester._state.active_test_proc)
        self.assertEqual(self._holders(), [])


class PreemptedTestScoringTests(unittest.TestCase):
    """Tier 2 - a preempted test must not be scored as a channel failure.

    Guards BUGS.md 2026-07-20 (preempted channel tests finalized FAILED, so
    score_test_quality applied the fail floor and a recording starting on a busy account
    tanked the health score of a channel nothing was wrong with).
    DESIGN-prerecord-checks.md section 8; the invariant is that a CANCELLED test is
    excluded from scoring entirely and leaves the channel's health score byte-identical.
    """

    def setUp(self):
        self.t = make_test_app()
        with self.t.app.app_context():
            acct = seed.make_account(name='Scoring Acct')
            ch = seed.make_channel(acct, name='Scoring Channel')
            ch.health_score = 87.0
            ch.health_score_sample_count = 4
            db.session.commit()
            self.channel_id = ch.id

    def tearDown(self):
        self.t.cleanup()

    def _score_state(self):
        with self.t.app.app_context():
            db.session.expire_all()
            ch = db.session.get(Channel, self.channel_id)
            return (ch.health_score, ch.health_score_sample_count)

    def test_cancelled_test_is_not_scored(self):
        """score_test_quality returns None for CANCELLED - it must never reach the
        fail-floor branch that FAILED takes.

        CHARACTERIZATION, not a regression guard: score_test_quality already excluded
        non-COMPLETED/non-FAILED rows before the fix, so this passes with the fix
        reverted. The half that was broken was that nothing ever *wrote* CANCELLED - the
        guard for that is TesterPreemptionRaceTests, whose row.status assertions do fail
        without the fix. This test pins the other half of the contract so a later change
        to the scoring branch cannot silently re-score aborted tests."""
        with self.t.app.app_context():
            ch = db.session.get(Channel, self.channel_id)
            test = seed.make_channel_test(ch, status='CANCELLED')
            db.session.commit()
            cfg = load_config()
            self.assertIsNone(score_test_quality(test, cfg))

    def test_failed_test_is_still_scored_at_the_fail_floor(self):
        """Control: the fail floor must still apply to a genuine FAILED test, or this
        change would have disabled failure scoring rather than narrowed it."""
        with self.t.app.app_context():
            ch = db.session.get(Channel, self.channel_id)
            test = seed.make_channel_test(ch, status='FAILED')
            db.session.commit()
            result = score_test_quality(test, load_config())
            self.assertIsNotNone(result)
            score, breakdown = result
            self.assertTrue(breakdown['fail_floor_applied'])

    def test_cancelled_test_leaves_channel_health_score_untouched(self):
        """The end-to-end invariant: blending a CANCELLED observation is a no-op.

        Also characterization (same reason as test_cancelled_test_is_not_scored) - it
        seeds the CANCELLED row directly rather than driving the tester. Its paired
        control below proves the assertion can detect a score change at all."""
        before = self._score_state()
        with self.t.app.app_context():
            ch = db.session.get(Channel, self.channel_id)
            test = seed.make_channel_test(ch, status='CANCELLED')
            db.session.commit()
            test_id = test.id
        apply_test_health_observation(self.t.app, test_id)
        self.assertEqual(self._score_state(), before)

    def test_failed_test_does_move_the_channel_health_score(self):
        """Control for the test above - proves the harness would have detected a change."""
        before = self._score_state()
        with self.t.app.app_context():
            ch = db.session.get(Channel, self.channel_id)
            test = seed.make_channel_test(ch, status='FAILED')
            db.session.commit()
            test_id = test.id
        apply_test_health_observation(self.t.app, test_id)
        self.assertNotEqual(self._score_state(), before)

    def test_status_label_names_cancelled_explicitly(self):
        """Every display surface derives its label from _test_status_label; a CANCELLED
        row must not render as FAIL there (CLAUDE.md: states are enumerated)."""
        with self.t.app.app_context():
            ch = db.session.get(Channel, self.channel_id)
            cancelled = seed.make_channel_test(ch, status='CANCELLED')
            failed = seed.make_channel_test(ch, status='FAILED')
            db.session.commit()
            self.assertEqual(_test_status_label(cancelled), 'CANCELLED')
            self.assertEqual(_test_status_label(failed), 'FAIL')

    def test_tally_excludes_cancelled_from_tested_count(self):
        """pass + warn + fail must equal tested_count, or the health bar under-fills."""
        with self.t.app.app_context():
            ch = db.session.get(Channel, self.channel_id)
            tests = [
                seed.make_channel_test(ch, status='COMPLETED'),
                seed.make_channel_test(ch, status='FAILED'),
                seed.make_channel_test(ch, status='CANCELLED'),
            ]
            db.session.commit()
            counts = _tally(tests)
            self.assertEqual(counts['tested_count'], 2)
            self.assertEqual(
                counts['pass_count'] + counts['warn_count'] + counts['fail_count'],
                counts['tested_count'])


class ConsecutiveFailureStreakTests(unittest.TestCase):
    """dev/changelog/478 - Channel.consecutive_test_failures, maintained by
    apply_test_health_observation alongside the health_score blend it already commits.
    A distinct signal from health_score: a channel with a good prior history can stay
    well above the failing band through weeks of hard failures (the real case:
    channel 9083 sat at effective score 46.7 after 17 straight days FAILED)."""

    def setUp(self):
        self.t = make_test_app()
        with self.t.app.app_context():
            acct = seed.make_account(name='Streak Acct')
            ch = seed.make_channel(acct, name='Streak Channel', health_score=90)
            db.session.commit()
            self.channel_id = ch.id

    def tearDown(self):
        self.t.cleanup()

    def _streak(self):
        with self.t.app.app_context():
            db.session.expire_all()
            return db.session.get(Channel, self.channel_id).consecutive_test_failures

    def _observe(self, status, error_detail='no data received'):
        with self.t.app.app_context():
            ch = db.session.get(Channel, self.channel_id)
            test = seed.make_channel_test(ch, status=status, error_detail=error_detail)
            db.session.commit()
            test_id = test.id
        apply_test_health_observation(self.t.app, test_id)

    def test_starts_at_zero(self):
        self.assertEqual(self._streak(), 0)

    def test_failed_test_increments(self):
        self._observe('FAILED')
        self.assertEqual(self._streak(), 1)
        self._observe('FAILED')
        self.assertEqual(self._streak(), 2)
        self._observe('FAILED')
        self.assertEqual(self._streak(), 3)

    def test_completed_test_resets_to_zero(self):
        self._observe('FAILED')
        self._observe('FAILED')
        self.assertEqual(self._streak(), 2)
        self._observe('COMPLETED', error_detail=None)
        self.assertEqual(self._streak(), 0)

    def test_cancelled_test_neither_extends_nor_resets(self):
        self._observe('FAILED')
        self._observe('FAILED')
        self.assertEqual(self._streak(), 2)
        self._observe('CANCELLED', error_detail='Interrupted by recording start')
        self.assertEqual(self._streak(), 2, 'a CANCELLED test is not the channel\'s '
                         'fault - same exclusion as score_test_quality')
        self._observe('FAILED')
        self.assertEqual(self._streak(), 3, 'the streak must resume from where it left '
                         'off, not restart at 1')


if __name__ == '__main__':
    unittest.main()
