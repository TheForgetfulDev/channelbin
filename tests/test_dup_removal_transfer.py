"""Remove Duplicate Channels: transfer-on-removal (dev/docs/BUGS.md 2026-08-14).

Removing a duplicate channel used to just detach it (from a job, a channel group, or the
guide) with no side effects, silently orphaning its guide listing/group membership/
scheduled recordings/health-check enrollment. The three removal endpoints now accept an
optional `transfer` flag (`removals[].keep_channel_id` names the destination per removed
channel) that reuses transfer_channel_state() - the same transfer Re-point already does -
without requiring either channel to be lifecycle-'missing'.

Covers:
  - JobRemoveDuplicatesTransferTests: POST .../on-demand/<job_id>/remove-duplicates
  - GroupRemoveMembersTransferTests: POST .../channel-groups/<id>/members/remove
  - GuideRemoveDuplicatesTransferTests: POST /api/guide/channels/remove-duplicates
  - CrossGroupIsolationTests: two unrelated duplicate sets removed in one request each
    transfer only to their own kept channel - the scenario that motivated per-removal
    keep_channel_id instead of a single request-wide destination.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_dup_removal_transfer
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.database import ChannelGroupMember  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

SHARED_URL = 'http://example.test/live/shared'
OTHER_URL = 'http://example.test/live/other'


def _pair(acc, url=SHARED_URL, name_a='Alpha', name_b='Beta', **a_overrides):
    a = seed.make_channel(acc, name=name_a, **a_overrides)
    b = seed.make_channel(acc, name=name_b)
    a.stream_url = b.stream_url = url
    db.session.flush()
    return a, b


class JobRemoveDuplicatesTransferTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acc = seed.make_account()
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _job(self, channels):
        job = seed.make_test_job(name='Job', channels=channels)
        db.session.commit()
        return job

    def test_transfer_moves_group_membership_and_test_enrollment(self):
        a, b = _pair(self.acc)
        b.test_enabled = False  # a keeps the True default
        fg = seed.make_group(name='Failover', members=[a])
        job = self._job([a, b])

        resp = self.t.client.post(
            f'/api/channel-tests/on-demand/{job.id}/remove-duplicates',
            json={'removals': [{'channel_id': a.id, 'keep_channel_id': b.id}], 'transfer': True})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['transferred'], [a.id])
        self.assertEqual(data['transfer_skipped'], [])

        db.session.refresh(b)
        self.assertTrue(b.test_enabled)
        self.assertIsNotNone(
            ChannelGroupMember.query.filter_by(group_id=fg.id, channel_id=b.id).first())
        self.assertEqual(ChannelGroupMember.query.filter_by(channel_id=a.id).count(), 0)

    def test_transfer_false_keeps_old_behavior_no_transfer(self):
        """Default-on is a modal-level choice - the endpoint itself must still honor an
        explicit transfer=false exactly like before this feature existed."""
        a, b = _pair(self.acc)
        b.test_enabled = False
        job = self._job([a, b])

        resp = self.t.client.post(
            f'/api/channel-tests/on-demand/{job.id}/remove-duplicates',
            json={'removals': [{'channel_id': a.id, 'keep_channel_id': b.id}], 'transfer': False})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['transferred'], [])
        db.session.refresh(b)
        self.assertFalse(b.test_enabled)

    def test_transfer_skipped_when_keeper_does_not_share_stream_url(self):
        """A stale/tampered keep_channel_id must not silently transfer to the wrong
        channel - removal proceeds, transfer is refused and reported."""
        a, b = _pair(self.acc)
        c = seed.make_channel(self.acc, name='Gamma')
        c.stream_url = OTHER_URL
        job = self._job([a, b, c])

        resp = self.t.client.post(
            f'/api/channel-tests/on-demand/{job.id}/remove-duplicates',
            json={'removals': [{'channel_id': a.id, 'keep_channel_id': c.id}], 'transfer': True})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['transferred'], [])
        self.assertEqual(len(data['transfer_skipped']), 1)
        self.assertIn(a.id, data['removed'])


class GroupRemoveMembersTransferTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acc = seed.make_account()
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_transfer_moves_scheduled_recording(self):
        a, b = _pair(self.acc)
        group = seed.make_group(name='G', members=[a, b])
        rec = seed.make_recording(status='SCHEDULED', channel_id=a.id,
                                  url='http://olduser:oldpass@example.test/live/1')
        db.session.commit()

        resp = self.t.client.post(
            f'/api/channel-groups/{group.id}/members/remove',
            json={'removals': [{'channel_id': a.id, 'keep_channel_id': b.id}], 'transfer': True})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['transferred'], [a.id])

        db.session.refresh(rec)
        self.assertEqual(rec.channel_id, b.id)

    def test_plain_channel_ids_payload_still_works(self):
        """Backward compatibility: the plain remove-member action (not dedup) never sends
        removals/transfer, only a bare channel_ids list."""
        a, b = _pair(self.acc)
        group = seed.make_group(name='G', members=[a, b])
        db.session.commit()

        resp = self.t.client.post(f'/api/channel-groups/{group.id}/members/remove',
                                  json={'channel_ids': [a.id]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['removed'], [a.id])
        self.assertEqual(
            ChannelGroupMember.query.filter_by(group_id=group.id, channel_id=a.id).count(), 0)


class GuideRemoveDuplicatesTransferTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acc = seed.make_account()
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_transfer_moves_guide_listing(self):
        a, b = _pair(self.acc, in_guide=True)
        a.guide_sort_order = 5
        b.in_guide = False
        db.session.commit()

        resp = self.t.client.post(
            '/api/guide/channels/remove-duplicates',
            json={'removals': [{'channel_id': a.id, 'keep_channel_id': b.id}], 'transfer': True})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['transferred'], [a.id])

        db.session.refresh(a)
        db.session.refresh(b)
        self.assertFalse(a.in_guide)
        self.assertTrue(b.in_guide)
        self.assertEqual(b.guide_sort_order, 5)


class CrossGroupIsolationTests(unittest.TestCase):
    """Two unrelated duplicate sets removed in one request must each transfer only to
    their own kept channel - the exact cross-set leak this guards against."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acc = seed.make_account()
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_two_duplicate_sets_transfer_independently(self):
        a1, a2 = _pair(self.acc, url=SHARED_URL, name_a='A1', name_b='A2')
        b1, b2 = _pair(self.acc, url=OTHER_URL, name_a='B1', name_b='B2')
        a2.test_enabled = False
        b2.test_enabled = False
        job = seed.make_test_job(name='Job', channels=[a1, a2, b1, b2])
        db.session.commit()

        resp = self.t.client.post(
            f'/api/channel-tests/on-demand/{job.id}/remove-duplicates',
            json={'removals': [
                {'channel_id': a1.id, 'keep_channel_id': a2.id},
                {'channel_id': b1.id, 'keep_channel_id': b2.id},
            ], 'transfer': True})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(sorted(data['transferred']), sorted([a1.id, b1.id]))
        self.assertEqual(data['transfer_skipped'], [])

        db.session.refresh(a2)
        db.session.refresh(b2)
        self.assertTrue(a2.test_enabled)
        self.assertTrue(b2.test_enabled)

    def test_mismatched_keep_channel_id_across_sets_is_rejected(self):
        """A removal naming a keep_channel_id from a DIFFERENT duplicate set (different
        stream_url) must be rejected server-side, not silently cross-wired."""
        a1, a2 = _pair(self.acc, url=SHARED_URL, name_a='A1', name_b='A2')
        b1, b2 = _pair(self.acc, url=OTHER_URL, name_a='B1', name_b='B2')
        job = seed.make_test_job(name='Job', channels=[a1, a2, b1, b2])
        db.session.commit()

        resp = self.t.client.post(
            f'/api/channel-tests/on-demand/{job.id}/remove-duplicates',
            json={'removals': [{'channel_id': a1.id, 'keep_channel_id': b2.id}], 'transfer': True})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['transferred'], [])
        self.assertEqual(len(data['transfer_skipped']), 1)
        self.assertIn(a1.id, data['removed'])


if __name__ == '__main__':
    unittest.main()
