"""Tier 2 - a conversion that is still advancing is never killed (dev/changelog/865).

Guards the replacement of `run_conversion_supervised`'s whole-job deadline with two rules:
a bounded budget before ffmpeg muxes its first output frame, and after that liveness on
`out_time` alone with no upper bound.

The defect: `post_process.timeout_seconds` (1800) / `reencode_timeout_seconds` (14400)
killed ffmpeg on the clock regardless of progress, and the retry re-ran the identical
command from 0%. Recording 2 (6.6 GB / 12,949s of 1080p59.94) died at elapsed 14,404s
against the 14,400s number - calibrated on a machine encoding at ~1.0x realtime, where this
one manages ~0.5x - and attempt 2 was pacing 6.8h when it was cancelled by hand.

Covers, in order:
  - AdvancingConversionTests: the runner's two rules, including the latch that keeps a
    transiently-unreadable progress file from re-arming the pre-output budget.
  - StallDetectionDisabledTests: the wall clock survives as a fallback ONLY when
    stall_seconds is 0, because nothing else is then watching liveness.
  - ConfigMigrationTests: config_version 3 drops both retired keys.
  - SettingsClampTests: the new key's floor of 1 is enforced server-side.

No real ffmpeg - a fake Popen plus a controlled progress reader. See CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_conversion_progress_liveness
"""
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
import app.proc_utils as pumod  # noqa: E402
from app import db  # noqa: E402
from app.config import _cfg_m003_conversion_pre_output_timeout  # noqa: E402
from app.postprocessor import (  # noqa: E402
    run_conversion_supervised, _active_conversions, _active_lock,
)
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.test_conversion_monitor import _FakePopen  # noqa: E402


class _RunnerCase(unittest.TestCase):
    """Shared harness: one CONVERTING recording and a supervised run whose progress
    readings the test supplies."""

    def setUp(self):
        self.t = make_test_app()
        self.rec = seed.make_recording(status='CONVERTING', name='liveness')
        db.session.commit()
        self.rid = self.rec.id
        self.out = os.path.join(self.t._tmpdir, f'out_{self.rid}.mkv')
        self.cmd = ['/usr/bin/ffmpeg', '-i', 'src.ts', '-c', 'copy', '-y', self.out]

    def tearDown(self):
        with _active_lock:
            _active_conversions.clear()
            ppmod._cancel_requested.clear()
        self.t.cleanup()

    def _run(self, fake, progress_return, *, stall_seconds, pre_output_timeout,
             interval=0.05):
        with mock.patch.object(pumod.subprocess, 'Popen', return_value=fake), \
             mock.patch.object(pumod, 'read_progress_tail', side_effect=progress_return):
            return run_conversion_supervised(
                self.t.app, self.rid, self.cmd, self.out,
                expected_duration=100, pre_output_timeout=pre_output_timeout,
                interval=interval, stall_seconds=stall_seconds)


class AdvancingConversionTests(_RunnerCase):
    def test_advancing_conversion_outlives_the_pre_output_budget(self):
        """The whole point: a job longer than any fixed budget, still advancing, finishes.

        Under the old rule this same run was killed with reason 'timeout' the moment wall
        time passed the deadline, and the restart loop then re-ran it from zero.
        """
        ticks = iter(range(1, 500))

        def progress(_path):
            # out_time climbing steadily, the whole way past pre_output_timeout.
            return next(ticks) * 1_000_000, 4096, False

        fake = _FakePopen(alive_ticks=12, returncode=0)
        result = self._run(fake, progress, stall_seconds=30, pre_output_timeout=0.2)

        self.assertTrue(result.success,
                        f'an advancing conversion was killed: {result.reason} / {result.error_msg}')
        self.assertEqual(result.reason, 'success')
        self.assertFalse(fake.terminated,
                         'a conversion that never stopped advancing was terminated')

    def test_never_producing_output_is_still_killed(self):
        """The pre-output budget is a real rule, not a disabled one. Stall detection cannot
        see this case - it is gated on out_time > 0 precisely because a damaged source makes
        ffmpeg analyze for minutes before muxing (dev/docs/BUGS.md 2026-07-24)."""
        fake = _FakePopen(alive_ticks=None)
        result = self._run(fake, lambda p: (0, 0, False),
                           stall_seconds=30, pre_output_timeout=0.3)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'no_output')
        self.assertTrue(fake.terminated, 'a conversion producing nothing was not killed')

    def test_output_start_latches_against_an_unreadable_progress_file(self):
        """ffmpeg truncates and rewrites its -progress file, so a poll can legitimately read
        nothing back mid-write. Deciding "has output started" per-poll would let one of those
        empty reads re-arm the pre-output budget and kill a conversion that is minutes into
        producing output."""
        reads = iter([(2_000_000, 4096, False)] + [(None, None, False)] * 400)

        fake = _FakePopen(alive_ticks=10, returncode=0)
        result = self._run(fake, lambda p: next(reads),
                           stall_seconds=30, pre_output_timeout=0.15)

        self.assertNotEqual(result.reason, 'no_output',
                            'a transiently unreadable progress file re-armed the pre-output '
                            'budget after output had already started')
        self.assertTrue(result.success)

    def test_frozen_output_is_still_killed_as_a_stall(self):
        """Removing the deadline must not remove the other rule: once output has started and
        then stops, stall_seconds is the authority and still fires."""
        fake = _FakePopen(alive_ticks=None)
        result = self._run(fake, lambda p: (5_000_000, 4096, False),
                           stall_seconds=0.3, pre_output_timeout=600)

        self.assertEqual(result.reason, 'stalled')
        self.assertTrue(fake.terminated)


