"""A capture with no attempts left and data in hand ends and joins what it holds; it does not
fail beside its segments (dev/changelog/1404, BUGS.md 2026-11-02).

Principle 2 wants the join, principle 1 wants the give-up as visible as the FAILED row it
replaced, so these cover both halves:

  - GiveUpTests: each of the watchdog's give-up sites ends the capture and starts the join.
  - NothingCapturedTests: a capture holding nothing still fails.
  - HealthOutcomeTests: it scores 'failed' once, however the join turns out.
  - SurfaceTests: the words on the list, the recording page and the guide's mark.
  - RuleTests: a later airing of a program it holds part of is not a repeat.

No network and no real ffmpeg: every child is a local `sys.executable -c`, and the join is
stubbed where it is not the subject. The real pipeline runs in
tests/capture/scenario_dead_stream_gives_up.py.

  python3 -m unittest tests.test_capture_gave_up
"""
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.recorder as recorder  # noqa: E402
import app.watchdog as wdmod  # noqa: E402
from app import auto_rules, auto_rules_view, connection_limits as connlim, db  # noqa: E402
from app import recording_story  # noqa: E402
from app.concatenator import do_concatenation  # noqa: E402
from app.database import (  # noqa: E402
    Alert, AutoRule, CAPTURE_GAVE_UP, Channel, FAILURE_DEAD_STREAM_DETECTED,
    FAILURE_FAST_DELIVERY_DETECTED, FAILURE_MAX_CONSECUTIVE_FAILURES, GAVE_UP_REASONS,
    RECORDING_FAILED, RECORDING_FAILED_DEAD_STREAM, REC_STATUS_ABORTED, REC_STATUS_COMPLETED,
    REC_STATUS_CONCATENATING, REC_STATUS_FAILED, REC_STATUS_IN_PROGRESS, Recording,
    RecordingEvent, RecordingSegment, RuleMatch, detach_recording_references, rule_status,
)
from app.health_score import apply_capture_phase_health_observation  # noqa: E402
from app.routes.recordings import _index_row  # noqa: E402
from app.tz_utils import get_display_tz  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.watchdog import RestartHarness, sleeper  # noqa: E402


class _GiveUpHarness(RestartHarness):
    """The give-up sites driven through the real thread, with the real stop_recording and the
    join's thread stubbed so nothing past the hand-off runs."""

    def setUp(self):
        super().setUp()
        acct = seed.make_account(name='Give-up Account', max_connections=1)
        ch = seed.make_channel(acct, name='Give-up Channel')
        db.session.commit()
        self.account_id, self.channel_id = acct.id, ch.id
        rec = db.session.get(Recording, self.rid)
        rec.channel_id = ch.id
        self.scheduled_stop = rec.stop_time
        db.session.commit()
        connlim._holders.clear()
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', self.rid))

        self._extra_watchdog_cfg = {}
        self.joined = threading.Event()
        self.join_reasons = []

        def _join(app, recording_id, reason='Stop time reached; beginning the join'):
            self.join_reasons.append(reason)
            self.joined.set()

        self._patchers = [
            mock.patch('app.concatenator.do_concatenation', side_effect=_join),
            mock.patch.object(recorder, 'failover_group_member', return_value=False),
            mock.patch.object(recorder, 'persist_final_thumbnail'),
            mock.patch('app.health_score.apply_recording_health_observation'),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self._patchers):
            p.stop()
        connlim._holders.clear()
        super().tearDown()

    def _cfg(self, stall_timeout, restart_delay=0):
        return cfgmod._deep_merge(super()._cfg(stall_timeout, restart_delay),
                                  {'watchdog': self._extra_watchdog_cfg})

    def _give_up(self, *, event=CAPTURE_GAVE_UP, produces_data=False):
        self.state.process = sleeper()
        return self._run_until_event(event, stall_timeout=1, restart_delay=0,
                                     produces_data=produces_data)

    def _events(self, event_type):
        return RecordingEvent.query.filter_by(recording_id=self.rid,
                                              event_type=event_type).all()

    def _assert_joined_with_reason(self, rec, reason):
        self.assertEqual(rec.gave_up_reason, reason)
        self.assertIsNone(rec.failure_reason, 'the row is not FAILED, so it has no failure reason')
        self.assertEqual(self._events(RECORDING_FAILED), [])
        self.assertEqual(self._events(RECORDING_FAILED_DEAD_STREAM), [])
        self.assertTrue(self.joined.wait(10), 'the join was never started')
        self.assertLess(rec.stop_time, self.scheduled_stop,
                        'left in place, the rest of the window reads as recorded on the guide')
        self.assertEqual(rec.scheduled_stop_time, self.scheduled_stop)
        self.assertFalse(connlim.at_limit(self.account_id), 'the slot was not released')
        with recorder._lock:
            self.assertNotIn(self.rid, recorder._active)
        alerts = Alert.query.filter_by(recording_id=self.rid,
                                       alert_type='RECORDING_GAVE_UP').all()
        self.assertEqual(len(alerts), 1)


