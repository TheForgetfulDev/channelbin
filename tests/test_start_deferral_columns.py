"""Guards dev/docs/BUGS.md 2026-09-26 @ 07:40:20 PM: a held start said "starting".

A SCHEDULED recording whose start is held - behind a live mp4 conversion under
collision_policy: wait, or with no free connection slot on its account - retries every
poll, and for the whole wait the Recordings list read "starting" and the Dashboard read
"overdue by N min", neither naming why. The conversion wait had no surface at all beyond
one event-log line. The row now carries the wait as two columns
(Recording.start_deferred_since / _for, dev/changelog/1141): set by the deferral, cleared
on every way out of it, and rendered by both pages.

What each class pins:
  * StampTests - both kinds of deferral set the pair, `since` never moves across retries
    or a change of reason, and a row that is no longer SCHEDULED is never stamped.
  * ClearTests - every way out of the wait clears it: the start itself, a give-up, a
    cancel, an abort, a group cancelling its schedule, a missed-at-startup failure, and an
    edit that moves the start (but not one that leaves it alone).
  * RenderTests - the list row and the Dashboard row say what the start is waiting for.

No network, no real ffmpeg - _launch_segment is patched out at every start.
Run standalone:
  python3 -m unittest tests.test_start_deferral_columns
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, recorder, channel_groups  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.postprocessor import _active_conversions, _active_lock  # noqa: E402
from app.database import (  # noqa: E402
    Recording, REC_STATUS_SCHEDULED, REC_STATUS_FAILED, REC_STATUS_ABORTED,
    REC_STATUS_IN_PROGRESS,
)


class _Case(unittest.TestCase):
    """One account with a single connection, so a second recording on it always waits.

    Same fixture shape as tests/test_ongoing_alerts_clear_themselves.py, including the
    sandboxed DVR dir that keeps start_recording off its directory-probe FAILED path.
    """

    start_scheduler = False

    def setUp(self):
        self.t = make_test_app(start_scheduler=self.start_scheduler)
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self._policy('cancel')
        self.account = seed.make_account(name='One Slot', max_connections=1)
        self.channel = seed.make_channel(self.account, name='The Only Feed')
        db.session.commit()
        self.account_id = self.account.id
        self.channel_id = self.channel.id
        connlim._holders.clear()

    def tearDown(self):
        connlim._holders.clear()
        with _active_lock:
            _active_conversions.clear()
        self.t.cleanup()

    def _policy(self, collision_policy):
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False},
            'post_process': {'collision_policy': collision_policy},
        }})

    def _scheduled(self, start_offset=-600, name='waiter'):
        start = datetime.utcnow() + timedelta(seconds=start_offset)
        rec = seed.make_recording(
            status=REC_STATUS_SCHEDULED, name=name, channel_id=self.channel_id,
            start_time=start, stop_time=start + timedelta(hours=2))
        db.session.commit()
        return rec.id

    def _occupy_slot(self, holder_id=999999):
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', holder_id))
        return holder_id

    def _converting(self, name='The Long Movie'):
        rec = seed.make_recording(status='CONVERTING', name=name,
                                  channel_id=self.channel_id)
        db.session.commit()
        with _active_lock:
            _active_conversions[rec.id] = object()
        return rec.id

    def _attempt(self, rid):
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, rid)
        db.session.expire_all()
        return launch

    def _row(self, rid):
        db.session.expire_all()
        return db.session.get(Recording, rid)

    def _wait_on_slot(self, rid):
        self._occupy_slot()
        self._attempt(rid)
        r = self._row(rid)
        self.assertIsNotNone(r.start_deferred_since, 'the wait must be recorded first')
        return r


class StampTests(_Case):

    def test_a_slot_wait_names_the_account(self):
        rid = self._scheduled()
        r = self._wait_on_slot(rid)
        self.assertEqual(r.status, REC_STATUS_SCHEDULED)
        self.assertEqual(r.start_deferred_for, 'a connection slot on "One Slot"')

    def test_a_conversion_wait_names_the_recording_converting(self):
        self._policy('wait')
        self._converting('The Long Movie')
        rid = self._scheduled()
        self._attempt(rid)
        r = self._row(rid)
        self.assertIsNotNone(r.start_deferred_since)
        self.assertEqual(r.start_deferred_for,
                         'the mp4 conversion of "The Long Movie" to finish')

    def test_since_does_not_move_across_retries(self):
        rid = self._scheduled()
        first = self._wait_on_slot(rid).start_deferred_since
        self._attempt(rid)
        self._attempt(rid)
        self.assertEqual(self._row(rid).start_deferred_since, first,
                         'a retry re-stamped the start of the wait, so the row would claim '
                         'to have been waiting only since the last poll')

    def test_a_new_reason_updates_the_phrase_but_keeps_since(self):
        rid = self._scheduled()
        first = self._wait_on_slot(rid).start_deferred_since
        self._policy('wait')
        self._converting('The Long Movie')
        self._attempt(rid)
        r = self._row(rid)
        self.assertEqual(r.start_deferred_for,
                         'the mp4 conversion of "The Long Movie" to finish')
        self.assertEqual(r.start_deferred_since, first)

    def test_a_row_that_left_scheduled_is_never_stamped(self):
        """A cancel that commits between an attempt's check and its stamp has ended the
        wait; the stamp must not bring it back onto the cancelled row."""
        rid = self._scheduled()
        r = self._row(rid)
        r.status = REC_STATUS_ABORTED
        db.session.commit()
        recorder._note_start_deferred(rid, 'a connection slot on "One Slot"')
        r = self._row(rid)
        self.assertIsNone(r.start_deferred_since)
        self.assertIsNone(r.start_deferred_for)


class ClearTests(_Case):

    def _assert_cleared(self, rid):
        r = self._row(rid)
        self.assertIsNone(r.start_deferred_since)
        self.assertIsNone(r.start_deferred_for)

    def test_starting_clears_it(self):
        rid = self._scheduled()
        self._wait_on_slot(rid)
        connlim.release(self.account_id, 'recording', 999999)
        launch = self._attempt(rid)
        self.assertTrue(launch.called)
        self.assertEqual(self._row(rid).status, REC_STATUS_IN_PROGRESS)
        self._assert_cleared(rid)

    def test_giving_up_on_the_slot_clears_it(self):
        rid = self._scheduled()
        self._wait_on_slot(rid)
        r = self._row(rid)
        r.stop_time = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self._attempt(rid)
        self.assertEqual(self._row(rid).status, REC_STATUS_FAILED)
        self._assert_cleared(rid)

    def test_giving_up_on_a_conversion_clears_it(self):
        self._policy('wait')
        self._converting()
        rid = self._scheduled()
        self._attempt(rid)
        self.assertIsNotNone(self._row(rid).start_deferred_since)
        r = self._row(rid)
        r.stop_time = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self._attempt(rid)
        self.assertEqual(self._row(rid).status, REC_STATUS_FAILED)
        self._assert_cleared(rid)

    def test_cancelling_clears_it(self):
        rid = self._scheduled()
        self._wait_on_slot(rid)
        resp = self.client.post(f'/recordings/{rid}/cancel')
        self.assertIn(resp.status_code, (200, 302))
        self._assert_cleared(rid)

    def test_aborting_clears_it(self):
        rid = self._scheduled()
        self._wait_on_slot(rid)
        with mock.patch('app.scheduler.unschedule_recording'):
            recorder.abort_recording(self.t.app, rid)
        self._assert_cleared(rid)

    def test_a_group_cancelling_its_schedule_clears_it(self):
        rid = self._scheduled()
        self._wait_on_slot(rid)
        with mock.patch('app.scheduler.unschedule_recording'):
            channel_groups.deregister_cancelled_recordings([rid])
        self._assert_cleared(rid)

    def test_an_edit_that_moves_the_start_clears_it(self):
        from app.routes.recordings import _apply_edit_and_reschedule
        rid = self._scheduled()
        r = self._wait_on_slot(rid)
        new_start = datetime.utcnow() + timedelta(hours=1)
        with self.t.app.test_request_context(), \
             mock.patch('app.routes.recordings.unschedule_recording'), \
             mock.patch('app.routes.recordings.schedule_recording'):
            _apply_edit_and_reschedule(rid, r.name, r.url, new_start,
                                       new_start + timedelta(hours=1))
        self._assert_cleared(rid)

    def test_an_edit_that_keeps_the_start_keeps_the_wait(self):
        """A rename is not a new plan: the start job re-arms at the same past time and the
        wait carries on, so it must keep dating from when it really began."""
        from app.routes.recordings import _apply_edit_and_reschedule
        rid = self._scheduled()
        r = self._wait_on_slot(rid)
        since = r.start_deferred_since
        with self.t.app.test_request_context(), \
             mock.patch('app.routes.recordings.unschedule_recording'), \
             mock.patch('app.routes.recordings.schedule_recording'):
            _apply_edit_and_reschedule(rid, 'renamed', r.url, r.start_time, r.stop_time)
        self.assertEqual(self._row(rid).start_deferred_since, since)


class StartupSweepClearTests(_Case):
    # The sweep ends by re-registering on-demand jobs, which needs a live scheduler.
    start_scheduler = True

    def test_missed_at_startup_clears_it(self):
        """The startup sweep fails a SCHEDULED row whose window ended while the process
        was down. It was the one way out of a wait that cleared neither the row nor the
        standing slot-wait alert."""
        from app.scheduler import resume_in_progress_recordings
        rid = self._scheduled()
        self._wait_on_slot(rid)
        r = self._row(rid)
        r.stop_time = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        resume_in_progress_recordings(self.t.app)
        self.assertEqual(self._row(rid).status, REC_STATUS_FAILED)
        r = self._row(rid)
        self.assertIsNone(r.start_deferred_since)
        self.assertIsNone(r.start_deferred_for)
        from app.database import Alert
        self.assertEqual(0, Alert.query.filter_by(
            alert_type='RECORDING_WAITING_FOR_CONNECTION_SLOT', recording_id=rid,
            dismissed_at=None).count())


class RenderTests(_Case):

    def test_the_list_row_says_what_it_is_waiting_for(self):
        rid = self._scheduled(start_offset=-600, name='Held Show')
        self._wait_on_slot(rid)
        html = self.client.get('/recordings').get_data(as_text=True)
        self.assertIn('waiting for a connection slot on &#34;One Slot&#34; · 10 min late',
                      html)

    def test_a_late_row_that_is_not_held_still_says_starting(self):
        self._scheduled(start_offset=-5, name='Just Due')
        html = self.client.get('/recordings').get_data(as_text=True)
        self.assertIn('<span class="rel">starting</span>', html)

    def test_the_dashboard_row_says_what_it_is_waiting_for(self):
        rid = self._scheduled(name='Held Show')
        self._wait_on_slot(rid)
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('<span class="d-k">Waiting for</span>'
                      'a connection slot on &#34;One Slot&#34;', html)


if __name__ == '__main__':
    unittest.main()
