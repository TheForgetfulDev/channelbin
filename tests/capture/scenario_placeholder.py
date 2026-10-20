"""The provider's offline placeholder - a finite black clip, drained in seconds and closed
cleanly - is discarded rather than joined, the capture restarts, and the finished file holds
the channel and none of the clip (dev/changelog/957, 1223).

The clip stands until ChannelBin has discarded a segment of it; the test then clears it, as
the real channel came back on recordings 17 and 19. It cannot be `times: 1`: ffmpeg opens the
clip as a file and fetches it again, and the second fetch is a new connection
(DESIGN-simulator.md §3.12).

Guards the placeholder discard (dev/docs/BUGS.md 2026-09-14 06:10 AM): with
`classify_placeholder_segment` answering "not a placeholder", the clip is joined into the
finished file and the scenario fails on the joined file's placeholder seconds.
"""
import unittest

from app.database import REC_STATUS_COMPLETED, SEGMENT_DISCARDED

from .support import CHANNEL_LOOP, PLACEHOLDER_LOOP, CaptureCase, at


class PlaceholderScenario(CaptureCase):
    def test_a_placeholder_clip_is_discarded_and_the_file_holds_only_the_channel(self):
        ch = self.channel()
        clip = self.sim.inject('placeholder', self.label(ch.id), clip_seconds=60.0,
                               start=at(-5), end=at(600))
        rid = self.record(ch, seconds=35)
        self.wait_event(rid, SEGMENT_DISCARDED, 40)
        self.sim.clear(clip)
        rec = self.finish(rid, 150)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        discarded = [s for s in self.segments(rid) if s.excluded]
        self.assertIn(len(discarded), (1, 2), 'the clip was cleared after the first discard')
        self.assertEqual(rec.discarded_segment_count, len(discarded))

        answer = self.answer(rid)
        self.assertNoProblems(answer)
        # Per segment: the discarded ones hold the clip and nothing of the channel, the kept
        # ones the channel and nothing of the clip - and ChannelBin's discarded_seconds is
        # the clip's content, 60 s a copy.
        self.assertAgrees(answer, self.claims(rid))
        self.assertAlmostEqual(answer.placeholder_seconds, 60.0 * len(discarded), delta=1.0)
        self.assertEqual(answer.profiles[-1], CHANNEL_LOOP)

        joined = self.answer_joined(rid)
        self.assertNoProblems(joined)
        self.assertEqual(joined.placeholder_seconds, 0.0, joined.describe())
        self.assertNotIn(PLACEHOLDER_LOOP, joined.profiles)
        self.assertAlmostEqual(joined.distinct_seconds, answer.distinct_seconds, delta=0.3)


if __name__ == '__main__':
    unittest.main()
