"""Rolling a channel's health score back by hand: full reset, and step back one observation.

`Channel.health_score` is a lossy exponential average (app/health_score.py::
blend_health_score), so an observation cannot be subtracted back out of it. Both user
actions are therefore a REPLAY of the observations that still count, in observation order
(app/health_recompute.py) - the "already done is a fact you recorded" rule applied to a
number rather than to a migration step.

The load-bearing invariant is the first test below: a replay with nothing excluded has to
reproduce the score the live blending path actually produced, or every rollback silently
invents a different number. That works because every blending path persists the
`source_weight` it ran with, and the ledger reads those back rather than re-deriving them.

Also covered: a reset really does return the channel to "never observed" (score, sample
count, failure streak and the manual offset all together); step-back is repeatable down to
no score; nothing is deleted; both actions are loud on the channel's Activity Timeline; and
a reset channel becomes selectable again in a group, which is the concrete symptom that
motivated the feature (a mistimed first test pinning a high-bitrate channel below its
peers - dev/changelog/895).

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_health_rollback
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (Channel, ChannelEvent, ChannelHealthExclusion, ChannelTest,  # noqa: E402
                          Recording, CHANNEL_HEALTH_ROLLBACK,
                          CHANNEL_HEALTH_OVERRIDE_CHANGED,
                          CHANNEL_FAILOVER_HEALTH_OBSERVATION)
from app.health_recompute import (SOURCE_FAILOVER, SOURCE_RECORDING, SOURCE_TEST,  # noqa: E402
                                  apply_rollback, observation_ledger, replay,
                                  recompute_failure_streak, rollback_preview)
from app.health_score import apply_test_health_observation, observation_weight  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = seed.make_account()
        self.channel = seed.make_channel(self.account, name='Rollback Channel')
        db.session.commit()
        self.cfg = load_config()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _blended_test(self, status='COMPLETED', frame_pct=100.0, drops=0,
                      minutes_ago=60, duration=120):
        """A ChannelTest put through the REAL blending path, so its stored quality_score,
        blend_breakdown and the channel's score are exactly what production would hold."""
        ct = seed.make_channel_test(
            self.channel, status=status, duration_seconds=duration,
            frame_pct=frame_pct, drop_count=drops)
        # seed.make_channel_test stamps test_started_at itself, so the age is set after.
        ct.test_started_at = datetime.utcnow() - timedelta(minutes=minutes_ago)
        db.session.commit()
        apply_test_health_observation(self.t.app, ct.id)
        db.session.expire_all()
        return ct


