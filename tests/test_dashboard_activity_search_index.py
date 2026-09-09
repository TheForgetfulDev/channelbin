"""Tier 2 - a search-index rebuild shows up in the nav bar's background-task indicator.

dev/docs/BUGS.md 2026-08-04 / dev/changelog/461. `_activity_status_dict()` (app/routes/
dashboard.py) already surfaces an account sync, a channel health test and a conversion/
concatenation as `bg_tasks` entries, but had no entry for a search-index rebuild - so every
sync had a real 50-90 second window where meaningful background work was happening with
nothing in the UI to show it. That is exactly the silent-background-state CLAUDE.md's
founding product principle exists to prevent.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app import search_index as SI  # noqa: E402
from app.database import SearchIndexState  # noqa: E402

ACTIVITY_URL = '/api/activity/status'


class RebuildActivityIndicatorTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _state(self, name, status):
        db.session.add(SearchIndexState(name=name, status=status))
        db.session.commit()

    def test_a_building_index_appears_as_a_background_task(self):
        self._state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_BUILDING)
        body = self.t.client.get(ACTIVITY_URL).get_json()
        labels = [t['label'] for t in body['background']['tasks']]
        self.assertIn('Search index rebuild', labels)
        self.assertEqual(body['background']['state'], 'active')

    def test_the_detail_names_which_index(self):
        self._state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_BUILDING)
        body = self.t.client.get(ACTIVITY_URL).get_json()
        task = next(t for t in body['background']['tasks'] if t['label'] == 'Search index rebuild')
        self.assertIn(SI.SEARCH_INDEX_PROGRAMS, task['detail'])

    def test_both_building_indexes_are_reported(self):
        self._state(SI.SEARCH_INDEX_CHANNELS, SI.STATUS_BUILDING)
        self._state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_BUILDING)
        body = self.t.client.get(ACTIVITY_URL).get_json()
        labels = [t['label'] for t in body['background']['tasks']]
        self.assertEqual(labels.count('Search index rebuild'), 2)

    def test_an_ok_index_is_not_reported_as_active(self):
        self._state(SI.SEARCH_INDEX_PROGRAMS, SI.STATUS_OK)
        body = self.t.client.get(ACTIVITY_URL).get_json()
        labels = [t['label'] for t in body['background']['tasks']]
        self.assertNotIn('Search index rebuild', labels)
        self.assertEqual(body['background']['state'], 'hidden')

    def test_no_state_rows_at_all_is_hidden(self):
        self.assertEqual(SearchIndexState.query.count(), 0)
        body = self.t.client.get(ACTIVITY_URL).get_json()
        self.assertEqual(body['background']['state'], 'hidden')


if __name__ == '__main__':
    unittest.main()
