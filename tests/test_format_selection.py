"""Regression tests for the pure auto-select-format engine (app/channel_groups.py::
healthy_channels / format_buckets / plan_format_selection). DB-free core only - the
functions take a supplied channel list + a channel_id -> latest-test map and return
buckets/plans, matching the split that makes plan_reconcile testable in
tests/test_channel_groups.py.

Planned end-to-end 2026-08-06 and shipped in dev/changelog/494 (healthy = PASS
or WARN, four named strategies, Balanced's 60% coverage floor). The real health-check-20
fixture at the bottom guards the concrete numbers reviewed during planning: all four
strategies land on 1920x1080 @ 60 with 27 channels kept.

Runnable with no third-party deps:  python3 tests/test_format_selection.py
(or:  python3 -m unittest tests.test_format_selection)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.channel_groups import (  # noqa: E402
    FORMAT_STRATEGIES, BALANCED_COVERAGE_FLOOR, HEALTHY_TEST_LABELS,
    healthy_channels, format_buckets, plan_format_selection,
)

HD60 = ('1920x1080', 60)
SD60 = ('1280x720', 60)
SD30 = ('1280x720', 30)


class FakeChannel:
    def __init__(self, id):
        self.id = id


class FakeTest:
    """Shape-compatible stand-in for ChannelTest: enough fields for both
    _test_status_label (status, error_detail) and format_buckets (resolution, fps,
    bitrate_kbps, bits_per_pixel_frame)."""
    def __init__(self, resolution=None, fps=None, status='COMPLETED', error_detail=None,
                 bitrate_kbps=None, bits_per_pixel_frame=None):
        self.resolution = resolution
        self.fps = fps
        self.status = status
        self.error_detail = error_detail
        self.bitrate_kbps = bitrate_kbps
        self.bits_per_pixel_frame = bits_per_pixel_frame


def _pass(key, bitrate=None, bpp=None):
    return FakeTest(key[0], key[1], status='COMPLETED', error_detail=None,
                     bitrate_kbps=bitrate, bits_per_pixel_frame=bpp)


def _warn(key, bitrate=None, bpp=None):
    return FakeTest(key[0], key[1], status='COMPLETED', error_detail='dropped frames',
                     bitrate_kbps=bitrate, bits_per_pixel_frame=bpp)


def _fail(key=None):
    return FakeTest(key[0] if key else None, key[1] if key else None, status='FAILED')


def _cancelled(key=None):
    return FakeTest(key[0] if key else None, key[1] if key else None, status='CANCELLED')


def _channels(n, start=1):
    return [FakeChannel(i) for i in range(start, start + n)]


class HealthyChannelsTests(unittest.TestCase):
    def test_pass_and_warn_are_healthy(self):
        self.assertEqual(HEALTHY_TEST_LABELS, frozenset({'PASS', 'WARN'}))
        chans = _channels(2)
        latest = {1: _pass(HD60), 2: _warn(HD60)}
        self.assertEqual({c.id for c in healthy_channels(chans, latest)}, {1, 2})

    def test_fail_cancelled_and_never_tested_are_excluded(self):
        chans = _channels(3)
        latest = {1: _fail(HD60), 2: _cancelled(HD60)}  # channel 3 has no entry at all
        self.assertEqual(healthy_channels(chans, latest), [])


class FormatBucketsTests(unittest.TestCase):
    def test_groups_by_resolution_and_fps(self):
        chans = _channels(3)
        latest = {1: _pass(HD60), 2: _pass(HD60), 3: _pass(SD60)}
        buckets = {b['key']: b for b in format_buckets(chans, latest)}
        self.assertEqual(set(buckets), {HD60, SD60})
        self.assertEqual(buckets[HD60]['count'], 2)
        self.assertEqual(buckets[SD60]['count'], 1)
        self.assertEqual(buckets[HD60]['pixels'], 1920 * 1080)
        self.assertEqual(buckets[HD60]['label'], '1920x1080 @ 60')

    def test_drops_unhealthy_and_unknown_format(self):
        chans = _channels(3)
        latest = {1: _pass(HD60), 2: _fail(HD60), 3: FakeTest(None, None)}
        buckets = format_buckets(chans, latest)
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0]['count'], 1)

    def test_median_bitrate_and_bpp_none_when_unmeasured(self):
        chans = _channels(2)
        latest = {1: _pass(HD60), 2: _pass(HD60)}
        buckets = format_buckets(chans, latest)
        self.assertIsNone(buckets[0]['median_bitrate_kbps'])
        self.assertIsNone(buckets[0]['median_bpp'])

    def test_median_bitrate_computed(self):
        chans = _channels(3)
        latest = {1: _pass(HD60, bitrate=3000), 2: _pass(HD60, bitrate=4000), 3: _pass(HD60, bitrate=5000)}
        buckets = format_buckets(chans, latest)
        self.assertEqual(buckets[0]['median_bitrate_kbps'], 4000)


class PlanFormatSelectionTests(unittest.TestCase):
    def test_all_strategies_present(self):
        plan = plan_format_selection([], {})
        self.assertEqual(set(plan['strategies']), set(FORMAT_STRATEGIES))

    def test_no_buckets_returns_no_winner_shape_with_rationale(self):
        plan = plan_format_selection(_channels(2), {})  # neither channel tested
        for strategy in FORMAT_STRATEGIES:
            entry = plan['strategies'][strategy]
            self.assertIsNone(entry['key'])
            self.assertEqual(entry['channel_ids'], [])
            self.assertEqual(entry['count'], 0)
            self.assertTrue(entry['rationale'])  # never an unexplained empty pick

    def test_eligible_and_excluded_counts(self):
        chans = _channels(4)
        latest = {1: _pass(HD60), 2: _pass(HD60), 3: _fail(HD60)}  # 4 never tested
        plan = plan_format_selection(chans, latest)
        self.assertEqual(plan['total'], 4)
        self.assertEqual(plan['eligible_count'], 2)
        self.assertEqual(plan['excluded_count'], 2)

    def test_highest_bitrate_picks_the_higher_median_even_at_lower_resolution(self):
        chans = _channels(2)
        latest = {1: _pass(HD60, bitrate=3000), 2: _pass(SD60, bitrate=6000)}
        plan = plan_format_selection(chans, latest)
        self.assertEqual(plan['strategies']['highest_bitrate']['key'], SD60)

    def test_highest_resolution_ignores_bitrate(self):
        chans = _channels(2)
        latest = {1: _pass(HD60, bitrate=3000), 2: _pass(SD60, bitrate=6000)}
        plan = plan_format_selection(chans, latest)
        self.assertEqual(plan['strategies']['highest_resolution']['key'], HD60)

    def test_most_channels_ignores_bitrate_and_resolution(self):
        chans = _channels(5)
        latest = {1: _pass(HD60, bitrate=9000), 2: _pass(SD60), 3: _pass(SD60),
                  4: _pass(SD60), 5: _pass(SD60)}
        plan = plan_format_selection(chans, latest)
        self.assertEqual(plan['strategies']['most_channels']['key'], SD60)
        self.assertEqual(plan['strategies']['most_channels']['count'], 4)

    def test_balanced_excludes_bucket_below_coverage_floor(self):
        # 10 in the big bucket, 5 in a tiny one that beats it on bitrate alone.
        # floor = ceil(0.60 * 10) = 6, so the 5-channel bucket must NOT win balanced
        # even though its bitrate is higher - that is exactly what distinguishes
        # 'balanced' from 'highest_bitrate'.
        chans = _channels(15)
        latest = {}
        for i in range(1, 11):
            latest[i] = _pass(HD60, bitrate=3000)
        for i in range(11, 16):
            latest[i] = _pass(SD60, bitrate=9000)
        plan = plan_format_selection(chans, latest)
        self.assertEqual(plan['strategies']['highest_bitrate']['key'], SD60)
        self.assertEqual(plan['strategies']['balanced']['key'], HD60)

    def test_balanced_coverage_floor_constant(self):
        self.assertEqual(BALANCED_COVERAGE_FLOOR, 0.60)

    def test_unmeasured_bitrate_never_silently_wins_highest_bitrate(self):
        chans = _channels(2)
        latest = {1: _pass(HD60, bitrate=None), 2: _pass(SD60, bitrate=100)}
        plan = plan_format_selection(chans, latest)
        self.assertEqual(plan['strategies']['highest_bitrate']['key'], SD60)

    def test_deterministic_tiebreak_not_insertion_order(self):
        # Identical count/bitrate/pixels/fps - the resolution string is the final
        # tie-break, so swapping the map insertion order must not change the winner.
        chans = _channels(2)
        latest_a_first = {1: _pass(('1280x720', 60), bitrate=3000),
                          2: _pass(('1920x1080', 30), bitrate=3000)}
        latest_b_first = {2: _pass(('1920x1080', 30), bitrate=3000),
                          1: _pass(('1280x720', 60), bitrate=3000)}
        p1 = plan_format_selection(chans, latest_a_first)
        p2 = plan_format_selection(chans, latest_b_first)
        self.assertEqual(p1['strategies']['most_channels']['key'],
                         p2['strategies']['most_channels']['key'])

    def test_health_check_20_fixture_all_strategies_agree(self):
        """Real numbers measured from dvr.db during the 2026-08-06 planning session
        (dev/changelog/494): 27 @ 1920x1080/60 (median 3.74 Mb/s), 22 @ 1280x720/60
        (median 3.17 Mb/s), 13 @ 1280x720/30 (median 3.60 Mb/s). All four strategies are
        expected to land on the first."""
        latest = {}
        cid = 1
        for _ in range(27):
            latest[cid] = _pass(HD60, bitrate=3740, bpp=0.030); cid += 1
        for _ in range(22):
            latest[cid] = _pass(SD60, bitrate=3170, bpp=0.057); cid += 1
        for _ in range(13):
            latest[cid] = _pass(SD30, bitrate=3600, bpp=0.130); cid += 1
        chans = _channels(cid - 1)
        plan = plan_format_selection(chans, latest)
        for strategy in FORMAT_STRATEGIES:
            entry = plan['strategies'][strategy]
            self.assertEqual(entry['key'], HD60, f'{strategy} did not pick {HD60}')
            self.assertEqual(entry['count'], 27, f'{strategy} kept the wrong count')


if __name__ == '__main__':
    unittest.main(verbosity=2)
