"""A group member on one account dies mid-recording - the connection closes and every reopen
is refused - and the recording fails over to the member on the other account, which records
the rest (dev/changelog/754, 1223).

Two simulated accounts on two backends, one seat each, so the failover crosses accounts the
way it has to when the first account's only seat is the one that died.

A characterization of today's pipeline, not a guard for one past defect.
"""
import unittest

from app.database import GROUP_FAILOVER, REC_STATUS_COMPLETED
from sim import answer_key as ak

from .support import BURST, KEYFRAME_SECONDS, CaptureCase, at


class GroupFailoverScenario(CaptureCase):
    def test_a_dead_member_fails_over_to_the_member_on_the_other_account(self):
        first, second = self.channel('cove-tv'), self.channel('reed-tv')
        first_label, second_label = self.label(first.id), self.label(second.id)
        group = self.group([first, second])
        # The first member's first connection closes 3 MB in (about 13 s), and every open
        # after the one already playing is refused, so it dies on its first reconnect. The
        # refusals start once that first connection is serving: a window on the clock alone
        # sometimes began after the close, and the member reconnected in-process once more
        # before it died. A connection fault reaches only connections opened after it, which
        # is why the close is set before the recording starts.
        self.sim.inject('close_mid_stream', first_label, after_bytes=3_000_000, times=1)
        rid = self.record(group=group, seconds=40)
        self.wait_capturing(rid, 30, min_bytes=300_000)
        self.sim.inject('refuse_at_open', first_label, start=at(-1), end=at(600))
        rec = self.finish(rid, 150)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        self.assertIn('failing over', self.assertEvent(rid, GROUP_FAILOVER).detail)
        self.assertEqual(rec.channel_id, second.id)

        answer = self.answer(rid)
        self.assertNoProblems(answer)
        # Which member captured each segment, by ChannelBin's rows and by the frames in them.
        self.assertAgrees(answer, self.claims(rid))
        self.assertEqual(answer.channels, (first_label, second_label))
        # The failover can cost nothing: the second member's connect burst reaches back over
        # the time the first member took to die, and on one run in seven it covered all of
        # it. What can be said every time is what any loss is put down to, and that the
        # second member repeats no more than one burst - which starts at the keyframe at or
        # before it, so up to one keyframe interval more (DESIGN-simulator.md §3.10).
        self.assertLessEqual({x.reason for x in answer.lost},
                             {ak.REASON_REFUSED, ak.REASON_NOT_SENT, ak.REASON_NOT_RECORDED},
                             answer.describe())
        self.assertLessEqual(answer.repeated_seconds, BURST + KEYFRAME_SECONDS,
                             answer.describe())

        joined = self.answer_joined(rid)
        self.assertNoProblems(joined)
        self.assertEqual(joined.channels, (first_label, second_label))


if __name__ == '__main__':
    unittest.main()
