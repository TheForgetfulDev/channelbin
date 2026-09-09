"""Regression tests for the pure channel-group format/reconcile engine
(app/channel_groups.py). These cover the DB-free core only - the functions that
take supplied objects and a channel_id→test map and return plans/keys/outliers,
which is exactly the surface the engine was split to make testable
(plan_reconcile / group_reference_key / recording_members / group_format_outliers /
format_key / classify_group_formats / suggest_candidates).

Runnable with no third-party deps:  python3 tests/test_channel_groups.py
(or:  python3 -m unittest tests.test_channel_groups)

Two of these tests are pinned directly to BUGS.md testable invariants:
  - test_suggest_720p_in_mixed_group_is_different  ← 2026-07-17 Part B1 entry
  - test_reconcile_plan_is_idempotent_after_apply  ← pure half of the 2026-07-17
    Part E reconcile-idempotency entry (the ChannelEvent/alert half is DB-backed
    and out of scope for this pure suite - see module note at bottom).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.channel_groups import (  # noqa: E402
    effective_score, rank_members, pick_best_member, recording_members,
    format_key, format_label, classify_group_formats,
    group_reference_key, group_format_outliers, plan_reconcile,
    suggest_candidates, is_streaking,
)

HD = ('1920x1080', 60)
SD = ('1280x720', 30)


class FakeTest:
    def __init__(self, resolution=None, fps=None, bitrate_kbps=None):
        self.resolution = resolution
        self.fps = fps
        self.bitrate_kbps = bitrate_kbps


class FakeChannel:
    def __init__(self, id, health=None, adj=0, disabled=None, name='', epg='', streak=0):
        self.id = id
        self.health_score = health
        self.manual_health_adjustment = adj
        # Per-membership disabled state used by _memberships() below - the real
        # column lives on ChannelGroupMember since groups unification 2/4.
        self.disabled = disabled
        self.name = name or f'ch{id}'
        self.epg_channel_id = epg
        self.consecutive_test_failures = streak


class FakeMembership:
    """Shape-compatible stand-in for ChannelGroupMember rows (2/4 membership model)."""
    def __init__(self, channel, recording=True, tested=True):
        self.channel = channel
        self.channel_id = channel.id
        self.recording_enabled = recording
        self.test_enabled = tested


def _memberships(channels):
    """Membership rows for a channel list. A FakeChannel's `.disabled` marks it
    recording-disabled - the successor of the old status enum's 'manual'."""
    return [FakeMembership(ch, recording=not ch.disabled) for ch in channels]


class FakeGroup:
    # format_strategy defaults to a lock-managing value rather than the model's own
    # health_check_only default: a FakeGroup that carries a lock at all is by definition
    # a group that manages its format, and the two values that manage none
    # (health_check_only, unmanaged) get their own tests below.
    def __init__(self, id=1, name='G', res=None, fps=None, strategy='manual'):
        self.id = id
        self.name = name
        self.format_resolution = res
        self.format_fps = fps
        self.format_strategy = strategy

    @property
    def locked_format_key(self):
        if self.format_resolution and self.format_fps:
            return (self.format_resolution, int(self.format_fps))
        return None


def _test(key):
    """FakeTest for a (res, fps) key, or None for an untested channel."""
    return None if key is None else FakeTest(key[0], key[1])


# ── format_key / format_label ────────────────────────────────────────────────

class FormatKeyTests(unittest.TestCase):
    def test_known(self):
        self.assertEqual(format_key(FakeTest('1920x1080', 60)), ('1920x1080', 60))

    def test_none_test(self):
        self.assertIsNone(format_key(None))

    def test_missing_resolution(self):
        self.assertIsNone(format_key(FakeTest('', 60)))

    def test_missing_fps(self):
        self.assertIsNone(format_key(FakeTest('1920x1080', None)))

    def test_fractional_fps_rounds(self):
        # broadcast fractional rates must compare equal to their nominal integer
        self.assertEqual(format_key(FakeTest('1920x1080', 59.94)), ('1920x1080', 60))
        self.assertEqual(format_key(FakeTest('1280x720', 29.97)), ('1280x720', 30))

    def test_label(self):
        self.assertEqual(format_label(('1920x1080', 60)), '1920x1080 @ 60')
        self.assertEqual(format_label(None), 'unknown')


# ── ranking helpers ──────────────────────────────────────────────────────────

