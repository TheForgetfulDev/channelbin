"""A feed that slows to a trickle and stays connected: the watchdog's growth check calls it a
stall, kills the capture and starts a new segment, and the recording completes from what both
segments hold (dev/changelog/1223).

The trickle is `throttle` at 2% of real time, the shape dev/changelog/429 recorded on recording
71 (delivery falling to 24 kB/s, 75 stalls in 91 minutes). At that rate ffmpeg's 256 KiB output
blocks stop landing, so the file stops growing while the socket keeps delivering - which no
read timeout can see, and which only the watchdog can end.

The provider recovers the moment ChannelBin notices: the test clears the fault when the
STALL_DETECTED event lands, so the restarted segment meets a healthy feed. Left to a fixed
window, whether the restart landed before or after the feed recovered decided which of two
watchdog paths ran, and the scenario would have measured the race.

A characterization of today's pipeline end to end, not a guard for one past defect.
"""
import unittest

from app.database import REC_STATUS_COMPLETED, RESTART_SUCCEEDED, STALL_DETECTED
from sim import answer_key as ak

from .support import BURST, CaptureCase, at


class StallAndRestartScenario(CaptureCase):
    def test_a_stalled_feed_is_restarted_on_a_new_segment_and_the_answer_key_agrees(self):
        ch = self.channel()
        rid = self.record(ch, seconds=40)
        trickle = self.sim.inject('throttle', self.label(ch.id), rate=0.02, start=at(10),
                                  end=at(600))
        self.wait_event(rid, STALL_DETECTED, 60)
        self.sim.clear(trickle)
        rec = self.finish(rid, 150)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        self.assertIn('stalled', self.assertEvent(rid, STALL_DETECTED).detail)
        self.assertEvent(rid, RESTART_SUCCEEDED)
        segs = self.segments(rid)
        self.assertGreaterEqual(len(segs), 2)

        answer = self.answer(rid)
        self.assertNoProblems(answer)
        self.assertAgrees(answer, self.claims(rid))
        unsent = self.lost(answer, ak.REASON_NOT_SENT)
        self.assertEqual(len(unsent), 1, answer.describe())
        self.assertEqual({x.reason for x in answer.lost},
                         {ak.REASON_NOT_SENT, ak.REASON_NOT_RECORDED}, answer.describe())
        # What ChannelBin counts as downtime runs from the last growth to the new segment's
        # first, on the wall clock. The content lost is that, less what the new connection's
        # burst replayed - so the downtime may run over the loss, never short of it by more
        # than one burst.
        self.assertGreaterEqual(rec.total_downtime_seconds, unsent[0].seconds - BURST,
                                answer.describe())

        # The join drops nothing the segments held.
        joined = self.answer_joined(rid)
        self.assertNoProblems(joined)
        self.assertAlmostEqual(joined.distinct_seconds, answer.distinct_seconds, delta=0.3)


if __name__ == '__main__':
    unittest.main()
