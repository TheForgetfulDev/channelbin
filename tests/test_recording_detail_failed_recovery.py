"""What a FAILED recording's detail page tells you, and what it offers you to do.

Guards dev/docs/BUGS.md 2026-08-24. Three defects on one page, all visible on the same
recording - a 5-hour capture that concatenated fine and then lost its post-processing to
a host crash:

  * the status strip's branch chain ended in a trailing `else` that stood for a real
    state, so it announced "the stream could not be reached before producing any
    segments" about a capture that had produced 17.9 GB, and told the user in the same
    breath that "segments kept on disk" - false, a successful concat consumes them
  * the only visible action was "Record again", which re-records what is already on
    disk; the action that actually recovers it was in the kebab
  * the Run Timeline placed events at their raw timestamps, so two edits made two days
    before the recording stretched the axis to 62 hours and squeezed the run into 8% of
    the bar

No ffmpeg, no network, no /dvr - a seeded row rendered through the test client, with the
"file on disk" being a real file under the test's temp dir.
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
from app.database import RecordingEvent, RecordingSegment  # noqa: E402


class FailedRecordingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _failed_with_ts_on_disk(self):
        """The incident's shape: capture done, concat done, post-processing never
        finished, so the recoverable .ts is on disk and the segments are gone."""
        out = os.path.join(self.t._tmpdir, 'nascar.ts')
        with open(out, 'wb') as fh:
            fh.write(b'\0' * 8192)
        start = datetime(2026, 8, 23, 18, 0, 0)
        rec = seed.make_recording(
            status='FAILED', name='NASCAR Cup Series',
            start_time=start, stop_time=start + timedelta(hours=5),
            output_path=out, final_file_size=8192)
        rec.started_at = start
        rec.completed_at = start + timedelta(hours=15)
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=1,
            file_path=os.path.join(self.t._tmpdir, 'consumed_seg_001.ts'),
            started_at=start, ended_at=start + timedelta(hours=5),
            exit_reason='STOP_TIME_REACHED', bytes_recorded=8192))
        db.session.commit()
        return rec, out

    def _page(self, rec):
        resp = self.t.client.get(f'/recordings/{rec.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    # ── the status strip tells the truth ─────────────────────────────────────
    def test_does_not_claim_the_stream_was_unreachable(self):
        rec, _ = self._failed_with_ts_on_disk()
        html = self._page(rec)
        self.assertNotIn('could not be reached before producing any segments', html,
                         'a capture that produced a concatenated file was never unreachable')

    def test_does_not_claim_segments_are_kept_on_disk(self):
        rec, _ = self._failed_with_ts_on_disk()
        html = self._page(rec)
        self.assertNotIn('segments kept on disk', html,
                         'a successful concat consumes its segments - saying otherwise sends '
                         'the user looking for files that are gone')

    def test_names_the_state_it_is_actually_in(self):
        rec, _ = self._failed_with_ts_on_disk()
        html = self._page(rec)
        self.assertIn('post-processing did not complete', html)

    def test_a_capture_that_produced_nothing_still_says_so(self):
        """The trailing else keeps its one real meaning."""
        rec = seed.make_recording(status='FAILED', name='dead feed')
        db.session.commit()
        html = self._page(rec)
        self.assertIn('could not be reached before producing any segments', html)

    # ── the recovery action is visible ───────────────────────────────────────
    def test_retry_conversion_is_a_visible_button_not_a_kebab_item(self):
        rec, _ = self._failed_with_ts_on_disk()
        html = self._page(rec)
        self.assertIn('class="btn btn-primary" data-act="retry-convert"', html,
                      'the action that recovers the recording must be the primary button')

    def test_record_again_stays_available(self):
        rec, _ = self._failed_with_ts_on_disk()
        html = self._page(rec)
        self.assertIn('Record again', html,
                      'promoting recovery must not remove the re-record path')

    def test_retry_concatenation_is_promoted_when_segments_are_what_survived(self):
        """The other recoverable shape: concat never ran, segment files still there."""
        seg = os.path.join(self.t._tmpdir, 'live_seg_001.ts')
        with open(seg, 'wb') as fh:
            fh.write(b'\0' * 4096)
        rec = seed.make_recording(status='FAILED', name='concat never ran')
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=1, file_path=seg,
            started_at=rec.start_time, ended_at=rec.stop_time,
            exit_reason='STOP_TIME_REACHED', bytes_recorded=4096))
        db.session.commit()
        html = self._page(rec)
        self.assertIn('class="btn btn-primary" data-act="retry-concat"', html)

    def test_no_recovery_button_when_nothing_survived(self):
        rec = seed.make_recording(status='FAILED', name='nothing captured')
        db.session.commit()
        html = self._page(rec)
        self.assertNotIn('btn btn-primary" data-act="retry-conv', html)
        self.assertNotIn('btn btn-primary" data-act="retry-concat', html)

    # ── the Run Timeline is a timeline of the run ────────────────────────────
    def test_edits_made_before_the_run_do_not_stretch_the_axis(self):
        rec, _ = self._failed_with_ts_on_disk()
        # Two edits made two days ahead of the recording, exactly as the incident had.
        for days, detail in ((2, 'Start/stop time edited: first'),
                             (2, 'Start/stop time edited: second')):
            db.session.add(RecordingEvent(
                recording_id=rec.id, event_type='RECORDING_EDITED',
                timestamp=rec.started_at - timedelta(days=days), detail=detail))
        db.session.add(RecordingEvent(
            recording_id=rec.id, event_type='SEGMENT_STARTED',
            timestamp=rec.started_at, detail='Segment 1 started'))
        db.session.commit()

        html = self._page(rec)

        # The symptom as the user described it: the capture occupied a sliver of its own
        # timeline. The green capture span is the run; with a 5-hour capture and nothing
        # else in the window it should own essentially the whole bar, not 8% of it.
        widths = [float(w) for w in re.findall(
            r'class="run-tl-span rec[^"]*"[^>]*width:([\d.]+)%', html)]
        self.assertTrue(widths, f'no capture span rendered on the timeline: {html[:400]}')
        self.assertGreater(max(widths), 80.0,
                           f'the capture must own its own timeline, got {max(widths)}% - an '
                           f'event from before the run is still stretching the axis')

        # Milestone dots carry their label in data-tip; an edit from before the run has
        # no place on the bar at all.
        tips = ' '.join(re.findall(r'data-tip="([^"]*)"', html))
        self.assertNotIn('Edited', tips)

        self.assertIn('Start/stop time edited: first', html,
                      'the edit must still be in the activity log - only the bar drops it')

    def test_a_post_capture_chain_reentry_does_not_stretch_the_axis(self):
        """Capture ends once. The post-capture chain reuses CAPTURE_COMPLETE to mark its
        own re-entries, and one of those - a restart resuming post-processing 10.5 hours
        later - is neither "stop time reached" nor part of the run."""
        rec, _ = self._failed_with_ts_on_disk()
        capture_end = rec.started_at + timedelta(hours=5)
        db.session.add(RecordingEvent(
            recording_id=rec.id, event_type='CAPTURE_COMPLETE', timestamp=capture_end,
            detail='Stop time reached; beginning concatenation'))
        db.session.add(RecordingEvent(
            recording_id=rec.id, event_type='CAPTURE_COMPLETE',
            timestamp=capture_end + timedelta(hours=10, minutes=30),
            detail='Concatenation was already complete - resuming post-processing'))
        db.session.commit()

        html = self._page(rec)
        widths = [float(w) for w in re.findall(
            r'class="run-tl-span rec[^"]*"[^>]*width:([\d.]+)%', html)]
        self.assertTrue(widths)
        self.assertGreater(max(widths), 80.0,
                           f'the capture must own its own timeline, got {max(widths)}% - a '
                           f'post-capture re-entry is still stretching the axis')

    def test_an_edit_made_during_the_run_stays_on_the_timeline(self):
        """The filter is "before the run", not "edits are uninteresting"."""
        rec, _ = self._failed_with_ts_on_disk()
        db.session.add(RecordingEvent(
            recording_id=rec.id, event_type='RECORDING_STOP_TIME_ADJUSTED',
            timestamp=rec.started_at + timedelta(hours=4), detail='Stop time adjusted'))
        db.session.commit()
        html = self._page(rec)
        self.assertIn('Stop time adjusted', html)


if __name__ == '__main__':
    unittest.main()
