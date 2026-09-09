"""Tier 1 pure units for the health-score math (app/health_score.py). All pure - a fake
channel object and a cfg dict, no DB, no I/O (blend_health_score's docstring: "Pure - no I/O").

Covers the count-based decay blend, the duration-scaled observation_weight() formula, the
proportional-loss + instability recording/test quality formula, the capture-quality
correction formula, and the failover-share half of BUGS.md 2026-07-17 08:50: a group
recording's final member is scored from only its own post-failover share, so a clean final
window (0 downtime / 0 restarts) scores 100 no matter how bad the abandoned feeds were.

Also see the dev/docs/BUGS.md entry for the health-score-fix work (2026-08-06) this file
guards: blend_health_score used to weigh a new observation only by elapsed time since the
last one, never by how bad it was, so a bad observation arriving soon after a prior one was
diluted to near-zero regardless of severity.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.health_score import (  # noqa: E402
    blend_health_score, observation_weight, score_recording_metrics_quality,
    score_capture_quality_correction, apply_failover_health_observation,
)
from app import db  # noqa: E402
from app.database import Channel, ChannelEvent, CHANNEL_FAILOVER_HEALTH_OBSERVATION  # noqa: E402
from app.routes.channels import _build_channel_timeline  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

_CFG = {'channel_testing': {
    'health_score_half_life_samples': 5,
    'reference_minutes': 2,
    'instability_penalty_per_hr': 2.0,
    'recording_score': {
        'damage_penalty_per_pct_missing': 3,
        'near_empty_penalty_per_pct': 2,
        'capture_quality_source_weight': 1.0,
    },
}}


class FakeChannel:
    def __init__(self, health_score=None, updated_at=None, sample_count=0):
        self.health_score = health_score
        self.health_score_updated_at = updated_at
        self.health_score_sample_count = sample_count


class BlendHealthScoreTests(unittest.TestCase):
    def test_first_observation_taken_as_is(self):
        obs = datetime(2026, 7, 17, 12, 0)
        score, count, updated, bd = blend_health_score(FakeChannel(None), 80, obs, 1, _CFG)
        self.assertEqual(score, 80.0)
        self.assertEqual(count, 1)
        self.assertEqual(updated, obs)
        self.assertTrue(bd['first_observation'])

    def test_blend_moves_toward_new_observation(self):
        """Count-based decay: with half_life_samples=5 and weight=1, one observation's
        decay is 0.5**(1/5) ~= 0.87, so effective_alpha ~= 0.129 regardless of elapsed time."""
        anchor = datetime(2026, 7, 17, 12, 0)
        ch = FakeChannel(health_score=100.0, updated_at=anchor, sample_count=1)
        obs = anchor + timedelta(hours=1)
        score, count, updated, bd = blend_health_score(ch, 0, obs, 1, _CFG)
        self.assertTrue(0 < score < 100)
        self.assertAlmostEqual(bd['effective_alpha'], 0.129, delta=0.01)
        self.assertAlmostEqual(score, 87.1, delta=1.0)
        self.assertEqual(count, 2)
        self.assertEqual(updated, obs)

    def test_bad_observation_close_in_time_moves_score_same_as_far_apart(self):
        """The original bug: a bad observation arriving soon after the last one used to be
        diluted to near-zero weight purely from elapsed time (real example: channel 142011,
        13h after the last update, effective_alpha ~= 0.078, 100 -> 97.4). Under count-based
        decay, elapsed time plays no role at all - a bad observation 1 minute later moves the
        score by exactly as much as one a month later, for the same source_weight."""
        anchor = datetime(2026, 7, 17, 12, 0)
        soon = anchor + timedelta(minutes=1)
        far = anchor + timedelta(days=30)
        score_soon = blend_health_score(
            FakeChannel(100.0, anchor, 1), 10, soon, 3, _CFG)[0]
        score_far = blend_health_score(
            FakeChannel(100.0, anchor, 1), 10, far, 3, _CFG)[0]
        self.assertAlmostEqual(score_soon, score_far, delta=0.001)
        self.assertLess(score_soon, 90)  # a real, visible drop - not diluted to ~near-zero

    def test_stale_observation_does_not_move_anchor_backwards(self):
        """observed_at older than the current anchor contributes zero weight (explicit check
        under count-based decay, no elapsed-time term to fall back on) but must NOT pull
        health_score_updated_at backwards (docstring invariant)."""
        anchor = datetime(2026, 7, 17, 12, 0)
        ch = FakeChannel(health_score=90.0, updated_at=anchor, sample_count=3)
        older = anchor - timedelta(days=5)
        score, _, updated, bd = blend_health_score(ch, 0, older, 1, _CFG)
        self.assertEqual(updated, anchor)           # anchor unmoved
        self.assertEqual(score, 90.0)                # exactly zero weight on the stale obs
        self.assertEqual(bd['effective_alpha'], 0.0)

    def test_source_weight_increases_pull(self):
        anchor = datetime(2026, 7, 17, 12, 0)
        obs = anchor + timedelta(hours=1)
        light = blend_health_score(
            FakeChannel(100.0, anchor, 1), 0, obs, 1, _CFG)[0]
        heavy = blend_health_score(
            FakeChannel(100.0, anchor, 1), 0, obs, 3, _CFG)[0]
        self.assertLess(heavy, light)  # heavier source pulls further toward 0

    def test_float_source_weight_generalizes(self):
        """observation_weight() produces continuous floats (e.g. 5.48), not the old flat
        1/3 split - the decay**source_weight exponent must handle that without error."""
        anchor = datetime(2026, 7, 17, 12, 0)
        obs = anchor + timedelta(hours=1)
        score, _, _, bd = blend_health_score(FakeChannel(100.0, anchor, 1), 0, obs, 5.48, _CFG)
        self.assertTrue(0 <= score < 100)
        self.assertGreater(bd['effective_alpha'], 0)


class ObservationWeightTests(unittest.TestCase):
    def test_default_test_length_gets_weight_one(self):
        # 2 minutes == reference_minutes -> weight 1.0
        self.assertAlmostEqual(observation_weight(120, _CFG), 1.0, delta=0.001)

    def test_longer_observation_gets_more_weight_sqrt_scaled(self):
        # 1 hour recording: sqrt(60/2) = sqrt(30) ~= 5.477
        self.assertAlmostEqual(observation_weight(3600, _CFG), 5.477, delta=0.01)

    def test_long_test_outweighs_short_recording(self):
        """The bug that started the redesign: a 30-min test should outweigh a 15-min
        recording (it observed twice the real time), not lose to it 3-to-1 under the old
        flat source_weight split."""
        test_weight = observation_weight(30 * 60, _CFG)
        recording_weight = observation_weight(15 * 60, _CFG)
        self.assertGreater(test_weight, recording_weight)

    def test_zero_duration_does_not_error(self):
        w = observation_weight(0, _CFG)
        self.assertGreater(w, 0)
        self.assertLess(w, 1)


class RecordingMetricsQualityTests(unittest.TestCase):
    def test_clean_recording_scores_100(self):
        score, bd = score_recording_metrics_quality(0, 0, 3600, _CFG)
        self.assertEqual(score, 100)
        self.assertEqual(bd['penalties'], [])

    def test_downtime_penalized_proportionally(self):
        # 360s lost of 3600s = 10% -> base 90, no restarts -> no instability
        score, bd = score_recording_metrics_quality(360, 0, 3600, _CFG)
        self.assertEqual(score, 90)
        self.assertEqual(bd['base'], 90.0)

    def test_restarts_penalized_by_instability_rate(self):
        # 0 downtime, 2 restarts in 1 hour x 2.0/hr = -4 -> 96
        score, _ = score_recording_metrics_quality(0, 2, 3600, _CFG)
        self.assertEqual(score, 96)

    def test_score_clamped_to_zero(self):
        score, _ = score_recording_metrics_quality(3600, 100, 60, _CFG)
        self.assertEqual(score, 0)

    def test_five_hour_recording_with_small_real_loss_no_longer_tanks(self):
        """The defect that triggered the redesign: a 5-hour recording (18000s) with 19
        restarts but only 285s (1.58%) actually lost used to score 8/100 under the old
        flat-per-event formula - functionally indistinguishable from total failure. Under
        the proportional formula it should score in the low 90s."""
        score, _ = score_recording_metrics_quality(285, 19, 18000, _CFG)
        self.assertGreater(score, 85)
        self.assertLess(score, 100)

    def test_many_short_interruptions_score_worse_than_one_long_one(self):
        """Same total downtime (300s), different event counts: 10 short stalls should score
        worse than 1 long stall of the same total length (chronic flakiness vs. one
        contained incident) - the whole point of the separate instability term."""
        one_long, _ = score_recording_metrics_quality(300, 1, 3600, _CFG)
        ten_short, _ = score_recording_metrics_quality(300, 10, 3600, _CFG)
        self.assertLess(ten_short, one_long)

    def test_failover_final_member_share_clean_window_scores_100(self):
        """BUGS.md 2026-07-17 08:50: the final member's share is (cumulative - snapshot).
        A clean final window scores 100 even when the whole recording's cumulative counts
        (attributed to earlier, abandoned feeds) were catastrophic."""
        cumulative = {'downtime': 400.0, 'restarts': 30}
        snapshot = {'downtime': 400.0, 'restarts': 30}  # everything happened before failover
        share_downtime = max(0.0, cumulative['downtime'] - snapshot['downtime'])
        share_restarts = max(0, cumulative['restarts'] - snapshot['restarts'])
        score, _ = score_recording_metrics_quality(share_downtime, share_restarts, 300, _CFG)
        self.assertEqual(score, 100)

    def test_whole_recording_scored_when_no_failover(self):
        # sanity: same formula on cumulative (share == cumulative) still penalizes
        score, _ = score_recording_metrics_quality(280, 30, 300, _CFG)
        self.assertEqual(score, 0)


class CaptureQualityCorrectionTests(unittest.TestCase):
    def test_no_damage_or_near_empty_scores_100(self):
        score, bd = score_capture_quality_correction(0, 0, 3600, _CFG)
        self.assertEqual(score, 100)
        self.assertEqual(bd['penalties'], [])

    def test_damage_penalized_by_pct_missing(self):
        # 360s missing of 3600s = 10% x 3/pct = -30 -> 70
        score, _ = score_capture_quality_correction(360, 0, 3600, _CFG)
        self.assertEqual(score, 70)

    def test_near_empty_penalized_by_pct(self):
        # 360s near-empty of 3600s = 10% x 2/pct = -20 -> 80
        score, _ = score_capture_quality_correction(0, 360, 3600, _CFG)
        self.assertEqual(score, 80)

    def test_both_signals_combine_and_never_read_downtime(self):
        """Confirms the double-counting resolution: this formula's signature has no
        downtime/restart parameter at all - only timeline damage and near-empty are scored
        here, since the primary observation already accounts for downtime."""
        score, bd = score_capture_quality_correction(360, 360, 3600, _CFG)
        self.assertEqual(score, 50)  # 100 - 30 - 20
        reasons = {p['reason'] for p in bd['penalties']}
        self.assertEqual(reasons, {'timeline_damage', 'near_empty'})

    def test_clamped_to_zero(self):
        score, _ = score_capture_quality_correction(3600, 3600, 3600, _CFG)
        self.assertEqual(score, 0)


class FailoverHealthObservationVisibilityTests(unittest.TestCase):
    """Guards dev/docs/BUGS.md 2026-08-10: a group-backed recording that failed over
    mid-flight dinged the *abandoned* channel's health_score with no visible record
    anywhere - not a ChannelEvent, not on that channel's own Activity Timeline (the
    recording's own channel_id had already moved to the new member). This asserts both
    halves: apply_failover_health_observation now writes a ChannelEvent carrying a
    blend_breakdown, and _build_channel_timeline (the Activity Timeline's builder) surfaces
    it with that breakdown attached, the same way it already does for ChannelTest/Recording
    entries."""

    def setUp(self):
        self.t = make_test_app()
        with self.t.app.app_context():
            acct = seed.make_account(name='Failover Acct')
            ch = seed.make_channel(acct, name='Abandoned Channel')
            ch.health_score = 100.0
            ch.health_score_sample_count = 2
            db.session.commit()
            self.channel_id = ch.id
            rec = seed.make_recording(status='IN_PROGRESS', channel_id=ch.id)
            db.session.commit()
            self.recording_id = rec.id

    def tearDown(self):
        self.t.cleanup()

    def test_writes_a_channel_event_with_blend_breakdown(self):
        apply_failover_health_observation(
            self.t.app, self.channel_id, self.recording_id,
            reason='restart produced no data')
        with self.t.app.app_context():
            events = ChannelEvent.query.filter_by(
                channel_id=self.channel_id,
                event_type=CHANNEL_FAILOVER_HEALTH_OBSERVATION).all()
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertIn('restart produced no data', event.detail)
            extra = json.loads(event.extra_data)
            self.assertEqual(extra['recording_id'], self.recording_id)
            self.assertEqual(extra['reason'], 'restart produced no data')
            bd = extra['blend_breakdown']
            self.assertEqual(bd['old_score'], 100.0)
            self.assertLess(bd['new_score'], 100.0)

    def test_appears_on_the_channel_activity_timeline_with_breakdown(self):
        apply_failover_health_observation(
            self.t.app, self.channel_id, self.recording_id,
            reason='restart produced no data')
        with self.t.app.app_context():
            entries = _build_channel_timeline(self.channel_id)
            failover_entries = [e for e in entries if e['kind'] == 'channel_event'
                                and e['obj'].event_type == CHANNEL_FAILOVER_HEALTH_OBSERVATION]
            self.assertEqual(len(failover_entries), 1)
            entry = failover_entries[0]
            self.assertIsNotNone(entry['blend_breakdown'])
            self.assertEqual(entry['blend_breakdown']['old_score'], 100.0)

    def test_channel_score_is_actually_reduced(self):
        """Control: proves the fail floor genuinely moves the score, so the entry above
        is reporting a real event rather than a no-op."""
        with self.t.app.app_context():
            ch = db.session.get(Channel, self.channel_id)
            self.assertEqual(ch.health_score, 100.0)
        apply_failover_health_observation(
            self.t.app, self.channel_id, self.recording_id, reason='restart produced no data')
        with self.t.app.app_context():
            db.session.expire_all()
            ch = db.session.get(Channel, self.channel_id)
            self.assertLess(ch.health_score, 100.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
