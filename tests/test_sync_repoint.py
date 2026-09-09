"""Duplicate-repoint recovery helper (DESIGN-sync-resilience.md §6, changelog/246).

For a channel flagged 'missing' (DESIGN-sync-resilience.md §5 / changelog/245) whose
stream_url survives on another, non-missing channel, this offers a Re-point action that
transfers guide listing, group membership (all groups, both kinds - the M:N model since
Groups unification), SCHEDULED (non-group-backed) recordings, and channel-test enrollment
to the survivor. The missing channel row itself is always kept (soft state).

Covers:
  - RepointCandidateDetectionTests: _repoint_candidates_for_channels() - the batched
    survivor-lookup helper (prefilter, missing-vs-missing exclusion, oldest-first pick).
  - RepointTransferTests: transfer_channel_state() - the actual transfer, one behavior at
    a time. Shared with "Remove Duplicate Channels" removal-with-transfer - see
    tests/test_channel_tests.py, tests/test_channel_groups.py, tests/test_guide.py for
    that caller's own coverage.
  - RepointRouteTests: the POST /channels/<id>/repoint route - server-side validation and
    the end-to-end happy path via the Flask test client.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_sync_repoint
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Channel, ChannelGroupMember, ChannelEvent, RecordingEvent, RECORDING_REPOINTED,
    CHANNEL_ADDED_TO_GUIDE, CHANNEL_REMOVED_FROM_GUIDE,
)
from app.accounts import transfer_channel_state  # noqa: E402
from app.routes.channels import (  # noqa: E402
    _repoint_candidates_for_channels, _lifecycle_states_for_channels,
)
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

CFG = {'sync': {'channel_missing_after_days': 7, 'channel_new_within_days': 3}}
SHARED_URL = 'http://example.test/live/shared'


def _make_channel(account, stream_id, name, url=SHARED_URL, **kw):
    """seed.make_channel already hardcodes stream_url/guide_sort_order from stream_id, so
    those two are applied here as post-creation attribute writes instead of constructor
    kwargs (a constructor kwarg would collide with seed.make_channel's own Channel(...)
    call, which already passes both explicitly)."""
    guide_sort_order = kw.pop('guide_sort_order', None)
    ch = seed.make_channel(account, stream_id=stream_id, name=name, **kw)
    ch.stream_url = url
    if guide_sort_order is not None:
        ch.guide_sort_order = guide_sort_order
    return ch


def _mark_missing(channel, account, days_gone=10, synced_days_ago=1):
    now = datetime.utcnow()
    channel.last_seen_at = now - timedelta(days=days_gone)
    account.last_sync_at = now - timedelta(days=synced_days_ago)


class RepointCandidateDetectionTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Acct A')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _lifecycle(self, channels):
        return _lifecycle_states_for_channels(channels, CFG)

    def test_missing_duplicate_flagged_channel_finds_survivor(self):
        missing = _make_channel(self.account, 1, 'Missing Ch', is_duplicate_stream_url=True)
        survivor = _make_channel(self.account, 2, 'Survivor Ch', is_duplicate_stream_url=True)
        _mark_missing(missing, self.account)
        db.session.commit()

        lifecycle = self._lifecycle([missing, survivor])
        candidates = _repoint_candidates_for_channels([missing, survivor], CFG, lifecycle)
        self.assertEqual(candidates.get(missing.id), survivor)
        self.assertNotIn(survivor.id, candidates)  # survivor is not itself missing

    def test_prefilter_skips_channel_not_flagged_duplicate(self):
        """is_duplicate_stream_url is the cheap prefilter - a missing channel that isn't
        flagged is skipped even though a URL-sharing survivor exists."""
        missing = _make_channel(self.account, 1, 'Missing Ch', is_duplicate_stream_url=False)
        _make_channel(self.account, 2, 'Survivor Ch', is_duplicate_stream_url=False)
        _mark_missing(missing, self.account)
        db.session.commit()

        lifecycle = self._lifecycle([missing])
        candidates = _repoint_candidates_for_channels([missing], CFG, lifecycle)
        self.assertEqual(candidates, {})

    def test_survivor_also_missing_is_excluded(self):
        missing = _make_channel(self.account, 1, 'Missing Ch', is_duplicate_stream_url=True)
        also_missing = _make_channel(self.account, 2, 'Also Missing', is_duplicate_stream_url=True)
        _mark_missing(missing, self.account)
        _mark_missing(also_missing, self.account)
        db.session.commit()

        lifecycle = self._lifecycle([missing, also_missing])
        candidates = _repoint_candidates_for_channels([missing, also_missing], CFG, lifecycle)
        self.assertEqual(candidates, {})

    def test_multiple_survivors_picks_oldest_by_id(self):
        missing = _make_channel(self.account, 1, 'Missing Ch', is_duplicate_stream_url=True)
        older = _make_channel(self.account, 2, 'Older Survivor', is_duplicate_stream_url=True)
        newer = _make_channel(self.account, 3, 'Newer Survivor', is_duplicate_stream_url=True)
        self.assertLess(older.id, newer.id)
        _mark_missing(missing, self.account)
        db.session.commit()

        lifecycle = self._lifecycle([missing, older, newer])
        candidates = _repoint_candidates_for_channels([missing, older, newer], CFG, lifecycle)
        self.assertEqual(candidates.get(missing.id), older)

    def test_non_missing_channel_has_no_candidate(self):
        ch = _make_channel(self.account, 1, 'Healthy Ch', is_duplicate_stream_url=True)
        _make_channel(self.account, 2, 'Other', is_duplicate_stream_url=True)
        db.session.commit()

        lifecycle = self._lifecycle([ch])
        candidates = _repoint_candidates_for_channels([ch], CFG, lifecycle)
        self.assertEqual(candidates, {})


class RepointTransferTests(unittest.TestCase):
    """transfer_channel_state() - the actual mutation, one behavior at a time."""

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Acct A')
        db.session.commit()
        self.cfg = CFG

    def tearDown(self):
        self.t.cleanup()

    def _pair(self, missing_kw=None, survivor_kw=None):
        missing_kw = missing_kw or {}
        survivor_kw = survivor_kw or {}
        missing = _make_channel(self.account, 1, 'Missing Ch', **missing_kw)
        survivor = _make_channel(self.account, 2, 'Survivor Ch', **survivor_kw)
        db.session.commit()
        return missing, survivor

    def test_guide_membership_transfers_when_survivor_not_in_guide(self):
        missing, survivor = self._pair(
            missing_kw={'in_guide': True, 'guide_sort_order': 42},
            survivor_kw={'in_guide': False})
        transfer_channel_state(missing, survivor, self.cfg)
        db.session.commit()

        self.assertFalse(missing.in_guide)
        self.assertTrue(survivor.in_guide)
        self.assertEqual(survivor.guide_sort_order, 42)
        events = ChannelEvent.query.filter_by(channel_id=survivor.id).all()
        self.assertTrue(any(e.event_type == CHANNEL_ADDED_TO_GUIDE for e in events))
        missing_events = ChannelEvent.query.filter_by(channel_id=missing.id).all()
        self.assertTrue(any(e.event_type == CHANNEL_REMOVED_FROM_GUIDE for e in missing_events))

    def test_guide_membership_not_moved_when_missing_was_never_in_guide(self):
        missing, survivor = self._pair(
            missing_kw={'in_guide': False},
            survivor_kw={'in_guide': False, 'guide_sort_order': 7})
        transfer_channel_state(missing, survivor, self.cfg)
        db.session.commit()

        self.assertFalse(survivor.in_guide)
        self.assertEqual(survivor.guide_sort_order, 7)

    def test_guide_membership_left_alone_when_survivor_already_in_guide(self):
        missing, survivor = self._pair(
            missing_kw={'in_guide': True, 'guide_sort_order': 42},
            survivor_kw={'in_guide': True, 'guide_sort_order': 99})
        transfer_channel_state(missing, survivor, self.cfg)
        db.session.commit()

        self.assertFalse(missing.in_guide)
        self.assertTrue(survivor.in_guide)
        self.assertEqual(survivor.guide_sort_order, 99)  # untouched, not overwritten by missing's

    def test_group_membership_transfers_when_survivor_ungrouped(self):
        missing, survivor = self._pair()
        group = seed.make_group(name='Failover Group', members=[missing])
        db.session.commit()

        transfer_channel_state(missing, survivor, self.cfg)
        db.session.commit()

        self.assertEqual(ChannelGroupMember.query.filter_by(channel_id=missing.id).count(), 0)
        member = ChannelGroupMember.query.filter_by(group_id=group.id, channel_id=survivor.id).first()
        self.assertIsNotNone(member)

    def test_group_membership_skipped_when_survivor_already_a_member(self):
        """No silent partial transfer: if the survivor is already in the SAME group, the
        missing channel's redundant membership row is dropped, not duplicated."""
        missing, survivor = self._pair()
        group = seed.make_group(name='Shared Group', members=[missing, survivor])
        db.session.commit()

        message = transfer_channel_state(missing, survivor, self.cfg)
        db.session.commit()

        self.assertEqual(ChannelGroupMember.query.filter_by(channel_id=missing.id).count(), 0)
        self.assertEqual(
            ChannelGroupMember.query.filter_by(group_id=group.id, channel_id=survivor.id).count(), 1)
        self.assertIn('Shared Group', message)

    def test_membership_transfers_across_every_group_a_channel_belongs_to(self):
        """M:N since Groups unification - a channel can be in a recording-source failover
        group AND a health-check-only monitoring group at once; both should move."""
        missing, survivor = self._pair()
        fg = seed.make_group(name='Failover', members=[missing])
        cg = seed.make_group(name='Monitor Bag', members=[missing], recording=False, in_guide=False)
        db.session.commit()

        transfer_channel_state(missing, survivor, self.cfg)
        db.session.commit()

        self.assertTrue(ChannelGroupMember.query.filter_by(group_id=fg.id, channel_id=survivor.id).first())
        self.assertTrue(ChannelGroupMember.query.filter_by(group_id=cg.id, channel_id=survivor.id).first())

    def test_scheduled_recording_repointed_with_event(self):
        missing, survivor = self._pair()
        rec = seed.make_recording(status='SCHEDULED', channel_id=missing.id,
                                  url='http://olduser:oldpass@example.test/live/1')
        db.session.commit()

        transfer_channel_state(missing, survivor, self.cfg)
        db.session.commit()

        db.session.refresh(rec)
        self.assertEqual(rec.channel_id, survivor.id)
        self.assertNotEqual(rec.url, 'http://olduser:oldpass@example.test/live/1')
        events = RecordingEvent.query.filter_by(recording_id=rec.id, event_type=RECORDING_REPOINTED).all()
        self.assertEqual(len(events), 1)
        self.assertNotIn('oldpass', events[0].detail)  # creds masked

    def test_non_scheduled_recordings_never_touched(self):
        missing, survivor = self._pair()
        in_progress = seed.make_recording(status='IN_PROGRESS', channel_id=missing.id)
        completed = seed.make_recording(status='COMPLETED', channel_id=missing.id)
        db.session.commit()

        transfer_channel_state(missing, survivor, self.cfg)
        db.session.commit()

        db.session.refresh(in_progress)
        db.session.refresh(completed)
        self.assertEqual(in_progress.channel_id, missing.id)
        self.assertEqual(completed.channel_id, missing.id)

    def test_group_backed_scheduled_recording_never_touched(self):
        """A group-backed recording's channel_id is owned by the group's own member-
        selection/failover logic (app/recorder.py) - this helper must not fight it."""
        missing, survivor = self._pair()
        group = seed.make_group(name='G', members=[missing])
        rec = seed.make_recording(status='SCHEDULED', channel_id=missing.id, group_id=group.id)
        db.session.commit()

        transfer_channel_state(missing, survivor, self.cfg)
        db.session.commit()

        db.session.refresh(rec)
        self.assertEqual(rec.channel_id, missing.id)
        self.assertEqual(RecordingEvent.query.filter_by(recording_id=rec.id).count(), 0)

    def test_test_enabled_transfers_when_missing_enabled_and_survivor_not(self):
        missing, survivor = self._pair(
            missing_kw={'test_enabled': True}, survivor_kw={'test_enabled': False})
        transfer_channel_state(missing, survivor, self.cfg)
        db.session.commit()
        self.assertTrue(survivor.test_enabled)

    def test_test_enabled_not_claimed_as_moved_when_already_true(self):
        missing, survivor = self._pair(
            missing_kw={'test_enabled': True}, survivor_kw={'test_enabled': True})
        message = transfer_channel_state(missing, survivor, self.cfg)
        db.session.commit()
        self.assertTrue(survivor.test_enabled)
        self.assertNotIn('channel-test enrollment', message)

    def test_missing_channel_row_is_kept(self):
        missing, survivor = self._pair(missing_kw={'in_guide': True})
        transfer_channel_state(missing, survivor, self.cfg)
        db.session.commit()
        self.assertIsNotNone(db.session.get(Channel, missing.id))