class ReplayFidelityTests(_Base):
    """A replay with nothing excluded reproduces the live blending path's own answer.

    This is what makes every rollback trustworthy: if the engine disagreed with history
    when nothing was removed, the number it produces after removing something would be
    unexplainable in exactly the way this feature exists to prevent.
    """

    def test_replay_of_full_ledger_matches_the_blended_score(self):
        for i, pct in enumerate((60.0, 100.0, 85.0, 100.0)):
            self._blended_test(frame_pct=pct, minutes_ago=300 - i * 40, duration=120 + i * 30)
        channel = db.session.get(Channel, self.channel.id)
        live_score = channel.health_score
        self.assertIsNotNone(live_score)

        ledger = observation_ledger(channel.id, self.cfg)
        self.assertEqual(len(ledger), 4)
        score, count, updated_at = replay(ledger, self.cfg)

        self.assertAlmostEqual(score, live_score, places=6)
        self.assertEqual(count, channel.health_score_sample_count)
        self.assertEqual(updated_at, channel.health_score_updated_at)

    def test_ledger_reads_back_the_weight_each_blend_actually_used(self):
        """Weights come from the stored blend_breakdown, NOT re-derived from the row.

        The two agree for a row nothing has touched, which is why this test forces them
        apart: the stored weight is the historical fact, and re-deriving it would quietly
        rewrite history for any row whose duration was corrected afterwards.
        """
        ct = self._blended_test(duration=900)
        derived = observation_weight(ct.duration_seconds, self.cfg)
        ct.blend_breakdown = json.dumps({'source_weight': derived * 4})
        db.session.commit()

        weight = observation_ledger(self.channel.id, self.cfg)[0].weight
        self.assertAlmostEqual(weight, derived * 4, places=6)
        self.assertNotAlmostEqual(weight, derived, places=6)

    def test_a_row_that_stored_no_weight_falls_back_to_its_duration(self):
        """Rows predating the breakdown columns still replay, from what they do carry."""
        ct = self._blended_test(duration=900)
        ct.blend_breakdown = None
        db.session.commit()
        self.assertAlmostEqual(observation_ledger(self.channel.id, self.cfg)[0].weight,
                               observation_weight(900, self.cfg), places=6)

    def test_recordings_and_failover_events_are_observations_too(self):
        """The ledger is not just tests - four sources move this score."""
        self._blended_test()
        rec = seed.make_recording(
            status='COMPLETED', channel_id=self.channel.id,
            completed_at=datetime.utcnow() - timedelta(minutes=30),
            health_quality_score=72,
            health_blend_breakdown=json.dumps({'source_weight': 3.0}),
            capture_quality_breakdown=json.dumps({'final': 88, 'penalties': []}))
        db.session.add(ChannelEvent(
            channel_id=self.channel.id, timestamp=datetime.utcnow() - timedelta(minutes=10),
            event_type=CHANNEL_FAILOVER_HEALTH_OBSERVATION, detail='seed',
            extra_data=json.dumps({'quality': 5, 'blend_breakdown': {'source_weight': 2.0}})))
        db.session.commit()

        kinds = [o.kind for o in observation_ledger(self.channel.id, self.cfg)]
        self.assertEqual(kinds.count(SOURCE_TEST), 1)
        self.assertEqual(kinds.count(SOURCE_RECORDING), 1)
        self.assertEqual(kinds.count(SOURCE_FAILOVER), 1)
        self.assertIn('capture_correction', kinds)
        self.assertEqual(rec.health_quality_score, 72)

    def test_ledger_is_ordered_oldest_first(self):
        self._blended_test(minutes_ago=10)
        self._blended_test(minutes_ago=500)
        self._blended_test(minutes_ago=200)
        stamps = [o.observed_at for o in observation_ledger(self.channel.id, self.cfg)]
        self.assertEqual(stamps, sorted(stamps))


