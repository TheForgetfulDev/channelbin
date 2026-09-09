"""Tier 2 - a watchdog give-up names the reason ffmpeg actually gave, not just its shape.

Guards dev/docs/BUGS.md 2026-08-04 "A dead-stream abort names no cause, even though ffmpeg's
own reason is stored one row away". Design and reasoning: dev/changelog/436 (the original
observation, recording 73's "HTTP error 405 Method Not Allowed"), dev/changelog/465.

The defect was two-layered, not a simple omission. `_mark_recording_failed` composed its
terminal event `detail` and its `log.error(log_msg, ...)` (which `app/__init__.py`'s
`_AlertHandler` turns into the alert a person actually reads) from generic shape-only text,
never touching the ffmpeg stderr tail that `collect_segment_diagnostics` had already captured
seconds earlier for the sibling DIAGNOSTICS event. The naive fix - reading
`self._give_up_diagnostics` inside `_mark_recording_failed` - looks right but is a no-op for
two of the three give-up call sites: the stall-handling block that precedes both the
dead-stream and max-consecutive-failures give-ups already calls `collect_segment_diagnostics`
once (for the STALL_DETECTED/DIAGNOSTICS pair), and that call is documented as idempotent -
consuming the spool - so `_give_up()`'s own second call re-reads an already-emptied spool and
returns ''. The real fix threads the first collection's `stderr_tail` through explicitly as a
`cause` parameter at those two call sites; the third call site (a give-up after a *new*
segment's restart failed) has no such prior read and correctly falls back to
`self._give_up_diagnostics`.

No network and no provider host: every child here is `sys.executable -c ...`, a local argv
with no URL in it, which tests/support/netguard.py permits. `persist_final_thumbnail` is
stubbed out for the same reason the sibling connection-release suite stubs it - it is
best-effort production code that reads `load_config()`'s real `/dvr/live_thumbnails` default
at runtime, not something these tests assert on.
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.recorder as recorder  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    RECORDING_FAILED, RECORDING_FAILED_DEAD_STREAM, Recording, RecordingEvent,
)
from tests.support import seed  # noqa: E402
from tests.test_downtime_accounting import _RestartHarness  # noqa: E402


class _FailureCauseHarness(_RestartHarness):
    """Same rig as the downtime/segment-teardown/connection-release suites, plus a
    thumbnail-persist stub so a give-up doesn't touch the real /dvr default, and a real
    account+channel (so the give-up's connlim.release(rec.channel.account_id, ...) call -
    unrelated to what these tests assert on, but reached on every give-up path - has
    something to resolve instead of raising on a bare seeded recording's null channel_id)."""

    _extra_watchdog_cfg = {}

    def setUp(self):
        super().setUp()
        acct = seed.make_account(name='Cause Test Account')
        ch = seed.make_channel(acct, name='Cause Test Channel')
        db.session.commit()
        rec = db.session.get(Recording, self.rid)
        rec.channel_id = ch.id
        db.session.commit()

        self._extra_watchdog_cfg = {}
        self._thumb_patcher = mock.patch.object(recorder, 'persist_final_thumbnail')
        self._thumb_patcher.start()

    def tearDown(self):
        self._thumb_patcher.stop()
        super().tearDown()

    def _cfg(self, stall_timeout, restart_delay=0):
        cfg = super()._cfg(stall_timeout, restart_delay)
        cfg['watchdog'].update(self._extra_watchdog_cfg)
        return cfg

    def _terminal_event(self, event_type):
        return RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=event_type
        ).order_by(RecordingEvent.id.desc()).first()

    def _exited_capture_with_stderr_file(self, code, stderr_text):
        """Same shape as _WatchdogHarness._exited_capture, but writes stderr_text straight
        to the spool file rather than passing it as a subprocess -c argv string - a URL in
        argv is exactly what tests/support/netguard.py's Popen guard exists to catch, even
        though nothing here is a real network access."""
        path, fh = recorder._open_segment_stderr_spool(self.t.app, self.rid, 0)
        fh.write(stderr_text.encode())
        fh.flush()
        self.state.stderr_path, self.state.stderr_fh = path, fh
        proc = subprocess.Popen([sys.executable, '-c', f'import sys; sys.exit({code})'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait(timeout=30)
        self.state.process = proc
        return proc


class DeadStreamAbortNamesCauseTests(_FailureCauseHarness):

    def test_dead_stream_abort_detail_quotes_the_ffmpeg_error(self):
        # dead_stream_max_retry_attempts=0: keep this test on the immediate give-up path -
        # the retry path is exercised separately in tests/test_dead_stream_retry.py.
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1, 'dead_stream_max_retry_attempts': 0}
        self._exited_capture(code=8, stderr_text='[http @ 0x1] HTTP error 405 Method Not Allowed\n')
        with mock.patch.object(recorder, 'failover_group_member', return_value=False), \
             self.assertLogs('app.watchdog', level='ERROR') as logs:
            self._run_until_event(RECORDING_FAILED_DEAD_STREAM, stall_timeout=1,
                                  restart_delay=0, produces_data=False)

        evt = self._terminal_event(RECORDING_FAILED_DEAD_STREAM)
        self.assertIn('HTTP error 405 Method Not Allowed', evt.detail,
                      f'terminal event detail named no cause: {evt.detail!r}')
        self.assertTrue(
            any('HTTP error 405 Method Not Allowed' in line for line in logs.output),
            f'the ERROR log (source of the alert body) named no cause: {logs.output!r}')

    def test_dead_stream_abort_masks_credentials_in_the_quoted_cause(self):
        # dead_stream_max_retry_attempts=0: keep this test on the immediate give-up path -
        # the retry path is exercised separately in tests/test_dead_stream_retry.py.
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1, 'dead_stream_max_retry_attempts': 0}
        self._exited_capture_with_stderr_file(
            code=8,
            stderr_text=('[http @ 0x1] Error opening input file '
                          'http://host.example/live/realuser/realpass/2077000\n'))
        with mock.patch.object(recorder, 'failover_group_member', return_value=False):
            self._run_until_event(RECORDING_FAILED_DEAD_STREAM, stall_timeout=1,
                                  restart_delay=0, produces_data=False)

        evt = self._terminal_event(RECORDING_FAILED_DEAD_STREAM)
        self.assertNotIn('realuser', evt.detail, 'credentials leaked into the terminal event')
        self.assertNotIn('realpass', evt.detail, 'credentials leaked into the terminal event')
        self.assertIn('/live/***/***/2077000', evt.detail,
                      f'expected the masked stream path, got: {evt.detail!r}')


class MaxConsecutiveFailuresAbortNamesCauseTests(_FailureCauseHarness):

    def test_max_consecutive_failures_abort_detail_quotes_the_ffmpeg_error(self):
        self._extra_watchdog_cfg = {'max_consecutive_failures': 1}
        self._exited_capture(code=8, stderr_text='[http @ 0x1] HTTP error 405 Method Not Allowed\n')
        with mock.patch.object(recorder, 'failover_group_member', return_value=False), \
             self.assertLogs('app.watchdog', level='ERROR') as logs:
            self._run_until_event(RECORDING_FAILED, stall_timeout=1, restart_delay=0,
                                  produces_data=False)

        evt = self._terminal_event(RECORDING_FAILED)
        self.assertIn('HTTP error 405 Method Not Allowed', evt.detail,
                      f'terminal event detail named no cause: {evt.detail!r}')
        self.assertTrue(
            any('HTTP error 405 Method Not Allowed' in line for line in logs.output),
            f'the ERROR log (source of the alert body) named no cause: {logs.output!r}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
