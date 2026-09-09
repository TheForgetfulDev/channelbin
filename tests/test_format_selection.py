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


class RankingPopulationTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-09-09 07:02 - a format lock could be won by a bucket holding
    no recording-enabled member.

    A lock's only job is to filter recording candidates, so a bucket containing none of
    them cannot be a correct answer to it. Measured on the live database before the fix:
    group 5's winning 1280x720@30 bucket held 40 healthy channels and zero
    recording-enabled ones, so all 29 members the group could record from were filtered
    out and every recording fell through the zero-survivor override.
    """

    def _mixed(self):
        """Three 720p30 channels the group does not record from, two 1080p60 it does.
        Raw membership makes 720p30 the `most_channels` winner; recordability does not."""
        latest = {1: _pass(SD30, bitrate=5000), 2: _pass(SD30, bitrate=5000),
                  3: _pass(SD30, bitrate=5000),
                  4: _pass(HD60, bitrate=4000), 5: _pass(HD60, bitrate=4000)}
        return _channels(5), latest, {4, 5}

    def test_a_bucket_with_no_recordable_member_cannot_win(self):
        chans, latest, rank_ids = self._mixed()
        plan = plan_format_selection(chans, latest, rank_ids=rank_ids)
        for strategy in FORMAT_STRATEGIES:
            self.assertEqual(HD60, plan['strategies'][strategy]['key'],
                             f'{strategy} picked a format the group cannot record from')

    def test_the_same_data_without_the_narrowing_still_picks_the_majority(self):
        """The narrowing is what changes the answer - not some other edit in the same
        pass. With rank_ids omitted the old winner comes straight back."""
        chans, latest, _ = self._mixed()
        plan = plan_format_selection(chans, latest)
        self.assertEqual(SD30, plan['strategies']['most_channels']['key'])

    def test_membership_counts_stay_over_every_member(self):
        """apply_format_plan can DELETE the members a winning bucket leaves out, so
        `channel_ids`/`count` must keep covering everyone. Narrowing them too would delete
        every health-check-only member as a side effect of a ranking change."""
        chans, latest, rank_ids = self._mixed()
        entry = plan_format_selection(chans, latest,
                                      rank_ids=rank_ids)['strategies']['highest_bitrate']
        self.assertEqual([4, 5], entry['channel_ids'])
        self.assertEqual(2, entry['count'])
        self.assertEqual(2, entry['rank_count'])
        sd = next(b for b in plan_format_selection(chans, latest, rank_ids=rank_ids)['buckets']
                  if b['key'] == SD30)
        self.assertEqual(3, sd['count'], 'the 720p30 bucket still reports its 3 members')
        self.assertEqual(0, sd['rank_count'], 'none of them is a recording source')

    def test_a_group_with_nothing_recording_enabled_ranks_over_everyone(self):
        """The fallback is load-bearing: a clone's members are all Recording-off by model
        default, so narrowing to an empty set would leave it with no format at all."""
        chans, latest, _ = self._mixed()
        plan = plan_format_selection(chans, latest, rank_ids=None)
        self.assertEqual(SD30, plan['strategies']['most_channels']['key'])
        self.assertEqual(5, plan['rank_total'])

    def test_ranking_reads_the_narrowed_median_not_the_whole_bucket(self):
        """A bucket's median must describe the members that would record from it. Here the
        two recordable 1080p60 members are slow and the three that are not are fast, so a
        whole-bucket median would pick 1080p60 and a recordable one must not."""
        latest = {1: _pass(HD60, bitrate=9000), 2: _pass(HD60, bitrate=9000),
                  3: _pass(HD60, bitrate=1000),
                  4: _pass(SD30, bitrate=4000)}
        chans = _channels(4)
        plan = plan_format_selection(chans, latest, rank_ids={3, 4})
        self.assertEqual(SD30, plan['strategies']['highest_bitrate']['key'])
        hd = next(b for b in plan['buckets'] if b['key'] == HD60)
        self.assertEqual(9000, hd['median_bitrate_kbps'])
        self.assertEqual(1000, hd['rank_median_bitrate_kbps'])

    def test_balanced_measures_its_coverage_floor_in_recordable_channels(self):
        """Balanced's floor is a coverage rule, and coverage of members the group cannot
        record from is not coverage. Six unrecordable 720p60 members must not raise the
        bar that the recordable buckets have to clear."""
        latest = {}
        for i in range(1, 7):
            latest[i] = _pass(SD60, bitrate=9000)
        latest[7] = _pass(HD60, bitrate=8000)
        latest[8] = _pass(SD30, bitrate=3000)
        plan = plan_format_selection(_channels(8), latest, rank_ids={7, 8})
        self.assertEqual(HD60, plan['strategies']['balanced']['key'])


class BucketStatusCountsTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-09-09 07:02 - the picker called an all-warning bucket
    "healthy" and offered no way to tell.

    A WARN still counts toward a format decision (HEALTHY_TEST_LABELS) and that stays
    true: excluding warning channels would let a diagnostic defect move a real lock, which
    is the opposite of what the diagnostic is for. The split is disclosed instead.
    """

    def test_pass_and_warn_are_counted_separately(self):
        latest = {1: _pass(HD60, bitrate=5000), 2: _warn(HD60, bitrate=5000),
                  3: _warn(HD60, bitrate=5000)}
        bucket = format_buckets(_channels(3), latest)[0]
        self.assertEqual(3, bucket['count'])
        self.assertEqual(1, bucket['pass_count'])
        self.assertEqual(2, bucket['warn_count'])

    def test_an_all_warning_winner_says_so_in_its_rationale(self):
        latest = {1: _warn(HD60, bitrate=5000), 2: _warn(HD60, bitrate=5000)}
        entry = plan_format_selection(_channels(2), latest)['strategies']['highest_bitrate']
        self.assertEqual(HD60, entry['key'])
        self.assertEqual(0, entry['pass_count'])
        self.assertIn('warning', entry['rationale'])

    def test_a_clean_winner_does_not_claim_a_warning(self):
        latest = {1: _pass(HD60, bitrate=5000), 2: _pass(HD60, bitrate=5000)}
        entry = plan_format_selection(_channels(2), latest)['strategies']['highest_bitrate']
        self.assertEqual(2, entry['pass_count'])
        self.assertNotIn('warning', entry['rationale'])


class NoWinnerRationaleTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-09-09 07:02. Three different situations produce no winner and
    they take three different actions to fix, so one sentence covering all of them is not
    an observable failure path (CLAUDE.md)."""

    def test_nothing_measured_at_all(self):
        entry = plan_format_selection(_channels(2), {})['strategies']['highest_bitrate']
        self.assertIsNone(entry['key'])
        self.assertIn('healthy channels', entry['rationale'])

    def test_measured_but_none_recordable(self):
        latest = {1: _pass(SD30, bitrate=5000), 2: _pass(SD30, bitrate=5000)}
        entry = plan_format_selection(_channels(2), latest,
                                      rank_ids={99})['strategies']['highest_bitrate']
        self.assertIsNone(entry['key'])
        self.assertIn('set to record from', entry['rationale'])

    def test_balanced_finds_nothing_broad_enough(self):
        """Every bucket is a single channel, so the floor over the largest excludes none -
        the reachable case is a strategy-specific one, and Balanced names itself in it."""
        latest = {1: _pass(HD60, bitrate=9000)}
        plan = plan_format_selection(_channels(1), latest, rank_ids={1})
        self.assertEqual(HD60, plan['strategies']['balanced']['key'],
                         'a single bucket always clears its own floor')
        self.assertIn(str(int(BALANCED_COVERAGE_FLOOR * 100)),
                      plan['strategies']['balanced']['rationale'])


class RankMembersQualityTieBreakTests(unittest.TestCase):
    """dev/changelog/890: everything that indicates higher quality breaks a tie
    before id does. Health score still strictly dominates - these only separate members
    that have already scored identically, which is what keeps this out of the blended
    quality score CLAUDE.md's "Format lock filters, health score ranks" rule forbids."""

    def _member(self, cid, score=100, streak=0):
        ch = FakeChannel(cid)
        ch.health_score = score
        ch.manual_health_adjustment = 0
        ch.consecutive_test_failures = streak
        return ch

    def test_picture_size_breaks_a_bitrate_tie_before_id(self):
        from app.channel_groups import rank_members
        low_id, high_id = self._member(1), self._member(2)
        latest = {1: _pass(SD30, bitrate=5000), 2: _pass(HD60, bitrate=5000)}
        self.assertEqual([2, 1], [c.id for c in rank_members([low_id, high_id], latest)])

    def test_frame_rate_breaks_a_size_tie_before_id(self):
        from app.channel_groups import rank_members
        low_id, high_id = self._member(1), self._member(2)
        latest = {1: _pass(SD30, bitrate=5000), 2: _pass(SD60, bitrate=5000)}
        self.assertEqual([2, 1], [c.id for c in rank_members([low_id, high_id], latest)])

    def test_health_score_still_wins_over_every_quality_signal(self):
        """The whole point of the rule this sits under: a dead 8 Mb/s feed must never
        outrank a live 3 Mb/s one."""
        from app.channel_groups import rank_members
        healthy, pretty = self._member(1, score=90), self._member(2, score=40)
        latest = {1: _pass(SD30, bitrate=3000), 2: _pass(HD60, bitrate=8000)}
        self.assertEqual([1, 2], [c.id for c in rank_members([healthy, pretty], latest)])

    def test_an_untested_member_never_sorts_as_if_it_were_the_best(self):
        from app.channel_groups import rank_members
        tested, untested = self._member(2), self._member(1)
        latest = {2: _pass(SD30, bitrate=100)}
        self.assertEqual([2, 1], [c.id for c in rank_members([untested, tested], latest)])

    def test_id_is_still_the_final_tie_break(self):
        from app.channel_groups import rank_members
        a, b = self._member(7), self._member(3)
        latest = {7: _pass(HD60, bitrate=5000), 3: _pass(HD60, bitrate=5000)}
        self.assertEqual([3, 7], [c.id for c in rank_members([a, b], latest)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
