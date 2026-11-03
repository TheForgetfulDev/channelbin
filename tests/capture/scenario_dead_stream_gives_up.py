"""A stream that captures for a while and then dies for good, with no retry left and most of
its window still to run: the capture ends there and what it holds is joined, rather than a
FAILED row beside its segments (dev/changelog/1404).

What this guards is the whole give-up on a real ffmpeg and a real join:

- the row ends COMPLETED with a real file, and gave_up_reason says why it is short;
- nothing idles to the stop time: stop_time moves back to the give-up, as a manual Stop's does,
  so the rest of the window is free on the guide, and scheduled_stop_time keeps what was asked;
- the capture scores 'failed', not the success formula, which sees nothing of the lost time;
- exactly one alert says so, and it survives the join.
"""
import unittest

from app.database import (Alert, Channel, CAPTURE_GAVE_UP,
                          FAILURE_DEAD_STREAM_DETECTED, REC_STATUS_COMPLETED,
                          RECORDING_FAILED_DEAD_STREAM, Recording, STALL_DETECTED)
from sim import answer_key as ak

from .support import CaptureCase, at


class DeadStreamGivesUpScenario(CaptureCase):
    config = {'watchdog': {'dead_stream_max_retry_attempts': 0}}

    def test_a_stream_that_dies_for_good_is_joined_and_says_it_gave_up(self):
        ch = self.channel()
        rid = self.record(ch, seconds=120)
        self.sim.inject('throttle', self.label(ch.id), rate=0.02, start=at(15), end=at(900))
        self.wait_event(rid, STALL_DETECTED, 60)
        self.sim.inject('refuse_at_open', self.label(ch.id), start=at(-1), end=at(900))
        rec = self.finish(rid, 180)

        self.assertEqual(rec.status, REC_STATUS_COMPLETED, self.event_types(rid))
        self.assertEqual(rec.gave_up_reason, FAILURE_DEAD_STREAM_DETECTED)
        self.assertIsNone(rec.failure_reason)
        self.assertEqual(self.events(rid, RECORDING_FAILED_DEAD_STREAM), [],
                         'a capture that held data is not a failed dead stream')
        self.assertEvent(rid, CAPTURE_GAVE_UP)
        self.assertTrue(rec.output_path or rec.final_file_size, 'no file came out of the join')
        self.assertLess(rec.completed_at, rec.scheduled_stop_time,
                        'ended at the give-up, not the stop')
        self.assertLess(rec.stop_time, rec.scheduled_stop_time)

        rec = self.fresh(Recording, rid)
        self.assertLess(rec.health_quality_score, 50, 'scored as a failed stream')
        self.assertGreater(self.fresh(Channel, ch.id).health_score_sample_count, 0)

        alerts = Alert.query.filter_by(recording_id=rid, alert_type='RECORDING_GAVE_UP').all()
        self.assertEqual(len(alerts), 1)
        self.assertIsNone(alerts[0].dismissed_at, 'a record of the loss stays until dismissed')

        answer = self.answer(rid, window=self.window(rid))
        self.assertNoProblems(answer)
        self.assertAgrees(answer, self.claims(rid))
        self.assertTrue(self.lost(answer, ak.REASON_REFUSED), answer.describe())


if __name__ == '__main__':
    unittest.main()