class ResetTests(_Base):
    def test_reset_returns_the_channel_to_never_observed(self):
        self._blended_test(status='FAILED', minutes_ago=200)
        self._blended_test(status='FAILED', minutes_ago=100)
        channel = db.session.get(Channel, self.channel.id)
        self.assertIsNotNone(channel.health_score)
        self.assertEqual(channel.consecutive_test_failures, 2)

        result = apply_rollback(channel, 'reset', self.cfg)
        db.session.commit()
        db.session.expire_all()

        channel = db.session.get(Channel, self.channel.id)
        self.assertIsNone(channel.health_score)
        self.assertEqual(channel.health_score_sample_count, 0)
        self.assertIsNone(channel.health_score_updated_at)
        # The streak is a second score-adjacent signal - a reset channel still reading
        # "failed its last 2 checks" is still reported as failing (channel_failing_reason).
        self.assertEqual(channel.consecutive_test_failures, 0)
        self.assertEqual(result['excluded'], 2)

    def test_reset_clears_the_manual_offset_and_says_so(self):
        """A hand-set offset on a channel with no observations is unexplainable, so it goes
        too - with its own CHANNEL_HEALTH_OVERRIDE_CHANGED event."""
        self._blended_test()
        channel = db.session.get(Channel, self.channel.id)
        channel.manual_health_adjustment = 25
        channel.manual_health_note = 'nudged by hand'
        db.session.commit()

        apply_rollback(channel, 'reset', self.cfg)
        db.session.commit()
        db.session.expire_all()

        channel = db.session.get(Channel, self.channel.id)
        self.assertEqual(channel.manual_health_adjustment, 0)
        self.assertIsNone(channel.manual_health_note)
        ev = ChannelEvent.query.filter_by(
            channel_id=channel.id, event_type=CHANNEL_HEALTH_OVERRIDE_CHANGED).one()
        self.assertEqual(json.loads(ev.extra_data)['cleared_by'], 'health_reset')

    def test_reset_deletes_nothing(self):
        """Excluding, not deleting - an observation may be a Recording."""
        ct = self._blended_test()
        rec = seed.make_recording(
            status='COMPLETED', channel_id=self.channel.id,
            completed_at=datetime.utcnow(), health_quality_score=40,
            health_blend_breakdown=json.dumps({'source_weight': 1.0}))
        db.session.commit()

        channel = db.session.get(Channel, self.channel.id)
        apply_rollback(channel, 'reset', self.cfg)
        db.session.commit()

        self.assertIsNotNone(db.session.get(ChannelTest, ct.id))
        self.assertIsNotNone(db.session.get(Recording, rec.id))
        self.assertEqual(ChannelHealthExclusion.query.filter_by(
            channel_id=channel.id).count(), 2)

    def test_reset_writes_one_rollback_event_naming_the_move(self):
        self._blended_test()
        channel = db.session.get(Channel, self.channel.id)
        before = channel.health_score
        apply_rollback(channel, 'reset', self.cfg)
        db.session.commit()

        ev = ChannelEvent.query.filter_by(
            channel_id=channel.id, event_type=CHANNEL_HEALTH_ROLLBACK).one()
        extra = json.loads(ev.extra_data)
        self.assertEqual(extra['action'], 'reset')
        self.assertAlmostEqual(extra['score_before'], before, places=6)
        self.assertIsNone(extra['score_after'])
        self.assertEqual(len(extra['observations_excluded']), 1)
        self.assertIn('reset', ev.detail.lower())

    def test_a_new_observation_after_a_reset_starts_the_score_over(self):
        """The excluded history stays excluded - the next test is a first observation."""
        self._blended_test(frame_pct=20.0, minutes_ago=400)
        channel = db.session.get(Channel, self.channel.id)
        apply_rollback(channel, 'reset', self.cfg)
        db.session.commit()

        fresh = self._blended_test(frame_pct=100.0, minutes_ago=1)
        channel = db.session.get(Channel, self.channel.id)
        self.assertEqual(channel.health_score_sample_count, 1)
        self.assertAlmostEqual(channel.health_score, float(fresh.quality_score), places=6)


