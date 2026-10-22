"""The provider serves its offline placeholder for the whole window and the channel never comes
back - prod recording 26, three hours of a race on a channel that answered every connection
with the 600 s black clip (dev/changelog/1263, 1264).

Three clips in a row trip the dead-stream retry, which raises the "not capturing" alert; the
retry a minute later gets three more clips, and the next retry would come after the window
ends, so the capture ends there and the join fails `ALL_SEGMENTS_PLACEHOLDER`. What this
guards is what the recording does and says on the way:

- the retry names the placeholder as its cause, not whatever line ffmpeg's stderr tail
  happened to start on (dev/docs/BUGS.md 2026-10-21 10:36 PM);
- no retry is scheduled past the window, and the capture ends at that trip rather than
  idling to the stop time (dev/changelog/1264);
- one RECORDING_RETRYING alert, cleared when the recording ends (dev/changelog/1264);
- no discarded segment calls ffmpeg's reconnects at the end of the clip a dropped stream
  (dev/docs/BUGS.md 2026-10-21 10:45 PM);
- the capture gap is the whole window rather than missing (dev/docs/BUGS.md 2026-10-21
  10:46 PM).
"""
import unittest

from app.alerts import RECORDING_RETRYING
from app.database import (CAPTURE_COMPLETE, DIAGNOSTICS, FAILURE_ALL_SEGMENTS_PLACEHOLDER,
                          REC_STATUS_FAILED, Alert, Recording, RECORDING_RETRY_SCHEDULED)
from app.watchdog import PLACEHOLDER_CAUSE

from .support import CaptureCase, at


class PlaceholderWholeWindowScenario(CaptureCase):
    def test_a_window_of_nothing_but_placeholder_fails_and_says_why(self):
        ch = self.channel()
        self.sim.inject('placeholder', self.label(ch.id), clip_seconds=60.0,
                        start=at(-5), end=at(900))
        rid = self.record(ch, seconds=100)
        rec = self.finish(rid, 180)

        self.assertEqual(rec.status, REC_STATUS_FAILED, self.event_types(rid))
        self.assertEqual(rec.failure_reason, FAILURE_ALL_SEGMENTS_PLACEHOLDER)
        segs = self.segments(rid)
        self.assertGreaterEqual(len(segs), 6, 'three clips, the retry, three more')
        self.assertTrue(all(s.excluded for s in segs), [s.excluded_reason for s in segs])

        retries = self.events(rid, RECORDING_RETRY_SCHEDULED)
        self.assertEqual(len(retries), 1, 'the second retry would come after the window')
        self.assertIn(PLACEHOLDER_CAUSE, retries[0].detail)
        self.assertEqual(rec.dead_stream_retry_count, 1)
        ended = self.assertEvent(rid, CAPTURE_COMPLETE)
        self.assertIn('before another retry could come', ended.detail)
        self.assertIn(PLACEHOLDER_CAUSE, ended.detail)
        self.assertLess(rec.completed_at, rec.stop_time, 'ended at the trip, not the stop')

        alerts = Alert.query.filter_by(recording_id=rid, alert_type=RECORDING_RETRYING).all()
        self.assertEqual(len(alerts), 1)
        self.assertIsNotNone(alerts[0].dismissed_at, 'nothing is retried once it ends')

        for ev in self.events(rid, DIAGNOSTICS):
            self.assertNotIn('stream dropped', ev.detail or '')

        rec = self.fresh(Recording, rid)
        self.assertAlmostEqual(rec.capture_gap_seconds, rec.duration_seconds, delta=0.5)

        answer = self.answer(rid)
        self.assertNoProblems(answer)
        self.assertAgrees(answer, self.claims(rid))
        self.assertAlmostEqual(answer.placeholder_seconds, 60.0 * len(segs), delta=1.0)


if __name__ == '__main__':
    unittest.main()
