"""The recording page's open segment reports what has been captured so far.

Guards dev/docs/BUGS.md 2026-09-19 @ 10:25:20 AM: RecordingSegment.bytes_recorded is written
only when a segment ends, so while one was capturing its Segments row read 0 B with no Avg
rate and the page's Size stat left it out, while the banner above - which stats the open
file - kept growing. And the Content cell's tooltip blamed old data for a length that is
simply not measured until the segments are joined.

No ffmpeg, no network, no /dvr: the open segment's file is a local file under the test's
temp dir, addressed through the row's own file_path.
"""
import os
import re
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, recorder  # noqa: E402
from app.routes.recordings import filesize_filter  # noqa: E402

LIVE_BYTES = 37_500_000        # 300 Mb over 60 s -> 5.0 Mb/s
DONE_BYTES = 12_000_000


class LiveSegmentRowTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        now = datetime.utcnow()
        acct = seed.make_account()
        ch = seed.make_channel(acct)
        self.rec = seed.make_recording(status='IN_PROGRESS', channel_id=ch.id,
                                       started_at=now - timedelta(minutes=5))
        done = seed.make_segment(self.rec, ch, now - timedelta(minutes=5),
                                 now - timedelta(seconds=60), segment_number=1)
        done.bytes_recorded = DONE_BYTES
        self.live_started = now - timedelta(seconds=60)
        live = seed.make_segment(self.rec, ch, self.live_started, None, segment_number=2)
        live.bytes_recorded = None
        live.file_path = os.path.join(self.t._tmpdir, 'live_seg_002.ts')
        with open(live.file_path, 'wb') as f:
            f.truncate(LIVE_BYTES)
        db.session.commit()
        state = recorder.RecordingState()
        state.current_segment_num = 2
        with recorder._lock:
            recorder._active[self.rec.id] = state

    def tearDown(self):
        with recorder._lock:
            recorder._active.pop(self.rec.id, None)
        self.ctx.pop()
        self.t.cleanup()

    def _page(self):
        resp = self.t.app.test_client().get(f'/recordings/{self.rec.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _live_row(self, html):
        panel = html.split('id="panel-segments-slot"', 1)[1]
        rows = re.findall(r'<tr>(.*?)</tr>', panel.split('<tbody>', 1)[1], re.S)
        live = [r for r in rows if 'exit-pill live' in r]
        self.assertEqual(len(live), 1)
        return live[0]

    def test_open_segment_row_shows_live_size(self):
        row = self._live_row(self._page())
        self.assertIn(f'<td class="num" data-label="Size">{filesize_filter(LIVE_BYTES)}</td>', row)
        self.assertNotIn('<td class="num" data-label="Size">0 B</td>', row)

    def test_open_segment_row_shows_avg_rate(self):
        row = self._live_row(self._page())
        oldest = (datetime.utcnow() - self.live_started).total_seconds()
        m = re.search(r'([\d.]+) Mb/s', row)
        self.assertIsNotNone(m, 'live row has no Avg rate')
        # The page divides the live size by the segment's age at render time, which is at
        # least the seeded 60 s and at most its age now. A loaded box can put seconds between
        # setUp and the render, so the bound comes from the clock, not a fixed tolerance
        # (dev/changelog/1216).
        fastest = round(LIVE_BYTES * 8 / 60 / 1e6, 1)
        slowest = round(LIVE_BYTES * 8 / oldest / 1e6, 1)
        self.assertTrue(slowest <= float(m.group(1)) <= fastest,
                        '%s Mb/s is outside %s-%s' % (m.group(1), slowest, fastest))

    def test_the_pages_size_includes_open_segment(self):
        """While capturing, the page's one size is the strip's (dev/changelog/1285), and its
        first render counts the open segment the way the SSE frames that follow it do."""
        html = self._page()
        self.assertIn(f'id="live-bytes">{filesize_filter(DONE_BYTES + LIVE_BYTES)}<', html)
        stat = html.split('id="hero-right"', 1)[1].split('id="panel-segments-slot"', 1)[0]
        self.assertNotIn('<span class="sk">Size</span>', stat)

    def test_content_tip_does_not_blame_old_data_while_recording(self):
        row = self._live_row(self._page())
        self.assertNotIn('Segments captured before ChannelBin', row)
        self.assertIn('measured when the segments are joined', row)


if __name__ == '__main__':
    unittest.main()