class RepointRouteTests(unittest.TestCase):
    """POST /channels/<id>/repoint - server-side re-validation + the happy path."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.account = seed.make_account(name='Acct A')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _missing_and_survivor(self):
        missing = _make_channel(self.account, 1, 'Missing Ch',
                                is_duplicate_stream_url=True, in_guide=True)
        survivor = _make_channel(self.account, 2, 'Survivor Ch',
                                 is_duplicate_stream_url=True, in_guide=False)
        _mark_missing(missing, self.account)
        db.session.commit()
        return missing, survivor

    def _post(self, channel_id, survivor_id):
        return self.t.client.post(f'/channels/{channel_id}/repoint',
                                  json={'survivor_channel_id': survivor_id})

    def test_happy_path_repoints_and_commits(self):
        missing, survivor = self._missing_and_survivor()
        resp = self._post(missing.id, survivor.id)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertIn('message', data)

        db.session.refresh(missing)
        db.session.refresh(survivor)
        self.assertFalse(missing.in_guide)
        self.assertTrue(survivor.in_guide)

    def test_unknown_channel_404s(self):
        resp = self.t.client.post('/channels/999999/repoint',
                                  json={'survivor_channel_id': 1})
        self.assertEqual(resp.status_code, 404)

    def test_missing_survivor_id_400s(self):
        missing, survivor = self._missing_and_survivor()
        resp = self.t.client.post(f'/channels/{missing.id}/repoint', json={})
        self.assertEqual(resp.status_code, 400)

    def test_survivor_same_as_source_400s(self):
        missing, survivor = self._missing_and_survivor()
        resp = self._post(missing.id, missing.id)
        self.assertEqual(resp.status_code, 400)

    def test_stream_url_mismatch_409s(self):
        missing, survivor = self._missing_and_survivor()
        survivor.stream_url = 'http://example.test/live/different'
        db.session.commit()
        resp = self._post(missing.id, survivor.id)
        self.assertEqual(resp.status_code, 409)

    def test_source_not_actually_missing_409s(self):
        missing, survivor = self._missing_and_survivor()
        missing.last_seen_at = datetime.utcnow()  # recently seen again - no longer missing
        db.session.commit()
        resp = self._post(missing.id, survivor.id)
        self.assertEqual(resp.status_code, 409)

    def test_survivor_also_missing_409s(self):
        missing, survivor = self._missing_and_survivor()
        _mark_missing(survivor, self.account)
        db.session.commit()
        resp = self._post(missing.id, survivor.id)
        self.assertEqual(resp.status_code, 409)


if __name__ == '__main__':
    unittest.main()
