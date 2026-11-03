"""The feed's video stops for longer than ffmpeg's own 10 s discontinuity threshold while
its audio flows, and comes back before the watchdog's frame check would restart the capture:
the capture keeps the gap in the file instead of pulling the video back over it.

Guards dev/docs/BUGS.md 2026-11-02 (a capture closed up a video-only gap over 10 s, out of
step with the audio): ffmpeg reading an MPEG-TS or HLS input closes any forward timestamp
jump over -dts_delta_threshold per stream, so a 15 s video outage was written with every
frame after it 15 s ahead of its sound, and no scan could see it because the gap was gone.
With the capture's own bound (proc_utils.TIMELINE_GAP_KEEP_SECONDS) the joined file's video
shows the outage as one hole, its audio runs on through, the two end together, and the
timeline scan measures the hole. With the fix reverted the video ends 15 s before the audio
and the scan finds nothing. Both forms of a stream a provider serves, TS and HLS.

The outage is shorter than the stall timeout (30 s, the production default) so the frame
check never fires and the whole capture is one segment - scenario_video_stops covers the
restart. No fault was added for this: `video_stops` already withholds the video alone, over
both forms.
"""
import unittest

from app.database import CAPTURE_VIDEO_STOPPED, REC_STATUS_COMPLETED
from app.probe import scan_timeline_jumps
from sim import answer_key as ak

from .support import CAPTURE_LOOPS, CAPTURE_WORLD, CHANNEL_LOOP, OTHER_LOOP, CaptureCase, at

OUTAGE_SECONDS = 15.0

# CAPTURE_WORLD's cove backend, served as HLS as well: the panel's every stream is an .m3u8,
# a master playlist with the channel loop and one other quality.
HLS_WORLD = {
    'seed': 31,
    'backends': [
        {'name': 'cove', 'channels': 8, 'content': CHANNEL_LOOP, 'burst_seconds': 4.0,
         'radio_share': 0.0, 'no_id_share': 0.0, 'hls_variant': OTHER_LOOP,
         'hls_segment_seconds': 4.0},
    ],
    'panels': [
        {'name': 'cove-tv', 'backend': 'cove', 'url_shape': {'live_m3u8': 1.0}},
    ],
    'logins': [
        {'backend': 'cove', 'username': 'coveuser', 'password': 'covepass1',
         'panels': ['cove-tv'], 'max_connections': 1},
    ],
}


class _KeptGapCase(CaptureCase):
    config = {'watchdog': {'stall_timeout_seconds': 30}}

    def _check(self, hls=False):
        ch = self.channel()
        rid = self.record(ch, seconds=60)
        self.sim.inject('video_stops', self.label(ch.id), start=at(12),
                        end=at(12 + OUTAGE_SECONDS))
        rec = self.finish(rid, 150)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        self.assertNotIn(CAPTURE_VIDEO_STOPPED, self.event_types(rid))
        self.assertEqual(len(self.segments(rid)), 1, self.event_types(rid))

        if hls:
            # The answer key does not read an HLS recording (sim/answer_key.py), so the
            # outage is judged by the file alone, at the segmenting's granularity.
            withheld, delta = OUTAGE_SECONDS, 5.0
        else:
            answer = self.answer(rid)
            self.assertNoProblems(answer)
            self.assertAgrees(answer, self.claims(rid))
            withheld = sum(x.seconds for x in self.lost(answer, ak.REASON_WITHHELD))
            self.assertAlmostEqual(withheld, OUTAGE_SECONDS, delta=3.0, msg=answer.describe())
            delta = 3.0

        # The hole is in the file, on the video alone, and the streams end together.
        scan = scan_timeline_jumps(self.joined_path(rid))
        video, audio = scan.get(0, {}), scan.get(1, {})
        self.assertEqual(len(video.get('jumps', [])), 1, scan)
        prev, dts, cadence = video['jumps'][0]
        self.assertAlmostEqual(dts - prev, withheld, delta=delta, msg=scan)
        self.assertEqual(audio.get('jumps'), [], scan)
        self.assertAlmostEqual(video['last_dts'], audio['last_dts'], delta=1.5, msg=scan)
        self.assertGreaterEqual(rec.timeline_gap_seconds, withheld - delta, scan)


class VideoGapKeptScenario(_KeptGapCase):
    world_source = CAPTURE_WORLD
    loops = CAPTURE_LOOPS

    def test_a_long_video_only_gap_stays_a_gap_on_ts(self):
        self._check()


class VideoGapKeptOverHlsScenario(_KeptGapCase):
    world_source = HLS_WORLD
    loops = CAPTURE_LOOPS

    def test_a_long_video_only_gap_stays_a_gap_on_hls(self):
        self._check(hls=True)


if __name__ == '__main__':
    unittest.main()
