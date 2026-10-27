"""The capture tier's short set, run in the default suite: one real recording against the
simulator, start to converted file, with a stall ffmpeg repairs inside the segment
(DESIGN-simulator.md §4, D3; dev/changelog/1223).

The rest of the tier - eight scenarios, minutes each - runs from its own runner,
`./run_tests.sh --capture` (tests/capture/). This one stays on the suite's tiny test loop,
which the suite renders anyway, so a fresh checkout pays about 1.5 s for it, and keeps the
default conversion (no `reencode_mode` override), so the mp4 step is the real one.

The stall is `silent_socket`: the provider stops sending and keeps the connection, the shape
dev/changelog/958 measured, and ffmpeg's `-rw_timeout` reconnects within the segment. The new
connection replays the connect burst, so the file holds a few seconds twice - the answer key's
own finding (DESIGN-simulator.md §3.12) - and ChannelBin's segment diagnostics name the
reconnect.

A characterization of the pipeline end to end, not a guard for one past defect.
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import (CONVERSION_DONE, DIAGNOSTICS, REC_STATUS_COMPLETED,  # noqa: E402
                          STALL_DETECTED)
from tests.capture.support import CAPTURE_WORLD, CaptureCase  # noqa: E402

SMOKE_WORLD = copy.deepcopy(CAPTURE_WORLD)
for _b in SMOKE_WORLD['backends']:
    _b['content'] = 'test_tiny'


class CaptureSmokeTests(CaptureCase):
    world_source = SMOKE_WORLD
    loops = ('test_tiny',)
    config = {'recording': {'post_process': {'reencode_mode': 'damaged'}}}

    def test_a_recording_survives_a_silent_socket_and_converts(self):
        ch = self.channel()
        # A connection fault reaches only connections opened after it, and record() can start
        # within a millisecond, so the silence is set before the recording is scheduled
        # (dev/docs/BUGS.md 2026-10-27 12:21 PM).
        self.sim.inject('silent_socket', self.label(ch.id), after_bytes=400_000, times=1)
        rid = self.record(ch, seconds=12)
        rec = self.finish(rid, 90)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        self.assertNotIn(STALL_DETECTED, self.event_types(rid),
                         'ffmpeg repaired it inside the segment')
        self.assertEqual(len(self.segments(rid)), 1)
        self.assertTrue(any('reconnected' in (e.detail or '')
                            for e in self.events(rid, DIAGNOSTICS)), self.event_types(rid))
        self.assertEvent(rid, CONVERSION_DONE)
        self.assertTrue(rec.output_path.endswith('.mp4') and os.path.exists(rec.output_path))

        answer = self.answer(rid)
        self.assertNoProblems(answer)
        self.assertAgrees(answer, self.claims(rid))
        self.assertGreater(answer.repeated_seconds, 0.5, answer.describe())
        self.assertEqual(len({s.row for s in answer.stretches}), 2, answer.describe())


if __name__ == '__main__':
    unittest.main()
