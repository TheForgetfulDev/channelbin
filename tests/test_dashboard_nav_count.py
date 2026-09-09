"""Tier 2 - the Dashboard sidebar nav-count badge (`dashboard_activity_count` in
`_activity_status_dict()`, app/routes/dashboard.py) reflects every Dashboard-visible
in-progress activity, not just recordings actively capturing.

Before this fix the badge only counted `IN_PROGRESS` recordings, so a running account
sync, a running channel health check, or a converting/concatenating recording - all of
which already render as a row somewhere on the Dashboard page - left the badge at zero.
dev/changelog (see the entry added alongside this file).

Search index rebuild is a deliberate exclusion: it has its own `bg_tasks` entry (used by
the top-bar chip-job indicator) but no Dashboard page section shows it, so it must not
inflate this count.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import channel_tester  # noqa: E402
from app import search_index as SI  # noqa: E402
from app.database import Account, SearchIndexState  # noqa: E402

ACTIVITY_URL = '/api/activity/status'


class DashboardActivityCountTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _count(self):
        return self.t.client.get(ACTIVITY_URL).get_json()['dashboard_activity_count']

    def test_nothing_active_is_zero(self):
        self.assertEqual(self._count(), 0)

    def test_capturing_recording_counts(self):
        seed.make_recording(status='IN_PROGRESS')
        db.session.commit()
        self.assertEqual(self._count(), 1)

    def test_converting_and_concatenating_recordings_both_count(self):
        seed.make_recording(status='CONVERTING')
        seed.make_recording(status='CONCATENATING')
        db.session.commit()
        self.assertEqual(self._count(), 2)

    def test_paused_and_retrying_recordings_count(self):
        """dev/docs/BUGS.md 2026-08-15: both render as a row in the Dashboard's
        "Recordings in progress" section, so the badge that claims to count that section
        has to include them. They were absent from the section AND the badge."""
        seed.make_recording(status='PAUSED')
        seed.make_recording(status='RETRYING')
        db.session.commit()
        self.assertEqual(self._count(), 2)

    def test_syncing_account_counts(self):
        seed.make_account(name='Syncing Account')
        db.session.commit()
        Account.query.first().status = 'SYNCING'
        db.session.commit()
        self.assertEqual(self._count(), 1)

    def test_running_health_check_counts(self):
        channel_tester._state.is_running = True
        self.assertEqual(self._count(), 1)

    def test_search_index_rebuild_does_not_count(self):
        db.session.add(SearchIndexState(name=SI.SEARCH_INDEX_PROGRAMS, status=SI.STATUS_BUILDING))
        db.session.commit()
        body = self.t.client.get(ACTIVITY_URL).get_json()
        # It's still in the background chip's task list (a different indicator)...
        labels = [task['label'] for task in body['background']['tasks']]
        self.assertIn('Search index rebuild', labels)
        # ...but must not inflate the Dashboard badge.
        self.assertEqual(body['dashboard_activity_count'], 0)

    def test_combined_total_sums_every_dashboard_visible_type_and_excludes_rebuild(self):
        seed.make_recording(status='IN_PROGRESS')
        seed.make_recording(status='CONVERTING')
        seed.make_account(name='Syncing Account 2')
        db.session.commit()
        Account.query.first().status = 'SYNCING'
        db.session.commit()
        channel_tester._state.is_running = True
        db.session.add(SearchIndexState(name=SI.SEARCH_INDEX_PROGRAMS, status=SI.STATUS_BUILDING))
        db.session.commit()
        # 1 capturing + 1 converting + 1 syncing account + 1 health check = 4,
        # with the search-index rebuild excluded.
        self.assertEqual(self._count(), 4)


if __name__ == '__main__':
    unittest.main(verbosity=2)