class RankingTests(unittest.TestCase):
    def test_effective_score_neutral_when_unscored(self):
        self.assertEqual(effective_score(FakeChannel(1, health=None)), 50)

    def test_effective_score_clamped(self):
        self.assertEqual(effective_score(FakeChannel(1, health=95, adj=20)), 100)
        self.assertEqual(effective_score(FakeChannel(1, health=10, adj=-50)), 0)

    def test_rank_tiebreak_is_id_ascending(self):
        a = FakeChannel(3, health=80)
        b = FakeChannel(1, health=80)
        c = FakeChannel(2, health=80)
        self.assertEqual([ch.id for ch in rank_members([a, b, c])], [1, 2, 3])

    def test_pick_best_member(self):
        lo = FakeChannel(1, health=40)
        hi = FakeChannel(2, health=90)
        self.assertIs(pick_best_member([lo, hi]), hi)
        self.assertIsNone(pick_best_member([]))

    def test_bitrate_breaks_a_score_tie(self):
        # Equal scores (both 80) - the higher-bitrate member must win, not the lower id.
        low_bitrate = FakeChannel(1, health=80)
        high_bitrate = FakeChannel(2, health=80)
        latest = {1: FakeTest(bitrate_kbps=2000), 2: FakeTest(bitrate_kbps=5000)}
        self.assertEqual([ch.id for ch in rank_members([low_bitrate, high_bitrate], latest)],
                         [2, 1])
        self.assertIs(pick_best_member([low_bitrate, high_bitrate], latest), high_bitrate)

    def test_bitrate_tie_falls_through_to_id(self):
        a = FakeChannel(2, health=80)
        b = FakeChannel(1, health=80)
        latest = {1: FakeTest(bitrate_kbps=3000), 2: FakeTest(bitrate_kbps=3000)}
        self.assertEqual([ch.id for ch in rank_members([a, b], latest)], [1, 2])

    def test_measured_bitrate_beats_unmeasured_on_a_score_tie(self):
        unmeasured = FakeChannel(1, health=80)
        measured = FakeChannel(2, health=80)
        # Channel 1 has no entry in the map at all; channel 2's test has no bitrate reading.
        untested_measured = FakeChannel(3, health=80)
        latest = {2: FakeTest(bitrate_kbps=None), 3: FakeTest(bitrate_kbps=1)}
        self.assertEqual(
            [ch.id for ch in rank_members([unmeasured, measured, untested_measured], latest)],
            [3, 1, 2])

    def test_no_latest_by_channel_keeps_id_tiebreak(self):
        # Omitting latest_by_channel entirely (every caller predating that parameter)
        # must behave exactly as before - pure id ascending on a score tie.
        a = FakeChannel(2, health=80)
        b = FakeChannel(1, health=80)
        self.assertEqual([ch.id for ch in rank_members([a, b])], [1, 2])


class StreakRankingTests(unittest.TestCase):
    """dev/changelog/478: a channel on an active consecutive-failure streak ranks
    last-resort in rank_members/pick_best_member - never excluded outright, so a group
    where every member is streaking still hands back its best-scoring one (CLAUDE.md
    Product Principle 2: never abandon the recording)."""

    def test_is_streaking(self):
        self.assertTrue(is_streaking(FakeChannel(1, streak=3), streak_threshold=3))
        self.assertFalse(is_streaking(FakeChannel(1, streak=2), streak_threshold=3))
        self.assertFalse(is_streaking(FakeChannel(1, streak=99), streak_threshold=0),
                         'a 0 threshold disables the streak concept entirely')

    def test_streaking_member_ranks_below_a_healthy_lower_scored_one(self):
        # Streaking member scores HIGHER than the healthy one - streak still wins the
        # ranking (this is exactly the real-data case: ch 9083 sat at score 46.7 after
        # 17 straight failures, well above a merely-mediocre healthy member).
        streaking = FakeChannel(1, health=90, streak=5)
        healthy = FakeChannel(2, health=40, streak=0)
        self.assertEqual([ch.id for ch in rank_members([streaking, healthy], streak_threshold=3)],
                         [2, 1])
        self.assertIs(pick_best_member([streaking, healthy], streak_threshold=3), healthy)

    def test_all_streaking_still_picks_the_best_scored_one(self):
        # Never abandon the recording for lack of a "clean" candidate.
        a = FakeChannel(1, health=30, streak=5)
        b = FakeChannel(2, health=70, streak=5)
        self.assertIs(pick_best_member([a, b], streak_threshold=3), b)

    def test_streak_below_threshold_does_not_demote(self):
        a = FakeChannel(1, health=90, streak=2)
        b = FakeChannel(2, health=40, streak=0)
        self.assertIs(pick_best_member([a, b], streak_threshold=3), a)

    def test_default_threshold_matches_config_default(self):
        # rank_members/pick_best_member's own default (no cfg in scope) must equal
        # channel_testing.failing_streak_threshold's own default (app/config.py).
        from app.config import _DEFAULTS
        from app.channel_groups import DEFAULT_FAILING_STREAK_THRESHOLD
        self.assertEqual(DEFAULT_FAILING_STREAK_THRESHOLD,
                         _DEFAULTS['channel_testing']['failing_streak_threshold'])


