"""Two recordings on one single-connection account, the second due while the first runs: the
second waits for the slot instead of opening a second connection, so the provider never sees
the account over its limit and never cuts the first one off (dev/changelog/854, 1223).

What the provider does over the limit is the measured one (DESIGN-simulator.md §3.10): it
serves the new connection and ends the oldest about 5.5 s later. So a ChannelBin that opened
the second connection anyway would cost the first recording the rest of its window - the
failure the ceiling exists to prevent, played out against a provider that behaves the way a
real one did.

Guards dev/docs/BUGS.md 2026-08-29 09:32 AM (a recording opened a connection over its
account's limit instead of waiting for a slot): with the slot acquire patched to always
succeed, both connections open at once and the first recording is cut off.
"""
import unittest

from app.database import REC_STATUS_COMPLETED, RECORDING_START_DEFERRED
from sim import answer_key as ak

from .support import CaptureCase


class ConnectionLimitScenario(CaptureCase):
    def test_a_second_recording_waits_for_the_slot_and_the_first_is_never_cut_off(self):
        first = self.record(self.channel('cove-tv', 0), seconds=25, name='first')
        second = self.record(self.channel('cove-tv', 1), seconds=30, start_in=8, name='second')
        self.finish(first, 120)
        self.finish(second, 150)

        for rid in (first, second):
            self.assertEqual(self.status(rid), REC_STATUS_COMPLETED, self.event_types(rid))
        self.assertIn('every connection slot', self.assertEvent(
            second, RECORDING_START_DEFERRED).detail)
        first_end = max(s.ended_at for s in self.segments(first))
        second_start = min(s.started_at for s in self.segments(second))
        self.assertGreaterEqual(second_start, first_end)

        # The provider's side: never two of the login's connections at once, and nobody cut
        # off for being over the limit.
        served = sorted((r for r in self.sim.ledger.entries(endpoint='stream')
                         if r.status in ak.SERVED), key=lambda r: r.at)
        for a, b in zip(served, served[1:]):
            self.assertLessEqual(a.at + a.seconds, b.at + 0.5,
                                 f'ledger rows {a.seq} and {b.seq} overlap')
        self.assertFalse([r.seq for r in served if 'over the limit' in (r.note or '')])

        answer = self.answer(first, window=self.window(first))
        self.assertNoProblems(answer)
        self.assertAgrees(answer, self.claims(first))
        self.assertEqual(self.lost(answer, ak.REASON_NOT_SENT), [], answer.describe())
        later = self.answer(second)
        self.assertNoProblems(later)
        self.assertAgrees(later, self.claims(second))


if __name__ == '__main__':
    unittest.main()
