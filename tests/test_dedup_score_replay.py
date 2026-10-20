"""Removing duplicate channels from a health check replays each channel's score from the
tests that remain (dev/docs/BUGS.md 2026-10-20, dev/changelog/1244).

remove_duplicate_channels deleted a channel's ChannelTest rows on that check through
delete_tests_collecting_screenshots() and never replayed the score, so whatever those tests
did to Channel.health_score stayed baked in with no test left on record to explain it - while
deleting one test from Test History replayed (dev/changelog/1149). Now the route goes through
health_recompute.apply_tests_deletion(), the bulk form of that replay: a channel where a
deleted test counted is rewritten from what remains, a channel where none did is left exactly
where it was (no replay, so no pruned residual is dropped), and the move is on the timeline.

  python3 -m unittest tests.test_dedup_score_replay
"""
import unittest
from datetime import datetime, timedelta

from app import db
from app.database import (CHANNEL_HEALTH_RECOMPUTED, Channel, ChannelEvent, ChannelTest)
from app.health_score import apply_test_health_observation
from tests.support import seed
from tests.test_health_rollback import _Base


class _DedupBase(_Base):
    def setUp(self):
        super().setUp()
        self.loser = self.channel
        self.keeper = seed.make_channel(self.account, name='Keeper')
        self.loser.stream_url = self.keeper.stream_url = 'http://example.test/live/shared'
        self.job = seed.make_test_job(name='Dedup', channels=[self.loser, self.keeper])
        db.session.commit()

    def _counted_test(self, channel, job_id, frame_pct=100.0, minutes_ago=60):
        ct = seed.make_channel_test(channel, status='COMPLETED', duration_seconds=120,
                                    frame_pct=frame_pct, drop_count=0, job_id=job_id)
        ct.test_started_at = datetime.utcnow() - timedelta(minutes=minutes_ago)
        db.session.commit()
        apply_test_health_observation(self.t.app, ct.id)
        db.session.expire_all()
        return ct

    def _remove_loser(self):
        return self.client.post(
            f'/api/channel-tests/on-demand/{self.job.id}/remove-duplicates',
            json={'removals': [{'channel_id': self.loser.id, 'keep_channel_id': self.keeper.id}],
                  'transfer': False})

    def _recomputes(self, channel_id):
        return ChannelEvent.query.filter_by(channel_id=channel_id,
                                            event_type=CHANNEL_HEALTH_RECOMPUTED).all()


class CountedTestsAreReplayedTests(_DedupBase):
    def test_a_channel_whose_only_tests_were_on_this_check_loses_its_score(self):
        self._counted_test(self.loser, self.job.id, frame_pct=40.0, minutes_ago=200)
        self._counted_test(self.loser, self.job.id, frame_pct=100.0, minutes_ago=100)
        self.assertIsNotNone(db.session.get(Channel, self.loser.id).health_score)

        resp = self._remove_loser()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        db.session.expire_all()
        loser = db.session.get(Channel, self.loser.id)
        self.assertEqual(ChannelTest.query.filter_by(channel_id=loser.id).count(), 0)
        self.assertIsNone(loser.health_score)
        self.assertEqual(loser.health_score_sample_count, 0)
        events = self._recomputes(loser.id)
        self.assertEqual(len(events), 1)
        self.assertIn('health check "Dedup"', events[0].detail)

    def test_a_test_on_another_check_keeps_counting(self):
        other = seed.make_test_job(name='Other', channels=[self.loser])
        db.session.commit()
        kept = self._counted_test(self.loser, other.id, frame_pct=100.0, minutes_ago=300)
        self._counted_test(self.loser, self.job.id, frame_pct=20.0, minutes_ago=100)
        self._remove_loser()
        db.session.expire_all()
        loser = db.session.get(Channel, self.loser.id)
        self.assertIsNotNone(db.session.get(ChannelTest, kept.id))
        self.assertEqual(loser.health_score_sample_count, 1)
        self.assertAlmostEqual(loser.health_score, 100.0, places=3)

    def test_the_keeper_is_untouched(self):
        self._counted_test(self.keeper, self.job.id, frame_pct=90.0)
        before = db.session.get(Channel, self.keeper.id).health_score
        self._counted_test(self.loser, self.job.id, frame_pct=50.0)
        self._remove_loser()
        db.session.expire_all()
        self.assertEqual(db.session.get(Channel, self.keeper.id).health_score, before)
        self.assertEqual(self._recomputes(self.keeper.id), [])


class UncountedTestsLeaveTheScoreAloneTests(_DedupBase):
    def test_no_replay_and_no_event_when_nothing_deleted_was_counting(self):
        """A never-scored row on this check and a counting one elsewhere: the score must not
        move, because a replay would also drop any pruned residual for no visible reason."""
        other = seed.make_test_job(name='Other', channels=[self.loser])
        db.session.commit()
        self._counted_test(self.loser, other.id, frame_pct=80.0)
        seed.make_channel_test(self.loser, job_id=self.job.id)  # never scored
        db.session.commit()
        loser = db.session.get(Channel, self.loser.id)
        before, count_before = loser.health_score, loser.health_score_sample_count

        self._remove_loser()
        db.session.expire_all()
        loser = db.session.get(Channel, self.loser.id)
        self.assertEqual(ChannelTest.query.filter_by(channel_id=loser.id, job_id=self.job.id).count(), 0)
        self.assertEqual(loser.health_score, before)
        self.assertEqual(loser.health_score_sample_count, count_before)
        self.assertEqual(self._recomputes(loser.id), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
