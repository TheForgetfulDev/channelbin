"""Provider-removed ("missing" lifecycle) channels surfaced on the group/health-check
detail page (dev/changelog/653) - the notice-and-modal mechanism channel search already
had for this, replicated to the unified group_detail.html via group_detail_rows().

Covers:
  - group_detail_rows()['missing_channels']: a flat list of this group/check's own
    members whose lifecycle is 'missing', computed once (lifecycle_states_for_channels)
    and shared with the row table and the dup-groups serialization rather than
    recomputed per consumer.
  - Each row in group_detail_rows()['rows'] carries lifecycle/lifecycle_date, matching
    channel search's own row payload shape (the row-level "Missing {date}" badge).
  - _serialize_dup_groups()'s lifecycle_by_channel passthrough: a duplicate set member
    that is ALSO missing still gets the right lifecycle/lifecycle_date in dup_groups when
    group_detail_rows() hands over its own already-computed dict instead of letting
    _serialize_dup_groups recompute it.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_group_missing_channels
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.routes.channel_groups import group_detail_rows  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402


def _make_missing(account, stream_id, name, days_gone=30, synced_days_ago=1, **kw):
    ch = seed.make_channel(account, stream_id=stream_id, name=name, **kw)
    now = datetime.utcnow()
    ch.last_seen_at = now - timedelta(days=days_gone)
    account.last_sync_at = now - timedelta(days=synced_days_ago)
    return ch


class GroupDetailRowsMissingChannelsTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Acct A')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_missing_channels_lists_only_missing_members(self):
        missing_ch = _make_missing(self.account, 1, 'Gone')
        fresh_ch = seed.make_channel(self.account, stream_id=2, name='Fresh')
        self.account.last_sync_at = datetime.utcnow()
        grp = seed.make_group(name='G', members=[missing_ch, fresh_ch],
                              in_guide=False)
        db.session.commit()

        payload = group_detail_rows(grp, None)
        self.assertEqual([c['channel_id'] for c in payload['missing_channels']], [missing_ch.id])
        entry = payload['missing_channels'][0]
        self.assertEqual(entry['channel_name'], 'Gone')
        self.assertEqual(entry['account_name'], 'Acct A')
        self.assertEqual(entry['lifecycle_date'], missing_ch.last_seen_at.strftime('%Y-%m-%d'))

    def test_no_missing_members_gives_empty_list(self):
        ch = seed.make_channel(self.account, stream_id=1, name='Fresh')
        self.account.last_sync_at = datetime.utcnow()
        grp = seed.make_group(name='G', members=[ch], in_guide=False)
        db.session.commit()

        payload = group_detail_rows(grp, None)
        self.assertEqual(payload['missing_channels'], [])

    def test_row_carries_lifecycle_fields(self):
        missing_ch = _make_missing(self.account, 1, 'Gone')
        fresh_ch = seed.make_channel(self.account, stream_id=2, name='Fresh')
        self.account.last_sync_at = datetime.utcnow()
        grp = seed.make_group(name='G', members=[missing_ch, fresh_ch],
                              in_guide=False)
        db.session.commit()

        payload = group_detail_rows(grp, None)
        by_id = {r['channel_id']: r for r in payload['rows']}
        self.assertEqual(by_id[missing_ch.id]['lifecycle'], 'missing')
        self.assertEqual(by_id[missing_ch.id]['lifecycle_date'],
                         missing_ch.last_seen_at.strftime('%Y-%m-%d'))
        self.assertIsNone(by_id[fresh_ch.id]['lifecycle'])
        self.assertEqual(by_id[fresh_ch.id]['lifecycle_date'], '')

    def test_health_check_only_group_membership_also_reports_missing(self):
        """A health-check-only (non-system) group resolves membership the same way a
        recording-source group does for this purpose - check_target_channels(), not
        member_channels()."""
        missing_ch = _make_missing(self.account, 1, 'Gone')
        job = seed.make_test_job(name='Check', channels=[missing_ch])
        db.session.commit()

        payload = group_detail_rows(job.group, job)
        self.assertEqual([c['channel_id'] for c in payload['missing_channels']], [missing_ch.id])

    def test_dup_groups_lifecycle_matches_missing_channels_for_a_dup_that_is_also_missing(self):
        """Guards the _serialize_dup_groups(lifecycle_by_channel=...) passthrough - if
        group_detail_rows() ever went back to letting it recompute lifecycle on its own,
        a duplicate member that is ALSO missing could disagree with missing_channels
        about its own lifecycle_date."""
        shared_url = 'http://example.test/live/shared'
        missing_dup = _make_missing(self.account, 1, 'MissingDup')
        other_dup = seed.make_channel(self.account, stream_id=2, name='OtherDup')
        self.account.last_sync_at = datetime.utcnow()
        missing_dup.stream_url = other_dup.stream_url = shared_url
        grp = seed.make_group(name='G', members=[missing_dup, other_dup],
                              in_guide=False)
        db.session.commit()

        payload = group_detail_rows(grp, None)
        self.assertEqual(len(payload['dup_groups']), 1)
        dup_row = next(c for c in payload['dup_groups'][0]['channels']
                       if c['channel_id'] == missing_dup.id)
        self.assertEqual(dup_row['lifecycle'], 'missing')
        self.assertEqual(dup_row['lifecycle_date'],
                         payload['missing_channels'][0]['lifecycle_date'])


class SuggestMarksProviderRemovedTests(unittest.TestCase):
    """GET /api/channel-groups/suggest - "+ Add Matching Channels" - carries the same
    lifecycle pair as the member rows, so a candidate the provider has dropped is marked
    rather than offered as an ordinary one (dev/docs/BUGS.md 2026-09-19 @ 02:38:01 PM)."""

    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Acct A')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _suggest(self):
        member = seed.make_channel(self.account, stream_id=1, name='FS1', epg_channel_id='fs1')
        gone = _make_missing(self.account, 2, 'FS1', epg_channel_id='fs1')
        live = seed.make_channel(self.account, stream_id=3, name='FS1', epg_channel_id='fs1')
        grp = seed.make_group(name='FS1', members=[member], in_guide=False)
        db.session.commit()
        resp = self.t.client.get(f'/api/channel-groups/suggest?group_id={grp.id}')
        self.assertEqual(resp.status_code, 200)
        return gone, live, {r['channel_id']: r for r in resp.get_json()['results']}

    def test_a_removed_candidate_is_marked_missing_with_its_date(self):
        gone, _live, by_id = self._suggest()
        self.assertEqual(by_id[gone.id]['lifecycle'], 'missing')
        self.assertEqual(by_id[gone.id]['lifecycle_date'],
                         gone.last_seen_at.strftime('%Y-%m-%d'))

    def test_a_removed_candidate_is_still_listed_and_selectable(self):
        """Marked, never filtered: whether a dropped feed is still wanted is the user's call."""
        gone, _live, by_id = self._suggest()
        self.assertIn(gone.id, by_id)
        self.assertTrue(by_id[gone.id]['selectable'])

    def test_a_live_candidate_carries_no_state(self):
        _gone, live, by_id = self._suggest()
        self.assertIsNone(by_id[live.id]['lifecycle'])
        self.assertEqual(by_id[live.id]['lifecycle_date'], '')


if __name__ == '__main__':
    unittest.main()
