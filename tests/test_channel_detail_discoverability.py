"""Tier 2 - channel-detail / health-check-detail actions are reachable without prior state.

Guards the BUGS.md 2026-07-18 entry: the health-check detail page gated its
"+ Add to Guide" button on the channel having at least one ChannelTest row, so a
never-tested channel offered no way to add it to the guide even though the add
endpoint needs nothing but a channel id.

Also pins the manual health-score adjustment form to being rendered outright on the
channel detail page rather than hidden behind a <details> disclosure.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402


class AddToGuideVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()

    def tearDown(self):
        self.t.cleanup()

    def _payload_row(self, channel):
        """The channel's row out of the unified detail page's embedded row payload.

        Since dev/changelog/273 the Channels table is rendered from this JSON by
        static/js/group-detail.js rather than by Jinja, so "is the action offered"
        is decided by the row's own fields plus the JS gate below - which is exactly
        the pair this test has to hold together."""
        job = seed.make_test_job(channels=[channel])
        db.session.commit()
        resp = self.t.client.get(f'/channels/health-checks/{job.id}')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        blob = body.split('rows: ', 1)[1].split(',\n  counts:', 1)[0]
        rows = json.loads(blob)
        return next(r for r in rows if r['channel_id'] == channel.id)

    def test_add_to_guide_offered_for_never_tested_channel(self):
        """No ChannelTest rows at all - the row must still carry in_guide=False (which is
        the only thing the add action is gated on) and no test result."""
        ch = seed.make_channel(self.acc, stream_id=1, name='Untested Ch', in_guide=False)
        row = self._payload_row(ch)
        self.assertIsNone(row['last_test'])
        self.assertFalse(row['in_guide'])

    def test_in_guide_channel_row_says_so(self):
        """Guards the opposite over-correction: an already-in-guide channel is marked."""
        ch = seed.make_channel(self.acc, stream_id=2, name='Guide Ch', in_guide=True)
        self.assertTrue(self._payload_row(ch)['in_guide'])

    def test_js_does_not_gate_add_action_on_a_test(self):
        """The JS builds the row menu from the row payload; gating on `last_test` here
        would re-introduce the never-tested-channel dead end."""
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           '..', 'static', 'js', 'group-detail.js')
        with open(src, encoding='utf-8') as fh:
            js = fh.read()
        gate = next(line for line in js.splitlines() if "act: 'add-guide'" in line)
        self.assertIn('!r.in_guide', gate)
        self.assertNotIn('last_test', gate)


class GuideConfigCompletenessTests(unittest.TestCase):
    """Guards the BUGS.md 2026-07-25 stray-fetch entry: guide.js's fetchSavedSearches()
    runs unconditionally on DOMContentLoaded (not gated on the guide grid or any UI element),
    so every page that loads guide.js must give GUIDE_CONFIG a savedSearchesUrl - an omitted
    key is JS `undefined`, and fetch(undefined) resolves relative to the current page path
    rather than raising, so the missing key silently becomes a stray same-origin request
    instead of an obvious error."""

    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        # GUIDE_CONFIG (and guide.js) only render when the channel has EPG data
        # (templates/channels/detail.html: `{% if channel.epg_channel_id and
        # total_epg_count > 0 %}`) - an EPG-less channel never hits the bug at all.
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        seed.make_epg_entry(self.ch)

    def tearDown(self):
        self.t.cleanup()

    def test_guide_config_defines_saved_searches_url(self):
        resp = self.t.client.get(f'/channels/{self.ch.id}')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('savedSearchesUrl:', body)
        self.assertIn('deleteSavedSearchUrlBase:', body)


class ManualAdjustmentDiscoverabilityTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')

    def tearDown(self):
        self.t.cleanup()

    def test_adjustment_reachable_without_a_disclosure(self):
        """Since the revamp (dev/changelog/348) the offset lives in the Settings modal, so
        the invariant is expressed against that surface: the Settings section renders as a
        visible card with its chip bar, and the page carries the offset's endpoint and
        current value - no <details> in the way."""
        resp = self.t.client.get(f'/channels/{self.ch.id}')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn(f'/channels/{self.ch.id}/health-adjustment', body)
        self.assertIn('data-section="settings"', body)
        self.assertIn('id="cd-setbar"', body)
        self.assertIn('healthAdjustment:', body)
        self.assertNotIn('<summary', body)

    def test_adjustment_reachable_on_a_never_tested_channel(self):
        """It sits outside the health-score guard - a channel with no score can still be tuned."""
        self.assertIsNone(self.ch.health_score)
        body = self.t.client.get(f'/channels/{self.ch.id}').get_data(as_text=True)
        self.assertIn('data-section="settings"', body)
        self.assertIn('healthAdjustment:', body)


if __name__ == '__main__':
    unittest.main()
