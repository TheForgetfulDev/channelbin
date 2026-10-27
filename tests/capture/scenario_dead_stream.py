"""A channel that refuses every connection at open, on every attempt, and comes back minutes
later: three early failures call it dead, the recording waits in RETRYING for the scheduler's
retry, and the retry records the rest of the window (dev/changelog/105, 593, 1223).

The refusal is 405 Method Not Allowed, the answer dev/changelog/436 measured, and the channel
comes back once ChannelBin has given up on it for now - the test clears the fault on
RECORDING_RETRY_SCHEDULED, the shape of dev recording 18, whose member served again about
90 s after it stopped. The first retry waits the hard-coded one minute, so this scenario runs
about two and a half.

A characterization of today's pipeline, not a guard for one past defect.
"""
import unittest

from app.database import (REC_STATUS_COMPLETED, Recording, RECORDING_RESUMED,
                          RECORDING_RETRY_SCHEDULED)
from sim import answer_key as ak

from .support import BURST, KEYFRAME_SECONDS, CaptureCase, at


class DeadStreamScenario(CaptureCase):
    def test_a_refused_channel_is_retried_and_the_retry_records_the_rest(self):
        ch = self.channel()
        refusal = self.sim.inject('refuse_at_open', self.label(ch.id), start=at(-5),
                                  end=at(900))
        rid = self.record(ch, seconds=110)
        self.wait_event(rid, RECORDING_RETRY_SCHEDULED, 90)
        self.sim.clear(refusal)
        rec = self.finish(rid, 220)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        self.assertEvent(rid, RECORDING_RESUMED)
        segs = self.segments(rid)
        self.assertGreaterEqual(len(segs), 4, 'three refused segments, then the retry')

        gap = self.fresh(Recording, rid).capture_gap_seconds
        answer = self.answer(rid, window=self.window(rid))
        self.assertNoProblems(answer)
        self.assertAgrees(answer, self.claims(rid))
        self.assertTrue(self.lost(answer, ak.REASON_REFUSED), answer.describe())
        # Everything before the retry is lost, and ChannelBin's capture gap - wall clock in
        # the window with no segment capturing - is the measure of it: the content lost is
        # that gap less the retry's connect burst, which reaches back into it. A refused
        # segment's seconds are in the gap, not in the coverage: it captured nothing
        # (dev/changelog/1247). The burst starts at the keyframe at or before BURST seconds
        # back (DESIGN-simulator.md §3.10), so it can reach up to KEYFRAME_SECONDS further:
        # a tolerance of 1 s around BURST alone failed one run in six on a 2 s keyframe
        # interval (dev/docs/BUGS.md 2026-10-26 11:56 PM).
        msg = f'{answer.describe()}\ngap {gap:.2f}'
        self.assertLessEqual(answer.lost_seconds, gap - BURST + 1.0, msg)
        self.assertGreaterEqual(answer.lost_seconds, gap - BURST - KEYFRAME_SECONDS - 1.0, msg)
        self.assertGapsInsideDowntime(rid)

    def test_a_silent_channel_is_retried_and_its_gaps_sit_inside_its_downtime(self):
        """The README's demo recording shape (dev/changelog/1295, 1303): the connection is
        accepted and nothing arrives, so each attempt sits for ffmpeg's read timeout and
        dies with no bytes. Three of those call the stream dead, the retry a minute later
        records the rest. Every one of those empty segments is a capture gap, and Downtime
        has to hold all of it - the growth clock alone charged each one a poll interval
        short, and the demo's eleven put Capture gaps a minute over Downtime."""
        ch = self.channel()
        silence = self.sim.inject('no_answer', self.label(ch.id), start=at(-5), end=at(900))
        rid = self.record(ch, seconds=110)
        self.wait_event(rid, RECORDING_RETRY_SCHEDULED, 90)
        self.sim.clear(silence)
        rec = self.finish(rid, 220)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        self.assertEvent(rid, RECORDING_RESUMED)
        segs = self.segments(rid)
        self.assertGreaterEqual(len(segs), 4, 'three silent segments, then the retry')
        self.assertEqual([s.bytes_recorded or 0 for s in segs[:3]], [0, 0, 0],
                         'the silent attempts should have written nothing')
        self.assertGapsInsideDowntime(rid)


if __name__ == '__main__':
    unittest.main()
