"""Tier 2 - rendering DIAGNOSTICS events on the recording detail page (dev/changelog/334).

Characterization tests, not regression guards: nothing here was a defect producing wrong
output. dev/changelog/331 and 332 taught the post-processor to write two DIAGNOSTICS events
per recording (kind='timeline_scan' and kind='capture_health'), and until dev/changelog/334
none of it reached a screen.

The one thing that is a real hazard rather than a characterization is malformed extra_data:
the column is free-form JSON text, so a bad blob reaching a per-row template filter would
500 the whole page. test_malformed_extra_data_does_not_500 covers it.

No ffmpeg, no network, no /dvr - these render a seeded row through the test client.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import DIAGNOSTICS, RecordingEvent, add_recording_event  # noqa: E402
from app.routes.recordings import diag_view_filter  # noqa: E402

_DETAILS_TAG = '<details class="ev-extra">'
TIMELINE_EXTRA = {
    'kind': 'timeline_scan',
    'gap_basis': 'dts',
    'gap_threshold': 0.25,
    'backward_count': 3,
    'missing_seconds': 1.75,
    'span_seconds': 3600.5,
    'packet_count': 89412,
    'fps': 29.97,
}
HEALTH_EXTRA = {
    'kind': 'capture_health',
    'expected_frame_count': 107910,
    'adjusted_window_seconds': 3600.0,
    'scheduled_duration_seconds': 3600.0,
}


class DiagViewFilterTests(unittest.TestCase):
    """The filter is pure, so it needs no app context."""

    def test_timeline_scan_payload(self):
        view = diag_view_filter(json.dumps(TIMELINE_EXTRA))
        self.assertEqual(view['title'], 'Timeline scan details')
        labels = [label for label, _ in view['rows']]
        # 'kind' is the title, so it must not also be a row.
        self.assertNotIn('Kind', labels)
        self.assertEqual(len(view['rows']), len(TIMELINE_EXTRA) - 1)
        self.assertIn('FPS', labels)
        self.assertIn('Gap threshold (s)', labels)
        self.assertIn('Backward DTS steps', labels)
        # _seconds keys derive their unit suffix mechanically.
        self.assertIn('Missing (s)', labels)
        self.assertIn('Span (s)', labels)

    def test_capture_health_payload(self):
        view = diag_view_filter(json.dumps(HEALTH_EXTRA))
        self.assertEqual(view['title'], 'Capture health details')
        rows = dict(view['rows'])
        self.assertEqual(rows['Expected frame count'], 107910)
        self.assertEqual(rows['Adjusted window (s)'], 3600.0)
        self.assertEqual(rows['Scheduled duration (s)'], 3600.0)

    def test_failure_shapes_render_as_yes(self):
        """Both writers' failure payloads leave the columns NULL and say so in extra_data."""
        for kind, flag in (('timeline_scan', 'scan_failed'), ('capture_health', 'probe_failed')):
            view = diag_view_filter(json.dumps({'kind': kind, flag: True}))
            self.assertEqual(view['rows'], [(flag.replace('_', ' ').capitalize(), 'yes')])

    def test_unknown_kind_and_keys_still_render(self):
        """DIAGNOSTICS is a generic carrier - a future kind must render without a code change."""
        view = diag_view_filter(json.dumps({'kind': 'slate_scan', 'black_frame_count': 12}))
        self.assertEqual(view['title'], 'Slate scan details')
        self.assertEqual(view['rows'], [('Black frame count', 12)])

    def test_none_when_nothing_to_show(self):
        for raw in (None, '', '{}', json.dumps({'kind': 'timeline_scan'})):
            self.assertIsNone(diag_view_filter(raw), raw)

    def test_none_for_malformed_or_non_dict_json(self):
        for raw in ('{not json', '[]', '5', '"text"', 'null'):
            self.assertIsNone(diag_view_filter(raw), raw)

    def test_null_value_renders_as_em_dash(self):
        view = diag_view_filter(json.dumps({'kind': 'timeline_scan', 'fps': None}))
        self.assertEqual(view['rows'], [('FPS', '-')])


class DiagnosticsRenderTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.rec = seed.make_recording(status='COMPLETED')
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _add_event(self, extra_json, detail='Timeline scan: clean'):
        ev = RecordingEvent(recording_id=self.rec.id, event_type=DIAGNOSTICS,
                            detail=detail, extra_data=extra_json)
        db.session.add(ev)
        db.session.commit()
        return ev

    def _page(self):
        resp = self.t.client.get(f'/recordings/{self.rec.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_both_kinds_render_collapsed_disclosures(self):
        """A post-process emits two DIAGNOSTICS events; both get their own disclosure."""
        self._add_event(json.dumps(TIMELINE_EXTRA))
        self._add_event(json.dumps(HEALTH_EXTRA), detail='Capture health: 1920x1080')
        html = self._page()
        self.assertEqual(html.count(_DETAILS_TAG), 2)
        self.assertIn('Timeline scan details', html)
        self.assertIn('Capture health details', html)
        # Collapsed by default - `open` is what would make it expanded on load.
        self.assertNotIn('<details class="ev-extra" open>', html)
        self.assertIn('Backward DTS steps', html)
        self.assertIn('89412', html)

    def test_event_li_carries_db_id(self):
        """The disclosure re-apply keys on the DB id; without it the swap loses state."""
        ev = self._add_event(json.dumps(TIMELINE_EXTRA))
        self.assertIn(f'data-ev-id="{ev.id}"', self._page())

    def test_diagnostics_classed_info(self):
        self._add_event(json.dumps(TIMELINE_EXTRA))
        self.assertIn('class="ev info" data-ev-id=', self._page())

    def test_no_disclosure_without_extra_data(self):
        self._add_event(None, detail='Timeline scan: clean')
        html = self._page()
        self.assertIn('Timeline scan: clean', html)
        # Matched on the tag, not the bare class name: the page's own inline script
        # mentions .ev-extra in its selector and comment.
        self.assertNotIn(_DETAILS_TAG, html)

    def test_malformed_extra_data_does_not_500(self):
        """extra_data is free-form JSON text; a bad blob must degrade, not break the page."""
        self._add_event('{"kind": "timeline_scan", oops')
        html = self._page()
        self.assertIn('Timeline scan: clean', html)
        self.assertNotIn(_DETAILS_TAG, html)

    def test_diagnostics_is_not_a_timeline_milestone(self):
        """Diagnostics are not milestones - they must not put a dot on the run-timeline strip."""
        self._add_event(json.dumps(TIMELINE_EXTRA))
        html = self._page()
        # The milestone strip renders a label per event it recognises; DIAGNOSTICS has no
        # timeline_label entry, so the type appears exactly once - in the event-log row.
        self.assertEqual(html.count('>DIAGNOSTICS<'), 1)
        self.assertIn(f'<span class="ev-type">{DIAGNOSTICS}</span>', html)

    def test_add_recording_event_payload_round_trips_to_the_page(self):
        """End-to-end through the real writer helper, not a hand-built row."""
        add_recording_event(self.rec.id, DIAGNOSTICS, detail='Capture health: x',
                            extra=HEALTH_EXTRA)
        db.session.commit()
        self.assertIn('Expected frame count', self._page())


if __name__ == '__main__':
    unittest.main()
