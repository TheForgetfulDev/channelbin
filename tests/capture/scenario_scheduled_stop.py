"""A recording that runs its scheduled window with nothing wrong and stops on time holds
everything the provider sent up to the stop, with the stop itself the only thing that ended
the capture.

Guards dev/docs/BUGS.md 2026-10-26: the stop job SIGTERMed ffmpeg and the watchdog, woken by
the same stop event, SIGTERMed it again a few milliseconds later. To ffmpeg 7.1 a second
signal is an immediate exit - no trailer, and the 3 to 4 seconds its muxer still held were
gone, while ChannelBin's own figures read a complete recording. The full-stack
`scheduled_recording` scenario caught it on four nightly runs of six; this is the same check
at the capture tier, where it runs in the suite's own process against the real scheduler,
recorder and watchdog.
"""
import json
import unittest

from app.database import DIAGNOSTICS, REC_STATUS_COMPLETED

from .support import CaptureCase


class ScheduledStopScenario(CaptureCase):
    def test_a_scheduled_stop_keeps_everything_sent_up_to_the_stop(self):
        ch = self.channel()
        rid = self.record(ch, seconds=20)
        rec = self.finish(rid, 90)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        self.assertEqual(len(self.segments(rid)), 1, self.event_types(rid))
        self.assertFalse(rec.total_stall_count or rec.total_restart_count,
                         self.event_types(rid))

        answer = self.answer(rid)
        self.assertNoProblems(answer)
        self.assertAgrees(answer, self.claims(rid))
        self.assertEqual(answer.lost, [], answer.describe())

        # ffmpeg says so itself when it was told to leave at once: every I/O call after a
        # second signal fails with "Immediate exit requested", trailer included.
        tails = [json.loads(e.extra_data or '{}').get('stderr_tail', '')
                 for e in self.events(rid, DIAGNOSTICS)
                 if json.loads(e.extra_data or '{}').get('kind') == 'capture_stderr']
        self.assertEqual(len(tails), 1, [e.detail for e in self.events(rid)])
        self.assertNotIn('Immediate exit requested', tails[0], tails[0])


if __name__ == '__main__':
    unittest.main()
