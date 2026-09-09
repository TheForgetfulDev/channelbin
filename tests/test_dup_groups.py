"""Tier 2 - duplicate-set serialization for the Remove Duplicates modal.

Covers the 2026-07-18 dedupe-clarity work (changelog/184): the modal now pre-selects a
keeper server-side and shows category/score/test context per row, and the health-check
detail page raises a duplicate banner whose severity depends on how many members of a
set are still enabled.

The keeper rule is deliberately server-side so it can be asserted here rather than in JS,
and so it shares effective_score() with group failover instead of re-deriving ranking.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.accounts import duplicate_groups_within  # noqa: E402
from app.routes.channel_tests import _serialize_dup_groups  # noqa: E402

SHARED_URL = 'http://example.test/live/shared'
CFG = {'sync': {'channel_missing_after_days': 7, 'channel_new_within_days': 3}}


def _days_ago(n):
    return datetime.utcnow() - timedelta(days=n)


class SerializeDupGroupsTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()

    def tearDown(self):
        self.t.cleanup()

    def _dupe_pair(self, **overrides):
        """Two channels sharing one stream_url - a duplicate set of 2, oldest first."""
        a = seed.make_channel(self.acc, stream_id=1, name='Alpha',
                              category_name='Sports', **overrides)
        b = seed.make_channel(self.acc, stream_id=2, name='Beta', category_name='Sports')
        a.stream_url = b.stream_url = SHARED_URL
        db.session.flush()
        return a, b

    def test_tests_by_channel_and_disabled_ids_are_genuinely_optional(self):
        """app/routes/channel_groups.py calls this with only groups+cfg - a required
        tests_by_channel/disabled_ids param would 500 it. cfg itself IS required (every
        scope has one to hand over - unlike test results, which only health-check callers
        have)."""
        a, b = self._dupe_pair()
        groups = _serialize_dup_groups(duplicate_groups_within([a, b]), CFG)
        self.assertEqual(len(groups), 1)
        row = groups[0]['channels'][0]
        self.assertEqual(row['test_status'], 'WAITING')
        self.assertFalse(row['disabled'])

    def test_row_carries_the_decision_making_context(self):
        a, b = self._dupe_pair()
        a.health_score = 70.0
        db.session.flush()
        t = seed.make_channel_test(a)
        t.status = 'COMPLETED'   # the factory defaults to FAILED
        groups = _serialize_dup_groups(duplicate_groups_within([a, b]), CFG,
                                       {a.id: t}, {b.id})
        by_id = {c['channel_id']: c for c in groups[0]['channels']}
        self.assertEqual(by_id[a.id]['category_name'], 'Sports')
        self.assertEqual(by_id[a.id]['score'], 70)
        self.assertEqual(by_id[a.id]['test_status'], 'PASS')
        self.assertIsNotNone(by_id[a.id]['tested_et'])
        self.assertTrue(by_id[a.id]['is_oldest'])
        self.assertFalse(by_id[b.id]['is_oldest'])
        self.assertTrue(by_id[b.id]['disabled'])

    def test_warn_status_when_completed_with_an_error_detail(self):
        a, b = self._dupe_pair()
        t = seed.make_channel_test(a)
        t.status, t.error_detail = 'COMPLETED', 'low bitrate'
        groups = _serialize_dup_groups(duplicate_groups_within([a, b]), CFG, {a.id: t})
        by_id = {c['channel_id']: c for c in groups[0]['channels']}
        self.assertEqual(by_id[a.id]['test_status'], 'WARN')

    def test_in_guide_member_is_the_suggested_keeper(self):
        """Even when it's the newer, lower-scoring entry."""
        a, b = self._dupe_pair()
        a.health_score = 95.0
        b.in_guide = True
        b.health_score = 10.0
        db.session.flush()
        groups = _serialize_dup_groups(duplicate_groups_within([a, b]), CFG)
        self.assertEqual(groups[0]['suggested_keep_id'], b.id)

    def test_falls_back_to_highest_effective_score(self):
        """Nothing in the guide → the feed most likely to work wins."""
        a, b = self._dupe_pair()
        a.health_score = 20.0
        b.health_score = 80.0
        db.session.flush()
        groups = _serialize_dup_groups(duplicate_groups_within([a, b]), CFG)
        self.assertEqual(groups[0]['suggested_keep_id'], b.id)

    def test_manual_adjustment_counts_toward_the_fallback(self):
        a, b = self._dupe_pair()
        a.health_score = 50.0
        a.manual_health_adjustment = 30
        b.health_score = 60.0
        db.session.flush()
        groups = _serialize_dup_groups(duplicate_groups_within([a, b]), CFG)
        self.assertEqual(groups[0]['suggested_keep_id'], a.id)

    def test_score_tie_breaks_to_the_oldest_entry(self):
        a, b = self._dupe_pair()
        a.health_score = b.health_score = 50.0
        db.session.flush()
        groups = _serialize_dup_groups(duplicate_groups_within([a, b]), CFG)
        self.assertEqual(groups[0]['suggested_keep_id'], min(a.id, b.id))

    def test_several_in_guide_falls_back_to_score_among_those_only(self):
        """A higher-scoring non-guide channel must not beat the in-guide candidates."""
        a, b = self._dupe_pair()
        c = seed.make_channel(self.acc, stream_id=3, name='Gamma')
        c.stream_url = SHARED_URL
        a.in_guide = b.in_guide = True
        a.health_score, b.health_score, c.health_score = 40.0, 60.0, 99.0
        db.session.flush()
        groups = _serialize_dup_groups(duplicate_groups_within([a, b, c]), CFG)
        self.assertEqual(groups[0]['suggested_keep_id'], b.id)

    def test_missing_channel_carries_the_lifecycle_badge_fields(self):
        """dev/docs/BUGS.md 2026-08-14: the modal never showed provider-removed status at
        all - lifecycle/lifecycle_date must reach the row so the JS badge can render."""
        a, b = self._dupe_pair()
        a.last_seen_at = _days_ago(10)
        self.acc.last_sync_at = _days_ago(1)
        db.session.flush()
        groups = _serialize_dup_groups(duplicate_groups_within([a, b]), CFG)
        by_id = {c['channel_id']: c for c in groups[0]['channels']}
        self.assertEqual(by_id[a.id]['lifecycle'], 'missing')
        self.assertTrue(by_id[a.id]['lifecycle_date'])
        self.assertIsNone(by_id[b.id]['lifecycle'])
        self.assertEqual(by_id[b.id]['lifecycle_date'], '')

    def test_missing_channel_is_never_the_suggested_keeper_even_in_guide(self):
        """dev/docs/BUGS.md 2026-08-14: today an in-guide-but-missing channel wins
        outright, defaulting the keep-selection to a channel the provider dropped."""
        a, b = self._dupe_pair()
        a.in_guide = True
        a.last_seen_at = _days_ago(10)
        self.acc.last_sync_at = _days_ago(1)
        db.session.flush()
        groups = _serialize_dup_groups(duplicate_groups_within([a, b]), CFG)
        self.assertEqual(groups[0]['suggested_keep_id'], b.id)

    def test_missing_channel_excluded_even_when_it_scores_highest(self):
        a, b = self._dupe_pair()
        a.health_score = 99.0
        a.last_seen_at = _days_ago(10)
        b.health_score = 10.0
        self.acc.last_sync_at = _days_ago(1)
        db.session.flush()
        groups = _serialize_dup_groups(duplicate_groups_within([a, b]), CFG)
        self.assertEqual(groups[0]['suggested_keep_id'], b.id)

    def test_all_missing_falls_back_to_normal_ranking(self):
        """Never leave no candidate: if every member is missing, the existing in-guide/
        score ranking still picks one."""
        a, b = self._dupe_pair()
        a.health_score, b.health_score = 20.0, 80.0
        a.last_seen_at = b.last_seen_at = _days_ago(10)
        self.acc.last_sync_at = _days_ago(1)
        db.session.flush()
        groups = _serialize_dup_groups(duplicate_groups_within([a, b]), CFG)
        self.assertEqual(groups[0]['suggested_keep_id'], b.id)


