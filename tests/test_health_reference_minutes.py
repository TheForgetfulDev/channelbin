"""`channel_testing.reference_minutes` tracks the default health-check duration.

The reference is the observation length that carries weight 1.0 in
app/health_score.py::observation_weight. It was set to 2 minutes when a health check ran
120s; the test default later became 30s and nothing moved the reference, so every default
check carried sqrt(0.5/2) = half weight and the score responded to everything at half
speed. See dev/changelog/1016 and dev/docs/BUGS.md 2026-09-17.

The reference is a SPEED knob and not a balance one - it divides every observation's
duration alike, so it cancels out of any comparison between two observations. That property
is what makes "follow the test duration" a safe answer rather than one that quietly
re-weights recordings against checks, so it is asserted here directly.
"""
import os
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config as config_mod  # noqa: E402
from app.config import config_default  # noqa: E402
from app.health_score import observation_weight  # noqa: E402
from tests.support.config_sandbox import ConfigSandbox  # noqa: E402


class ReferenceFollowsTestDurationTests(unittest.TestCase):
    """The default itself, read the way production reads it (merged config, no overrides)."""

    def test_one_default_length_health_check_is_a_full_weight_observation(self):
        duration = config_default('channel_testing.test_duration_seconds')
        self.assertAlmostEqual(
            observation_weight(duration, {}), 1.0, places=6,
            msg='a default-length health check is not a full-weight data point, so every '
                'check moves the health score less than one observation and the score '
                'responds to a channel going bad more slowly than intended')

    def test_the_reference_equals_the_default_test_duration(self):
        self.assertAlmostEqual(
            config_default('channel_testing.reference_minutes'),
            config_default('channel_testing.test_duration_seconds') / 60.0, places=6,
            msg='the two defaults have drifted apart again - that drift is the defect')


class ReferenceIsASpeedKnobTests(unittest.TestCase):
    """Characterization, not a regression guard: these assert the property the change was
    argued from, and they pass against the old default too. They exist so a later retune
    cannot quietly turn the reference into a source-balance knob without a red test."""

    def _weight(self, seconds, ref):
        return observation_weight(seconds, {'channel_testing': {'reference_minutes': ref}})

    def test_the_reference_cancels_out_of_the_ratio_between_two_observations(self):
        for ref in (0.5, 2, 7.5):
            self.assertAlmostEqual(
                self._weight(3600, ref) / self._weight(30, ref),
                (3600 / 30.0) ** 0.5, places=6,
                msg=f'at reference_minutes={ref} a 1-hour recording stopped being worth a '
                    'fixed number of 30s checks, so the reference has become a '
                    'test-vs-recording balance knob and retuning it silently re-weights '
                    'the two sources against each other')

    def test_quartering_the_reference_doubles_every_weight(self):
        for seconds in (5, 30, 120, 3600):
            self.assertAlmostEqual(self._weight(seconds, 0.5),
                                   self._weight(seconds, 2) * 2, places=6)


class ReferenceMinutesMigrationTests(unittest.TestCase):
    """The transform in isolation. `2` was the old default and so was never a choice; any
    other stored value was deliberate and survives."""

    def _migrate(self, channel_testing):
        from app.config import _cfg_m007_reference_minutes_follows_test_duration as fn
        cfg = {'channel_testing': dict(channel_testing)} if channel_testing is not None else {}
        return fn(cfg)

    def test_the_old_default_is_dropped_so_the_new_one_applies(self):
        out = self._migrate({'reference_minutes': 2, 'test_duration_seconds': 30})
        self.assertNotIn('reference_minutes', out['channel_testing'],
                         'an existing install keeps the 120s-era reference forever and its '
                         'health scores keep reacting at half speed')
        self.assertEqual(out['channel_testing']['test_duration_seconds'], 30,
                         'the migration disturbed a setting it has no business touching')

    def test_a_hand_tuned_value_is_left_alone(self):
        out = self._migrate({'reference_minutes': 7.5})
        self.assertEqual(out['channel_testing']['reference_minutes'], 7.5,
                         'a deliberate choice was overwritten by a migration')

    def test_an_install_that_never_stored_the_key_is_untouched(self):
        self.assertEqual(self._migrate({'test_duration_seconds': 30}),
                         {'channel_testing': {'test_duration_seconds': 30}})

    def test_a_config_with_no_channel_testing_section_does_not_crash(self):
        self.assertEqual(self._migrate(None), {})

    def test_it_is_registered_so_it_actually_runs(self):
        from app.config import (CONFIG_MIGRATIONS, CURRENT_CONFIG_VERSION,
                                _cfg_m007_reference_minutes_follows_test_duration as fn)
        self.assertIn(fn, [f for _v, _d, f in CONFIG_MIGRATIONS],
                      'the transform exists but nothing runs it, so no config is migrated')
        self.assertEqual(CURRENT_CONFIG_VERSION, CONFIG_MIGRATIONS[-1][0])


class ReferenceMinutesMigrationEndToEndTests(ConfigSandbox):
    """Through migrate_config() against a real file - the case that matters is the install
    already running with `reference_minutes: 2` written out by an older build."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.backup_dir = tempfile.mkdtemp(prefix='cb-cfgbackup-')

    def _run(self):
        config_mod.migrate_config(
            config_overrides={'config_backup': {'backup_dir': self.backup_dir}})
        with open(config_mod._CONFIG_PATH) as fh:  # direct-config-read: asserting on stored bytes
            return yaml.safe_load(fh)

    def test_an_existing_install_loses_the_stale_reference_on_startup(self):
        from app.config import (CONFIG_MIGRATIONS, CURRENT_CONFIG_VERSION,
                                _cfg_m007_reference_minutes_follows_test_duration as fn)
        # Pinned to this migration's own version rather than CURRENT-1, which stops
        # exercising it the moment a later migration is added.
        before = next(v for v, _d, f in CONFIG_MIGRATIONS if f is fn) - 1
        self._write_cfg({'config_version': before,
                         'channel_testing': {'reference_minutes': 2,
                                             'test_duration_seconds': 30,
                                             'health_score_half_life_samples': 5}})
        written = self._run()
        self.assertNotIn('reference_minutes', written['channel_testing'])
        self.assertEqual(written['channel_testing']['health_score_half_life_samples'], 5,
                         'the migration disturbed a neighbouring setting')
        self.assertEqual(written['config_version'], CURRENT_CONFIG_VERSION,
                         'the stamp did not move, so this migration would run again forever')

    def test_choosing_two_again_afterwards_sticks(self):
        """Why this is a migration and not a value computed at read time: once the stamp is
        current, a user who deliberately sets 2 keeps it. A read-time rule could not tell
        "never set" from "chosen" and would strip it on every boot."""
        from app.config import CURRENT_CONFIG_VERSION
        self._write_cfg({'config_version': CURRENT_CONFIG_VERSION,
                         'channel_testing': {'reference_minutes': 2}})
        self.assertEqual(self._run()['channel_testing']['reference_minutes'], 2)


if __name__ == '__main__':
    unittest.main()
