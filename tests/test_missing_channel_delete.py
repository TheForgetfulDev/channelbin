"""Bulk-delete channels missing from the provider feed (changelog/248, "Bulk-delete
channels missing from the provider feed").

A 2026-06-29 identity-change bug permanently orphaned thousands of Channel rows on a real
account - channel_lifecycle_state() (changelog/245) already flags them 'missing' and the
repoint recovery (changelog/246) only helps when a surviving duplicate exists. This is the
bulk cleanup path for the common case where there's nothing to re-point to.

Covers:
  - _missing_delete_candidates(): the eligible/blocked_inuse/blocked_active partition
    shared by the preview and delete routes (in_guide/grouped excluded outright; a
    non-terminal Recording blocks deletion outright; a terminal Recording is not a
    blocker).
  - GET /api/channels/missing-delete-preview: bucketed counts + blocked lists.
  - POST /channels/missing-delete: actually deletes the eligible channels (and their
    ChannelGroupMember/ChannelTest/ChannelEvent/EPGEntry rows, and any screenshot files on
    disk), unlinks (channel_id=NULL, row kept) terminal Recordings, and leaves blocked
    channels and their children completely untouched. Also refreshes the touched
    account(s)' channel_count/epg_entry_count and re-runs the global duplicate-URL flag
    recompute (BUGS.md 2026-07-21 03:37:39 PM ET - these stored counters/flags previously only
    self-healed on the next full provider sync, which isn't always available on demand due
    to rate limiting).
  - MissingDeleteCandidatesGroupScopeTests + the *_scoped_* preview/delete route tests
    (dev/changelog/653): the same preview/delete mechanism, scoped by group_id to one
    group/health check's own channels instead of the whole database, with that group's
    own membership rows excluded from the "blocked because grouped" check.
  - The explicit channel_ids scope (dev/changelog/772), which is what makes the same
    mechanism reachable as the channel detail page's single delete and the Browse tab's
    selection delete: id parsing, the partition under a requested set, the refusal prose
    for a request that can act on nothing, and the routes end to end. The id list narrows
    which channels are CONSIDERED and asserts nothing else - missing-ness and every
    blocker are re-derived server-side, and an empty selection must never widen into a
    whole-account sweep.
  - group_id AND channel_ids together (dev/changelog/874) - the group/health-check
    detail page's own mobile bulk-select sheet, narrowed to the selection rather than
    the whole group. The two scopes intersect rather than one winning outright, and the
    combination still carries group_id's own-membership exemption, so a selected
    channel is not blocked merely for belonging to the very group it was selected from.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_missing_channel_delete
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.exc import OperationalError  # noqa: E402

from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Channel, ChannelTest, ChannelEvent, EPGEntry, Recording,
)
from app.routes.channels import (  # noqa: E402
    _BadRequestedIds, _MAX_REQUESTED_DELETE_IDS, _missing_delete_candidates,
    _not_missing_ids, _parse_requested_ids, _refusal_reason,
)
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

CFG = {'sync': {'channel_missing_after_days': 7, 'channel_new_within_days': 3}}


def _missing_channel(account, stream_id, name='Missing Ch', days_gone=30, synced_days_ago=1, **kw):
    ch = seed.make_channel(account, stream_id=stream_id, name=name, **kw)
    now = datetime.utcnow()
    ch.last_seen_at = now - timedelta(days=days_gone)
    account.last_sync_at = now - timedelta(days=synced_days_ago)
    return ch


class MissingDeleteCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Acct A')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_plain_missing_channel_is_eligible(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(self.account.id, CFG)
        self.assertEqual([c.id for c in eligible], [ch.id])
        self.assertEqual(blocked_inuse, [])
        self.assertEqual(blocked_active, [])

    def test_non_missing_channel_is_not_a_candidate_at_all(self):
        seed.make_channel(self.account, stream_id=1, name='Fresh')
        self.account.last_sync_at = datetime.utcnow()
        db.session.commit()
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(self.account.id, CFG)
        self.assertEqual(eligible, [])
        self.assertEqual(blocked_inuse, [])
        self.assertEqual(blocked_active, [])

    def test_in_guide_channel_is_blocked_inuse_not_eligible(self):
        ch = _missing_channel(self.account, 1, in_guide=True)
        db.session.commit()
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(self.account.id, CFG)
        self.assertEqual(eligible, [])
        self.assertEqual([c.id for c in blocked_inuse], [ch.id])

    def test_grouped_channel_is_blocked_inuse_not_eligible(self):
        ch = _missing_channel(self.account, 1)
        seed.make_group(name='G', members=[ch])
        db.session.commit()
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(self.account.id, CFG)
        self.assertEqual(eligible, [])
        self.assertEqual([c.id for c in blocked_inuse], [ch.id])

    def test_active_recording_blocks_deletion_outright(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        for status in ('SCHEDULED', 'IN_PROGRESS', 'PAUSED', 'CONCATENATING'):
            with self.subTest(status=status):
                rec = seed.make_recording(status=status, channel_id=ch.id)
                db.session.commit()
                eligible, blocked_inuse, blocked_active = _missing_delete_candidates(self.account.id, CFG)
                self.assertEqual(eligible, [])
                self.assertEqual([c.id for c in blocked_active], [ch.id])
                db.session.delete(rec)
                db.session.commit()

    def test_terminal_recording_does_not_block_deletion(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        for status in ('COMPLETED', 'FAILED', 'ABORTED'):
            with self.subTest(status=status):
                rec = seed.make_recording(status=status, channel_id=ch.id)
                db.session.commit()
                eligible, blocked_inuse, blocked_active = _missing_delete_candidates(self.account.id, CFG)
                self.assertEqual([c.id for c in eligible], [ch.id])
                db.session.delete(rec)
                db.session.commit()

    def test_account_scoping(self):
        acct_b = seed.make_account(name='Acct B')
        ch_a = _missing_channel(self.account, 1, name='A1')
        ch_b = _missing_channel(acct_b, 1, name='B1')
        db.session.commit()

        eligible, _, _ = _missing_delete_candidates(self.account.id, CFG)
        self.assertEqual([c.id for c in eligible], [ch_a.id])

        eligible_all, _, _ = _missing_delete_candidates(None, CFG)
        self.assertEqual({c.id for c in eligible_all}, {ch_a.id, ch_b.id})


class MissingDeleteCandidatesGroupScopeTests(unittest.TestCase):
    """group_id scoping (dev/changelog/653) - the group/health-check detail page's own
    "Delete Missing Channels", reusing the same partitioning with the candidate set
    narrowed to one group's own channels and that group's own membership rows excluded
    from the "blocked because grouped" check (every candidate is, by construction, a
    member of the group being viewed)."""

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Acct A')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_channel_in_only_this_group_is_eligible(self):
        ch = _missing_channel(self.account, 1)
        grp = seed.make_group(name='G', members=[ch], in_guide=False)
        db.session.commit()
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(
            None, CFG, group_id=grp.id)
        self.assertEqual([c.id for c in eligible], [ch.id])
        self.assertEqual(blocked_inuse, [])
        self.assertEqual(blocked_active, [])

    def test_in_guide_channel_is_still_blocked_when_group_scoped(self):
        ch = _missing_channel(self.account, 1, in_guide=True)
        grp = seed.make_group(name='G', members=[ch], in_guide=False)
        db.session.commit()
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(
            None, CFG, group_id=grp.id)
        self.assertEqual(eligible, [])
        self.assertEqual([c.id for c in blocked_inuse], [ch.id])

    def test_membership_in_a_second_group_still_blocks(self):
        ch = _missing_channel(self.account, 1)
        grp = seed.make_group(name='G', members=[ch], in_guide=False)
        other = seed.make_group(name='Other', members=[ch], in_guide=False)
        db.session.commit()
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(
            None, CFG, group_id=grp.id)
        self.assertEqual(eligible, [])
        self.assertEqual([c.id for c in blocked_inuse], [ch.id])
        self.assertGreaterEqual(other.id, grp.id)  # both groups genuinely exist

    def test_unrelated_group_membership_never_enters_the_candidate_set(self):
        ch_g = _missing_channel(self.account, 1, name='InG')
        ch_h = _missing_channel(self.account, 2, name='InH')
        grp = seed.make_group(name='G', members=[ch_g], in_guide=False)
        seed.make_group(name='H', members=[ch_h], in_guide=False)
        db.session.commit()
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(
            None, CFG, group_id=grp.id)
        self.assertEqual([c.id for c in eligible], [ch_g.id])

    def test_health_check_only_groups_own_membership_row_does_not_self_block(self):
        """A non-system, health-check-only group also has real ChannelGroupMember rows
        (unlike is_system, whose membership is computed) - the self-exclusion has to
        apply to it too, not just to a recording-source group."""
        ch = _missing_channel(self.account, 1)
        grp = seed.make_group(name='Check', members=[ch], recording=False, in_guide=False)
        db.session.commit()
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(
            None, CFG, group_id=grp.id)
        self.assertEqual([c.id for c in eligible], [ch.id])

    def test_is_system_group_channels_are_blocked_inuse_via_in_guide(self):
        """The system group's target IS "every in_guide channel"
        (app/channel_groups.py::check_target_channels), so a missing member is virtually
        always blocked_inuse - not because of grouping (is_system stores no membership
        rows), but because it is by definition still in the guide."""
        ch = _missing_channel(self.account, 1, in_guide=True)
        sys_grp = seed.make_group(name='TV Guide Channels', is_system=True,
                                  recording=False, in_guide=False)
        db.session.commit()
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(
            None, CFG, group_id=sys_grp.id)
        self.assertEqual(eligible, [])
        self.assertEqual([c.id for c in blocked_inuse], [ch.id])

    def test_group_id_overrides_account_id(self):
        acct_b = seed.make_account(name='Acct B')
        ch = _missing_channel(acct_b, 1)
        grp = seed.make_group(name='G', members=[ch], in_guide=False)
        db.session.commit()
        # account_id names a DIFFERENT account than the channel's own - group_id must win.
        eligible, _, _ = _missing_delete_candidates(self.account.id, CFG, group_id=grp.id)
        self.assertEqual([c.id for c in eligible], [ch.id])

    def test_unknown_group_id_returns_nothing(self):
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(
            None, CFG, group_id=999999)
        self.assertEqual((eligible, blocked_inuse, blocked_active), ([], [], []))


class MissingDeleteCandidatesCombinedScopeTests(unittest.TestCase):
    """group_id AND channel_ids together (dev/changelog/874) - the group/health-check
    detail page's own selection-delete, narrowed to the channels picked there rather
    than the whole group. Fixes a real bug: without the group_id, a selected channel
    that is a member of the very group being viewed was reported blocked_inuse (in a
    group - visit each to remove first) for the only group it could ever have been
    removed from first, which made the bulk sheet's delete action always refuse."""

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Acct A')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_selection_is_exempt_from_its_own_group_s_membership_block(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        group = seed.make_group(name='G', members=[ch], in_guide=False)
        db.session.commit()
        eligible, blocked_inuse, _active = _missing_delete_candidates(
            None, CFG, group_id=group.id, channel_ids=[ch.id])
        self.assertEqual([c.id for c in eligible], [ch.id])
        self.assertEqual(blocked_inuse, [])

    def test_channel_ids_narrows_the_group_s_candidate_set_to_the_selection(self):
        picked = _missing_channel(self.account, 1, name='Picked')
        not_picked = _missing_channel(self.account, 2, name='Not Picked')
        db.session.commit()
        group = seed.make_group(name='G', members=[picked, not_picked], in_guide=False)
        db.session.commit()
        eligible, _inuse, _active = _missing_delete_candidates(
            None, CFG, group_id=group.id, channel_ids=[picked.id])
        self.assertEqual([c.id for c in eligible], [picked.id])

    def test_membership_in_a_second_group_still_blocks(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        group = seed.make_group(name='G', members=[ch], in_guide=False)
        seed.make_group(name='Other', members=[ch], in_guide=False)
        db.session.commit()
        eligible, blocked_inuse, _active = _missing_delete_candidates(
            None, CFG, group_id=group.id, channel_ids=[ch.id])
        self.assertEqual(eligible, [])
        self.assertEqual([c.id for c in blocked_inuse], [ch.id])

    def test_in_guide_channel_still_blocks(self):
        ch = _missing_channel(self.account, 1, in_guide=True)
        db.session.commit()
        group = seed.make_group(name='G', members=[ch], in_guide=False)
        db.session.commit()
        eligible, blocked_inuse, _active = _missing_delete_candidates(
            None, CFG, group_id=group.id, channel_ids=[ch.id])
        self.assertEqual(eligible, [])
        self.assertEqual([c.id for c in blocked_inuse], [ch.id])

    def test_empty_intersection_returns_nothing(self):
        in_group = _missing_channel(self.account, 1, name='In Group')
        outside = _missing_channel(self.account, 2, name='Outside')
        db.session.commit()
        group = seed.make_group(name='G', members=[in_group], in_guide=False)
        db.session.commit()
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(
            None, CFG, group_id=group.id, channel_ids=[outside.id])
        self.assertEqual((eligible, blocked_inuse, blocked_active), ([], [], []))


class MissingDeletePreviewRouteTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Acct A')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_preview_buckets_and_blocked_list(self):
        _missing_channel(self.account, 1, name='TenDays', days_gone=10)
        _missing_channel(self.account, 2, name='FortyDays', days_gone=40)
        blocked = _missing_channel(self.account, 3, name='InGuide', days_gone=10, in_guide=True)
        db.session.commit()

        resp = self.t.client.get(f'/api/channels/missing-delete-preview?account_id={self.account.id}')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['eligible_count'], 2)
        self.assertEqual(data['blocked_inuse_count'], 1)
        self.assertEqual(data['blocked_inuse'][0]['id'], blocked.id)
        self.assertIn(f'/channels/{blocked.id}', data['blocked_inuse'][0]['url'])

        bucket_counts = {b['label']: b['count'] for b in data['buckets']}
        self.assertEqual(bucket_counts['7-14 days'], 1)
        self.assertEqual(bucket_counts['30-90 days'], 1)

    def test_preview_no_missing_channels_is_all_zero(self):
        resp = self.t.client.get('/api/channels/missing-delete-preview')
        data = resp.get_json()
        self.assertEqual(data['eligible_count'], 0)
        self.assertEqual(data['blocked_inuse_count'], 0)
        self.assertEqual(data['blocked_active_count'], 0)

    def test_preview_reports_how_many_of_the_batch_are_hidden(self):
        """dev/docs/DESIGN-channel-hiding.md §11 "Counts": hidden channels stay eligible for
        this sweep - hidden means out of the way, not protected - so the preview has to say
        how many of the batch the user cannot currently see in the search, or the delete
        count disagrees with what the search page shows."""
        _missing_channel(self.account, 1, name='HiddenOne', days_gone=10, hidden=True)
        _missing_channel(self.account, 2, name='HiddenTwo', days_gone=10, hidden=True)
        _missing_channel(self.account, 3, name='Visible', days_gone=10)
        db.session.commit()

        resp = self.t.client.get(f'/api/channels/missing-delete-preview?account_id={self.account.id}')
        data = resp.get_json()
        self.assertEqual(data['eligible_count'], 3)
        self.assertEqual(data['eligible_hidden_count'], 2)

    def test_preview_scoped_to_a_group(self):
        eligible_ch = _missing_channel(self.account, 1, name='InGroup', days_gone=10)
        outside_ch = _missing_channel(self.account, 2, name='NotInGroup', days_gone=10)
        grp = seed.make_group(name='G', members=[eligible_ch],
                              in_guide=False)
        db.session.commit()

        resp = self.t.client.get(f'/api/channels/missing-delete-preview?group_id={grp.id}')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['eligible_count'], 1)
        self.assertEqual(data['blocked_inuse_count'], 0)
        # The other missing channel isn't a member of this group at all - it must not
        # show up in the preview in any form (not eligible, not blocked).
        self.assertNotIn(outside_ch.id, [c['id'] for c in data['blocked_inuse']])


class MissingDeleteRouteTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.account = seed.make_account(name='Acct A')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _post(self, account_id=None):
        return self.t.client.post('/channels/missing-delete',
                                  json={'account_id': account_id})

    def _post_group(self, group_id):
        return self.t.client.post('/channels/missing-delete',
                                  json={'group_id': group_id})

    def test_deletes_eligible_channel_and_its_children(self):
        ch = _missing_channel(self.account, 1)
        seed.make_channel_test(ch)
        db.session.add(ChannelEvent(channel_id=ch.id, event_type='TEST', detail='x'))
        seed.make_epg_entry(ch)
        db.session.commit()
        ch_id = ch.id

        resp = self._post(self.account.id)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['deleted_count'], 1)

        self.assertIsNone(db.session.get(Channel, ch_id))
        self.assertEqual(ChannelTest.query.filter_by(channel_id=ch_id).count(), 0)
        self.assertEqual(ChannelEvent.query.filter_by(channel_id=ch_id).count(), 0)
        self.assertEqual(EPGEntry.query.filter_by(channel_id=ch_id).count(), 0)

    def test_terminal_recording_survives_with_channel_id_nulled(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        rec = seed.make_recording(status='COMPLETED', channel_id=ch.id, name='Old Recording')
        db.session.commit()
        rec_id = rec.id

        resp = self._post(self.account.id)
        data = resp.get_json()
        self.assertEqual(data['deleted_count'], 1)
        self.assertEqual(data['recordings_preserved'], 1)

        survived = db.session.get(Recording, rec_id)
        self.assertIsNotNone(survived)
        self.assertIsNone(survived.channel_id)
        self.assertEqual(survived.name, 'Old Recording')

    def test_active_recording_blocks_and_channel_survives(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        seed.make_recording(status='IN_PROGRESS', channel_id=ch.id)
        db.session.commit()
        ch_id = ch.id

        resp = self._post(self.account.id)
        data = resp.get_json()
        self.assertEqual(data['deleted_count'], 0)
        self.assertIsNotNone(db.session.get(Channel, ch_id))

    def test_in_guide_channel_survives(self):
        ch = _missing_channel(self.account, 1, in_guide=True)
        db.session.commit()
        ch_id = ch.id

        resp = self._post(self.account.id)
        data = resp.get_json()
        self.assertEqual(data['deleted_count'], 0)
        self.assertIsNotNone(db.session.get(Channel, ch_id))

    def test_screenshot_file_removed_from_disk(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        shot_path = os.path.join(self.t._tmpdir, 'screenshot_for_delete_test.jpg')
        with open(shot_path, 'wb') as f:
            f.write(b'fake jpg bytes')
        seed.make_channel_test(ch, screenshot_path=shot_path)
        db.session.commit()

        resp = self._post(self.account.id)
        self.assertEqual(resp.get_json()['deleted_count'], 1)
        self.assertFalse(os.path.exists(shot_path))

    def test_empty_scope_returns_zero(self):
        resp = self._post(self.account.id)
        data = resp.get_json()
        self.assertEqual(data['deleted_count'], 0)
        self.assertEqual(data['recordings_preserved'], 0)

    def test_only_targets_requested_account(self):
        acct_b = seed.make_account(name='Acct B')
        keep = _missing_channel(acct_b, 1, name='KeepMe')
        _missing_channel(self.account, 1, name='DeleteMe')
        db.session.commit()
        keep_id = keep.id

        resp = self._post(self.account.id)
        self.assertEqual(resp.get_json()['deleted_count'], 1)
        self.assertIsNotNone(db.session.get(Channel, keep_id))

    def test_account_channel_and_epg_counts_refreshed_after_delete(self):
        """BUGS.md 2026-07-21 03:37:39 PM ET - Account.channel_count/epg_entry_count are stamped only at
        sync time (app/accounts.py::_mark_success_and_commit); a bulk delete outside a sync
        must refresh them itself or the dashboard/accounts page show stale numbers until the
        next full sync, which isn't always available on demand (provider rate limiting)."""
        survivor = seed.make_channel(self.account, stream_id=99, name='Survivor')
        seed.make_epg_entry(survivor)
        ch = _missing_channel(self.account, 1)
        seed.make_epg_entry(ch)
        seed.make_epg_entry(ch)
        self.account.channel_count = 999
        self.account.epg_entry_count = 999
        db.session.commit()

        resp = self._post(self.account.id)
        self.assertEqual(resp.get_json()['deleted_count'], 1)

        db.session.refresh(self.account)
        self.assertEqual(self.account.channel_count, 1)
        self.assertEqual(self.account.epg_entry_count, 1)

    def test_hidden_channel_count_refreshed_after_delete(self):
        """This delete removes rows outright rather than going through channel_hiding's one
        recompute, so it is the one other caller of refresh_hidden_channel_counts() - a
        deleted hidden channel must leave the stored count, not linger in it until the next
        sync (dev/docs/DESIGN-channel-hiding.md §11 "Counts")."""
        seed.make_channel(self.account, stream_id=99, name='Survivor', hidden=True)
        _missing_channel(self.account, 1, name='DeletedHidden', hidden=True)
        self.account.hidden_channel_count = 999
        db.session.commit()

        resp = self._post(self.account.id)
        self.assertEqual(resp.get_json()['deleted_count'], 1)

        db.session.refresh(self.account)
        self.assertEqual(self.account.hidden_channel_count, 1)

    def test_duplicate_flag_cleared_on_surviving_channel_after_delete(self):
        """BUGS.md 2026-07-21 03:37:39 PM ET - Channel.is_duplicate_stream_url is a stored flag
        (app/accounts.py::_recompute_duplicate_stream_urls, sync-time only); once the other
        half of a duplicate pair is bulk-deleted, the survivor's flag must be cleared too,
        not just left stale until the next full sync."""
        shared_url = 'http://example.test/live/shared-dup'
        ch = _missing_channel(self.account, 1, name='Missing Dup', is_duplicate_stream_url=True)
        ch.stream_url = shared_url
        survivor = seed.make_channel(self.account, stream_id=2, name='Survivor Dup',
                                     is_duplicate_stream_url=True)
        survivor.stream_url = shared_url
        db.session.commit()
        survivor_id = survivor.id

        resp = self._post(self.account.id)
        self.assertEqual(resp.get_json()['deleted_count'], 1)

        db.session.expire_all()
        self.assertFalse(db.session.get(Channel, survivor_id).is_duplicate_stream_url)

    def test_group_scoped_delete_only_deletes_that_group_s_eligible_channels(self):
        """The group/health-check detail page's "Delete Missing Channels" (dev/changelog/653)
        - deleting a channel that's a member of ONLY the group being viewed removes it
        (and, as a side effect of hard-deleting the Channel row, its ChannelGroupMember
        row for that group), while a channel missing in an unrelated group is untouched."""
        target = _missing_channel(self.account, 1, name='InGroup')
        other = _missing_channel(self.account, 2, name='Elsewhere')
        grp = seed.make_group(name='G', members=[target], in_guide=False)
        seed.make_group(name='Other', members=[other], in_guide=False)
        db.session.commit()
        target_id, other_id = target.id, other.id

        resp = self._post_group(grp.id)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['deleted_count'], 1)

        self.assertIsNone(db.session.get(Channel, target_id))
        self.assertIsNotNone(db.session.get(Channel, other_id))

    def test_group_scoped_delete_leaves_channel_in_another_group_untouched(self):
        ch = _missing_channel(self.account, 1)
        grp = seed.make_group(name='G', members=[ch], in_guide=False)
        seed.make_group(name='Other', members=[ch], in_guide=False)
        db.session.commit()
        ch_id = ch.id

        resp = self._post_group(grp.id)
        self.assertEqual(resp.get_json()['deleted_count'], 0)
        self.assertIsNotNone(db.session.get(Channel, ch_id))


class MissingDeleteScreenshotOrderingTests(unittest.TestCase):
    """A delete that never commits must leave the screenshot files alone.

    Guards dev/docs/BUGS.md 2026-08-16 @ 03:59:17 PM ET. `_do_delete_and_commit` unlinked
    the screenshots INSIDE its `retry_on_locked` closure, ahead of the commit, so a commit
    that failed permanently left every ChannelTest row in place pointing at a file that had
    already been destroyed - and re-running the delete could never repair it, because the
    files were the only copy. The route now collects the paths inside the closure and
    unlinks them only after it has durably returned, the ordering `delete_recording` uses.
    """

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.account = seed.make_account(name='Acct A')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_screenshot_survives_a_commit_that_never_succeeds(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        shot_path = os.path.join(self.t._tmpdir, 'screenshot_locked_commit.jpg')
        with open(shot_path, 'wb') as f:
            f.write(b'fake jpg bytes')
        test_row = seed.make_channel_test(ch, screenshot_path=shot_path)
        db.session.commit()
        ch_id, test_id = ch.id, test_row.id

        def _locked_commit(*_args, **_kwargs):
            raise OperationalError('COMMIT', {}, Exception('database is locked'))

        # Every attempt loses its commit, so retry_on_locked exhausts its five and re-raises
        # - the "permanently locked" case, not a transient one. The sleep patch only keeps
        # the backoff from adding 2.25s of real waiting to the suite.
        with mock.patch.object(db.session, 'commit', _locked_commit), \
                mock.patch('app.db_utils.time.sleep', lambda _s: None):
            resp = self.t.client.post('/channels/missing-delete',
                                      json={'account_id': self.account.id})

        self.assertEqual(resp.status_code, 500)
        db.session.rollback()
        self.assertTrue(os.path.exists(shot_path),
                        'screenshot was unlinked even though the delete never committed')
        self.assertIsNotNone(db.session.get(Channel, ch_id))
        self.assertIsNotNone(db.session.get(ChannelTest, test_id))


class RequestedIdParsingTests(unittest.TestCase):
    """_parse_requested_ids: the client-supplied half of the scope (dev/changelog/772).

    None and [] are deliberately different answers - None means "no explicit-id scope,
    use account/group", [] means "an explicit scope that selects nothing" - so a route
    cannot silently widen an empty selection into a whole-account sweep.
    """

    def test_none_means_no_explicit_scope(self):
        self.assertIsNone(_parse_requested_ids(None))
        self.assertIsNone(_parse_requested_ids(''))

    def test_empty_list_is_an_explicit_scope_selecting_nothing(self):
        self.assertEqual(_parse_requested_ids([]), [])

    def test_ints_and_numeric_strings_both_parse(self):
        self.assertEqual(_parse_requested_ids([3, '4']), [3, 4])

    def test_comma_separated_string_parses(self):
        self.assertEqual(_parse_requested_ids('7,8, 9'), [7, 8, 9])

    def test_duplicates_collapse_preserving_order(self):
        self.assertEqual(_parse_requested_ids([5, 2, 5, 2, 9]), [5, 2, 9])

    def test_non_numeric_is_rejected(self):
        with self.assertRaises(_BadRequestedIds):
            _parse_requested_ids(['not-an-id'])

    def test_bool_is_rejected_rather_than_read_as_channel_one(self):
        with self.assertRaises(_BadRequestedIds):
            _parse_requested_ids([True])

    def test_non_list_is_rejected(self):
        with self.assertRaises(_BadRequestedIds):
            _parse_requested_ids({'id': 1})

    def test_over_the_cap_is_rejected(self):
        with self.assertRaises(_BadRequestedIds):
            _parse_requested_ids(list(range(_MAX_REQUESTED_DELETE_IDS + 1)))


class MissingDeleteCandidatesChannelIdScopeTests(unittest.TestCase):
    """The explicit-id scope of the shared partition (dev/changelog/772).

    Requesting ids narrows WHICH channels are considered and asserts nothing else: whether
    each is actually missing from its feed, and whether anything blocks it, is re-derived
    from the database on every call exactly as it is for the account and group scopes.
    """

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Acct A')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_only_the_requested_channel_is_considered(self):
        wanted = _missing_channel(self.account, 1, name='Wanted')
        _missing_channel(self.account, 2, name='Other')
        db.session.commit()
        eligible, _inuse, _active = _missing_delete_candidates(
            None, CFG, channel_ids=[wanted.id])
        self.assertEqual([c.id for c in eligible], [wanted.id])

    def test_a_live_channel_lands_in_no_list_at_all(self):
        live = seed.make_channel(self.account, stream_id=1, name='Still Listed')
        live.last_seen_at = datetime.utcnow()
        self.account.last_sync_at = datetime.utcnow()
        db.session.commit()
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(
            None, CFG, channel_ids=[live.id])
        self.assertEqual((eligible, blocked_inuse, blocked_active), ([], [], []))
        self.assertEqual(_not_missing_ids([live.id], [], [], []), [live.id])

    def test_in_guide_still_blocks_under_an_explicit_request(self):
        ch = _missing_channel(self.account, 1, in_guide=True)
        db.session.commit()
        eligible, blocked_inuse, _active = _missing_delete_candidates(
            None, CFG, channel_ids=[ch.id])
        self.assertEqual(eligible, [])
        self.assertEqual([c.id for c in blocked_inuse], [ch.id])

    def test_group_membership_still_blocks_under_an_explicit_request(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        seed.make_group(name='G', members=[ch], in_guide=False)
        db.session.commit()
        eligible, blocked_inuse, _active = _missing_delete_candidates(
            None, CFG, channel_ids=[ch.id])
        self.assertEqual(eligible, [])
        self.assertEqual([c.id for c in blocked_inuse], [ch.id])

    def test_scheduled_recording_still_blocks_under_an_explicit_request(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        seed.make_recording(status='SCHEDULED', channel_id=ch.id)
        db.session.commit()
        eligible, _inuse, blocked_active = _missing_delete_candidates(
            None, CFG, channel_ids=[ch.id])
        self.assertEqual(eligible, [])
        self.assertEqual([c.id for c in blocked_active], [ch.id])

    def test_empty_id_list_selects_nothing_rather_than_everything(self):
        _missing_channel(self.account, 1)
        db.session.commit()
        eligible, _inuse, _active = _missing_delete_candidates(None, CFG, channel_ids=[])
        self.assertEqual(eligible, [])

    def test_channel_ids_requested_outside_the_group_are_dropped_not_substituted(self):
        """group_id and channel_ids combine (dev/changelog/874) rather than one winning
        outright: the candidate set is the group's own channels narrowed to the
        requested ids, so an id that isn't actually a member yields nothing - it must
        never fall back to the group's whole membership, which would silently widen a
        selection-scoped delete back into a group-wide one."""
        in_group = _missing_channel(self.account, 1, name='In Group')
        outside = _missing_channel(self.account, 2, name='Outside')
        db.session.commit()
        group = seed.make_group(name='G', members=[in_group], in_guide=False)
        db.session.commit()
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(
            None, CFG, group_id=group.id, channel_ids=[outside.id])
        self.assertEqual((eligible, blocked_inuse, blocked_active), ([], [], []))

    def test_account_id_is_ignored_when_channel_ids_are_given(self):
        other_account = seed.make_account(name='Acct B')
        db.session.commit()
        elsewhere = _missing_channel(other_account, 1, name='Elsewhere')
        db.session.commit()
        eligible, _inuse, _active = _missing_delete_candidates(
            self.account.id, CFG, channel_ids=[elsewhere.id])
        self.assertEqual([c.id for c in eligible], [elsewhere.id])


class RefusalReasonTests(unittest.TestCase):
    """_refusal_reason: why an explicitly-requested delete did nothing (dev/changelog/772).

    A delete that acted on nothing answering with success and a count of zero is the
    silence product principle 1 exists against, so every reachable state is named.
    """

    class _Ch:
        def __init__(self, cid):
            self.id = cid

    def test_account_and_group_sweeps_never_refuse(self):
        self.assertIsNone(_refusal_reason(None, [], [self._Ch(1)], []))

    def test_something_eligible_never_refuses(self):
        self.assertIsNone(_refusal_reason([1], [self._Ch(1)], [], []))

    def test_empty_selection_says_so(self):
        self.assertIn('No channels were selected', _refusal_reason([], [], [], []))

    def test_active_recording_reason_names_the_recording(self):
        reason = _refusal_reason([1], [], [], [self._Ch(1)])
        self.assertIn('recording scheduled or in progress', reason)
        self.assertIn('That channel', reason)

    def test_in_use_reason_names_the_guide_and_groups(self):
        reason = _refusal_reason([1], [], [self._Ch(1)], [])
        self.assertIn('TV Guide or in a channel group', reason)

    def test_not_missing_reason_names_the_provider_feed(self):
        reason = _refusal_reason([1], [], [], [])
        self.assertIn("still listed in the provider's feed", reason)

    def test_plural_subject_for_a_multi_channel_request(self):
        reason = _refusal_reason([1, 2], [], [self._Ch(1), self._Ch(2)], [])
        self.assertIn('None of those channels', reason)


class ChannelIdScopeRouteTests(unittest.TestCase):
    """The preview and delete routes under an explicit channel_ids scope - the channel
    detail page's single delete and the Browse tab's selection delete
    (dev/changelog/772)."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.account = seed.make_account(name='Acct A')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _preview(self, ids):
        return self.t.client.get(
            '/api/channels/missing-delete-preview?channel_ids='
            + ','.join(str(i) for i in ids))

    def _delete(self, ids):
        return self.t.client.post('/channels/missing-delete', json={'channel_ids': ids})

    def test_single_channel_delete_removes_only_that_channel(self):
        target = _missing_channel(self.account, 1, name='Target')
        bystander = _missing_channel(self.account, 2, name='Bystander')
        db.session.commit()
        target_id, bystander_id = target.id, bystander.id

        resp = self._delete([target_id])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['deleted_count'], 1)
        self.assertIsNone(db.session.get(Channel, target_id))
        self.assertIsNotNone(db.session.get(Channel, bystander_id))

    def test_selection_delete_takes_the_eligible_ones_and_counts_the_rest(self):
        ok_one = _missing_channel(self.account, 1, name='Ok One')
        ok_two = _missing_channel(self.account, 2, name='Ok Two')
        guarded = _missing_channel(self.account, 3, name='Guarded', in_guide=True)
        db.session.commit()
        ids = [ok_one.id, ok_two.id, guarded.id]
        guarded_id = guarded.id

        data = self._delete(ids).get_json()
        self.assertEqual(data['deleted_count'], 2)
        self.assertEqual(data['blocked_inuse_count'], 1)
        self.assertEqual(data['blocked_active_count'], 0)
        self.assertEqual(data['not_missing_count'], 0)
        self.assertIsNotNone(db.session.get(Channel, guarded_id))

    def test_a_live_channel_is_never_deleted_and_is_reported(self):
        live = seed.make_channel(self.account, stream_id=1, name='Still Listed')
        live.last_seen_at = datetime.utcnow()
        self.account.last_sync_at = datetime.utcnow()
        db.session.commit()
        live_id = live.id

        resp = self._delete([live_id])
        self.assertEqual(resp.status_code, 409)
        self.assertIn("still listed in the provider's feed", resp.get_json()['error'])
        self.assertIsNotNone(db.session.get(Channel, live_id))

    def test_blocked_only_request_is_refused_with_the_reason(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        seed.make_recording(status='SCHEDULED', channel_id=ch.id)
        db.session.commit()
        ch_id = ch.id

        resp = self._delete([ch_id])
        self.assertEqual(resp.status_code, 409)
        self.assertIn('recording scheduled or in progress', resp.get_json()['error'])
        self.assertIsNotNone(db.session.get(Channel, ch_id))

    def test_empty_selection_is_refused_not_treated_as_a_whole_account_sweep(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        ch_id = ch.id

        resp = self._delete([])
        self.assertEqual(resp.status_code, 409)
        self.assertIn('No channels were selected', resp.get_json()['error'])
        self.assertIsNotNone(db.session.get(Channel, ch_id),
                             'an empty selection widened into an account-wide delete')

    def test_unknown_channel_id_is_refused_rather_than_silently_succeeding(self):
        resp = self._delete([424242])
        self.assertEqual(resp.status_code, 409)

    def test_malformed_ids_are_rejected_with_400(self):
        resp = self._delete(['nope'])
        self.assertEqual(resp.status_code, 400)
        self.assertIn('channel_ids', resp.get_json()['error'])

    def test_over_the_cap_is_rejected_with_400(self):
        resp = self._delete(list(range(_MAX_REQUESTED_DELETE_IDS + 1)))
        self.assertEqual(resp.status_code, 400)

    def test_account_sweep_with_nothing_eligible_still_succeeds(self):
        """The refusal is for explicit requests only - an account sweep that finds
        nothing is a legitimate no-op, and must not start answering 409."""
        resp = self.t.client.post('/channels/missing-delete',
                                  json={'account_id': self.account.id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['deleted_count'], 0)

    def test_preview_reports_a_live_channel_as_not_missing(self):
        live = seed.make_channel(self.account, stream_id=1, name='Still Listed')
        live.last_seen_at = datetime.utcnow()
        self.account.last_sync_at = datetime.utcnow()
        db.session.commit()

        data = self._preview([live.id]).get_json()
        self.assertEqual(data['eligible_count'], 0)
        self.assertEqual(data['not_missing_count'], 1)
        self.assertEqual(data['not_missing'][0]['name'], 'Still Listed')
        self.assertTrue(data['not_missing'][0]['exists'])

    def test_preview_reports_an_unknown_id_as_not_missing_and_gone(self):
        data = self._preview([424242]).get_json()
        self.assertEqual(data['not_missing_count'], 1)
        self.assertFalse(data['not_missing'][0]['exists'])
        self.assertEqual(data['not_missing'][0]['name'], 'Channel #424242')

    def test_preview_scoped_to_ids_ignores_other_missing_channels(self):
        wanted = _missing_channel(self.account, 1, name='Wanted')
        _missing_channel(self.account, 2, name='Other')
        db.session.commit()

        data = self._preview([wanted.id]).get_json()
        self.assertEqual(data['eligible_count'], 1)

    def test_preview_rejects_malformed_ids_with_400(self):
        resp = self.t.client.get(
            '/api/channels/missing-delete-preview?channel_ids=abc')
        self.assertEqual(resp.status_code, 400)

    def test_account_and_group_previews_report_no_not_missing(self):
        """not_missing only means anything for a requested set - the derived scopes must
        keep reporting zero rather than inventing a number."""
        _missing_channel(self.account, 1)
        db.session.commit()
        data = self.t.client.get(
            f'/api/channels/missing-delete-preview?account_id={self.account.id}').get_json()
        self.assertEqual(data['not_missing_count'], 0)
        self.assertEqual(data['not_missing'], [])

    def test_screenshot_file_removed_for_an_explicit_delete(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        shot = os.path.join(self.t._tmpdir, 'explicit_delete_shot.jpg')
        with open(shot, 'wb') as f:
            f.write(b'fake jpg bytes')
        seed.make_channel_test(ch, screenshot_path=shot)
        db.session.commit()

        self._delete([ch.id])
        self.assertFalse(os.path.exists(shot),
                         'the explicit-id delete left a screenshot behind')


class GroupAndChannelIdCombinedScopeRouteTests(unittest.TestCase):
    """The preview and delete routes with group_id AND channel_ids together
    (dev/changelog/874) - the group/health-check detail page's mobile bulk-select
    sheet's own "Delete N missing channels" action, scoped to the selection rather
    than the whole group. Route-level counterpart to
    MissingDeleteCandidatesCombinedScopeTests, which covers the same fix one layer
    down."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.account = seed.make_account(name='Acct A')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _preview(self, group_id, ids):
        return self.t.client.get(
            f'/api/channels/missing-delete-preview?group_id={group_id}&channel_ids='
            + ','.join(str(i) for i in ids))

    def _delete(self, group_id, ids):
        return self.t.client.post('/channels/missing-delete',
                                  json={'group_id': group_id, 'channel_ids': ids})

    def test_preview_does_not_block_the_selection_on_its_own_group(self):
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        group = seed.make_group(name='G', members=[ch], in_guide=False)
        db.session.commit()

        data = self._preview(group.id, [ch.id]).get_json()
        self.assertEqual(data['eligible_count'], 1)
        self.assertEqual(data['blocked_inuse_count'], 0)

    def test_delete_removes_the_selected_member_of_its_own_group(self):
        """The bug this fixes: before the group_id exemption reached the channel_ids
        path, this exact request 409'd every time - a selected channel is always a
        member of the group whose page it was selected from."""
        ch = _missing_channel(self.account, 1)
        db.session.commit()
        group = seed.make_group(name='G', members=[ch], in_guide=False)
        db.session.commit()
        ch_id, group_id = ch.id, group.id

        resp = self._delete(group_id, [ch_id])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['deleted_count'], 1)
        self.assertIsNone(db.session.get(Channel, ch_id))

    def test_delete_leaves_the_group_s_other_missing_member_untouched(self):
        picked = _missing_channel(self.account, 1, name='Picked')
        not_picked = _missing_channel(self.account, 2, name='Not Picked')
        db.session.commit()
        group = seed.make_group(name='G', members=[picked, not_picked], in_guide=False)
        db.session.commit()
        not_picked_id = not_picked.id

        self._delete(group.id, [picked.id])
        self.assertIsNotNone(db.session.get(Channel, not_picked_id))

    def test_selection_still_reports_not_missing_when_scoped_to_a_group(self):
        """Explicit-id semantics (refusal reasons, not_missing reporting) still apply
        when a group_id rides along - the selection is exactly as "the user asked
        about these ids" as a bare channel_ids request, and must not go quiet just
        because a group happens to be in scope too."""
        live = seed.make_channel(self.account, stream_id=1, name='Still Listed')
        live.last_seen_at = datetime.utcnow()
        self.account.last_sync_at = datetime.utcnow()
        db.session.commit()
        group = seed.make_group(name='G', members=[live], in_guide=False)
        db.session.commit()

        resp = self._delete(group.id, [live.id])
        self.assertEqual(resp.status_code, 409)
        self.assertIn("still listed in the provider's feed", resp.get_json()['error'])

        preview = self._preview(group.id, [live.id]).get_json()
        self.assertEqual(preview['not_missing_count'], 1)


class BlockedActiveCopyTests(unittest.TestCase):
    """Guards dev/docs/BUGS.md 2026-08-21: the preview modal's blocked-active notice read
    "excluded - has a recording in progress", but the server blocks on
    ACTIVE_RECORDING_STATUSES, which includes SCHEDULED. A channel excluded because a
    recording is scheduled against it sent the reader hunting for a capture that was not
    running, and the same dialog's own footnote two paragraphs down said "scheduled or
    in-progress" - so the page contradicted itself.

    A source-text check rather than a rendered one: openMissingModal is DOM-driven and
    async, and what is actually at stake is that one sentence keeps describing the status
    set the server really uses.
    """

    MODAL_JS = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'static', 'js', 'missing-modal.js')

    def test_scheduled_really_is_a_blocker(self):
        """The premise. If this ever stops holding, the copy below is what has to move."""
        from app.database import REC_STATUS_SCHEDULED
        from app.routes.channels import ACTIVE_RECORDING_STATUSES
        self.assertIn(REC_STATUS_SCHEDULED, ACTIVE_RECORDING_STATUSES)

    def test_blocked_active_notice_names_scheduled_too(self):
        with open(self.MODAL_JS, encoding='utf-8') as f:
            src = f.read()
        self.assertIn('recording scheduled or in progress', src)
        self.assertNotIn("'excluded - has a recording in progress:'", src)


class ModalCompletionTests(unittest.TestCase):
    """Guards dev/docs/BUGS.md 2026-08-30: on the health check page, a successful delete
    left the confirmation dialog on screen with no way to tell the delete had finished -
    every OTHER caller of openMissingModal() passes an onDone that navigates away
    (location.reload()/location.href), which incidentally closes the modal as a side
    effect of the page unloading, but group-detail.js's onDone does an in-place
    refreshRows() instead, so nothing ever dismissed it. Also covers the sibling defect:
    the delete button gave no feedback that anything was happening while the request was
    in flight, which reads as a hang on a request that can genuinely take a second or two
    (the account/duplicate-URL counter recompute is a whole-table pass regardless of scope
    size, dev/changelog/684/685).

    Source-text checks, like BlockedActiveCopyTests above: openMissingModal is DOM-driven
    and async, and what matters is that the success path explicitly dismisses the modal
    and gives in-flight feedback, not any particular rendered state.
    """

    MODAL_JS = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'static', 'js', 'missing-modal.js')

    def setUp(self):
        with open(self.MODAL_JS, encoding='utf-8') as f:
            self.src = f.read()

    def test_success_path_closes_the_modal_itself(self):
        """Must not rely solely on opts.onDone to dismiss the dialog - that only holds for
        callers whose onDone navigates away."""
        self.assertIn('modal.closeModal()', self.src)

    def test_modal_closes_before_the_ondone_delay(self):
        """The close call must happen ahead of the onDone/reload setTimeout, not after -
        otherwise a caller that does an in-place refresh leaves the dialog up for the
        900ms toast window with nothing left to close it."""
        close_at = self.src.index('modal.closeModal()')
        timeout_at = self.src.index('setTimeout(() => { if (opts.onDone)')
        self.assertLess(close_at, timeout_at)

    def test_delete_button_shows_in_flight_feedback(self):
        """Matches the rest of the app's button-in-progress convention (e.g.
        'Removing…'/'Adding…' in channel-search.js, dup-modal.js)."""
        self.assertIn("del.textContent = 'Deleting…'", self.src)

    def test_failed_delete_restores_the_button_label(self):
        """A failed request must not strand the button reading 'Deleting…' forever."""
        catch_body = self.src[self.src.index('.catch((err) => {'):]
        self.assertIn('del.textContent = idleText', catch_body)


class ChannelDetailDeleteEntryPointTests(unittest.TestCase):
    """The channel detail page offers Delete channel only while the provider has stopped
    listing that channel (dev/changelog/772).

    Deleting one the provider still serves would be undone by the next sync, which upserts
    on (account_id, stream_id) - the row would come back stripped of its notes, score,
    guide row and group memberships while the user believed it was gone
    (DESIGN-sync-resilience.md 7).

    Patches the lifecycle helper rather than leaning on channel_missing_after_days: a
    runtime load_config() reads the real config.yaml, not this app's overrides
    (CLAUDE.md Testing).
    """

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Acct A')
        self.ch = seed.make_channel(self.account, stream_id=1, name='Some Channel')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _render(self, state):
        since = datetime.utcnow() - timedelta(days=30)
        with mock.patch('app.routes.channels._lifecycle_states_for_channels',
                        return_value={self.ch.id: (state, since)}):
            resp = self.t.client.get(f'/channels/{self.ch.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_missing_channel_offers_the_delete_action(self):
        body = self._render('missing')
        self.assertIn('data-act="delete-channel"', body)
        self.assertIn('isMissing: true', body)

    def test_live_channel_does_not_offer_the_delete_action(self):
        body = self._render(None)
        self.assertNotIn('data-act="delete-channel"', body)
        self.assertIn('isMissing: false', body)

    def test_page_carries_what_the_delete_action_needs(self):
        """The action is wired from the page's own CFG, so a kebab item with no urls
        behind it would render fine and do nothing when clicked."""
        body = self._render('missing')
        self.assertIn('missing-modal.js', body)
        self.assertIn('missingPreview', body)
        self.assertIn('missingDelete', body)


if __name__ == '__main__':
    unittest.main()
