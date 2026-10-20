"""One login sold through two services, added as two accounts ChannelBin cannot know are
one: the second recording opens a second connection on a one-seat login, the provider cuts
the oldest off a few seconds later, ffmpeg reconnects inside the segment, and the new
connection is answered from the provider's buffer again (DESIGN-simulator.md §3.10,
dev/changelog/1237).

Nothing is lost, and the file is longer than its window: it holds the seconds before each
drop twice, back to back, which is where a recording that counts 142% of its expected frames
comes from. ChannelBin reads how much from ffmpeg's own timestamp repair (the jump it closed
at each reconnect); the answer key reads it from the content. The two have to agree, and the
reconnect count has to match the connections the provider actually served, or the figure the
user is shown is a guess.

Guards dev/docs/BUGS.md 2026-10-19 @ 08:12:03 PM ET (a segment that reconnected in-process
said how many times and nothing about what each reconnect did to the file): with the replay
clause removed from record_segment_diagnostics, the capture_stderr event carries no
reconnect_replay_seconds and the agreement check fails.
"""
import json
import re
import unittest

from app.database import DIAGNOSTICS, REC_STATUS_COMPLETED
from sim import answer_key as ak

from .support import CAPTURE_LOOPS, CHANNEL_LOOP, CaptureCase

# One backend behind two panels and one login valid on both, counting its single seat once:
# the shape of real accounts 1 and 4 (dev/sim/worlds/fullstack.yaml's harbor).
SHARED_LOGIN_WORLD = {
    'seed': 29,
    'backends': [
        {'name': 'cove', 'channels': 8, 'content': CHANNEL_LOOP, 'burst_seconds': 4.0,
         'radio_share': 0.0, 'no_id_share': 0.0},
    ],
    'panels': [
        {'name': 'cove-tv', 'backend': 'cove', 'url_shape': {'bare': 1.0}},
        {'name': 'cove-plus', 'backend': 'cove', 'url_shape': {'bare': 1.0}},
    ],
    'logins': [
        {'backend': 'cove', 'username': 'coveuser', 'password': 'covepass1',
         'panels': ['cove-tv', 'cove-plus'], 'max_connections': 1},
    ],
}

_RECONNECTED = re.compile(r'reconnected (?:at least )?(\d+) times?')


class SharedLoginCutoffScenario(CaptureCase):
    world_source = SHARED_LOGIN_WORLD
    loops = CAPTURE_LOOPS

    def _capture_event(self, rid):
        events = [e for e in self.events(rid, DIAGNOSTICS)
                  if (json.loads(e.extra_data or '{}')).get('kind') == 'capture_stderr']
        self.assertEqual(len(events), 1, [e.detail for e in self.events(rid)])
        return events[0]

    def test_a_recording_cut_off_and_reconnected_says_what_it_holds_twice(self):
        first = self.record(self.channel('cove-tv', 0), seconds=30, name='first')
        second = self.record(self.channel('cove-plus', 1), seconds=30, start_in=9,
                             name='second')
        self.finish(first, 120)
        self.finish(second, 150)

        rows = [r for r in self.sim.ledger.entries(endpoint='stream') if r.status in ak.SERVED]
        self.assertTrue([r.seq for r in rows if 'cut off' in (r.note or '')],
                        'the provider never cut a connection off, so there was nothing '
                        'to reconnect')

        for rid in (first, second):
            with self.subTest(recording=rid):
                # The repair stayed inside the segment: one capture, no restart.
                self.assertEqual(self.status(rid), REC_STATUS_COMPLETED, self.event_types(rid))
                self.assertEqual(len(self.segments(rid)), 1)
                event = self._capture_event(rid)
                said = _RECONNECTED.search(event.detail)
                self.assertIsNotNone(said, event.detail)
                reconnects = int(said.group(1))
                label = self.serving(rid)[0]
                self.assertEqual(reconnects, len([r for r in rows if r.channel == label]) - 1,
                                 event.detail)

                answer = self.answer(rid)
                self.assertNoProblems(answer)
                self.assertAgrees(answer, self.claims(rid))
                self.assertLess(answer.lost_seconds, 0.5, answer.describe())
                self.assertGreater(answer.repeated_seconds, 1.0, answer.describe())

                # What ChannelBin read off ffmpeg's repair against what the file holds twice:
                # each reconnect's jump is measured in decode time, so a frame or two apart
                # from the content's shown time, per reconnect.
                extra = json.loads(event.extra_data)
                replays = extra.get('reconnect_replay_seconds')
                self.assertIsNotNone(replays, event.detail)
                self.assertEqual(len(replays), reconnects, event.detail)
                self.assertAlmostEqual(sum(replays), answer.repeated_seconds,
                                       delta=0.1 * reconnects + 0.3,
                                       msg=f'{event.detail}\n{answer.describe()}')
                self.assertIn(f'{sum(replays):.1f} s in all', event.detail)


if __name__ == '__main__':
    unittest.main()
