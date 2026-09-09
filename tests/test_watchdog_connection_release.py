"""Tier 2 - a watchdog give-up must release the account's connection slot.

Guards dev/docs/BUGS.md 2026-08-04 "The watchdog leaks the account's connection slot on
every give-up path". Design and reasoning: dev/changelog/436 (the original observation),
dev/changelog/464.

The defect was an omission, not a wrong branch: `_mark_recording_failed` kills ffmpeg and
pops `_active`, but never called `connlim.release`. The only two existing release sites
(`recorder.py::_teardown_active_ffmpeg` and its failover path) are never reached from a
watchdog give-up, so the account's slot accounting believed the dead recording still held
its connection forever - proven on live data: recording 73 acquired account 1's only slot,
was aborted 15s later, and 3+ hours later a different recording on the same account still
found the slot "held by other recording(s)".

No network and no provider host: every child here is `sys.executable -c ...`, a local argv
with no URL in it, which tests/support/netguard.py permits. `persist_final_thumbnail` is
stubbed out - it is best-effort production code that reads `load_config()`'s real
`/dvr/live_thumbnails` default at runtime, which is exactly the test-sandbox-escape shape
CLAUDE.md's testing rules warn about, and it is not what these tests assert on.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.recorder as recorder  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    RECORDING_FAILED, RECORDING_FAILED_DEAD_STREAM, REC_STATUS_FAILED, Recording,
)
from tests.support import seed  # noqa: E402
from tests.test_downtime_accounting import _RestartHarness, _sleeper  # noqa: E402


class _ConnectionReleaseHarness(_RestartHarness):
    """Same rig as the downtime/segment-teardown suites, plus a real account+channel
    (so `rec.channel.account_id` resolves) with a slot already registered the way
    `_try_acquire_slot_with_preemption` would have when the recording started."""

    # Per-test overrides merged into the base watchdog config (max_consecutive_failures /
    # early_fail_abort_count default to the base harness's 99 - tests that want a
    # give-up to actually fire override the relevant one here).
    _extra_watchdog_cfg = {}

    def setUp(self):
        super().setUp()
        acct = seed.make_account(name='One Slot', max_connections=1)
        ch = seed.make_channel(acct, name='Give-up Channel')
        db.session.commit()
        self.account_id = acct.id
        rec = db.session.get(Recording, self.rid)
        rec.channel_id = ch.id
        db.session.commit()

        connlim._holders.clear()
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', self.rid),
                        'setup could not acquire the slot the give-up is supposed to release')

        self._extra_watchdog_cfg = {}
        self._thumb_patcher = mock.patch.object(recorder, 'persist_final_thumbnail')
        self._thumb_patcher.start()

    def tearDown(self):
        self._thumb_patcher.stop()
        connlim._holders.clear()
        super().tearDown()

    def _cfg(self, stall_timeout, restart_delay=0):
        cfg = super()._cfg(stall_timeout, restart_delay)
        cfg['watchdog'].update(self._extra_watchdog_cfg)
        return cfg

    def _holders(self):
        return list(connlim._holders.get(self.account_id, []))


class MaxConsecutiveFailuresGiveUpReleasesSlotTests(_ConnectionReleaseHarness):

    def test_slot_is_released_when_the_watchdog_gives_up_with_no_group_to_fail_over_to(self):
        self._extra_watchdog_cfg = {'max_consecutive_failures': 1}
        self.state.process = _sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=False):
            self._run_until_event(RECORDING_FAILED, stall_timeout=1, restart_delay=0,
                                  produces_data=False)

        self.assertEqual(
            self._holders(), [],
            'the give-up left the dead recording as the account\'s connection holder')
        # The concrete real-world consequence (recording 73 -> 75): a fresh holder on the
        # same account must be able to acquire the now-free slot.
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', 99999),
                        'a later recording still could not get the slot the dead one leaked')


class DeadStreamGiveUpReleasesSlotTests(_ConnectionReleaseHarness):

    def test_slot_is_released_on_a_dead_stream_fast_fail_with_no_group_to_fail_over_to(self):
        # dead_stream_max_retry_attempts=0 keeps this test on the immediate give-up path it
        # was written to guard - the retry path (watchdog.dead_stream_max_retry_attempts > 0)
        # is exercised separately in tests/test_dead_stream_retry.py.
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1, 'dead_stream_max_retry_attempts': 0}
        self.state.process = _sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=False):
            self._run_until_event(RECORDING_FAILED_DEAD_STREAM, stall_timeout=1,
                                  restart_delay=0, produces_data=False)

        self.assertEqual(
            self._holders(), [],
            'the dead-stream give-up left the dead recording as the account\'s connection '
            'holder')


class ChannelLessRecordingGiveUpTests(_RestartHarness):
    """Guards dev/docs/BUGS.md 2026-08-15 "Watchdog give-up crashes on channel-less
    (manual URL) recordings". A manual URL-only recording has no channel, so the
    unconditional `connlim.release(rec.channel.account_id, ...)` at the end of both
    give-up paths raised AttributeError, killing the watchdog thread before it could
    reach the health-score observation and final-thumbnail persist that `_give_up`
    runs immediately after. `persist_final_thumbnail` only gets called if `fail_fn()`
    (the crash site) returned cleanly, so asserting it ran is the proof the give-up
    path completed rather than just that the terminal event was written (that commit
    happens inside `fail_fn()`, before the old crash point).

    Deliberately does NOT assign a channel to `self.rid` - `_WatchdogHarness.setUp`
    (the base rig) already seeds a channel-less recording by default.
    """

    def setUp(self):
        super().setUp()
        self._extra_watchdog_cfg = {}
        self._thumb_patcher = mock.patch.object(recorder, 'persist_final_thumbnail')
        self._thumb_mock = self._thumb_patcher.start()

    def tearDown(self):
        self._thumb_patcher.stop()
        super().tearDown()

    def _cfg(self, stall_timeout, restart_delay=0):
        cfg = super()._cfg(stall_timeout, restart_delay)
        cfg['watchdog'].update(self._extra_watchdog_cfg)
        return cfg

    def test_max_consecutive_failures_give_up_completes_without_channel(self):
        self.assertIsNone(db.session.get(Recording, self.rid).channel_id,
                          'harness precondition: this recording must be channel-less')
        self._extra_watchdog_cfg = {'max_consecutive_failures': 1}
        self.state.process = _sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=False):
            rec = self._run_until_event(RECORDING_FAILED, stall_timeout=1, restart_delay=0,
                                        produces_data=False)

        self.assertEqual(rec.status, REC_STATUS_FAILED)
        self._thumb_mock.assert_called_once_with(self.rid)

    def test_dead_stream_give_up_completes_without_channel(self):
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1, 'dead_stream_max_retry_attempts': 0}
        self.state.process = _sleeper()
        with mock.patch.object(recorder, 'failover_group_member', return_value=False):
            rec = self._run_until_event(RECORDING_FAILED_DEAD_STREAM, stall_timeout=1,
                                        restart_delay=0, produces_data=False)

        self.assertEqual(rec.status, REC_STATUS_FAILED)
        self._thumb_mock.assert_called_once_with(self.rid)


if __name__ == '__main__':
    unittest.main(verbosity=2)