class GiveUpTests(_GiveUpHarness):
    def test_retries_used_up_mid_window_joins_what_was_captured(self):
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1,
                                    'dead_stream_max_retry_attempts': 2}
        rec = db.session.get(Recording, self.rid)
        rec.dead_stream_retry_count = 2
        db.session.commit()
        rec = self._give_up()
        self._assert_joined_with_reason(rec, FAILURE_DEAD_STREAM_DETECTED)
        self.assertIn('2 retry attempt(s)', self._events(CAPTURE_GAVE_UP)[0].detail)
        self.assertIn('Ending the capture now', self.join_reasons[0])

    def test_retrying_turned_off_joins_too(self):
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1,
                                    'dead_stream_max_retry_attempts': 0}
        rec = self._give_up()
        self._assert_joined_with_reason(rec, FAILURE_DEAD_STREAM_DETECTED)
        self.assertIn('no retry left', self._events(CAPTURE_GAVE_UP)[0].detail)

    def test_max_failures_reached_on_the_next_stall_joins(self):
        self._extra_watchdog_cfg = {'max_consecutive_failures': 1}
        rec = db.session.get(Recording, self.rid)
        rec.consecutive_failures = 1
        db.session.commit()
        rec = self._give_up()
        self._assert_joined_with_reason(rec, FAILURE_MAX_CONSECUTIVE_FAILURES)

    def test_max_failures_reached_by_a_restart_with_no_data_joins(self):
        self._extra_watchdog_cfg = {'max_consecutive_failures': 1}
        rec = self._give_up()
        self._assert_joined_with_reason(rec, FAILURE_MAX_CONSECUTIVE_FAILURES)
        seg = (RecordingSegment.query.filter_by(recording_id=self.rid)
               .order_by(RecordingSegment.segment_number.desc()).first())
        self.assertIsNotNone(seg.ended_at, 'the restart\'s empty segment was left open')

    def test_the_cause_ffmpeg_gave_is_in_the_alert(self):
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1,
                                    'dead_stream_max_retry_attempts': 0}
        self._exited_capture(stderr_text='Connection refused\n')
        rec = self._run_until_event(CAPTURE_GAVE_UP, stall_timeout=1, restart_delay=0,
                                    produces_data=False)
        self._assert_joined_with_reason(rec, FAILURE_DEAD_STREAM_DETECTED)
        alert = Alert.query.filter_by(recording_id=self.rid,
                                      alert_type='RECORDING_GAVE_UP').one()
        self.assertIn('Connection refused', alert.body)


class EndCaptureDirectTests(_GiveUpHarness):
    """_end_capture_gave_up itself, for the states the thread only reaches by racing."""

    def _call(self, reason=FAILURE_DEAD_STREAM_DETECTED):
        wd = wdmod.WatchdogThread(self.rid, self.state, self.t.app)
        return wd._end_capture_gave_up(reason, 'the stream died')

    def test_a_row_another_owner_already_ended_is_left_alone(self):
        """_handle_launch_failure can fail the row while this thread is on its way."""
        rec = db.session.get(Recording, self.rid)
        rec.status = REC_STATUS_FAILED
        db.session.commit()
        self.assertTrue(self._call())
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, REC_STATUS_FAILED)
        self.assertIsNone(rec.gave_up_reason)
        self.assertEqual(self._events(CAPTURE_GAVE_UP), [])
        self.assertEqual(Alert.query.filter_by(recording_id=self.rid).count(), 0)
        self.assertFalse(self.joined.is_set())

    def test_a_capture_a_stop_or_pause_already_owns_is_left_alone(self):
        """They set the stop event and pop the live state before their own status write, so
        the row still reads open. Labelling it would call a user's Stop a give-up, and a Pause
        would resume under a score it never earned."""
        self.state.stop_event.set()
        self.assertTrue(self._call())
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertIsNone(rec.gave_up_reason)
        self.assertEqual(rec.stop_time, self.scheduled_stop)
        self.assertEqual(self._events(CAPTURE_GAVE_UP), [])
        self.assertEqual(Alert.query.filter_by(recording_id=self.rid).count(), 0)
        self.assertFalse(self.joined.is_set())

    def test_a_window_that_is_already_over_is_an_ordinary_end_not_a_give_up(self):
        """A full-length capture whose stop job is late must not read "before the stop time",
        take the failed score or raise the alert."""
        rec = db.session.get(Recording, self.rid)
        rec.stop_time = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertTrue(self._call())
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertIsNone(rec.gave_up_reason)
        self.assertEqual(self._events(CAPTURE_GAVE_UP), [])
        self.assertEqual(Alert.query.filter_by(recording_id=self.rid).count(), 0)
        self.assertTrue(self.joined.wait(10), 'the join was never started')

    def test_a_channel_less_recording_can_give_up(self):
        """A manual URL recording has no channel, and the end of a give-up used to crash on
        that (dev/docs/BUGS.md 2026-08-15)."""
        rec = db.session.get(Recording, self.rid)
        rec.channel_id = None
        db.session.commit()
        self.assertTrue(self._call())
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Recording, self.rid).gave_up_reason)
        self.assertTrue(self.joined.wait(10))

    def test_nothing_joinable_leaves_the_alert_to_the_join(self):
        """Placeholder clips hold bytes, so the capture reaches the join, which raises its own
        typed alert naming them. One failure, one alert (dev/changelog/930)."""
        seg = RecordingSegment.query.filter_by(recording_id=self.rid).one()
        seg.excluded_reason = 'PROVIDER_PLACEHOLDER'
        db.session.commit()
        self.assertTrue(self._call())
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).gave_up_reason,
                         FAILURE_DEAD_STREAM_DETECTED)
        self.assertEqual(Alert.query.filter_by(recording_id=self.rid).count(), 0)
        self.assertTrue(self.joined.wait(10))


