"""Deleting one health check from a channel's Test History, and replaying the score without it.

Unlike a reset or a step back (tests/test_health_rollback.py), this really deletes: the
ChannelTest row and its screenshot go, and the score is replayed from what remains
(app/health_recompute.py::apply_test_deletion, dev/changelog/1149). What has to hold:

  * a test that counted leaves the score exactly where a replay without it lands, and the
    failure streak is recomputed with it;
  * a test that did not count (already rolled back, or never scored) leaves the score
    untouched - no replay runs, so pruned residual is not silently dropped;
  * the dialog's promised number is the number the delete produces;
  * the route only deletes a test of the channel in the URL, and never one still running;
  * the screenshot is unlinked, and the timeline says what went and what the score did.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_health_test_delete
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.database import (Channel, ChannelEvent, ChannelHealthExclusion, ChannelTest,  # noqa: E402
                          CHANNEL_TEST_DELETED)
from app.health_recompute import (SOURCE_TEST, apply_rollback,  # noqa: E402
                                  observation_ledger, preview_test_deletion, replay)
from tests.support import seed  # noqa: E402
from tests.test_health_rollback import _Base  # noqa: E402


class _DeleteBase(_Base):
    def _delete(self, test_id, channel_id=None):
        return self.client.post(
            f'/api/channels/{channel_id or self.channel.id}/tests/{test_id}/delete')

    def _events(self):
        return (ChannelEvent.query
                .filter_by(channel_id=self.channel.id, event_type=CHANNEL_TEST_DELETED)
                .all())


class DeleteCountedTestTests(_DeleteBase):
    def test_score_is_the_replay_without_the_deleted_test(self):
        self._blended_test(frame_pct=100.0, minutes_ago=300)
        bad = self._blended_test(frame_pct=20.0, drops=9, minutes_ago=200)
        self._blended_test(frame_pct=100.0, minutes_ago=100)
        channel = db.session.get(Channel, self.channel.id)
        before = channel.health_score

        ledger = [o for o in observation_ledger(channel.id, self.cfg)
                  if o.key != (SOURCE_TEST, bad.id)]
        expected, expected_count, _ = replay(ledger, self.cfg)
        self.assertNotAlmostEqual(expected, before, places=3)

        resp = self._delete(bad.id)
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        channel = db.session.get(Channel, self.channel.id)
        self.assertAlmostEqual(channel.health_score, expected, places=6)
        self.assertEqual(channel.health_score_sample_count, expected_count)
        self.assertIsNone(db.session.get(ChannelTest, bad.id))

    def test_deleting_the_newest_failure_recomputes_the_streak(self):
        self._blended_test(status='COMPLETED', minutes_ago=300)
        self._blended_test(status='FAILED', frame_pct=0.0, minutes_ago=200)
        newest = self._blended_test(status='FAILED', frame_pct=0.0, minutes_ago=100)
        self.assertEqual(db.session.get(Channel, self.channel.id).consecutive_test_failures, 2)

        self._delete(newest.id)
        db.session.expire_all()
        self.assertEqual(db.session.get(Channel, self.channel.id).consecutive_test_failures, 1)

    def test_deleting_the_only_test_leaves_no_score(self):
        only = self._blended_test()
        self._delete(only.id)
        db.session.expire_all()
        channel = db.session.get(Channel, self.channel.id)
        self.assertIsNone(channel.health_score)
        self.assertEqual(channel.health_score_sample_count, 0)

    def test_event_names_the_test_and_the_score_move(self):
        self._blended_test(minutes_ago=300)
        bad = self._blended_test(frame_pct=20.0, drops=9, minutes_ago=100)
        before = db.session.get(Channel, self.channel.id).health_score

        self._delete(bad.id)
        db.session.expire_all()
        events = self._events()
        self.assertEqual(len(events), 1)
        extra = json.loads(events[0].extra_data)
        self.assertEqual(extra['test_id'], bad.id)
        self.assertTrue(extra['counted'])
        self.assertAlmostEqual(extra['score_before'], before, places=6)
        self.assertAlmostEqual(extra['score_after'],
                               db.session.get(Channel, self.channel.id).health_score, places=6)
        self.assertIn('Deleted Health check on', events[0].detail)
        self.assertIn('health score', events[0].detail)

    def test_preview_promises_what_the_delete_does(self):
        self._blended_test(minutes_ago=300)
        bad = self._blended_test(frame_pct=20.0, drops=9, minutes_ago=200)
        self._blended_test(minutes_ago=100)

        resp = self.client.get(f'/api/channels/{self.channel.id}/tests/{bad.id}/delete-preview')
        self.assertEqual(resp.status_code, 200)
        preview = resp.get_json()['preview']
        self.assertTrue(preview['counted'])

        self._delete(bad.id)
        db.session.expire_all()
        channel = db.session.get(Channel, self.channel.id)
        self.assertEqual(preview['score_after'], int(round(channel.health_score)))
        self.assertEqual(preview['observations_after'], channel.health_score_sample_count)

    def test_screenshot_file_is_unlinked(self):
        shot = os.path.join(self.t._tmpdir, 'shot-delete-me.jpg')
        with open(shot, 'wb') as f:
            f.write(b'jpg')
        ct = self._blended_test()
        ct = db.session.get(ChannelTest, ct.id)
        ct.screenshot_path = shot
        db.session.commit()

        self.assertEqual(self._delete(ct.id).status_code, 200)
        self.assertFalse(os.path.exists(shot))


class DeleteUncountedTestTests(_DeleteBase):
    def test_deleting_a_rolled_back_test_leaves_the_score_alone(self):
        self._blended_test(minutes_ago=300)
        newest = self._blended_test(frame_pct=20.0, drops=9, minutes_ago=100)
        apply_rollback(db.session.get(Channel, self.channel.id), 'step_back', self.cfg)
        db.session.commit()
        after_step_back = db.session.get(Channel, self.channel.id).health_score

        self._delete(newest.id)
        db.session.expire_all()
        self.assertAlmostEqual(db.session.get(Channel, self.channel.id).health_score,
                               after_step_back, places=6)
        # Its exclusion row goes with it - an id the next test may be issued.
        self.assertEqual(ChannelHealthExclusion.query.filter_by(
            source_kind=SOURCE_TEST, source_id=newest.id).count(), 0)
        self.assertFalse(json.loads(self._events()[0].extra_data)['counted'])

    def test_an_unscored_test_does_not_replay_and_drop_pruned_residual(self):
        """A stored score carrying residual from pruned tests must not move when a test that
        never counted is deleted - a replay there would be an unexplained score change."""
        self._blended_test()
        cancelled = seed.make_channel_test(self.channel, status='CANCELLED')
        channel = db.session.get(Channel, self.channel.id)
        channel.health_score = 73.25
        channel.health_score_sample_count = 5     # 4 of them pruned, nothing left to replay
        db.session.commit()

        preview = preview_test_deletion(channel, cancelled, self.cfg)
        self.assertFalse(preview['counted'])
        self.assertEqual(preview['score_after'], 73)

        self._delete(cancelled.id)
        db.session.expire_all()
        channel = db.session.get(Channel, self.channel.id)
        self.assertEqual(channel.health_score, 73.25)
        self.assertEqual(channel.health_score_sample_count, 5)


class RouteGuardTests(_DeleteBase):
    def test_a_test_of_another_channel_is_a_404(self):
        other = seed.make_channel(self.account, name='Other Channel')
        db.session.commit()
        ct = seed.make_channel_test(other, status='COMPLETED')
        db.session.commit()
        self.assertEqual(self._delete(ct.id).status_code, 404)
        self.assertEqual(self.client.get(
            f'/api/channels/{self.channel.id}/tests/{ct.id}/delete-preview').status_code, 404)
        self.assertIsNotNone(db.session.get(ChannelTest, ct.id))

    def test_missing_channel_or_test_is_a_404(self):
        ct = self._blended_test()
        self.assertEqual(self._delete(ct.id, channel_id=999999).status_code, 404)
        self.assertEqual(self._delete(999999).status_code, 404)

    def test_a_test_still_running_is_refused(self):
        running = seed.make_channel_test(self.channel, test_ended_at=None)
        db.session.commit()
        status = {'is_running': True, 'current_channel_id': self.channel.id}
        with mock.patch('app.routes.channels.get_status', return_value=status):
            resp = self._delete(running.id)
        self.assertEqual(resp.status_code, 409)
        self.assertIsNotNone(db.session.get(ChannelTest, running.id))

    def test_a_stranded_unfinished_row_can_be_deleted(self):
        """No run is going, so an unfinished row is debris from a killed run, not a live test."""
        stranded = seed.make_channel_test(self.channel, test_ended_at=None)
        db.session.commit()
        self.assertEqual(self._delete(stranded.id).status_code, 200)


class PageTests(_DeleteBase):
    def _test_history_html(self):
        html = self.client.get(f'/channels/{self.channel.id}').get_data(as_text=True)
        return html[html.index('data-section="tests"'):]

    def test_each_test_row_offers_delete(self):
        ct = self._blended_test()
        section = self._test_history_html()
        self.assertIn(f'data-act="delete-test" data-test-id="{ct.id}"', section)

    def test_a_rolled_back_test_is_marked_in_test_history(self):
        self._blended_test()
        apply_rollback(db.session.get(Channel, self.channel.id), 'reset', self.cfg)
        db.session.commit()
        self.assertIn('NOT COUNTED', self._test_history_html())

    def test_a_counting_test_is_not_marked(self):
        self._blended_test()
        self.assertNotIn('NOT COUNTED', self._test_history_html())


if __name__ == '__main__':
    unittest.main()