class StallDetectionDisabledTests(_RunnerCase):
    """stall_seconds: 0 leaves nothing watching liveness, so the wall clock stands in - the
    same fallback run_probe_until_stalled keeps for when /proc offers no progress signal.
    Characterization of a deliberately preserved behavior, not a regression guard: the old
    code killed this run too, on the same clock."""

    def test_wall_clock_applies_when_stall_detection_is_off(self):
        ticks = iter(range(1, 500))
        fake = _FakePopen(alive_ticks=None)
        result = self._run(fake, lambda p: (next(ticks) * 1_000_000, 4096, False),
                           stall_seconds=0, pre_output_timeout=0.3)

        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'timeout')
        self.assertIn('stall detection disabled', result.error_msg)


class ConfigMigrationTests(unittest.TestCase):
    """config_version 3: both whole-job deadlines are dropped rather than carried across.

    Neither answered the question the app now asks, and reusing one would turn a
    deliberately generous whole-job number into an absurd pre-output budget - the config
    this was written against carried a hand-raised timeout_seconds of 90800.
    """

    def test_both_retired_keys_are_dropped(self):
        cfg = {'recording': {'post_process': {
            'timeout_seconds': 90800, 'reencode_timeout_seconds': 14400,
            'stall_seconds': 300}}}
        with self.assertLogs('app.config', level='WARNING'):
            out = _cfg_m003_conversion_pre_output_timeout(cfg)
        pp = out['recording']['post_process']
        self.assertNotIn('timeout_seconds', pp)
        self.assertNotIn('reencode_timeout_seconds', pp)
        self.assertEqual(pp['stall_seconds'], 300, 'migration touched an unrelated key')

    def test_no_new_key_is_written(self):
        """The new key is left to its default rather than being materialized with a number
        derived from a value that meant something else."""
        cfg = {'recording': {'post_process': {'timeout_seconds': 1800}}}
        with self.assertLogs('app.config', level='WARNING'):
            out = _cfg_m003_conversion_pre_output_timeout(cfg)
        self.assertNotIn('pre_output_timeout_seconds', out['recording']['post_process'])

    def test_config_without_the_keys_is_untouched_and_silent(self):
        cfg = {'recording': {'post_process': {'stall_seconds': 300}}}
        with mock.patch.object(cfgmod.log, 'warning') as warn:
            out = _cfg_m003_conversion_pre_output_timeout(cfg)
        self.assertEqual(out['recording']['post_process'], {'stall_seconds': 300})
        warn.assert_not_called()

    def test_missing_section_is_survivable(self):
        self.assertEqual(_cfg_m003_conversion_pre_output_timeout({}), {})
        self.assertEqual(_cfg_m003_conversion_pre_output_timeout({'recording': {}}),
                         {'recording': {}})


class SettingsClampTests(unittest.TestCase):
    """The new key's floor is 1, not 0: 0 would kill every conversion on its first poll,
    before ffmpeg could possibly have muxed anything. Enforced server-side, per CLAUDE.md."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_zero_is_clamped_up_to_one(self):
        page = self.t.client.get('/settings').get_data(as_text=True)
        tok = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        with mock.patch('app.routes.settings.save_config', return_value=[]) as save, \
             mock.patch('app.routes.settings.load_for_edit',
                        return_value=({'recording': {'post_process': {}}},
                                      {'recording': {'post_process': {}}})):
            r = self.t.client.post('/api/settings/field', json={
                'path': 'recording.post_process.pre_output_timeout_seconds', 'value': 0},
                headers={'X-CSRFToken': tok})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        saved = save.call_args[0][0]
        self.assertEqual(saved['recording']['post_process']['pre_output_timeout_seconds'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
