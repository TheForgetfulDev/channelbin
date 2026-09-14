"""Tier 2 - what a recording reports about the time it did not capture, and about content
that arrived faster than real time.

Guards dev/docs/BUGS.md 2026-09-12 @ 08:29:47 PM ET. The defect was arithmetic, and it was
structural rather than off-by-some: "Content missing" was max(0, window - content), one
subtraction clamped at zero. A provider that buffers serves the last few seconds again when
a dropped connection is re-established, so a recording that stalls repeatedly comes back
with MORE content than its window held - and the surplus was silently deducted from the
real gap time before anything was displayed. Recording 14 (2026-09-05, 28 segments, 27
stalls) reported "missing 0s (0%)" on a capture with 135.7s of measured gaps.

Two quantities ship in its place and are never netted against each other:
`capture_gap_seconds`, wall clock inside the window when no capture process was running at
all, and `content_vs_capture_seconds`, the signed comparison of the finished file against
the time the capture actually ran. Neither is a claim about WHICH content was lost or
duplicated - that needs frame-level matching, which nothing here does.

`RecordingSegment.content_duration_seconds` is the per-segment half, ffprobed at concat
time because the watchdog only ever sees a file that is still growing.

The model arithmetic itself lives in tests/test_downtime_accounting.py alongside downtime,
which is the quantity it is most often confused with. This module covers the concat-time
measurement, the event text and the list-page tooltip.

Fixtures are synthesized locally with ffmpeg - no network, no provider streams, no /dvr.
"""
import os
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.concatenator import _measure_segment_content_durations  # noqa: E402
from app.database import Recording, RecordingSegment  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=120)


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow()

    def tearDown(self):
        self.t.cleanup()

    def _rec(self, window_seconds=3600, **kw):
        rec = seed.make_recording(start_time=self.now - timedelta(seconds=window_seconds),
                                  stop_time=self.now, **kw)
        db.session.commit()
        return rec

    def _seg(self, rec, number, start_ago, end_ago, file_path='/nonexistent.ts'):
        seg = RecordingSegment(
            recording_id=rec.id, segment_number=number, file_path=file_path,
            started_at=self.now - timedelta(seconds=start_ago),
            ended_at=self.now - timedelta(seconds=end_ago), bytes_recorded=4096)
        db.session.add(seg)
        db.session.commit()
        return seg


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class SegmentContentDurationTests(_Base):
    """The concat-time probe that fills RecordingSegment.content_duration_seconds."""

    def _make_ts(self, name, seconds):
        path = os.path.join(self.t._tmpdir, name)
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=10', '-t', str(seconds),
                '-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0',
                '-pix_fmt', 'yuv420p', path)
        return path

    def test_each_segment_gets_its_own_measured_content_length(self):
        """The per-segment answer only exists before concat: afterwards the joined file
        reports one duration and which segment contributed what is gone."""
        rec = self._rec()
        a = self._seg(rec, 0, 3600, 3595, self._make_ts('a.ts', 4))
        b = self._seg(rec, 1, 3590, 3580, self._make_ts('b.ts', 7))

        _measure_segment_content_durations(rec.id, [a, b])
        db.session.expire_all()

        self.assertAlmostEqual(
            db.session.get(RecordingSegment, a.id).content_duration_seconds, 4.0, delta=0.5)
        self.assertAlmostEqual(
            db.session.get(RecordingSegment, b.id).content_duration_seconds, 7.0, delta=0.5)

    def test_a_segment_that_cannot_be_probed_keeps_a_null_not_a_zero(self):
        """NULL means "not measured" and must stay distinguishable from a measured zero -
        a zero here would read as a segment that captured no content at all, which is a
        different and much worse finding than one whose file has gone missing."""
        rec = self._rec()
        good = self._seg(rec, 0, 3600, 3595, self._make_ts('good.ts', 4))
        gone = self._seg(rec, 1, 3590, 3580, os.path.join(self.t._tmpdir, 'absent.ts'))

        _measure_segment_content_durations(rec.id, [good, gone])
        db.session.expire_all()

        self.assertIsNotNone(
            db.session.get(RecordingSegment, good.id).content_duration_seconds)
        self.assertIsNone(
            db.session.get(RecordingSegment, gone.id).content_duration_seconds)

    def test_measuring_nothing_never_raises(self):
        """A diagnostic that can break the concat it runs inside is worse than no
        diagnostic (CLAUDE.md Product Principle 2) - this runs between the capture and the
        file the user is waiting for."""
        rec = self._rec()
        gone = self._seg(rec, 0, 3600, 3590, os.path.join(self.t._tmpdir, 'absent.ts'))

        _measure_segment_content_durations(rec.id, [gone])  # must not raise

        db.session.expire_all()
        self.assertIsNone(
            db.session.get(RecordingSegment, gone.id).content_duration_seconds)