# ── recording_members ────────────────────────────────────────────────────────

class RecordingMembersTests(unittest.TestCase):
    def test_excludes_members_with_recording_off(self):
        on = FakeChannel(1)
        off = FakeChannel(2, disabled='manual')
        result = recording_members(_memberships([on, off]))
        self.assertEqual([ch.id for ch in result], [1])


# ── group_reference_key ──────────────────────────────────────────────────────

class ReferenceKeyTests(unittest.TestCase):
    def test_derived_from_best_scored_member(self):
        g = FakeGroup()
        best = FakeChannel(1, health=90)
        worse = FakeChannel(2, health=40)
        latest = {1: _test(HD), 2: _test(SD)}
        self.assertEqual(group_reference_key(g, _memberships([best, worse]), latest), HD)

    def test_locked_overrides_derivation(self):
        g = FakeGroup(res='1280x720', fps=30)  # locked SD
        best = FakeChannel(1, health=90)
        latest = {1: _test(HD)}  # member is HD, but lock wins
        self.assertEqual(group_reference_key(g, _memberships([best]), latest), SD)

    def test_recording_disabled_excluded_from_derivation(self):
        g = FakeGroup()
        # highest score has Recording off → its format must NOT set the reference
        disabled_best = FakeChannel(1, health=99, disabled='manual')
        enabled = FakeChannel(2, health=50)
        latest = {1: _test(SD), 2: _test(HD)}
        self.assertEqual(group_reference_key(g, _memberships([disabled_best, enabled]), latest), HD)

    def test_none_when_no_member_records(self):
        """A group with nothing enabled for recording has no format reference: it is not
        a recording source, so there is no format it "should" be."""
        g = FakeGroup()
        only = FakeChannel(1, health=99, disabled='manual')
        self.assertIsNone(group_reference_key(g, _memberships([only]), {1: _test(HD)}))

    def test_none_when_no_known_format(self):
        g = FakeGroup()
        members = [FakeChannel(1), FakeChannel(2)]
        self.assertIsNone(group_reference_key(g, _memberships(members), {1: None, 2: None}))


# ── group_format_outliers ────────────────────────────────────────────────────

class OutlierTests(unittest.TestCase):
    def test_outliers_against_explicit_reference(self):
        members = [FakeChannel(1), FakeChannel(2), FakeChannel(3)]
        latest = {1: _test(HD), 2: _test(SD), 3: _test(SD)}
        ref, outliers = group_format_outliers(members, latest, reference_key=HD)
        self.assertEqual(ref, HD)
        self.assertEqual(sorted(ch.id for ch in outliers), [2, 3])

    def test_untested_never_outlier(self):
        members = [FakeChannel(1), FakeChannel(2)]
        latest = {1: _test(HD), 2: None}
        _, outliers = group_format_outliers(members, latest, reference_key=HD)
        self.assertEqual(outliers, [])

    def test_none_reference_means_no_outliers(self):
        members = [FakeChannel(1)]
        _, outliers = group_format_outliers(members, {1: _test(HD)}, reference_key=None)
        self.assertEqual(outliers, [])

    def test_legacy_derive_uses_best_member(self):
        # _UNSET default derives ref from best-ranked known-format member
        members = [FakeChannel(1, health=90), FakeChannel(2, health=40)]
        latest = {1: _test(HD), 2: _test(SD)}
        ref, outliers = group_format_outliers(members, latest)
        self.assertEqual(ref, HD)
        self.assertEqual([ch.id for ch in outliers], [2])


# ── classify_group_formats ───────────────────────────────────────────────────

class ClassifyTests(unittest.TestCase):
    def test_buckets_untested_and_distinct_count(self):
        chans = [FakeChannel(1), FakeChannel(2), FakeChannel(3), FakeChannel(4)]
        latest = {1: _test(HD), 2: _test(HD), 3: _test(SD), 4: None}
        cls = classify_group_formats(chans, latest)
        self.assertEqual(cls['distinct_known'], 2)
        self.assertEqual({ch.id for ch in cls['buckets'][HD]}, {1, 2})
        self.assertEqual({ch.id for ch in cls['buckets'][SD]}, {3})
        self.assertEqual([ch.id for ch in cls['untested']], [4])


# ── plan_reconcile ───────────────────────────────────────────────────────────
#
# Detection only. The engine used to auto-disable outliers and auto-re-enable them on
# recovery; that write half is gone (DESIGN-channel-groups-model.md 4.1) - a participation
# checkbox is written by a human and by nothing else, and the format lock filters where
# members are chosen instead.

