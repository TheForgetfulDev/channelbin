"""Tier 2 - the nav bar's Recording chip sees every recording whose window is open, not
only the ones actively capturing (`_activity_status_dict()`, app/routes/dashboard.py).

dev/docs/BUGS.md 2026-08-15: PAUSED and RETRYING were queried alongside SCHEDULED under a
`Recording.start_time > now` clause. Both statuses are reachable only from IN_PROGRESS, so
their start_time is always in the past - the clause matched them zero times, forever,
without erroring, and a paused or retrying recording left the chip dark as though nothing
were happening. RETRYING is the one that motivated the fix (a dead stream backing off
toward a reconnect is precisely what this indicator exists to surface), but PAUSED had been
broken on the same line longer.

The source-shape case at the bottom is what stops the two clauses being recombined: an
assertion on behavior alone would still pass if someone re-added the filter and a future
test happened to seed only past-start rows.
"""
import os
import re
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402

ACTIVITY_URL = '/api/activity/status'

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _past_window(**kw):
    """A recording whose window is already open - the only shape PAUSED/RETRYING can have."""
    now = datetime.utcnow()
    return seed.make_recording(start_time=now - timedelta(minutes=20),
                               stop_time=now + timedelta(minutes=40), **kw)


class WindowOpenStatusesAreActiveTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _rec(self):
        return self.t.client.get(ACTIVITY_URL).get_json()['recording']

    def test_retrying_recording_is_active_and_named(self):
        _past_window(status='RETRYING', name='Dead stream')
        db.session.commit()
        rec = self._rec()
        self.assertEqual(rec['state'], 'active')
        self.assertEqual([r['name'] for r in rec['active']], ['Dead stream'])
        self.assertEqual(rec['active'][0]['state_label'], 'Waiting to retry')
        self.assertEqual(rec['active'][0]['status'], 'RETRYING')

    def test_paused_recording_is_active_and_named(self):
        _past_window(status='PAUSED', name='Paused one')
        db.session.commit()
        rec = self._rec()
        self.assertEqual(rec['state'], 'active')
        self.assertEqual([r['name'] for r in rec['active']], ['Paused one'])
        self.assertEqual(rec['active'][0]['state_label'], 'Paused')

    def test_capturing_recording_is_still_labeled_recording(self):
        _past_window(status='IN_PROGRESS', name='Capturing')
        db.session.commit()
        rec = self._rec()
        self.assertEqual(rec['state'], 'active')
        self.assertEqual(rec['active'][0]['state_label'], 'Recording')

    def test_a_window_open_recording_is_not_counted_as_scheduled(self):
        """The chip's 'N scheduled' line must not absorb them: their window is running,
        so they are not something coming up later."""
        _past_window(status='RETRYING')
        _past_window(status='PAUSED')
        db.session.commit()
        rec = self._rec()
        self.assertEqual(rec['scheduled_count'], 0)
        self.assertIsNone(rec['next_scheduled'])
        self.assertEqual(len(rec['active']), 2)

    def test_future_scheduled_recording_alone_is_still_dim(self):
        """The pre-existing contract (tests/test_activity_chip_dim.py) must survive the
        query split: SCHEDULED keeps its future-start_time clause."""
        seed.make_recording(status='SCHEDULED', name='Later',
                            start_time=datetime.utcnow() + timedelta(hours=1),
                            stop_time=datetime.utcnow() + timedelta(hours=2))
        db.session.commit()
        rec = self._rec()
        self.assertEqual(rec['state'], 'dim')
        self.assertEqual(rec['active'], [])
        self.assertEqual(rec['next_scheduled']['name'], 'Later')

    def test_a_past_scheduled_row_is_not_dim(self):
        """A SCHEDULED row whose start slipped by is not "imminent" - it is a stuck job,
        and the dim state must not claim otherwise."""
        _past_window(status='SCHEDULED')
        db.session.commit()
        self.assertEqual(self._rec()['state'], 'hidden')


class WindowOpenQueryShapeTests(unittest.TestCase):
    """The clause that could never match must not come back."""

    def test_the_window_open_query_carries_no_start_time_clause(self):
        src = open(os.path.join(REPO, 'app', 'routes', 'dashboard.py'), encoding='utf-8').read()
        marker = 'live_recs = Recording.query.filter('
        self.assertIn(marker, src, 'the window-open recordings are no longer their own query')
        body = src.split(marker, 1)[1].split(').order_by', 1)[0]
        self.assertIn('WINDOW_OPEN_STATUSES', body)
        self.assertNotIn('start_time', body,
                         'A window-open recording always started in the past; filtering these '
                         'statuses on start_time matches nothing (dev/docs/BUGS.md 2026-08-15).')

    def test_every_window_open_status_has_an_indicator_label(self):
        """A status added to WINDOW_OPEN_STATUSES without a label here would KeyError in the
        chip payload rather than rendering as some other state - but only if the map is
        actually kept in step, which is what this asserts."""
        from app.database import WINDOW_OPEN_STATUSES
        from app.routes.dashboard import _WINDOW_OPEN_LABEL
        self.assertEqual(set(WINDOW_OPEN_STATUSES), set(_WINDOW_OPEN_LABEL))


class TooltipEscapingTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-08-15: buildRecTooltip/buildBgTooltip in base.html wrote
    recording, account and job names straight into tooltip.innerHTML while buildStatsTip six
    lines above them escaped its own. Recording names carry EPG program titles, so the input
    is provider-supplied."""

    def test_activity_tooltips_escape_every_name_they_interpolate(self):
        src = open(os.path.join(REPO, 'templates', 'base.html'), encoding='utf-8').read()
        block = src.split('function buildRecTooltip(data) {', 1)[1].split('function showTooltip', 1)[0]
        raw = re.findall(r"\+ ((?:r|ns|t)\.(?:name|label|detail|state_label)) ", block)
        self.assertEqual(raw, [], f'Interpolated into innerHTML without escHtml: {raw}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
