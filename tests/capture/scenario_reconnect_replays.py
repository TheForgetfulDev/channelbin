"""A provider that keeps dropping the connection and answers each reconnect from its buffer:
recording 32 on a real channel (dev/docs/BUGS.md 2026-11-03, dev/changelog/1422).

There the replays read as 1.61x delivery, so a capture that had lost nothing was stopped and
restarted at real-time speed. Reading behind the live edge then lost video at each reconnect, and
the recording played with its sound 1:52 ahead of its picture and scored 99. This is that feed
through the whole recorder, at that cadence: about 25 s of buffer every 40 s. The recording stays
in one capture with no real-time read, nothing is lost, the picture and sound end together, and
what the file holds twice is said.

ffmpeg's desync itself needs a replay inside a narrow window around its old 10 s threshold, too
rare to draw here. What keeps it from happening is the capture's -dts_delta_threshold, which
scenario_video_gap_kept guards.
"""
import json
import unittest

from app.database import (CAPTURE_PACING_ENABLED, DIAGNOSTICS, FAST_DELIVERY_DETECTED,
                          REC_STATUS_COMPLETED, Recording, TRACK_END_GAP_OUT_OF_STEP_SECONDS, Alert)
from app.probe import scan_timeline_jumps

from .support import CAPTURE_LOOPS, CHANNEL_LOOP, CaptureCase

# 25 s of buffer and about 40 s of live video at this loop's rate, per connection.
DROP_AFTER_BYTES = 14_000_000

WORLD = {
    'seed': 32,
    'backends': [
        {'name': 'cove', 'channels': 4, 'content': CHANNEL_LOOP, 'burst_seconds': 25.0,
         'radio_share': 0.0, 'no_id_share': 0.0},
    ],
    'panels': [{'name': 'cove-tv', 'backend': 'cove', 'url_shape': {'bare': 1.0}}],
    'logins': [{'backend': 'cove', 'username': 'coveuser', 'password': 'covepass1',
                'panels': ['cove-tv'], 'max_connections': 1}],
}


class ReconnectReplaysScenario(CaptureCase):
    world_source = WORLD
    loops = CAPTURE_LOOPS

    def test_a_feed_that_keeps_dropping_records_whole_and_in_step(self):
        channel = self.channel('cove-tv', 0)
        self.sim.inject('keeps_dropping', self.label(channel.id), after_bytes=DROP_AFTER_BYTES)
        rid = self.record(channel, seconds=170, name='keeps dropping')
        rec = self.finish(rid, 300)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        self.assertEqual(self.events(rid, FAST_DELIVERY_DETECTED), [], self.event_types(rid))
        self.assertEqual(self.events(rid, CAPTURE_PACING_ENABLED), [], self.event_types(rid))
        self.assertEqual(len(self.segments(rid)), 1, self.event_types(rid))

        capture = [e for e in self.events(rid, DIAGNOSTICS)
                   if json.loads(e.extra_data or '{}').get('kind') == 'capture_stderr']
        self.assertEqual(len(capture), 1, self.event_types(rid))
        replays = json.loads(capture[0].extra_data).get('reconnect_replay_seconds') or []
        self.assertGreaterEqual(len(replays), 3, capture[0].detail)
        self.assertGreater(sum(replays) / len(replays), 20, capture[0].detail)

        answer = self.answer(rid)
        self.assertNoProblems(answer)
        self.assertLess(answer.lost_seconds, 0.5, answer.describe())
        self.assertGreater(answer.repeated_seconds, 40, answer.describe())

        row = self.fresh(Recording, rid)
        self.assertIsNotNone(row.track_end_gap_seconds, 'the tracks were never compared')
        self.assertLess(row.track_end_gap_seconds, TRACK_END_GAP_OUT_OF_STEP_SECONDS,
                        self.event_types(rid))
        self.assertEqual(Alert.query.filter_by(alert_type='RECORDING_OUT_OF_STEP',
                                               recording_id=rid).count(), 0)

        video = scan_timeline_jumps(self.joined_path(rid)).get(0, {})
        self.assertEqual(video.get('jumps'), [], str(video))


if __name__ == '__main__':
    unittest.main()