class StepBackTests(_Base):
    def test_step_back_unwinds_only_the_newest_observation(self):
        old = self._blended_test(frame_pct=100.0, minutes_ago=400)
        self._blended_test(frame_pct=10.0, minutes_ago=10)
        channel = db.session.get(Channel, self.channel.id)
        dragged_down = channel.health_score

        apply_rollback(channel, 'step_back', self.cfg)
        db.session.commit()
        db.session.expire_all()

        channel = db.session.get(Channel, self.channel.id)
        self.assertGreater(channel.health_score, dragged_down)
        self.assertAlmostEqual(channel.health_score, float(old.quality_score), places=6)
        self.assertEqual(channel.health_score_sample_count, 1)

    def test_step_back_is_repeatable_down_to_no_score(self):
        for i in range(3):
            self._blended_test(minutes_ago=300 - i * 50)
        channel = db.session.get(Channel, self.channel.id)

        for expected_remaining in (2, 1, 0):
            result = apply_rollback(channel, 'step_back', self.cfg)
            db.session.commit()
            self.assertEqual(result['remaining'], expected_remaining)

        db.session.expire_all()
        channel = db.session.get(Channel, self.channel.id)
        self.assertIsNone(channel.health_score)
        self.assertEqual(channel.health_score_sample_count, 0)
        # Nothing left to unwind - the engine says so rather than writing an empty event.
        self.assertIsNone(apply_rollback(channel, 'step_back', self.cfg))

    def test_step_back_leaves_the_manual_offset_alone(self):
        """It unwinds one observation, and the offset was never one of them."""
        self._blended_test()
        self._blended_test(minutes_ago=5)
        channel = db.session.get(Channel, self.channel.id)
        channel.manual_health_adjustment = -15
        db.session.commit()

        apply_rollback(channel, 'step_back', self.cfg)
        db.session.commit()
        db.session.expire_all()

        self.assertEqual(db.session.get(Channel, self.channel.id).manual_health_adjustment, -15)

    def test_step_back_recomputes_the_failure_streak(self):
        self._blended_test(status='FAILED', minutes_ago=300)
        self._blended_test(status='FAILED', minutes_ago=200)
        self._blended_test(status='FAILED', minutes_ago=100)
        channel = db.session.get(Channel, self.channel.id)
        self.assertEqual(channel.consecutive_test_failures, 3)

        apply_rollback(channel, 'step_back', self.cfg)
        db.session.commit()
        db.session.expire_all()

        self.assertEqual(db.session.get(Channel, self.channel.id).consecutive_test_failures, 2)

    def test_streak_recompute_stops_at_the_newest_passing_test(self):
        self._blended_test(status='FAILED', minutes_ago=300)
        self._blended_test(status='COMPLETED', minutes_ago=200)
        self._blended_test(status='FAILED', minutes_ago=100)
        self.assertEqual(recompute_failure_streak(self.channel.id), 1)


class PreviewTests(_Base):
    """The confirm dialogs promise numbers; the same replay that will run produces them."""

    def test_preview_counts_available_step_backs_and_projects_the_next_score(self):
        old = self._blended_test(frame_pct=100.0, minutes_ago=400)
        self._blended_test(frame_pct=10.0, minutes_ago=10)
        channel = db.session.get(Channel, self.channel.id)

        preview = rollback_preview(channel, self.cfg)
        self.assertEqual(preview['available'], 2)
        self.assertEqual(preview['excluded_count'], 0)
        self.assertEqual(preview['next']['score_after'], round(old.quality_score))
        self.assertEqual(preview['next']['observations_after'], 1)

    def test_preview_promise_matches_what_the_action_does(self):
        self._blended_test(frame_pct=100.0, minutes_ago=400)
        self._blended_test(frame_pct=30.0, minutes_ago=10)
        channel = db.session.get(Channel, self.channel.id)
        promised = rollback_preview(channel, self.cfg)['next']['score_after']

        apply_rollback(channel, 'step_back', self.cfg)
        db.session.commit()
        db.session.expire_all()

        channel = db.session.get(Channel, self.channel.id)
        self.assertEqual(int(round(channel.health_score)), promised)

    def test_preview_names_observations_that_can_no_longer_be_replayed(self):
        """Retention deletes old ChannelTest rows, and a deleted row's contribution stays
        baked into the stored score with nothing left to replay it from. The shortfall is
        reported, not absorbed - otherwise a step-back moves the score by an amount nothing
        on screen accounts for (measured on the live database: channel 125 held 46 blended
        observations against 44 surviving rows).
        """
        ct = self._blended_test(minutes_ago=400)
        self._blended_test(minutes_ago=10)
        channel = db.session.get(Channel, self.channel.id)
        self.assertEqual(channel.health_score_sample_count, 2)

        db.session.delete(ct)          # what retention does
        db.session.commit()

        preview = rollback_preview(db.session.get(Channel, self.channel.id), self.cfg)
        self.assertEqual(preview['available'], 1)
        self.assertEqual(preview['unledgered'], 1)

    def test_preview_reports_no_shortfall_when_every_observation_survives(self):
        self._blended_test()
        self._blended_test(minutes_ago=5)
        preview = rollback_preview(db.session.get(Channel, self.channel.id), self.cfg)
        self.assertEqual(preview['unledgered'], 0)

    def test_preview_reports_nothing_available_once_everything_is_excluded(self):
        self._blended_test()
        channel = db.session.get(Channel, self.channel.id)
        apply_rollback(channel, 'reset', self.cfg)
        db.session.commit()
        db.session.expire_all()

        preview = rollback_preview(db.session.get(Channel, self.channel.id), self.cfg)
        self.assertEqual(preview['available'], 0)
        self.assertEqual(preview['excluded_count'], 1)
        self.assertIsNone(preview['next'])


