"""The capture tier is out of the default suite on purpose, by name, and its own runner can
still find every scenario (DESIGN-simulator.md §4, D3; dev/changelog/1223).

`run_tests.sh` treats a module that lands in no shard as an error, which is what keeps the
suite from quietly shrinking. The capture tier is the one deliberate exception, and these tests
are what make it deliberate: the default discovery reads `tests/test_*.py`, a scenario is named
`scenario_*.py`, and nothing named `test_*.py` may live beside the scenarios, where the `-j 1`
discover would collect it and the sharded listing would not.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import timing  # noqa: E402

CAPTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'capture')


class CaptureTierLayoutTests(unittest.TestCase):
    def test_no_default_suite_module_lives_in_the_capture_folder(self):
        stray = [f for f in os.listdir(CAPTURE_DIR) if f.startswith('test_')]
        self.assertEqual(stray, [], 'name a capture-tier module scenario_*.py, or move it '
                                    'to tests/ if it belongs in the default suite')

    def test_the_sharded_listing_collects_no_scenario(self):
        self.assertFalse([m for m in timing._discover_modules() if '.capture.' in m])
        # The -j 1 path is a loader.discover over tests/ with this pattern, and the test
        # above keeps every file under tests/capture/ from matching it.
        with open(timing.__file__) as f:
            self.assertIn("pattern='test_*.py'", f.read())

    def test_the_capture_runner_finds_every_scenario(self):
        on_disk = sorted(f[:-3] for f in os.listdir(CAPTURE_DIR)
                         if f.startswith('scenario_') and f.endswith('.py'))
        self.assertTrue(on_disk)
        self.assertEqual([m.rsplit('.', 1)[1] for m in timing._discover_capture_modules()],
                         on_disk)

    def test_every_scenario_module_holds_a_test(self):
        loader = unittest.TestLoader()
        for module in timing._discover_capture_modules():
            with self.subTest(module=module):
                self.assertGreater(loader.loadTestsFromName(module).countTestCases(), 0)

    def test_the_capture_tier_keeps_its_own_history(self):
        self.assertNotEqual(timing.CAPTURE_HISTORY_PATH, timing.HISTORY_PATH)


if __name__ == '__main__':
    unittest.main()