class NothingCapturedTests(_GiveUpHarness):
    def setUp(self):
        super().setUp()
        open(self.seg_path, 'wb').close()

    def test_a_dead_stream_that_captured_nothing_still_fails(self):
        self._extra_watchdog_cfg = {'early_fail_abort_count': 1,
                                    'dead_stream_max_retry_attempts': 0}
        rec = self._give_up(event=RECORDING_FAILED_DEAD_STREAM)
        self.assertEqual(rec.status, REC_STATUS_FAILED)
        self.assertEqual(rec.failure_reason, FAILURE_DEAD_STREAM_DETECTED)
        self.assertIsNone(rec.gave_up_reason)
        self.assertFalse(self.joined.is_set())

    def test_max_failures_that_captured_nothing_still_fails(self):
        self._extra_watchdog_cfg = {'max_consecutive_failures': 1}
        rec = self._give_up(event=RECORDING_FAILED)
        self.assertEqual(rec.status, REC_STATUS_FAILED)
        self.assertEqual(rec.failure_reason, FAILURE_MAX_CONSECUTIVE_FAILURES)
        self.assertIsNone(rec.gave_up_reason)


class _RowCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.dvr_dir = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr_dir, exist_ok=True)
        acct = seed.make_account(name='Row Account')
        self.ch = seed.make_channel(acct, name='Row Channel')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _row(self, **kw):
        now = datetime.utcnow()
        defaults = dict(status=REC_STATUS_COMPLETED, name='Race', channel_id=self.ch.id,
                        start_time=now - timedelta(hours=4), stop_time=now)
        rec = seed.make_recording(**{**defaults, **kw})
        db.session.commit()
        return rec


