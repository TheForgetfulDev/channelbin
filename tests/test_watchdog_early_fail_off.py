"""Tier 2 - watchdog.early_fail_abort_count: 0 turns the dead-stream trip-wire off.

Guards dev/docs/BUGS.md 2026-10-20 "early_fail_abort_count has no 0 = off": the two sibling
trip-wires (stall_move_count, dead_stream_max_retry_attempts) treat 0 as disabled, but the
watchdog compared `len(early_fail_times) >= early_fail_abort_count` with no guard, so a 0
aborted the recording on the very first early failure - the opposite of what the page's
sibling fields mean by it (dev/changelog/1242).

No network and no provider host: the capture stand-in is `sys.executable -c ...`.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.recorder as recorder  # noqa: E402
from app import db  # noqa: E402
from app.database import RESTART_ATTEMPTED, Recording, RecordingEvent  # noqa: E402
from app.watchdog import WatchdogThread  # noqa: E402
from tests.support.watchdog import RestartHarness  # noqa: E402


class ZeroMeansOffTests(RestartHarness):

    def _cfg(self, stall_timeout, restart_delay=0):
        # Every exited capture here is an early failure by the window and byte rules, and
        # the count is 0: off, not "abort at once".
        return cfgmod._deep_merge(super()._cfg(stall_timeout, restart_delay), {'watchdog': {
            'early_fail_abort_count': 0,
            'early_fail_window_seconds': 600,
            'early_fail_min_bytes': 10 ** 9,
            'dead_stream_max_retry_attempts': 10,
        }})

    def test_an_early_failure_under_a_zero_count_restarts_like_any_other_stall(self):
        self._exited_capture()
        with mock.patch.object(cfgmod, 'load_config', return_value=self._cfg(5)), \
             mock.patch.object(recorder, '_launch_segment',
                               self._stub_launch_next(produces_data=True)):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + 30
            restarted = False
            status = None
            while time.monotonic() < deadline:
                db.session.expire_all()
                status = db.session.get(Recording, self.rid).status
                restarted = bool(RecordingEvent.query.filter_by(
                    recording_id=self.rid, event_type=RESTART_ATTEMPTED).first())
                if restarted or status != 'IN_PROGRESS':
                    break
                time.sleep(0.1)
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        self.assertEqual(status, 'IN_PROGRESS',
                         'a count of 0 tripped the dead-stream abort on the first failure')
        self.assertTrue(restarted, 'the ordinary restart never happened')


if __name__ == '__main__':
    unittest.main()
