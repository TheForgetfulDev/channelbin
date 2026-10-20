"""A feed that comes back at another format after a stall: the restarted segment opens at
1280x720 where the recording opened at 720x576, and ChannelBin says so from that segment's
own capture-time probe and again from the whole-file survey (dev/changelog/754, 866, 1201,
1223).

The format moves when the stall is noticed - the test swaps the loop and clears the trickle on
STALL_DETECTED - which is the shape dev/changelog/866 recorded: a feed back at another frame
rate after a stall restart.

A characterization of today's pipeline, not a guard for one past defect.
"""
import json
import unittest

from app.database import REC_STATUS_COMPLETED, RECORDING_FORMAT_CHANGED, STALL_DETECTED
from sim.content import PROFILES

from .support import CHANNEL_LOOP, OTHER_LOOP, CaptureCase, at


def _size(profile: str) -> str:
    p = PROFILES[profile]
    return f'{p.width}x{p.height}'


class FormatChangeScenario(CaptureCase):
    def test_a_restart_at_another_format_is_disclosed_by_the_segment_that_changed(self):
        ch = self.channel()
        label = self.label(ch.id)
        rid = self.record(ch, seconds=45)
        trickle = self.sim.inject('throttle', label, rate=0.02, start=at(12), end=at(600))
        self.wait_event(rid, STALL_DETECTED, 60)
        self.sim.inject('format_change', label, profile=OTHER_LOOP, start=at(-1), end=at(600))
        self.sim.clear(trickle)
        rec = self.finish(rid, 150)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        answer = self.answer(rid)
        self.assertNoProblems(answer)
        self.assertAgrees(answer, self.claims(rid))

        # ChannelBin's capture-time probe of each segment against the loop the answer key
        # found in it.
        held = [(s, a) for s, a in zip(self.segments(rid), answer.segments) if not a.empty]
        self.assertGreaterEqual(len(held), 2, answer.describe())
        self.assertEqual(held[0][1].profiles, (CHANNEL_LOOP,))
        self.assertEqual(held[-1][1].profiles, (OTHER_LOOP,))
        for seg, part in held:
            self.assertEqual(seg.probe_resolution, _size(part.profiles[0]),
                             f'segment {seg.segment_number}')

        opened = [json.loads(e.extra_data) for e in self.events(rid, RECORDING_FORMAT_CHANGED)
                  if e.extra_data and json.loads(e.extra_data).get('basis') == 'segment_open']
        self.assertEqual([o['segment_number'] for o in opened], [held[-1][0].segment_number])
        self.assertTrue(any('changes format part-way through' in (e.detail or '')
                            for e in self.events(rid, RECORDING_FORMAT_CHANGED)),
                        'the whole-file survey says so too')


if __name__ == '__main__':
    unittest.main()