class HealthOutcomeTests(_RowCase):
    def _score(self):
        db.session.expire_all()
        return db.session.get(Channel, self.ch.id).health_score_sample_count or 0

    def test_a_give_up_scores_failed_at_the_join_boundary_and_only_once(self):
        rec = self._row(gave_up_reason=FAILURE_DEAD_STREAM_DETECTED, total_downtime_seconds=0.0)
        apply_capture_phase_health_observation(self.t.app, rec.id)
        db.session.expire_all()
        rec = db.session.get(Recording, rec.id)
        floor = cfgmod.load_config().get('channel_testing', {}).get(
            'recording_score', {}).get('fail_floor')
        self.assertLess(rec.health_quality_score, 50,
                        'scored by the success formula, which sees none of the lost hours')
        if floor is not None:
            self.assertEqual(rec.health_quality_score, floor)
        count = self._score()
        apply_capture_phase_health_observation(self.t.app, rec.id)
        self.assertEqual(self._score(), count, 'a re-entry blended the same capture twice')

    def test_it_weighs_the_whole_window_not_the_part_that_ran(self):
        """The give-up pulled stop_time back. A stream dead at twenty minutes of four hours
        must not count for less than the same stream dead at three."""
        rec = self._row(gave_up_reason=FAILURE_DEAD_STREAM_DETECTED)
        rec.scheduled_stop_time = rec.start_time + timedelta(hours=4)
        rec.stop_time = rec.start_time + timedelta(minutes=20)
        db.session.commit()
        with mock.patch('app.health_score.observation_weight', return_value=1.0) as weight:
            apply_capture_phase_health_observation(self.t.app, rec.id)
        self.assertEqual(weight.call_args_list[0].args[0], 4 * 3600)

    def test_any_other_capture_scores_on_its_own_numbers(self):
        rec = self._row(total_downtime_seconds=0.0)
        apply_capture_phase_health_observation(self.t.app, rec.id)
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rec.id).health_quality_score, 100)

    def test_a_give_up_whose_join_finds_nothing_still_scores_once(self):
        """Every segment a placeholder: the join's no-segments branch does not score that for
        a capture that ran to its stop time, but this one ended because the stream died."""
        rec = self._row(status=REC_STATUS_CONCATENATING,
                        gave_up_reason=FAILURE_DEAD_STREAM_DETECTED)
        path = os.path.join(self.dvr_dir, 'clip.ts')
        with open(path, 'wb') as fh:
            fh.write(b'\x47' * 4096)
        db.session.add(RecordingSegment(recording_id=rec.id, segment_number=0, file_path=path,
                                        bytes_recorded=4096, started_at=rec.start_time,
                                        ended_at=rec.stop_time,
                                        excluded_reason='PROVIDER_PLACEHOLDER'))
        db.session.commit()
        do_concatenation(self.t.app, rec.id)
        db.session.expire_all()
        rec = db.session.get(Recording, rec.id)
        self.assertEqual(rec.status, REC_STATUS_FAILED)
        self.assertIsNotNone(rec.health_quality_score)
        count = self._score()
        self.assertGreater(count, 0)
        do_concatenation(self.t.app, rec.id)
        self.assertEqual(self._score(), count, 'a Retry join blended the same capture twice')


class SurfaceTests(_RowCase):
    def test_every_reason_has_its_own_words(self):
        said = set()
        for reason in GAVE_UP_REASONS:
            clause = recording_story.gave_up_clause(self._row(gave_up_reason=reason))
            self.assertIn('stopped trying before the stop time', clause)
            said.add(clause)
        self.assertEqual(len(said), len(GAVE_UP_REASONS))

    def test_an_ordinary_recording_says_nothing(self):
        self.assertEqual(recording_story.gave_up_clause(self._row()), '')

    def test_a_reason_nobody_worded_is_loud(self):
        with self.assertRaises(ValueError):
            recording_story.gave_up_clause(self._row(gave_up_reason='SOMETHING_NEW'))

    def test_the_retries_are_counted_in_the_words(self):
        one = self._row(gave_up_reason=FAILURE_DEAD_STREAM_DETECTED, dead_stream_retry_count=1)
        many = self._row(gave_up_reason=FAILURE_DEAD_STREAM_DETECTED, dead_stream_retry_count=10)
        none = self._row(gave_up_reason=FAILURE_DEAD_STREAM_DETECTED)
        self.assertIn('1 retry did not', recording_story.gave_up_clause(one))
        self.assertIn('10 retries did not', recording_story.gave_up_clause(many))
        self.assertIn('no retry left', recording_story.gave_up_clause(none))

    def test_a_failed_row_does_not_claim_anything_was_kept(self):
        """Every segment was a placeholder, so the join failed and there is no file."""
        rec = self._row(status=REC_STATUS_FAILED, gave_up_reason=FAILURE_DEAD_STREAM_DETECTED)
        clause = recording_story.gave_up_clause(rec)
        self.assertIn('stopped trying before the stop time', clause)
        self.assertNotIn('kept what', clause)

    def test_the_recording_page_says_it_and_still_offers_the_file(self):
        rec = self._row(gave_up_reason=FAILURE_DEAD_STREAM_DETECTED, dead_stream_retry_count=2,
                        recorded_duration_seconds=3600.0, final_file_size=1024)
        lines = recording_story.what_happened(rec)
        self.assertTrue(lines[0].startswith('You still have a file'), lines)
        self.assertTrue(any('stopped trying before the stop time' in ln for ln in lines), lines)
        self.assertTrue(any('at fault' in ln for ln in lines), lines)

    def test_the_guide_mark_says_it_even_when_almost_everything_was_captured(self):
        rec = self._row(gave_up_reason=FAILURE_FAST_DELIVERY_DETECTED,
                        recorded_duration_seconds=4 * 3600.0)
        self.assertIn('faster than real time', recording_story.partial_note(rec))

    def test_the_list_row_does_not_say_recovered_automatically(self):
        rec = self._row(gave_up_reason=FAILURE_DEAD_STREAM_DETECTED, total_stall_count=4,
                        recorded_duration_seconds=3600.0, final_file_size=1024)
        db.session.add(RecordingSegment(recording_id=rec.id, segment_number=0,
                                        file_path=os.path.join(self.dvr_dir, 'gone.ts'),
                                        started_at=rec.start_time, bytes_recorded=1024,
                                        ended_at=rec.start_time + timedelta(hours=1)))
        db.session.commit()
        with self.t.app.test_request_context():
            health = _index_row(rec, datetime.utcnow(), get_display_tz(), set())['health']
        self.assertEqual(health['text'], 'gave up')
        self.assertEqual(health['cls'], 'bad')
        self.assertEqual(health['label'], '4')
        self.assertIn('stopped trying before the stop time', health['tip'])
        self.assertNotIn('recovered automatically', health['tip'])


