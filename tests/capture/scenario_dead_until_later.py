"""A channel that refuses every connection until a set time, the way an event channel is not on
until close to its event, and a recording whose window opens before it: the recording waits and
tries again for as long as its window is open, and records the channel once it comes on
(dev/docs/BUGS.md 2026-11-28, dev/changelog/1793).

Prod recording 88 died this way: three failures, a wait, three more, and the consecutive-failure
limit failed it on the tenth with two hours of its window left, because the count carried over
every wait. Scaled down here to a limit of 4: the old count reached it on the first try after the
first wait, while the channel was still refusing, and the recording failed with nothing in it.
Now each wait starts the count again, the second wait outlasts the refusal, and the rest of the
window is recorded.

Supersedes scenario_dead_stream_gives_up (dev/changelog/1404): a dead stream with its window open
no longer gives up at all.
"""
import unittest

from app.database import (RECORDING_FAILED, RECORDING_FAILED_DEAD_STREAM,
                          RECORDING_RETRY_SCHEDULED, REC_STATUS_COMPLETED)
from sim import answer_key as ak

from .support import CaptureCase, at

# When the channel comes on, from the recording's start: after the first wait's retry (about
# 70 s in, three refusals and a one-minute wait) and before the second's (two minutes later).
COMES_ON = 120
WINDOW = 250


class DeadUntilLaterScenario(CaptureCase):
    config = {'watchdog': {'max_consecutive_failures': 4}}

    def test_a_channel_that_comes_on_later_is_recorded_once_it_does(self):
        ch = self.channel()
        label = self.label(ch.id)
        self.sim.inject('refuse_at_open', label, start=at(-1), end=at(COMES_ON))
        rid = self.record(ch, seconds=WINDOW)
        rec = self.finish(rid, WINDOW + 120)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        self.assertIsNone(rec.gave_up_reason)
        self.assertEqual(self.events(rid, RECORDING_FAILED), [])
        self.assertEqual(self.events(rid, RECORDING_FAILED_DEAD_STREAM), [])
        self.assertGreaterEqual(len(self.events(rid, RECORDING_RETRY_SCHEDULED)), 2,
                                self.event_types(rid))

        answer = self.answer(rid)
        self.assertNoProblems(answer)
        self.assertAgrees(answer, self.claims(rid))
        self.assertEqual(answer.channels, (label,))
        # What was lost before the channel came on is put down to its refusals, and once the
        # second wait ended the rest of the window was recorded.
        self.assertTrue(self.lost(answer, ak.REASON_REFUSED), answer.describe())
        self.assertGreater(answer.held_seconds, 20, answer.describe())


if __name__ == '__main__':
    unittest.main()
