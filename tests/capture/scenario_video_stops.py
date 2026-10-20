"""The feed's video stops while its audio flows: the file keeps growing a 256 KiB block at a
time, so the growth check never fires, and the watchdog's frame count is what notices. The
capture is restarted on a fresh connection, the picture comes back, and the recording says so
twice - CAPTURE_VIDEO_STOPPED and CAPTURE_VIDEO_RESUMED (dev/changelog/1209, 1210, 1223).

The stall timeout is the production default, 30 s, not the tier's 8: the SD loop's audio
fills a 256 KiB block about every 13 s once the video is gone, the cadence dev/changelog/1209
measured, and the shape only exists where blocks land inside the stall timeout. At 20 s
the growth check fired first on two runs of four - the first block after the stop can come
later than the rest. The video comes back when ChannelBin reacts: the test clears the fault
on the CAPTURE_VIDEO_STOPPED event, since a fresh connection is what brought the picture back
on recording 76.

A characterization of today's pipeline, not a guard for one past defect.
"""
import unittest

from app.database import CAPTURE_VIDEO_RESUMED, CAPTURE_VIDEO_STOPPED, REC_STATUS_COMPLETED
from sim import answer_key as ak

from .support import BURST, CaptureCase, at


class VideoStopsScenario(CaptureCase):
    config = {'watchdog': {'stall_timeout_seconds': 30}}

    def test_video_that_stops_while_audio_flows_is_noticed_and_restarted(self):
        ch = self.channel()
        rid = self.record(ch, seconds=70)
        outage = self.sim.inject('video_stops', self.label(ch.id), start=at(10), end=at(600))
        self.wait_event(rid, CAPTURE_VIDEO_STOPPED, 70)
        self.sim.clear(outage)
        rec = self.finish(rid, 160)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        self.assertEvent(rid, CAPTURE_VIDEO_RESUMED)
        self.assertGreaterEqual(len(self.segments(rid)), 2)

        answer = self.answer(rid)
        self.assertNoProblems(answer)
        self.assertAgrees(answer, self.claims(rid))
        withheld = self.lost(answer, ak.REASON_WITHHELD)
        self.assertTrue(withheld, answer.describe())
        withheld_seconds = sum(x.seconds for x in withheld)
        # The frame count must stand still longer than the stall timeout before the stop is
        # called, so that much video is gone, less what the restarted connection's burst
        # reaches back over.
        self.assertGreater(withheld_seconds, 30.0 - BURST, answer.describe())
        # The joined file shows the outage as a hole in its video timeline - audio ran on
        # through it - and ChannelBin's timeline scan measures that hole. It reads longer
        # than the content lost, never shorter: the restarted connection's burst replays
        # audio the first segment already held, and the file's timeline counts it again.
        self.assertGreaterEqual(rec.timeline_gap_seconds, withheld_seconds - 1.0,
                                answer.describe())


if __name__ == '__main__':
    unittest.main()