class ListPageGapReportingTests(_Base):
    """The recordings list health pill - the surface where the old net figure was silent."""

    def _tip(self, rec_id):
        from app.routes.recordings import _index_row
        from app.tz_utils import get_display_tz
        db.session.expire_all()
        row = _index_row(db.session.get(Recording, rec_id), datetime.utcnow(),
                         get_display_tz(), set())
        return (row.get('health') or {}).get('tip') or ''

    def test_the_pill_reports_gap_time_a_surplus_used_to_cancel(self):
        """Recording 14's shape: a file longer than its window because the feed replayed
        its buffer at every join, over gaps that really happened. The old tooltip was gated
        on max(0, window - content) >= 2% of the window, which was zero here, so the row
        said nothing at all about the ten minutes it did not capture."""
        rec = self._rec(status='COMPLETED', recorded_duration_seconds=3900.0)
        self._seg(rec, 0, 3600, 2100)
        self._seg(rec, 1, 1500, 0)
        for _ in range(3):
            db.session.add(RecordingSegment(
                recording_id=rec.id, segment_number=9, file_path='/x.ts',
                started_at=self.now - timedelta(seconds=1500),
                ended_at=self.now, bytes_recorded=1, stall_count=1))
        db.session.commit()

        tip = self._tip(rec.id)

        self.assertIn('nothing capturing at all', tip)
        self.assertIn('more content than the time the capture ran', tip)

    def test_a_clean_capture_says_neither(self):
        """Every clean capture is a few seconds off its window (ffmpeg start latency, and a
        connect-time buffer on the other side). Saying so on every row is noise, which is
        why both halves are gated at 2% of the window - the detail page carries the exact
        figures unconditionally."""
        rec = self._rec(status='COMPLETED', recorded_duration_seconds=3610.0)
        self._seg(rec, 0, 3598, 0)
        db.session.commit()

        tip = self._tip(rec.id)

        self.assertNotIn('nothing capturing at all', tip)
        self.assertNotIn('content than the time the capture ran', tip)


class DetailPageRenderTests(_Base):
    """The detail page's two new stat rows and the segments table's Content column.

    A render test rather than an eyeball because the Jinja hazards defect class is silent:
    a Python builtin in an expression, arithmetic on a nullable column with no `or 0`
    guard, a `{% set %}` that does not cross a block boundary - each raises at render time
    on a page that looks fine in review.
    """

    def _html(self, rec_id):
        db.session.expire_all()
        with self.t.app.test_client() as client:
            resp = client.get(f'/recordings/{rec_id}')
            self.assertEqual(resp.status_code, 200)
            return resp.get_data(as_text=True)

    def test_the_page_shows_both_figures_and_the_per_segment_content(self):
        rec = self._rec(status='COMPLETED', recorded_duration_seconds=3900.0)
        a = self._seg(rec, 0, 3600, 2100)
        b = self._seg(rec, 1, 1500, 0)
        a.content_duration_seconds = 1800.0
        b.content_duration_seconds = 2100.0
        db.session.commit()

        html = self._html(rec.id)

        self.assertIn('Capture gaps', html)
        self.assertIn('Content vs capture time', html)
        self.assertNotIn('Content missing', html)

    def test_a_recording_with_no_measured_segment_content_still_renders(self):
        """Every segment captured before the column existed carries NULL, and the arithmetic
        beside it (content minus wall clock) must not run on that None."""
        rec = self._rec(status='COMPLETED', recorded_duration_seconds=3550.0)
        self._seg(rec, 0, 3600, 0)
        db.session.commit()

        html = self._html(rec.id)

        self.assertIn('Capture gaps', html)

    def test_a_scheduled_recording_renders_without_either_figure(self):
        """Both read None until the recording is over, and a stat row is skipped rather
        than rendering a zero that would claim nothing was missed."""
        rec = self._rec(status='SCHEDULED')

        html = self._html(rec.id)

        self.assertNotIn('Capture gaps', html)


if __name__ == '__main__':
    unittest.main()
