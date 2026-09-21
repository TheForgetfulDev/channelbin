"""Tier 0 - the Dashboard timeline keeps time, driven in a real DOM.

Guards dev/docs/BUGS.md 2026-09-21 "The Dashboard timeline froze at page load". Every
position the card draws comes from TL.data.now, the server's render time embedded in the
page, and nothing reassigned it - so the axis, the bars and a live recording's fill stayed
where they were while the row beneath them counted up from SSE. Invariants:

  (a) The page registers a 60-second tick.
  (b) After a minute the drawing has moved: the hour ticks, the grid lines and every bar
      slide left by PX_PER_MIN_DESKTOP pixels per elapsed minute, and the captured share of
      a live recording's bar grows.
  (c) The now-line does NOT move, and neither does the track width. TL.start is always
      now - TL_BACK_HOURS, so tlPx(now) is a constant: NOW is a fixed post and the drawing
      slides underneath it. This is asserted so a later change cannot "fix" it.
  (d) A hidden tab renders nothing, however many periods pass.
  (e) Becoming visible again renders immediately, catching up all of it at once.
  (f) The rebuild keeps the region whole - the scroller, the bar count and the tooltip text
      every bar carries survive it.

tests/support/dashboard_timeline.mjs replays this against the markup the dashboard route
really rendered; every assertion lives here.

  python3 -m unittest tests.test_dashboard_timeline_tick
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.database import REC_STATUS_IN_PROGRESS, REC_STATUS_SCHEDULED  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_recording  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'dashboard_timeline.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

# static/js/util.js - the one timeline scale, shared with the TV Guide. Restated here as the
# expected result rather than imported, because a test that derived it from the same source
# as the code would pass whatever that source said.
PX_PER_MIN_DESKTOP = 4

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='dashboard_timeline_js_')
    try:
        with t.app.app_context():
            acc = make_account(name='Acct One')
            ch = make_channel(acc, name='Channel One')
            # Seeded around the render time by make_recording's defaults: an hour in and an
            # hour to go, so the bar is inside the window with a fill that can grow.
            make_recording(status=REC_STATUS_IN_PROGRESS, name='Live', channel_id=ch.id)
            make_recording(status=REC_STATUS_SCHEDULED, name='Upcoming', channel_id=ch.id)
            db.session.commit()
        html = t.app.test_client().get('/').get_data(as_text=True)
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as f:
            f.write(html)
        proc = subprocess.run(['node', HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class TimelineClockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_the_page_ran_without_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_the_seeded_page_drew_a_timeline_to_measure(self):
        """Without this the movement assertions below would pass on an empty card."""
        self.assertTrue(self.obs['initial']['scrollerPresent'])
        self.assertGreaterEqual(self.obs['initial']['bars'], 2)
        self.assertTrue(self.obs['initial']['tickLefts'])
        self.assertTrue(self.obs['initial']['fillWidths'])

    def test_a_sixty_second_tick_is_registered(self):
        self.assertEqual(self.obs['ticksFound'], 1,
                         f"interval periods registered: {self.obs['intervalPeriods']}")

    def _shift(self, before, after, minutes):
        self.assertEqual(len(before), len(after))
        self.assertTrue(before)
        for b, a in zip(before, after):
            self.assertAlmostEqual(b - a, PX_PER_MIN_DESKTOP * minutes, places=1)

    def _shift_ticks(self, before, after, minutes):
        """The hour ticks, matched by the hour each one names rather than by position.

        The window is anchored to `now`, so advancing the clock across an hour boundary
        adds a tick at one end and drops one off the other: index N is a different hour in
        the two snapshots, and comparing by position reports a whole hour of travel
        (240px) as a regression. That made this test fail purely on what time of day the
        suite ran (dev/docs/BUGS.md 2026-09-21 @ 08:08:00 AM ET)."""
        b = dict(zip(before['tickLabels'], before['tickLefts']))
        a = dict(zip(after['tickLabels'], after['tickLefts']))
        shared = [k for k in b if k in a]
        self.assertTrue(shared, 'no hour survived the advance - nothing was compared')
        for k in shared:
            self.assertAlmostEqual(b[k] - a[k], PX_PER_MIN_DESKTOP * minutes, places=1, msg=k)

    def test_the_axis_slides_left_one_scale_step_per_minute(self):
        i, m = self.obs['initial'], self.obs['afterOneMinute']
        self._shift_ticks(i, m, 1)

    def test_the_grid_lines_sit_on_the_hour_ticks(self):
        """Both come from one loop over the same hours, so they can only ever be the same
        positions - which is also what lets the tick labels identify a grid line."""
        for key in ('initial', 'afterOneMinute', 'afterBecomingVisible'):
            snap = self.obs[key]
            self.assertEqual(snap['gridLefts'], snap['tickLefts'], key)

    def test_every_bar_slides_with_the_axis(self):
        i, m = self.obs['initial'], self.obs['afterOneMinute']
        self._shift(i['barLefts'], m['barLefts'], 1)

    def test_a_live_recordings_fill_grows(self):
        i, m = self.obs['initial'], self.obs['afterOneMinute']
        self.assertEqual(len(i['fillWidths']), len(m['fillWidths']))
        for b, a in zip(i['fillWidths'], m['fillWidths']):
            self.assertGreater(a, b)

    def test_the_now_line_and_the_track_width_never_move(self):
        for key in ('afterOneMinute', 'afterHiddenTicks', 'afterBecomingVisible'):
            self.assertEqual(self.obs[key]['nowLeft'], self.obs['initial']['nowLeft'], key)
            self.assertEqual(self.obs[key]['trackWidth'], self.obs['initial']['trackWidth'], key)

    def test_a_hidden_tab_renders_nothing(self):
        self.assertEqual(self.obs['afterHiddenTicks'], self.obs['afterOneMinute'])

    def test_becoming_visible_catches_up_every_skipped_period_at_once(self):
        m, v = self.obs['afterOneMinute'], self.obs['afterBecomingVisible']
        self._shift_ticks(m, v, 10)
        self._shift(m['barLefts'], v['barLefts'], 10)

    def test_the_rebuild_keeps_the_region_whole(self):
        for key in ('afterOneMinute', 'afterBecomingVisible'):
            after = self.obs[key]
            self.assertTrue(after['scrollerPresent'], key)
            self.assertEqual(after['bars'], self.obs['initial']['bars'], key)
            # The phone bottom sheet is built from the tapped node's own data-tip, so a
            # rebuild that dropped it would take the only way to read a bar on a phone.
            self.assertEqual(after['tips'], self.obs['initial']['tips'], key)
            self.assertEqual(after['counts'], self.obs['initial']['counts'], key)


if __name__ == '__main__':
    unittest.main()