class RouteTests(_Base):
    def _post(self, action, channel_id=None):
        return self.client.post(
            f'/channels/{channel_id or self.channel.id}/health/rollback',
            json={'action': action})

    def test_reset_route_rolls_the_score_back_and_returns_a_fresh_preview(self):
        self._blended_test()
        resp = self._post('reset')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['success'])
        self.assertIsNone(body['result']['score_after'])
        self.assertEqual(body['preview']['available'], 0)

        db.session.expire_all()
        self.assertIsNone(db.session.get(Channel, self.channel.id).health_score)

    def test_step_back_route(self):
        self._blended_test(minutes_ago=400)
        self._blended_test(minutes_ago=10)
        resp = self._post('step_back')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['preview']['available'], 1)

    def test_unknown_action_is_a_400(self):
        self._blended_test()
        self.assertEqual(self._post('obliterate').status_code, 400)

    def test_missing_channel_is_a_404(self):
        self.assertEqual(self._post('reset', channel_id=999999).status_code, 404)

    def test_nothing_left_to_unwind_is_a_409(self):
        resp = self._post('reset')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('error', resp.get_json())

    def test_detail_page_renders_the_rollback_controls(self):
        self._blended_test()
        html = self.client.get(f'/channels/{self.channel.id}').get_data(as_text=True)
        self.assertIn('data-act="health-step-back"', html)
        self.assertIn('data-act="health-reset"', html)
        self.assertIn('(1 available)', html)

    def test_detail_page_explains_a_reset_score_and_marks_the_excluded_row(self):
        self._blended_test()
        self._post('reset')
        html = self.client.get(f'/channels/{self.channel.id}').get_data(as_text=True)
        self.assertIn('No health score', html)
        self.assertIn('rolled back by hand', html)
        self.assertIn('NOT COUNTED', html)
        # Nothing left to unwind, so the kebab is gone rather than offering a refusal.
        self.assertNotIn('data-act="health-reset"', html)


class GroupSelectionTests(_Base):
    """The concrete symptom the feature was filed for: a mistimed first test pinned a
    high-bitrate channel below its peers and it stopped being picked to record."""

    def test_a_reset_channel_becomes_selectable_again(self):
        from app.channel_groups import effective_score, pick_best_member

        other = seed.make_channel(self.account, stream_id='2', name='Mediocre Channel')
        other.health_score = 55.0
        other.health_score_sample_count = 4
        db.session.commit()

        self._blended_test(status='FAILED')      # the mistimed first check
        channel = db.session.get(Channel, self.channel.id)
        self.assertLess(effective_score(channel), effective_score(other))
        self.assertEqual(pick_best_member([channel, other], {}).id, other.id)

        apply_rollback(channel, 'reset', self.cfg)
        db.session.commit()
        db.session.expire_all()

        channel = db.session.get(Channel, self.channel.id)
        # Unscored ranks as neutral 50 - still below 55, so the reset alone does not hand
        # it the win. What it does is stop the bad test from holding it at the fail floor.
        self.assertEqual(effective_score(channel), 50)
        self.assertGreater(effective_score(channel), 10)


if __name__ == '__main__':
    unittest.main()
