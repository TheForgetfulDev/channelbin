"""A SCHEDULED recording with nothing armed to start it is armed again without a restart.

Guards dev/docs/BUGS.md 2026-11-26 "A SCHEDULED recording left with no start job is armed
again only by a restart, unless it follows a game" (dev/changelog/1757):

  - the periodic check arms a recording due soon, or already due with its window open, that
    has no start_<id> job, at its own times, and says so on the recording;
  - one that still has its job, one far from its start, one whose window is over and one that
    is not SCHEDULED are left alone;
  - a start running right now has no job and is not armed a second time;
  - an arm that fails says nothing on the recording and the next check tries again;
  - the job is registered at startup and named on /jobs.

Throwaway temp SQLite DB through make_test_app(); nothing is started.

    python3 -m unittest tests.test_unarmed_start_check
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, scheduler  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, RECORDING_START_REARMED, REC_STATUS_ABORTED,
    REC_STATUS_SCHEDULED,
)


class _Case(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        self.channel = seed.make_channel(seed.make_account(), name='Somewhere')
        db.session.commit()
        self.now = datetime.utcnow()
        self.jobs = set()
        fake = mock.Mock()
        fake.get_job.side_effect = lambda job_id: object() if job_id in self.jobs else None
        for target, value in ((scheduler, '_scheduler'), (scheduler, '_app')):
            p = mock.patch.object(target, value, fake if value == '_scheduler' else self.t.app)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(scheduler, 'scheduler_is_live', return_value=True)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(scheduler, 'schedule_recording')
        self.schedule_recording = p.start()
        self.addCleanup(p.stop)

    def recording(self, starts_in, runs=60, status=REC_STATUS_SCHEDULED, armed=False):
        start = (self.now + timedelta(minutes=starts_in)).replace(microsecond=0)
        rec = seed.make_recording(status=status, name='show', channel_id=self.channel.id,
                                  start_time=start, stop_time=start + timedelta(minutes=runs))
        db.session.commit()
        if armed:
            self.jobs.add(f'start_{rec.id}')
        return rec.id

    def rearmed(self, rid):
        return RecordingEvent.query.filter_by(recording_id=rid,
                                              event_type=RECORDING_START_REARMED).all()


class UnarmedStartCheckTests(_Case):

    def test_a_recording_due_soon_with_no_job_is_armed_at_its_own_times(self):
        rid = self.recording(starts_in=10)
        rec = db.session.get(Recording, rid)
        window = (rec.start_time, rec.stop_time)
        self.assertEqual(scheduler._unarmed_start_job(), 1)
        self.assertEqual(self.schedule_recording.call_args.args[1:], (rid, *window))
        (ev,) = self.rearmed(rid)
        self.assertIn('nothing armed to start it', ev.detail)
        self.assertIn('it starts at ', ev.detail)

    def test_one_already_due_with_its_window_open_starts_now(self):
        rid = self.recording(starts_in=-20)
        self.assertEqual(scheduler._unarmed_start_job(), 1)
        self.assertIn('starts now', self.rearmed(rid)[0].detail)

    def test_what_is_left_alone(self):
        ids = {
            'still armed': self.recording(starts_in=10, armed=True),
            'far from its start': self.recording(starts_in=24 * 60),
            'window over': self.recording(starts_in=-120, runs=60),
            'not scheduled': self.recording(starts_in=10, status=REC_STATUS_ABORTED),
        }
        self.assertEqual(scheduler._unarmed_start_job(), 0)
        self.schedule_recording.assert_not_called()
        for why, rid in ids.items():
            self.assertEqual(self.rearmed(rid), [], why)

    def test_the_jobstore_is_read_only_for_recordings_close_to_their_start(self):
        for day in range(1, 30):
            self.recording(starts_in=day * 24 * 60)
        near = self.recording(starts_in=5, armed=True)
        scheduler._unarmed_start_job()
        asked = [c.args[0] for c in scheduler._scheduler.get_job.call_args_list]
        self.assertEqual(asked, [f'start_{near}'])

    def test_a_start_running_right_now_is_not_armed_again(self):
        rid = self.recording(starts_in=-1)
        with scheduler._starts_in_flight_lock:
            scheduler._starts_in_flight[rid] = 1
        self.assertEqual(scheduler.missing_starts([rid]), set())
        self.assertEqual(scheduler._unarmed_start_job(), 0)
        self.schedule_recording.assert_not_called()

    def test_a_start_is_in_flight_only_while_it_runs(self):
        rid = self.recording(starts_in=-1)
        seen = []
        with mock.patch('app.recorder.start_recording',
                        side_effect=lambda app, r: seen.append(dict(scheduler._starts_in_flight))):
            scheduler._start_watched(self.t.app, rid)
        self.assertEqual(seen, [{rid: 1}])
        self.assertEqual(scheduler._starts_in_flight, {})
        with mock.patch('app.recorder.start_recording', side_effect=RuntimeError('boom')), \
                mock.patch.object(scheduler, '_start_job_raised'):
            scheduler._start_watched(self.t.app, rid)
        self.assertEqual(scheduler._starts_in_flight, {})

    def test_one_of_two_starts_finishing_leaves_the_other_in_flight(self):
        rid = self.recording(starts_in=-1)
        inner = []

        def start_again_inside(app, r):
            if not inner:
                inner.append(1)
                scheduler._start_watched(self.t.app, rid)
                inner.append(dict(scheduler._starts_in_flight))

        with mock.patch('app.recorder.start_recording', side_effect=start_again_inside):
            scheduler._start_watched(self.t.app, rid)
        self.assertEqual(inner[1], {rid: 1}, 'the first start is still running')
        self.assertEqual(scheduler._starts_in_flight, {})

    def test_the_horizon_covers_a_long_pre_record_check_lead(self):
        rid = self.recording(starts_in=50)
        self.assertEqual(scheduler._unarmed_start_job(), 0)
        self.t.sandbox_config({'channel_testing': {'pre_check': {'lead_minutes': 45}}})
        self.assertEqual(scheduler._unarmed_start_job(), 1)
        self.assertEqual(len(self.rearmed(rid)), 1)

    def test_a_failed_arm_says_nothing_and_the_next_check_tries_again(self):
        rid = self.recording(starts_in=10)
        self.schedule_recording.side_effect = [RuntimeError('jobstore down'), None]
        with self.assertLogs('app.scheduler', 'WARNING'):
            self.assertEqual(scheduler._unarmed_start_job(), 0)
        self.assertEqual(self.rearmed(rid), [])
        self.assertEqual(scheduler._unarmed_start_job(), 1)
        self.assertEqual(len(self.rearmed(rid)), 1)

    def test_the_check_is_registered_and_named(self):
        from app.routes import jobs
        with mock.patch.object(scheduler, '_add_job') as add:
            scheduler.schedule_unarmed_start_check(self.t.app)
        self.assertEqual(add.call_args.kwargs['id'], scheduler.UNARMED_START_JOB_ID)
        self.assertIn(scheduler.UNARMED_START_JOB_ID, scheduler.RUN_NOW_SYSTEM_JOBS)
        self.assertIn(scheduler.UNARMED_START_JOB_ID, jobs.PROBE_JOBS)

    def test_the_not_armed_sentence_no_longer_asks_for_a_restart(self):
        import inspect
        from app import auto_rules, sports_follow
        for module in (auto_rules, sports_follow):
            self.assertNotIn('Restarting ChannelBin arms it', inspect.getsource(module))


if __name__ == '__main__':
    unittest.main()