class PlanReconcileTests(unittest.TestCase):
    def _mixed_group(self):
        """HD reference member (best) + two SD outliers, all recording-enabled."""
        g = FakeGroup()
        members = [
            FakeChannel(1, health=100),                 # HD, reference
            FakeChannel(2, health=80),                  # SD outlier
            FakeChannel(3, health=70),                  # SD outlier
        ]
        latest = {1: _test(HD), 2: _test(SD), 3: _test(SD)}
        return g, members, latest

    def test_detects_outliers_against_the_reference(self):
        g, members, latest = self._mixed_group()
        plan = plan_reconcile(g, _memberships(members), latest)
        self.assertEqual(plan['reference'], HD)
        self.assertEqual(plan['outliers'], [2, 3])
        self.assertTrue(plan['eligible_member_matches_reference'])

    def test_no_eligible_member_matches_a_lock_nothing_conforms_to(self):
        # Locked HD, the only member is SD: nothing matches, which is the loud state
        # callers surface. It is never a reason to skip a recording (15.2).
        g = FakeGroup(res='1920x1080', fps=60)
        members = [FakeChannel(1, health=100)]
        latest = {1: _test(SD)}
        plan = plan_reconcile(g, _memberships(members), latest)
        self.assertEqual(plan['outliers'], [1])
        self.assertFalse(plan['eligible_member_matches_reference'])

    def test_plan_carries_no_write_instructions(self):
        """The whole point of 4.1: detection produces a diff to SHOW, never a list of
        rows to change. A key here telling a caller to untick something would be the
        auto-disable engine growing back."""
        g, members, latest = self._mixed_group()
        plan = plan_reconcile(g, _memberships(members), latest)
        self.assertEqual(set(plan),
                         {'reference', 'locked', 'outliers',
                          'eligible_member_matches_reference', 'format_override'})

    def test_recording_disabled_member_is_still_reported_as_an_outlier(self):
        # It is filtered out of selection, but the mismatch is still a fact the group
        # page and the event log show - nothing is hidden just because it is off.
        g = FakeGroup()
        members = [FakeChannel(1, health=100), FakeChannel(2, health=80, disabled='manual')]
        latest = {1: _test(HD), 2: _test(SD)}
        plan = plan_reconcile(g, _memberships(members), latest)
        self.assertEqual(plan['reference'], HD)
        self.assertEqual(plan['outliers'], [2])

    def test_planning_twice_gives_the_same_answer(self):
        """Idempotent by construction now that nothing is applied between runs."""
        g, members, latest = self._mixed_group()
        ms = _memberships(members)
        self.assertEqual(plan_reconcile(g, ms, latest), plan_reconcile(g, ms, latest))


# ── suggest_candidates (BUGS.md Part B1 invariant) ───────────────────────────

class SuggestClassificationTests(unittest.TestCase):
    def test_suggest_720p_in_mixed_group_is_different(self):
        """BUGS.md 2026-07-17: for a group whose effective reference is 1920x1080@60,
        a 1280x720 candidate must classify as 'different' (not 'confirmed'), even when
        another group member is itself 720p - classification is against the single
        group reference, not per-seed."""
        seed = FakeChannel(1, name='FS2', epg='fs2')
        # candidate shares epg+name so it is suggested; its format is 720p
        candidate = FakeChannel(99, name='FS2', epg='fs2')
        latest = {1: _test(HD), 99: _test(SD)}
        matches = suggest_candidates(seed, [seed, candidate], latest, reference_key=HD)
        found = {ch.id: status for ch, _reason, status in matches}
        self.assertEqual(found.get(99), 'different')

    def test_suggest_matching_format_is_confirmed(self):
        seed = FakeChannel(1, name='FS2', epg='fs2')
        candidate = FakeChannel(99, name='FS2', epg='fs2')
        latest = {1: _test(HD), 99: _test(HD)}
        matches = suggest_candidates(seed, [seed, candidate], latest, reference_key=HD)
        found = {ch.id: status for ch, _reason, status in matches}
        self.assertEqual(found.get(99), 'confirmed')

    def test_suggest_unknown_format_is_unverified(self):
        seed = FakeChannel(1, name='FS2', epg='fs2')
        candidate = FakeChannel(99, name='FS2', epg='fs2')
        latest = {1: _test(HD), 99: None}  # candidate untested
        matches = suggest_candidates(seed, [seed, candidate], latest, reference_key=HD)
        found = {ch.id: status for ch, _reason, status in matches}
        self.assertEqual(found.get(99), 'unverified')


if __name__ == '__main__':
    unittest.main(verbosity=2)
