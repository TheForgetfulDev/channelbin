"""The recordings list's saved filters reach the page, and a bad stored value cannot 500 it.

The list is one /api/user-prefs row of user-written JSON, rendered into the page for
filter-bar.js (dev/changelog/1156). The behaviour of the saved filters themselves is driven
in a real DOM by tests/test_filter_bar_js.py and tests/test_recordings_page_live_js.py;
this file holds what only the route can answer.

  python3 -m unittest tests.test_recordings_saved_filters
"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.database import REC_STATUS_COMPLETED, UserPref  # noqa: E402
from app.routes.recordings import RECORDINGS_SAVED_FILTERS_PREF  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402


class SavedFiltersRouteTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        seed.make_recording(status=REC_STATUS_COMPLETED, name='Something')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _store(self, raw):
        db.session.add(UserPref(key=RECORDINGS_SAVED_FILTERS_PREF, value=raw))
        db.session.commit()

    def _rendered(self):
        resp = self.client.get('/recordings')
        self.assertEqual(resp.status_code, 200)
        m = re.search(r'<script type="application/json" id="rec-saved-filters">(.*?)</script>',
                      resp.get_data(as_text=True), re.S)
        self.assertIsNotNone(m, 'the saved-filter block is missing from the page')
        return json.loads(m.group(1))

    def test_nothing_stored_renders_an_empty_list_and_the_key(self):
        """The page posts back to the key it was handed, so the two cannot drift."""
        self.assertEqual(self._rendered(), {'key': RECORDINGS_SAVED_FILTERS_PREF, 'list': []})

    def test_a_stored_list_reaches_the_page(self):
        saved = [{'name': 'Done', 'filters': [['status', 'COMPLETED']], 'is_default': True}]
        self._store(json.dumps(saved))
        self.assertEqual(self._rendered()['list'], saved)

    def test_a_stored_value_that_is_not_a_list_is_nothing_saved(self):
        self._store(json.dumps({'name': 'not a list'}))
        self.assertEqual(self._rendered()['list'], [])

    def test_stored_text_that_is_not_json_does_not_500_the_page(self):
        self._store('{not json')
        with self.assertLogs('app.routes.recordings', level='WARNING'):
            self.assertEqual(self._rendered()['list'], [])


if __name__ == '__main__':
    unittest.main()
