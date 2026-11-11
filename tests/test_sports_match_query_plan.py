"""The sports match readers start from the matches, never from every visible channel
(app/sports_match.py::_offered). Throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_match_query_plan
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402

from app import db, sports_match  # noqa: E402
from app.database import Channel  # noqa: E402


class OfferedFilterPlanTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-11-10 11:37 AM: a league's page took 1.4 s to render because the hidden-channel
    filter let SQLite drive game_counts() and name_counts() from ix_channels_standing, walking every
    visible channel (~200,000 on the dev box) instead of the few matches for the page's events. The
    plan comes from the schema with no table statistics, so an empty test database gets the same one
    once the event list is as long as a league page's (it flips between 3 and 10 events here)."""

    EVENTS = list(range(1, 61))

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)

    def _plan(self, run) -> list:
        seen = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            seen.append((statement, parameters))
        event.listen(Engine, 'before_cursor_execute', capture)
        try:
            run()
        finally:
            event.remove(Engine, 'before_cursor_execute', capture)
        statement, parameters = next(s for s in reversed(seen) if 'channels' in s[0])
        with db.engine.connect() as conn:
            rows = conn.exec_driver_sql('EXPLAIN QUERY PLAN ' + statement, parameters).fetchall()
        return [r[-1] for r in rows]

    def assert_not_driven_by_channels(self, plan):
        self.assertFalse([step for step in plan if 'ix_channels_standing' in step], plan)
        self.assertNotRegex(plan[0], r'\bchannels\b', plan)

    def test_game_counts_starts_from_the_matches(self):
        self.assert_not_driven_by_channels(self._plan(lambda: sports_match.game_counts(self.EVENTS)))

    def test_name_counts_starts_from_the_matches(self):
        self.assert_not_driven_by_channels(self._plan(lambda: sports_match.name_counts(self.EVENTS)))

    def test_a_channel_id_lookup_uses_the_primary_key(self):
        plan = self._plan(lambda: db.session.query(Channel.id).filter(
            Channel.id.in_([1, 2, 3]), sports_match._offered()).all())
        self.assertFalse([step for step in plan if 'ix_channels_standing' in step], plan)
        self.assertIn('PRIMARY KEY', plan[0])


if __name__ == '__main__':
    unittest.main()
