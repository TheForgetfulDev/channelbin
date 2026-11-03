"""A feed that comes back from a stall declaring no video at all: the restarted segment holds
sound alone, and the join keeps that sound in the audio track with the picture missing for that
stretch, and says so (dev/changelog/1410).

Guards dev/docs/BUGS.md 2026-11-02 (the concat demuxer's match by position wrote that sound into
the video track and left the file short of it). With the join's layout plan removed, the
sound's packets vanish from the audio count and the join's note is never written.

The answer key places video frames, so it has nothing to say about a segment with none; what is
asserted is what the join and the recorder did with one.
"""
import json
import os
import subprocess
import time
import unittest

from app.database import DIAGNOSTICS, REC_STATUS_COMPLETED, STALL_DETECTED
from app.probe import stream_layout

from .support import CaptureCase, at


def _audio_packets(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a', '-count_packets',
                          '-show_entries', 'stream=nb_read_packets', '-of', 'csv=p=0', path],
                         capture_output=True, text=True, timeout=60).stdout
    return sum(int(x) for x in out.split() if x.isdigit())


class NoVideoTrackScenario(CaptureCase):
    config = {'recording': {'post_process': {'enabled': False}}}

    def test_a_segment_with_no_video_track_keeps_its_sound_in_the_audio_track(self):
        ch = self.channel()
        label = self.label(ch.id)
        rid = self.record(ch, seconds=60)
        trickle = self.sim.inject('throttle', label, rate=0.02, start=at(12), end=at(600))
        self.wait_event(rid, STALL_DETECTED, 60)
        gone = self.sim.inject('no_video_track', label, start=at(-1), end=at(600))
        self.sim.clear(trickle)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            kept = self.keeper.segments(rid)
            if any(stream_layout(p) == [('audio', 0x100)] for p in kept):
                break
            time.sleep(0.5)
        self.sim.clear(gone)
        rec = self.finish(rid, 150)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        kept = self.keeper.segments(rid)
        layouts = [stream_layout(p) for p in kept]
        self.assertIn([('audio', 0x100)], layouts, 'the fault never reached a segment')
        self.assertEqual(layouts[0][0][0], 'video')

        notes = [e for e in self.events(rid, DIAGNOSTICS)
                 if json.loads(e.extra_data or '{}').get('kind') == 'join_layout']
        self.assertEqual(len(notes), 1, self.event_types(rid))
        self.assertIn('had no video track', notes[0].detail)

        # Every segment's sound is in the file's audio track, give or take what -fflags
        # +discardcorrupt takes at a cut.
        held = sum(_audio_packets(p) for p in kept)
        self.assertGreaterEqual(_audio_packets(rec.output_path), held * 0.98,
                                f'{held} audio packets in {len(kept)} segments')
        self.assertTrue(os.path.getsize(rec.output_path) > 0)


if __name__ == '__main__':
    unittest.main()
