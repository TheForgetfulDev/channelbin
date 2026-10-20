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

from .support import BURST, CaptureCase, at


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
        # that gap less the retry's connect burst, which reaches back into it. Except that
        # covered_capture_seconds counts a refused segment's few seconds as coverage, though
        # it captured nothing, so the gap reads short by those spans - the answer key's
        # finding, tracked in the backlog. The term goes when that is fixed.
        empty = sum((s.ended_at - s.started_at).total_seconds() for s in segs
                    if not s.bytes_recorded and s.ended_at)
        self.assertAlmostEqual(answer.lost_seconds, gap - BURST + empty, delta=1.0,
                               msg=f'{answer.describe()}\ngap {gap:.2f}, empty spans '
                                   f'{empty:.2f}')


if __name__ == '__main__':
    unittest.main()