class RuleTests(_RowCase):
    def test_rule_status(self):
        for status in (REC_STATUS_COMPLETED, REC_STATUS_CONCATENATING, REC_STATUS_FAILED):
            self.assertEqual(rule_status(status, FAILURE_DEAD_STREAM_DETECTED), REC_STATUS_FAILED)
        self.assertEqual(rule_status(REC_STATUS_ABORTED, FAILURE_DEAD_STREAM_DETECTED),
                         REC_STATUS_ABORTED)
        self.assertEqual(rule_status(REC_STATUS_COMPLETED, None), REC_STATUS_COMPLETED)
        self.assertEqual(rule_status(REC_STATUS_IN_PROGRESS, None), REC_STATUS_IN_PROGRESS)

    def _lib(self, rid):
        return next(r for r in auto_rules._library({rid}) if r.id == rid)

    def _row_for(self, rid=None, removed_status=None):
        now = datetime.utcnow()
        return auto_rules._Row(
            id=1, title_key='race', episode_key='', start=now, stop=now, action='scheduled',
            recording_id=rid, removed_status=removed_status, removed_cancel_reason=None,
            channel_id=self.ch.id, group_id=None, program_title='Race')

    def test_a_later_airing_is_not_a_repeat_of_a_capture_that_gave_up(self):
        rec = self._row(gave_up_reason=FAILURE_DEAD_STREAM_DETECTED)
        lib = {rec.id: self._lib(rec.id)}
        self.assertFalse(auto_rules._pick_is_spoken_for(self._row_for(rec.id), lib))

    def test_it_still_counts_toward_the_rules_limit(self):
        """The limit must never become a retry, same as a FAILED pick. A characterization:
        a COMPLETED pick counts too, so this holds with or without rule_status()."""
        rec = self._row(gave_up_reason=FAILURE_DEAD_STREAM_DETECTED)
        lib = {rec.id: self._lib(rec.id)}
        self.assertTrue(auto_rules._pick_counts_for_limit(self._row_for(rec.id), lib))

    def test_an_ordinary_finished_recording_still_speaks_for_its_program(self):
        rec = self._row()
        lib = {rec.id: self._lib(rec.id)}
        self.assertTrue(auto_rules._pick_is_spoken_for(self._row_for(rec.id), lib))

    def test_deleting_it_keeps_the_answer(self):
        rec = self._row(gave_up_reason=FAILURE_DEAD_STREAM_DETECTED)
        rule = AutoRule(name='Races', search_params='q=race')
        db.session.add(rule)
        db.session.commit()
        now = datetime.utcnow()
        match = RuleMatch(rule_id=rule.id, recording_id=rec.id, title_key='race',
                          program_start_time=now, program_stop_time=now, action_taken='scheduled')
        db.session.add(match)
        db.session.commit()
        match_id = match.id
        detach_recording_references(rec.id)
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(db.session.get(RuleMatch, match_id).removed_status, REC_STATUS_FAILED)

    def test_the_rules_history_says_why(self):
        rec = self._row(gave_up_reason=FAILURE_DEAD_STREAM_DETECTED)
        now = datetime.utcnow()
        m = RuleMatch(id=1, rule_id=1, title_key='race', program_title='Race',
                      program_start_time=now - timedelta(hours=4), program_stop_time=now,
                      action_taken='scheduled', recording_id=rec.id, channel_id=self.ch.id)
        entry = auto_rules_view.ledger_entry(m, rec, {'channels': {}, 'groups': {}}, now=now,
                                             tz=timezone.utc, h24=True)
        self.assertEqual(entry['bucket'], 'bad')
        self.assertIn('not a repeat', entry['fate'])


if __name__ == '__main__':
    unittest.main()
