"""Tier 0 - the timing harness reports the whole run, including setUpClass.

Guards dev/docs/BUGS.md 2026-08-04 10:03 PM "suite timing harness reported a 34.6s module as
0.0s". Design and measurements: dev/changelog/468.

`_TimingResult` used to time only startTest..stopTest, so every second spent in
setUpClass/tearDownClass/setUpModule fell outside `per_module` entirely. That is not a
rounding error on this suite: tests/test_channel_search_page_js.py does ALL of its work in
one setUpClass (a node/jsdom run) and its 154 tests each report ~0.0s, so the single most
expensive module in the suite was invisible to the `[SUITE-TIMING]` movers list that exists
precisely to name what got slower.

Nothing here runs the real suite or writes tests/timing_history.jsonl - it drives
_TimingResult over a synthetic suite whose fixtures sleep known amounts.
  python3 -m unittest tests.test_suite_timing_harness
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import timing  # noqa: E402
from tests.support.timing import (  # noqa: E402
    CEILINGS_MS_PER_TEST, DRIFT_MIN_PRIOR, DRIFT_RECENT, MAX_LOG_RUNS, STATUS_PREFIX, _Tee,
    _assert_no_module_was_dropped, _charged, _cpu_snapshot, _discover_modules, _failed_ids,
    _foreign_cpu, _history_safe_ids, _module_weights, _ms_per_test, _open_log, _pack_shards,
    _passing_same_tier, _per_module, _per_test_drift, _prepare_log_dir, _pump, _status_line,
    _TimingResult,
)

FIXTURE_SLEEP = 0.30
TEST_SLEEP = 0.05


class _Expensive(unittest.TestCase):
    """Everything in setUpClass, nothing in the tests - test_channel_search_page_js's
    shape, which is the shape the old harness could not see at all."""

    @classmethod
    def setUpClass(cls):
        time.sleep(FIXTURE_SLEEP)

    def test_one(self):
        pass

    def test_two(self):
        pass


class _Cheap(unittest.TestCase):
    """The ordinary shape: no class fixture, the cost is inside the test."""

    def test_one(self):
        time.sleep(TEST_SLEEP)


def _run(*classes):
    """Drive _TimingResult over the given TestCase classes and return (result, wall)."""
    suite = unittest.TestSuite(
        unittest.TestLoader().loadTestsFromTestCase(c) for c in classes)
    runner = unittest.TextTestRunner(resultclass=_TimingResult, verbosity=0,
                                     stream=io.StringIO())
    t0 = time.perf_counter()
    result = runner.run(suite)
    return result, time.perf_counter() - t0


class FixtureTimeIsAttributedTests(unittest.TestCase):

    def test_setupclass_time_lands_in_the_fixture_bucket(self):
        """The headline defect: a class whose whole cost is setUpClass reported zero."""
        result, _ = _run(_Cheap, _Expensive)

        self.assertGreaterEqual(result.fixtures.get('_Expensive', 0.0),
                                FIXTURE_SLEEP * 0.8,
                                'setUpClass time was not charged to the class that paid it')

    def test_the_first_class_in_the_run_is_not_exempt(self):
        """The specific miss the first attempt at this still had. Anchoring the gap on the
        first stopTest instead of on startTestRun exempts whichever class runs FIRST - and
        alphabetically that is test_channel_search_page_js, the one module the fix was
        written for. Its 26s fixture would have stayed invisible."""
        result, _ = _run(_Expensive, _Cheap)

        self.assertGreaterEqual(result.fixtures.get('_Expensive', 0.0),
                                FIXTURE_SLEEP * 0.8,
                                'the run-leading class paid no fixture time')

    def test_in_test_plus_fixture_accounts_for_the_whole_run(self):
        """The invariant that makes the number trustworthy: nothing may fall between the
        two buckets, or the next expensive fixture hides in the remainder the same way."""
        result, wall = _run(_Expensive, _Cheap)

        accounted = sum(_per_module(result.timings).values()) + sum(result.fixtures.values())
        self.assertAlmostEqual(accounted, wall, delta=max(0.15, wall * 0.1),
                               msg=f'{wall - accounted:.2f}s of the run is in neither bucket')

    def test_a_cheap_class_is_not_charged_a_fixture_it_never_had(self):
        """The counterpart: the gap is real elapsed time, not a flat allowance."""
        result, _ = _run(_Cheap, _Expensive)

        self.assertLess(result.fixtures.get('_Cheap', 0.0), FIXTURE_SLEEP / 2)


class ChargedAndStatusLineTests(unittest.TestCase):
    """What the two figures are used for once recorded."""

    def test_charged_sums_the_tests_and_the_fixture_of_each_class(self):
        record = {'per_module': {'A': 1.0, 'B': 2.0}, 'per_module_fixture': {'A': 4.0}}

        self.assertEqual(_charged(record), {'A': 5.0, 'B': 2.0})

    def test_charged_reads_a_pre_2026_08_04_record_as_zero_fixture(self):
        """tests/timing_history.jsonl is committed and every record predating the
        per_module_fixture key carries none; a KeyError there would break the movers list
        on the first run after the upgrade."""
        self.assertEqual(_charged({'per_module': {'A': 1.0}}), {'A': 1.0})

    def test_a_class_with_only_fixture_time_still_appears(self):
        """test_channel_search_page_js is exactly this row - 0.0s of tests, 26s of
        setUpClass - and it must be rankable."""
        record = {'per_module': {}, 'per_module_fixture': {'JsdomTests': 26.0}}

        self.assertEqual(_charged(record), {'JsdomTests': 26.0})

    def _line(self, **over):
        # 1000 tests, not 10: the ceiling reads ms/test since dev/changelog/835, and a
        # 10-test record would be 10,000ms/test - over any ceiling, for the wrong reason.
        record = {'tier': '0-2', 'total_wall': 100.0, 'test_count': 1000,
                  'per_module': {'A': 60.0}, 'per_module_fixture': {'B': 33.5}}
        record.update(over)
        prior = [{'tier': '0-2', 'total_wall': 100.0, 'failures': 0, 'errors': 0,
                  'per_module': {'A': 60.0}}] * 5
        return _status_line(record, prior)

    def test_the_status_line_names_the_fixture_time_on_a_healthy_run(self):
        """Reported on INFO too, not only on WARNING - the blind spot lasted as long as it
        did because every run in that window was reporting INFO."""
        line = self._line()

        self.assertIn(f'{STATUS_PREFIX} INFO', line)
        self.assertIn('33.5s in setUpClass', line)

    def test_a_run_with_no_class_fixtures_says_nothing_about_them(self):
        line = self._line(per_module_fixture={})

        self.assertNotIn('setUpClass', line)


class PerTestDriftTests(unittest.TestCase):
    """Per-test cost, and the drift check built on it (dev/changelog/777).

    The rolling median absorbs any step that lands under TOL - the slower run joins the window
    the next run is compared against - and the hard ceiling that was supposed to backstop that
    has been re-baselined six times, the last two with no measurement behind them. ms/test is
    the figure every one of those notes names as the one to watch, and nothing computed it.
    """

    @staticmethod
    def _run(wall, tests, **over):
        record = {'tier': '0-2', 'jobs': 3, 'total_wall': wall, 'test_count': tests,
                  'total_charged': wall * 3, 'failures': 0, 'errors': 0,
                  'per_module': {'A': wall}, 'per_module_fixture': {}}
        record.update(over)
        return record

    def _history(self, *pairs):
        return [self._run(wall, tests) for wall, tests in pairs]

    def test_per_test_cost_is_derived_from_the_record(self):
        self.assertAlmostEqual(_ms_per_test(self._run(100.0, 1000)), 100.0)

    def test_a_record_that_collected_no_tests_has_no_per_test_cost(self):
        """Zero must read as absent, not as zero - a missing figure that reads as 0.0ms/test
        would look like the fastest suite ever recorded and drag every later comparison."""
        self.assertIsNone(_ms_per_test(self._run(100.0, 0)))
        self.assertIsNone(_ms_per_test({'total_wall': 100.0}))

    def test_a_step_that_stayed_up_is_drift(self):
        prior = self._history(*([(100.0, 1000)] * DRIFT_MIN_PRIOR),
                              *([(120.0, 1000)] * (DRIFT_RECENT - 1)))
        drift = _per_test_drift(prior, self._run(120.0, 1000))
        self.assertIsNotNone(drift)
        self.assertAlmostEqual(drift[0], 100.0)
        self.assertAlmostEqual(drift[1], 120.0)

    def test_one_contended_run_on_its_own_is_not_drift(self):
        """The suite's own history has these - 460.4s at 4001 tests between two runs at ~340s
        - and calling a single stretched run a drift is how the check would lose its
        credibility on the first week."""
        prior = self._history(*([(100.0, 1000)] * (DRIFT_RECENT + DRIFT_MIN_PRIOR)))
        self.assertIsNone(_per_test_drift(prior, self._run(160.0, 1000)))

    def test_a_suite_that_came_back_down_is_not_drifting_now(self):
        prior = self._history(*([(100.0, 1000)] * DRIFT_MIN_PRIOR),
                              *([(120.0, 1000)] * (DRIFT_RECENT - 1)))
        self.assertIsNone(_per_test_drift(prior, self._run(100.0, 1000)))

    def test_adding_tests_is_not_drift(self):
        """The whole reason the check reads ms/test rather than total_wall: six ceiling
        re-baselines in a row concluded "volume, not regression", and a wall-clock rule
        cannot tell those apart."""
        prior = self._history((100.0, 1000), (110.0, 1100), (120.0, 1200), (130.0, 1300),
                              (140.0, 1400), (150.0, 1500), (160.0, 1600), (170.0, 1700))
        self.assertIsNone(_per_test_drift(prior, self._run(180.0, 1800)))

    def test_a_short_history_yields_no_drift_verdict(self):
        prior = self._history(*([(100.0, 1000)] * DRIFT_RECENT))
        self.assertIsNone(_per_test_drift(prior, self._run(200.0, 1000)))

    def test_a_faster_suite_is_never_drift(self):
        prior = self._history(*([(200.0, 1000)] * DRIFT_MIN_PRIOR),
                              *([(100.0, 1000)] * (DRIFT_RECENT - 1)))
        self.assertIsNone(_per_test_drift(prior, self._run(100.0, 1000)))

    def test_drift_warns_even_though_the_wall_clock_is_inside_the_baseline(self):
        """The defect in one assertion: +20% is under TOL and under the ceiling, so before
        this the run reported INFO and its reading then became the baseline.

        50 -> 60ms/test rather than 100 -> 120: the ceiling reads ms/test since
        dev/changelog/835, and at 120 this would be over it, so the assertion would no
        longer be evidence that DRIFT is what caught the step.
        """
        prior = self._history(*([(50.0, 1000)] * DRIFT_MIN_PRIOR),
                              *([(60.0, 1000)] * (DRIFT_RECENT - 1)))
        line = _status_line(self._run(60.0, 1000), prior)

        self.assertTrue(line.startswith(f'{STATUS_PREFIX} WARNING'), line)
        self.assertIn('per-test cost drifting 50.0 -> 60.0ms/test', line)
        self.assertNotIn('over ceiling', line)

    def test_the_per_test_figure_is_named_on_a_healthy_line_too(self):
        prior = self._history(*([(100.0, 1000)] * DRIFT_MIN_PRIOR))
        line = _status_line(self._run(100.0, 1000), prior)

        self.assertTrue(line.startswith(f'{STATUS_PREFIX} INFO'), line)
        self.assertIn('100.0ms/test', line)

    def test_a_serial_run_is_not_drifted_against_shard_history(self):
        """ms/test is no more comparable across a -j change than total_wall is: the same
        tests cost ~150ms serially and ~84ms across three shards."""
        prior = self._history(*([(100.0, 1000)] * (DRIFT_RECENT + DRIFT_MIN_PRIOR)))
        serial = self._run(300.0, 1000, jobs=1)
        self.assertEqual(_passing_same_tier(prior, '0-2', 1), [])
        self.assertIsNone(_per_test_drift(_passing_same_tier(prior, '0-2', 1), serial))


class PerTestCeilingTests(unittest.TestCase):
    """The hard ceiling reads ms/test, not wall clock - dev/changelog/835.

    Guards dev/docs/BUGS.md 2026-08-27 "the suite timing ceiling fired on green runs". A
    wall-clock ceiling moves for two reasons that are not slowdowns - the suite grew, or
    something else was using the box - so it fired on 8 of the last 30 green runs and had been
    re-baselined seven times. ms/test moves for neither: it read 80.8ms over the first 30
    recorded green runs and 85.7ms over the last 30, through 68% growth in test count.
    """

    @staticmethod
    def _run(wall, tests, **over):
        record = {'tier': '0-2', 'jobs': 3, 'total_wall': wall, 'test_count': tests,
                  'failures': 0, 'errors': 0, 'per_module': {'A': wall},
                  'per_module_fixture': {}}
        record.update(over)
        return record

    def _prior(self, wall, tests, n=10):
        return [self._run(wall, tests) for _ in range(n)]

    def test_the_ceiling_is_expressed_in_ms_per_test(self):
        """The unit is the whole change, so it gets an assertion rather than a comment. The
        wall-clock ceilings this replaced were 450 and 400; the serial suite's own measured
        per-test cost is ~155ms, so anything in the hundreds is still a wall clock."""
        self.assertEqual(set(CEILINGS_MS_PER_TEST), {('0-2', 1), ('0-2', 3)})
        for value in CEILINGS_MS_PER_TEST.values():
            self.assertLess(value, 300.0, 'a ceiling this large is still a wall clock')

    def test_a_suite_that_only_grew_does_not_trip_the_ceiling(self):
        """The headline defect. The same per-test cost over twice the tests doubles the wall
        clock, which is what a wall-clock ceiling would have called a regression."""
        ceiling = CEILINGS_MS_PER_TEST[('0-2', 3)]
        per_test = ceiling / 2
        small = self._run(per_test * 2000 / 1000, 2000)
        large = self._run(per_test * 8000 / 1000, 8000)

        for record in (small, large):
            line = _status_line(record, self._prior(record['total_wall'], record['test_count']))
            self.assertIn(f'{STATUS_PREFIX} INFO', line)
            self.assertNotIn('over ceiling', line)
        self.assertGreater(large['total_wall'], 4 * small['total_wall'] - 1)

    def test_a_suite_that_got_slower_per_test_trips_the_ceiling(self):
        """The other direction, at an UNCHANGED test count - which is what the ceiling is
        for, and the case a wall-clock number can only reach by coincidence."""
        ceiling = CEILINGS_MS_PER_TEST[('0-2', 3)]
        record = self._run((ceiling + 20) * 4000 / 1000, 4000)

        line = _status_line(record, self._prior(record['total_wall'], 4000))

        self.assertTrue(line.startswith(f'{STATUS_PREFIX} WARNING'), line)
        self.assertIn(f'over ceiling {ceiling:.0f}ms/test', line)

    def test_the_healthy_line_names_the_ceiling_in_the_same_unit(self):
        """A number on the line in one unit and a comparison made in another is how a reader
        stops being able to check the tool's arithmetic."""
        ceiling = CEILINGS_MS_PER_TEST[('0-2', 3)]
        record = self._run(200.0, 4000)

        line = _status_line(record, self._prior(200.0, 4000))

        self.assertIn(f'ceiling {ceiling:.0f}ms/test', line)
        self.assertNotIn(f'ceiling {ceiling:.0f}s', line)

    def test_a_run_that_collected_nothing_is_not_judged_by_the_ceiling(self):
        """No per-test cost means no verdict. Reading it as 0.0ms/test would call a suite
        that ran nothing the healthiest run ever recorded."""
        record = self._run(500.0, 0)

        line = _status_line(record, self._prior(200.0, 4000))

        self.assertNotIn('over ceiling', line)

    def test_the_recorded_history_would_not_have_warned_under_this_ceiling(self):
        """Replay, not theory. The committed history is the fixture that proves the change
        does what it claims: the 400s wall ceiling fired on many more green -j 3 runs than
        the ms/test ceiling does, so the WARNING goes back to meaning something."""
        with open(timing.HISTORY_PATH, 'r', encoding='utf-8') as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        green = [r for r in records
                 if r.get('jobs', 1) == 3 and r.get('shard_order', 'forward') == 'forward'
                 and not r.get('failures', 0) and not r.get('errors', 0)]
        self.assertGreater(len(green), 100, 'history too thin to replay against')

        wall_hits = sum(1 for r in green if r['total_wall'] > 400.0)
        ceiling = CEILINGS_MS_PER_TEST[('0-2', 3)]
        costs = sorted(c for c in (_ms_per_test(r) for r in green) if c is not None)
        per_test_hits = sum(1 for c in costs if c > ceiling)
        median = costs[len(costs) // 2]

        self.assertGreaterEqual(wall_hits, 10)
        self.assertLessEqual(per_test_hits, 3)
        self.assertLess(per_test_hits, wall_hits)
        # The ceiling stays tied to what was actually measured. Every wall-clock re-baseline
        # drifted further from the suite's real cost until 400s sat five times over it, and a
        # backstop that far out backstops nothing.
        self.assertLess(ceiling, median * 1.75,
                        'ceiling has drifted away from the measured per-test cost')


class ForeignCpuTests(unittest.TestCase):
    """How much of the box went to work that was not this suite - dev/changelog/835.

    The two-core box has no swap and the suite is routinely run alongside a browser or a
    build; three runs of ONE unchanged tree once spanned 67.6s. That contention was the
    largest term in the number nobody could explain, and nothing recorded it.
    """

    def test_foreign_is_the_box_minus_our_own_process_tree(self):
        before, after = (100.0, 40.0), (160.0, 90.0)

        seconds, share = _foreign_cpu(before, after, wall=30.0)

        self.assertAlmostEqual(seconds, 10.0)          # 60 busy - 50 ours
        self.assertAlmostEqual(share, 10.0 / (30.0 * (os.cpu_count() or 1)))

    def test_an_idle_box_reports_no_foreign_work(self):
        """Our own CPU accounting for the whole box's busy time is what "the run had the
        machine to itself" looks like, and it must not read as a small positive."""
        self.assertEqual(_foreign_cpu((0.0, 0.0), (50.0, 50.0), wall=30.0)[0], 0.0)

    def test_sampling_skew_can_never_produce_a_negative_share(self):
        """The snapshots are not atomic with the run, so `own` can land slightly ahead of
        `busy`. A negative figure is an artifact, and printing one would discredit the
        number in the only place anybody reads it."""
        seconds, share = _foreign_cpu((0.0, 0.0), (10.0, 12.0), wall=30.0)

        self.assertEqual(seconds, 0.0)
        self.assertEqual(share, 0.0)

    def test_an_unmeasurable_box_yields_no_figure_rather_than_a_zero(self):
        """A platform with no /proc/stat records nothing. Absent must not read as idle."""
        self.assertIsNone(_foreign_cpu(None, (1.0, 1.0), wall=30.0))
        self.assertIsNone(_foreign_cpu((1.0, 1.0), None, wall=30.0))
        self.assertIsNone(_foreign_cpu((1.0, 1.0), (2.0, 2.0), wall=0.0))

    def test_a_snapshot_on_this_box_charges_our_own_work_to_us(self):
        """The claim the whole measurement rests on: CPU this process burns must land in the
        `own` term, or every run would report its own work as somebody else's."""
        before = _cpu_snapshot()
        if before is None:
            self.skipTest('no /proc/stat on this platform')
        sum(range(4_000_000))
        after = _cpu_snapshot()

        self.assertGreater(after[1] - before[1], 0.0)
        seconds, _ = _foreign_cpu(before, after, wall=10.0)
        self.assertLess(seconds, after[0] - before[0])

    def test_the_status_line_names_the_contention(self):
        record = {'tier': '0-2', 'jobs': 3, 'total_wall': 400.0, 'test_count': 4600,
                  'per_module': {'A': 1.0}, 'per_module_fixture': {},
                  'foreign_cpu_share': 0.34}
        prior = [dict(record, failures=0, errors=0) for _ in range(10)]

        self.assertIn('34% foreign CPU', _status_line(record, prior))

    def test_a_run_with_no_contention_figure_says_nothing_about_it(self):
        """Older records carry no such key, and inventing a 0% for them would assert an
        idle box that was never measured."""
        record = {'tier': '0-2', 'jobs': 3, 'total_wall': 400.0, 'test_count': 4600,
                  'per_module': {'A': 1.0}, 'per_module_fixture': {}}
        prior = [dict(record, failures=0, errors=0) for _ in range(10)]

        self.assertNotIn('foreign CPU', _status_line(record, prior))


class ShardSplitTests(unittest.TestCase):
    """The shard split must be a partition of the suite - dev/changelog/583.

    A module that lands in no shard is a suite that quietly runs fewer tests and still
    prints OK, which is the false-assurance failure that got the `--all` flag removed
    (dev/changelog/513). These assert the split is total and disjoint, over the real module
    list rather than a synthetic one, so a new test file cannot slip out of the run.
    """

    def _weights(self, modules):
        return {m: float(i % 7 + 1) for i, m in enumerate(modules)}

    def test_every_module_lands_in_exactly_one_shard(self):
        modules = _discover_modules()
        self.assertGreater(len(modules), 100, 'module discovery found almost nothing')

        for jobs in (2, 3, 5):
            shards, _load = _pack_shards(modules, jobs, self._weights(modules))
            assigned = [m for s in shards for m in s]
            self.assertCountEqual(assigned, modules, f'-j {jobs} is not a partition')

    def test_the_reverse_split_is_a_different_partition_of_the_same_modules(self):
        """The shuffle check relies on this: a valid but differently-ordered split is what
        proves the suite is order-independent rather than surviving one arrangement."""
        modules = _discover_modules()
        weights = self._weights(modules)
        fwd, _ = _pack_shards(modules, 3, weights)
        rev, _ = _pack_shards(modules, 3, weights, reverse=True)

        self.assertCountEqual([m for s in rev for m in s], modules)
        self.assertNotEqual(fwd, rev, 'reverse packing produced the identical split')

    def test_this_very_module_is_in_the_discovered_set(self):
        """_discover_modules() lists tests/test_*.py directly instead of importing the
        suite. If that ever drifts from what discovery finds, it drifts silently."""
        self.assertIn('tests.test_suite_timing_harness', _discover_modules())

    def test_a_dropped_module_is_a_hard_error_not_a_smaller_suite(self):
        with self.assertRaises(SystemExit) as caught:
            _assert_no_module_was_dropped(['a', 'b', 'c'], [['a'], ['b']])

        self.assertIn('c', str(caught.exception))

    def test_a_duplicated_module_is_a_hard_error_too(self):
        """Running a module twice inflates test_count and double-charges its time, so the
        history record stops meaning what it says."""
        with self.assertRaises(SystemExit) as caught:
            _assert_no_module_was_dropped(['a', 'b'], [['a', 'b'], ['b']])

        self.assertIn('b', str(caught.exception))

    def test_a_module_with_no_history_gets_the_mean_not_zero(self):
        """A brand new module weighted zero would pile into whichever shard is lightest at
        the end, which is how one shard ends up carrying every new test file."""
        history = [{'failures': 0, 'errors': 0,
                    'per_module': {'AlphaTests': 10.0}, 'per_module_fixture': {}}]
        weights = _module_weights(['tests.test_suite_timing_harness'], history)

        self.assertGreater(weights['tests.test_suite_timing_harness'], 0.0)

    def test_a_failed_run_never_supplies_the_weights(self):
        """Same rule the baseline already follows: a run that failed may have died early,
        so its per-module numbers describe a partial suite."""
        history = [
            {'failures': 0, 'errors': 0, 'per_module': {'X': 5.0}, 'per_module_fixture': {}},
            {'failures': 3, 'errors': 0, 'per_module': {'X': 999.0}, 'per_module_fixture': {}},
        ]
        weights = _module_weights(['tests.test_suite_timing_harness'], history)

        self.assertLess(max(weights.values()), 999.0)


class BaselineSegmentationTests(unittest.TestCase):
    """A parallel wall clock and a serial one measure different things (dev/changelog/583).

    Mixing them into one rolling median describes neither, so `jobs` segments the history
    exactly the way `tier` already did.
    """

    def _rec(self, **over):
        r = {'tier': '0-2', 'failures': 0, 'errors': 0, 'total_wall': 100.0}
        r.update(over)
        return r

    def test_a_serial_run_is_not_compared_against_shard_history(self):
        history = [self._rec(jobs=3) for _ in range(5)]

        self.assertEqual(_passing_same_tier(history, '0-2', 1), [])

    def test_a_sharded_run_is_not_compared_against_serial_history(self):
        history = [self._rec(jobs=1) for _ in range(5)]

        self.assertEqual(_passing_same_tier(history, '0-2', 3), [])

    def test_a_reverse_split_run_does_not_drag_the_forward_baseline(self):
        """--reverse-shards is deliberately worse-balanced (232.6s against 217.9s forward),
        so it is a correctness check, not a performance measurement. Letting its records
        into the forward median would raise the bar the wrong way and mask a regression."""
        history = [self._rec(jobs=3, shard_order='reverse') for _ in range(5)]

        self.assertEqual(_passing_same_tier(history, '0-2', 3), [])

    def test_a_reverse_run_is_compared_against_other_reverse_runs(self):
        history = [self._rec(jobs=3, shard_order='reverse') for _ in range(5)]

        self.assertEqual(len(_passing_same_tier(history, '0-2', 3, 'reverse')), 5)

    def test_a_record_written_before_sharding_existed_reads_as_serial(self):
        """The 300 committed records carry no `jobs` key and were all single-process, so
        they must stay comparable with a `-j 1` run rather than being discarded."""
        history = [self._rec() for _ in range(5)]

        self.assertEqual(len(_passing_same_tier(history, '0-2', 1)), 5)

    def test_the_status_line_names_the_shard_count_and_the_charged_time(self):
        """A parallel line must say it is parallel. total_wall alone does not distinguish
        "the suite got 1.8x faster" from "the suite got 1.8x smaller", and total_charged is
        the figure that shows the tests were all still run (and, read against the same -j,
        how much of the stretch is contention)."""
        record = {'tier': '0-2', 'jobs': 3, 'total_wall': 205.0, 'total_charged': 350.0,
                  'test_count': 2564, 'per_module': {'A': 60.0}, 'per_module_fixture': {}}
        prior = [{'tier': '0-2', 'jobs': 3, 'total_wall': 205.0, 'failures': 0,
                  'errors': 0, 'per_module': {'A': 60.0}}] * 5

        line = _status_line(record, prior)

        self.assertIn('3 shards', line)
        self.assertIn('350.0s charged', line)

    def test_a_serial_line_says_nothing_about_shards(self):
        record = {'tier': '0-2', 'jobs': 1, 'total_wall': 386.0, 'total_charged': 350.0,
                  'test_count': 2564, 'per_module': {'A': 60.0}, 'per_module_fixture': {}}
        prior = [{'tier': '0-2', 'total_wall': 386.0, 'failures': 0, 'errors': 0,
                  'per_module': {'A': 60.0}}] * 5

        self.assertNotIn('shards', _status_line(record, prior))


def _synthetic_failures():
    """A failing case and an erroring case, declared inside a function on purpose.

    A module-level TestCase that fails is collected by the real suite and turns it red, so
    these exist only for the duration of the test that asks for them.
    """
    class _Fails(unittest.TestCase):
        def test_boom(self):
            self.fail('deliberate')

    class _Errors(unittest.TestCase):
        def test_raises(self):
            raise RuntimeError('deliberate')

    return _Fails, _Errors


def _broken_fixture():
    class _BadSetup(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            raise RuntimeError('deliberate')

        def test_never_runs(self):
            pass

    return _BadSetup


class FailedIdTests(unittest.TestCase):
    """A red run must name its failures - dev/changelog/717.

    The history record carried only counts (`failures: 1`), so once the rerun came back
    green the identity of the failure was gone permanently and the only move left was to
    re-run the suite and guess.
    """

    def test_both_a_failure_and_an_error_are_named(self):
        fails, errors = _synthetic_failures()
        result, _ = _run(fails, errors, _Cheap)

        ids = _failed_ids(result)

        self.assertEqual(len(ids), 2, ids)
        self.assertTrue(any(i.endswith('.test_boom') for i in ids), ids)
        self.assertTrue(any(i.endswith('.test_raises') for i in ids), ids)

    def test_a_green_run_names_nothing(self):
        result, _ = _run(_Cheap, _Expensive)

        self.assertEqual(_failed_ids(result), [])

    def test_a_broken_setupclass_is_named_by_its_fixture(self):
        """A setUpClass explosion arrives as a _ErrorHolder rather than a test, and it is
        the single most useful thing to name: none of the class's tests ran at all."""
        result, _ = _run(_broken_fixture())

        ids = _failed_ids(result)

        self.assertEqual(len(ids), 1, ids)
        self.assertIn('setUpClass', ids[0])

    def test_the_ids_are_deduplicated_and_ordered(self):
        """They are merged across shards, so a stable order is what makes two records
        comparable at all."""
        class _Test:
            def __init__(self, tid):
                self._tid = tid

            def id(self):
                return self._tid

        result = mock.Mock(failures=[(_Test('b'), ''), (_Test('a'), '')],
                           errors=[(_Test('a'), '')])

        self.assertEqual(_failed_ids(result), ['a', 'b'])


class HistorySafeIdTests(unittest.TestCase):
    """What reaches tests/timing_history.jsonl is bounded - dev/docs/BUGS.md 2026-09-13.

    That file is committed and published, so a failing subTest's parameter suffix is
    published text. `dev/changelog/881` scrubbed one such leak by hand and deliberately left
    the write path alone, trusting the scanner as the control; thirteen days later the same
    test parametrized on the same `blocked_by` value put a personal name back in.
    """

    def test_a_subtest_suffix_is_dropped(self):
        # The dev/tasks/ name below is sample subTest text, not a citation - the whole
        # point of the test is that it must NOT reach timing_history.jsonl.
        leaked = ('tests.test_worklog.WorklogFrontMatterTests.test_blocked_by_resolves '
                  '(file=\'dev/tasks/TASKS-x-do-next.md\', dep="{\'somebody\': \'a note\'}")')  # task-ref-ok: sample text

        self.assertEqual(
            _history_safe_ids([leaked]),
            ['tests.test_worklog.WorklogFrontMatterTests.test_blocked_by_resolves'])

    def test_a_multiline_suffix_is_dropped_too(self):
        """A subTest parametrized on a multi-line value is still one id, and a regex that
        stops at the first newline would leave the rest of it in the file."""
        self.assertEqual(
            _history_safe_ids(['tests.test_x.FooTests.test_y (msg=\'one\ntwo\')']),
            ['tests.test_x.FooTests.test_y'])

    def test_a_fixture_id_keeps_its_class(self):
        """`setUpClass (tests.test_x.FooTests)` is an _ErrorHolder id, not a subTest - the
        parenthesis names the class, which is the only useful half."""
        self.assertEqual(_history_safe_ids(['setUpClass (tests.test_x.FooTests)']),
                         ['setUpClass (tests.test_x.FooTests)'])

    def test_a_plain_id_is_untouched(self):
        self.assertEqual(_history_safe_ids(['tests.test_x.FooTests.test_y']),
                         ['tests.test_x.FooTests.test_y'])

    def test_truncation_collapses_subtests_of_one_test_into_one_id(self):
        """Otherwise a test parametrized 40 ways spends the whole MAX_FAILED_IDS budget
        saying the same thing, and order still has to be stable across shards."""
        ids = _history_safe_ids([
            'tests.test_x.FooTests.test_y (case=1)',
            'tests.test_x.FooTests.test_y (case=2)',
            'tests.test_x.FooTests.test_z',
        ])

        self.assertEqual(ids, ['tests.test_x.FooTests.test_y', 'tests.test_x.FooTests.test_z'])

    def test_the_committed_history_carries_no_suffix_today(self):
        """The file itself, not just the function - the leak that prompted this was already
        committed, so a green function with a dirty file would still block a publish."""
        path = os.path.join(timing._REPO, 'tests', 'timing_history.jsonl')
        with open(path, encoding='utf-8') as fh:
            for lineno, line in enumerate(fh, 1):
                ids = json.loads(line).get('failed_ids') or []
                self.assertEqual(_history_safe_ids(ids), ids,
                                 'timing_history.jsonl:%d carries a subTest suffix' % lineno)


class LogCaptureTests(unittest.TestCase):
    """Worker output survives the run - dev/changelog/717.

    run_tests.sh streamed each shard to the terminal and kept nothing, so a failure that
    scrolled past was unrecoverable.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='timing_logs_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(timing, 'LOG_ROOT', self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_run_directory_is_created(self):
        path = _prepare_log_dir()

        self.assertIsNotNone(path)
        self.assertTrue(os.path.isdir(path))

    def test_two_runs_in_the_same_second_do_not_share_a_directory(self):
        """The directory name is a timestamp to one second; two runs colliding would have
        the second overwrite the first shard log, which is the artifact being kept."""
        first = _prepare_log_dir()
        second = _prepare_log_dir()

        self.assertNotEqual(first, second)

    def test_older_runs_are_pruned_but_more_than_one_is_kept(self):
        """Keeping only the current run would defeat the point: the case this exists for is
        a red run followed immediately by a green rerun."""
        for i in range(MAX_LOG_RUNS + 5):
            os.makedirs(os.path.join(self.tmp, f'20260101-0000{i:02d}'))

        _prepare_log_dir()

        self.assertEqual(len(os.listdir(self.tmp)), MAX_LOG_RUNS)

    def test_a_directory_that_cannot_be_made_turns_capture_off_rather_than_failing(self):
        """Capture is best-effort: it must never change the run's result."""
        with mock.patch.object(timing.os, 'makedirs', side_effect=OSError('nope')):
            self.assertIsNone(_prepare_log_dir())

        self.assertIsNone(_open_log(None, 'shard0.log'))

    def test_pump_writes_every_line_to_both_the_terminal_and_the_log(self):
        path = os.path.join(self.tmp, 'shard0.log')
        lines = ['test_one ... ok\n', 'test_two ... FAIL\n']
        live = io.StringIO()

        with mock.patch.object(timing.sys, 'stderr', live):
            _pump(io.StringIO(''.join(lines)), 0, threading.Lock(), open(path, 'w'))

        with open(path) as fh:
            captured = fh.read()
        self.assertEqual(captured, ''.join(lines))
        self.assertIn('[0] test_two ... FAIL', live.getvalue())

    def test_pump_survives_a_log_file_that_cannot_be_written(self):
        live = io.StringIO()
        broken = mock.Mock()
        broken.write.side_effect = OSError('disk full')

        with mock.patch.object(timing.sys, 'stderr', live):
            _pump(io.StringIO('test_one ... ok\n'), 1, threading.Lock(), broken)

        self.assertIn('[1] test_one ... ok', live.getvalue())

    def test_the_tee_writes_to_both_and_a_broken_capture_cannot_propagate(self):
        """The -j 1 path has no worker pipe, so it captures through this instead."""
        live = io.StringIO()
        broken = mock.Mock()
        broken.write.side_effect = OSError('disk full')
        broken.flush.side_effect = OSError('disk full')

        tee = _Tee(live, broken)
        tee.write('hello')
        tee.flush()

        self.assertEqual(live.getvalue(), 'hello')

        good = io.StringIO()
        _Tee(live, good).write(' world')
        self.assertEqual(good.getvalue(), ' world')
        self.assertEqual(live.getvalue(), 'hello world')


_SHARD_MODULE = '''
import unittest


class SyntheticShardTests(unittest.TestCase):

    def test_passes(self):
        pass

    def test_boom(self):
        self.fail('deliberate')
'''


class ShardedRunCarriesTheEvidenceTests(unittest.TestCase):
    """End to end over a real worker process - dev/changelog/717.

    The unit tests above cover the pieces; this one proves the parent actually merges what a
    worker reports and that the worker's output lands on disk, which is the whole path that
    used to drop the evidence. One shard, one synthetic module, no app import.
    """

    def test_a_failing_shard_reports_its_ids_and_leaves_a_log_behind(self):
        tmp = tempfile.mkdtemp(prefix='timing_shard_')
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, 'synthetic_shard_mod.py'), 'w') as fh:
            fh.write(_SHARD_MODULE)
        log_dir = os.path.join(tmp, 'logs')
        os.makedirs(log_dir)

        # The worker is a child process started from the repo root, so the synthetic module
        # reaches it the only way it can: on the inherited PYTHONPATH.
        env_patch = mock.patch.dict(
            os.environ, {'PYTHONPATH': tmp + os.pathsep + os.environ.get('PYTHONPATH', '')})
        env_patch.start()
        self.addCleanup(env_patch.stop)

        with mock.patch.object(timing.sys, 'stderr', io.StringIO()):
            merged = timing._run_sharded([['synthetic_shard_mod']], log_dir)

        self.assertFalse(merged['successful'])
        self.assertEqual(merged['failures'], 1)
        self.assertEqual(
            merged['failed_ids'], ['synthetic_shard_mod.SyntheticShardTests.test_boom'])

        with open(os.path.join(log_dir, 'shard0.log')) as fh:
            captured = fh.read()
        self.assertIn('test_boom', captured)
        self.assertIn('deliberate', captured)


if __name__ == '__main__':
    unittest.main(verbosity=2)
