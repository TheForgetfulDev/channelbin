"""Tier 2 - a recording whose file changes format part-way through says so on its own
surfaces, now that no alert does.

Guards dev/docs/BUGS.md 2026-09-27 @ 02:04:32 PM ET. The RECORDING_FORMAT_OVERRIDE and
RECORDING_FORMAT_CHANGED alerts were retired on the argument that the recording's detail page
is where a question about that file gets asked (dev/changelog/928). On that page both events
took the default `info` class, the Stats card's Video row showed one format with no flag that
the file held two, and the Recordings list health pill called the file a "clean capture".

No ffmpeg, no network, no /dvr - seeded rows rendered through `_index_row` and the test client.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.channel_groups import recording_formats  # noqa: E402
from app.database import (  # noqa: E402
    RECORDING_FORMAT_CHANGED, RECORDING_FORMAT_OVERRIDE, REC_STATUS_COMPLETED,
    SEGMENT_EXCLUDED_PLACEHOLDER, Recording, RecordingSegment, add_recording_event,
)
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402


def _seg(res, fps, excluded=None):
    return SimpleNamespace(probe_resolution=res, probe_fps=fps, excluded_reason=excluded)


class RecordingFormatsTests(unittest.TestCase):
    """The pure helper every surface reads, so the list and the detail page cannot
    disagree about whether a file is mixed."""

    def test_distinct_formats_in_the_order_they_first_appear(self):
        segs = [_seg('1920x1080', 59.94), _seg('1280x720', 29.97), _seg('1920x1080', 60)]
        self.assertEqual(recording_formats(segs), [('1920x1080', 60), ('1280x720', 30)])

    def test_fractional_rates_compare_equal_to_their_nominal(self):
        self.assertEqual(len(recording_formats([_seg('1920x1080', 59.94),
                                                _seg('1920x1080', 60.0)])), 1)

    def test_an_unprobed_segment_is_unknown_not_different(self):
        segs = [_seg('1920x1080', 60), _seg(None, None), _seg('1920x1080', None)]
        self.assertEqual(recording_formats(segs), [('1920x1080', 60)])

    def test_an_excluded_segment_is_not_part_of_the_file(self):
        """A discarded provider placeholder is 1080p30 and was never joined, so counting it
        would call a clean 1080p60 file mixed (dev/changelog/957)."""
        segs = [_seg('1920x1080', 60), _seg('1920x1080', 30, SEGMENT_EXCLUDED_PLACEHOLDER),
                _seg('1920x1080', 60)]
        self.assertEqual(recording_formats(segs), [('1920x1080', 60)])


class _Seeded(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow()

    def tearDown(self):
        self.t.cleanup()

    def _rec(self, formats, *, excluded_at=None):
        """A COMPLETED recording with one clean segment per (resolution, fps) in `formats`."""
        rec = seed.make_recording(start_time=self.now - timedelta(seconds=3600),
                                  stop_time=self.now, status=REC_STATUS_COMPLETED)
        for n, (res, fps) in enumerate(formats):
            db.session.add(RecordingSegment(
                recording_id=rec.id, segment_number=n, file_path=f'/nonexistent-{n}.ts',
                started_at=self.now - timedelta(seconds=3600 - n * 600),
                ended_at=self.now - timedelta(seconds=3000 - n * 600),
                bytes_recorded=4096, stall_count=0,
                probe_resolution=res, probe_fps=fps,
                excluded_reason=(SEGMENT_EXCLUDED_PLACEHOLDER if n == excluded_at else None)))
        db.session.commit()
        return rec

    def _row(self, rec_id):
        from app.routes.recordings import _index_row
        from app.tz_utils import get_display_tz
        db.session.expire_all()
        return _index_row(db.session.get(Recording, rec_id), datetime.utcnow(),
                          get_display_tz(), set())


class ListTooltipTests(_Seeded):

    def test_a_mixed_file_is_not_called_a_clean_capture(self):
        rec = self._rec([('1920x1080', 59.94), ('1280x720', 29.97)])
        health = self._row(rec.id)['health']
        self.assertNotIn('clean capture', health['tip'])
        self.assertIn('Format changed mid-recording: 1920x1080 @ 60, then 1280x720 @ 30',
                      health['tip'])
        self.assertIn('header describes only 1920x1080 @ 60', health['tip'])

    def test_the_pill_stays_green_because_the_file_plays(self):
        """A mixed concat and conversion exit 0 and play (dev/changelog/754) - the tooltip
        changes, the pill does not."""
        rec = self._rec([('1920x1080', 60), ('1280x720', 30)])
        self.assertEqual(self._row(rec.id)['health']['cls'], 'ok')

    def test_a_stalling_mixed_recording_keeps_the_clause(self):
        rec = self._rec([('1920x1080', 60), ('1280x720', 30)])
        db.session.get(Recording, rec.id).total_stall_count = 2
        db.session.commit()
        health = self._row(rec.id)['health']
        self.assertEqual(health['cls'], 'warn')
        self.assertIn('Format changed mid-recording', health['tip'])

    def test_a_uniform_file_still_reads_clean(self):
        rec = self._rec([('1920x1080', 60), ('1920x1080', 59.94)])
        row = self._row(rec.id)
        self.assertIn('clean capture', row['health']['tip'])
        self.assertNotIn('Format changed', row['health']['tip'])
        self.assertIsNone(row['format_change'])

    def test_a_discarded_placeholder_does_not_make_the_file_mixed(self):
        rec = self._rec([('1920x1080', 60), ('1920x1080', 30), ('1920x1080', 60)],
                        excluded_at=1)
        self.assertIsNone(self._row(rec.id)['format_change'])


class DetailPageTests(_Seeded):

    def _page(self, rec_id):
        resp = self.t.app.test_client().get(f'/recordings/{rec_id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_both_format_events_render_as_warnings(self):
        rec = self._rec([('1920x1080', 60)])
        add_recording_event(rec.id, RECORDING_FORMAT_OVERRIDE, detail='override-detail')
        add_recording_event(rec.id, RECORDING_FORMAT_CHANGED, detail='changed-detail')
        db.session.commit()
        html = self._page(rec.id)
        for ev_type in (RECORDING_FORMAT_OVERRIDE, RECORDING_FORMAT_CHANGED):
            li = html[:html.index(f'<span class="ev-type">{ev_type}</span>')]
            self.assertTrue(li[li.rindex('<li class="ev '):].startswith('<li class="ev warn"'),
                            ev_type)

    def test_the_video_row_flags_a_mixed_file(self):
        rec = self._rec([('1920x1080', 60), ('1280x720', 30)])
        html = self._page(rec.id)
        self.assertIn('720p30 · format changed', html)
        self.assertIn('Format changed mid-recording: 1920x1080 @ 60, then 1280x720 @ 30', html)
        row = html[html.index('<span class="sk">Video</span>'):]
        self.assertTrue(row.split('>', 2)[2].startswith('<span class="sv warn'))

    def test_the_video_row_is_plain_for_a_uniform_file(self):
        rec = self._rec([('1920x1080', 60), ('1920x1080', 60)])
        html = self._page(rec.id)
        self.assertNotIn('format changed', html)

    def test_the_video_row_never_describes_a_discarded_placeholder(self):
        """The row shows the latest segment's format; a trailing placeholder was never
        joined, so it must not be the one described (dev/changelog/957)."""
        rec = self._rec([('1920x1080', 60), ('1280x720', 30)], excluded_at=1)
        html = self._page(rec.id)
        row = html[html.index('<span class="sk">Video</span>'):][:300]
        self.assertIn('1080p60', row)
        self.assertNotIn('720p30', row)


if __name__ == '__main__':
    unittest.main()