class DupBannerRenderTests(unittest.TestCase):
    """The banner's content comes solely from renderDupBanner() in
    static/js/group-detail.js - the server just ships the shell plus the dup sets in
    the page's GROUP_DETAIL blob (changelog/273 unified the two detail pages)."""

    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()

    def tearDown(self):
        self.t.cleanup()

    def _detail_body(self, channels, disabled=()):
        job = seed.make_test_job(channels=channels, disabled=disabled)
        db.session.commit()
        resp = self.t.client.get(f'/channels/health-checks/{job.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_banner_shell_and_updater_are_both_present(self):
        ch = seed.make_channel(self.acc, stream_id=1, name='Solo')
        body = self._detail_body([ch])
        self.assertIn('id="gd-dup-banner"', body)
        self.assertIn('group-detail.js', body)
        js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               '..', 'static', 'js', 'group-detail.js')
        with open(js_path, encoding='utf-8') as fh:
            self.assertIn('function renderDupBanner()', fh.read())

    def test_dup_groups_payload_reaches_the_page_with_disabled_state(self):
        """The banner's quiet-vs-loud rule reads `disabled` off each member, so the
        page's dup sets must carry it - not just the results rows."""
        a = seed.make_channel(self.acc, stream_id=1, name='Alpha')
        b = seed.make_channel(self.acc, stream_id=2, name='Beta')
        a.stream_url = b.stream_url = SHARED_URL
        db.session.flush()
        body = self._detail_body([a, b], disabled=[b.id])
        payload = json.loads(body.split('dupGroups: ', 1)[1].split(',\n  missingChannels', 1)[0])
        self.assertEqual(len(payload), 1)
        by_id = {c['channel_id']: c for c in payload[0]['channels']}
        self.assertFalse(by_id[a.id]['disabled'])
        self.assertTrue(by_id[b.id]['disabled'])


if __name__ == '__main__':
    unittest.main()
