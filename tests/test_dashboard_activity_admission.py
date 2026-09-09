"""Tier 2 - a held admission ticket is visible on the activity payload.

dev/changelog/696. `_activity_status_dict()` (app/routes/dashboard.py) builds its background
rows from the database - a SYNCING account, a BUILDING index, a CONVERTING recording - so the
two admission kinds with no row of their own anywhere were invisible: `maintenance` covers the
retention sweep and the daily database maintenance, both of which hold the database axis for
their whole duration with nothing in the UI to show it. That is the silent-background-state
this project's founding principle exists to prevent, and it is also why an out-of-process
reader (dev/tools/search_timing_check.py) could not tell whether a timing run was contended:
the registry lives in the app process's memory and nowhere else.

The three kinds that DO have a database-backed row must not gain a second one from the
registry - that is what test_a_sync_ticket_does_not_duplicate_the_sync_row guards.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import admission  # noqa: E402

ACTIVITY_URL = '/api/activity/status'


class AdmissionActivityIndicatorTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.tickets = []

    def tearDown(self):
        for ticket in self.tickets:
            admission.release(ticket)
        self.t.cleanup()

    def _hold(self, kind, label, force=False):
        ticket = admission.try_start(kind, label, force=force)
        self.assertTrue(ticket.granted, f'could not take a {kind} ticket: {ticket}')
        self.tickets.append(ticket)
        return ticket

    def _background(self):
        return self.t.client.get(ACTIVITY_URL).get_json()['background']

    def test_a_maintenance_ticket_appears_as_a_background_task(self):
        self._hold(admission.KIND_MAINTENANCE, 'recording retention')
        bg = self._background()
        self.assertIn('Database maintenance', [t['label'] for t in bg['tasks']])
        self.assertEqual(bg['state'], 'active')

    def test_the_detail_names_the_particular_job(self):
        self._hold(admission.KIND_MAINTENANCE, 'recording retention')
        detail = [t['detail'] for t in self._background()['tasks']
                  if t['label'] == 'Database maintenance']
        self.assertEqual(detail, ['recording retention'])

    def test_an_unlabelled_maintenance_ticket_still_renders_a_detail(self):
        self._hold(admission.KIND_MAINTENANCE, '')
        detail = [t['detail'] for t in self._background()['tasks']
                  if t['label'] == 'Database maintenance']
        self.assertEqual(detail, ['Running'])

    def test_a_released_ticket_stops_being_reported(self):
        ticket = self._hold(admission.KIND_MAINTENANCE, 'database maintenance')
        admission.release(ticket)
        bg = self._background()
        self.assertEqual(bg['tasks'], [])
        self.assertEqual(bg['admission'], [])
        self.assertEqual(bg['state'], 'hidden')

    def test_a_sync_ticket_does_not_duplicate_the_sync_row(self):
        """Sync has a database-backed row with progress detail; the registry must not add a
        second one saying the same thing without it."""
        self._hold(admission.KIND_SYNC, 'account 3')
        labels = [t['label'] for t in self._background()['tasks']]
        self.assertEqual(labels, [], f'registry produced a duplicate row: {labels}')

    def test_a_tester_ticket_does_not_duplicate_the_health_test_row(self):
        self._hold(admission.KIND_TESTER, 'on-demand job 1')
        labels = [t['label'] for t in self._background()['tasks']]
        self.assertEqual(labels, [], f'registry produced a duplicate row: {labels}')

    def test_every_held_ticket_is_reported_with_its_kind_label_and_age(self):
        self._hold(admission.KIND_TESTER, 'on-demand job 1')
        # Forced: maintenance yields to the tester (BLOCKED_BY), and what this asserts is the
        # reporting of two concurrently-held tickets, not the yield order.
        self._hold(admission.KIND_MAINTENANCE, 'recording retention', force=True)
        held = self._background()['admission']
        self.assertEqual([(h['kind'], h['label']) for h in held],
                         [(admission.KIND_TESTER, 'on-demand job 1'),
                          (admission.KIND_MAINTENANCE, 'recording retention')])
        for entry in held:
            self.assertIsInstance(entry['age_seconds'], int)
            self.assertGreaterEqual(entry['age_seconds'], 0)

    def test_nothing_held_reports_an_empty_registry_not_a_missing_key(self):
        bg = self._background()
        self.assertEqual(bg['admission'], [])
        self.assertEqual(bg['state'], 'hidden')

    def test_maintenance_is_not_counted_as_dashboard_activity(self):
        """No Dashboard page section renders maintenance, so the nav count must not move -
        the same reasoning the search-index rebuild row already carries."""
        before = self.t.client.get(ACTIVITY_URL).get_json()['dashboard_activity_count']
        self._hold(admission.KIND_MAINTENANCE, 'database maintenance')
        after = self.t.client.get(ACTIVITY_URL).get_json()['dashboard_activity_count']
        self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()
