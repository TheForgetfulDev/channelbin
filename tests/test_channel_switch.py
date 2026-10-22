"""Switch channel and Block accounts on a running group recording (dev/changelog/1254).

The route records intent and the recording's watchdog makes the move - the one thread that
stops an ffmpeg and launches the next segment - so a user's switch cannot race the
watchdog's own failover or block check. What the dialog lists (switch_choices) is the same
reading the move makes (failover_group_member with a target); a block set from the page is
answered before it is saved (block_move_preview) and carried out at once rather than at the
watchdog's next 15 s cadence. The channel left is neither burned, demoted nor scored, and
the leave-behind answer goes through the one participation writer or the one block writer.
"""
import json
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, recorder  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app import account_blocks as blocks  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, RecordingSegment, Channel, ChannelEvent, ChannelGroupMember,
    ChannelGroupEvent, AccountBlock, GROUP_FAILOVER, DIAGNOSTICS, RECORDING_EDITED,
    GROUP_MEMBER_PARTICIPATION, REC_STATUS_IN_PROGRESS, REC_STATUS_SCHEDULED,
    TEST_STATUS_COMPLETED,
)
from app.routes import recordings as rec_routes  # noqa: E402


def _events(rid, event_type):
    return (RecordingEvent.query.filter_by(recording_id=rid, event_type=event_type)
            .order_by(RecordingEvent.id).all())


class _LiveGroupCase(unittest.TestCase):
    """A group recording running on the TV account's member, with a spare on another
    account, a third on a third account, and one member whose Recording switch is off."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': os.path.join(self.t._tmpdir, 'dvr'),
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False},
        }})
        self.tv = seed.make_account(name='TV Account', max_connections=1)
        self.spare = seed.make_account(name='Spare Account', max_connections=1)
        self.third_acct = seed.make_account(name='Third Account', max_connections=1)
        self.ch_tv = seed.make_channel(self.tv, name='TV Feed', health_score=100)
        self.ch_spare = seed.make_channel(self.spare, name='Spare Feed', health_score=80)
        self.ch_third = seed.make_channel(self.third_acct, name='Third Feed', health_score=60)
        self.ch_off = seed.make_channel(self.spare, name='Off Feed', health_score=70)
        self.group = seed.make_group(name='The Game',
                                     members=[self.ch_tv, self.ch_spare, self.ch_third, self.ch_off],
                                     disabled=[self.ch_off.id])
        now = datetime.utcnow()
        rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='live',
                                  channel_id=self.ch_tv.id, group_id=self.group.id,
                                  start_time=now - timedelta(minutes=10),
                                  stop_time=now + timedelta(hours=1))
        db.session.add(RecordingSegment(recording_id=rec.id, segment_number=1,
                                        channel_id=self.ch_tv.id,
                                        file_path=os.path.join(self.t._tmpdir, 'seg1.ts'),
                                        started_at=now - timedelta(minutes=10)))
        db.session.commit()
        self.rid = rec.id
        self.tv_id, self.spare_id, self.third_id = self.tv.id, self.spare.id, self.third_acct.id
        self.ch_tv_id, self.ch_spare_id = self.ch_tv.id, self.ch_spare.id
        self.ch_third_id, self.ch_off_id = self.ch_third.id, self.ch_off.id
        self.group_id = self.group.id
        connlim._holders.clear()
        self.assertTrue(connlim.try_acquire(self.tv_id, 'recording', self.rid))
        self.state = recorder.RecordingState(current_segment_num=1)
        recorder._active[self.rid] = self.state

    def tearDown(self):
        recorder._active.pop(self.rid, None)
        connlim._holders.clear()
        self.t.cleanup()

    def _choices(self):
        return {c.channel.id: c for c in recorder.switch_choices(self.rid)}

    def _busy_third(self):
        """Another recording holds the third account's one slot."""
        other = seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='other',
                                    channel_id=self.ch_third_id)
        db.session.commit()
        self.assertTrue(connlim.try_acquire(self.third_id, 'recording', other.id))
        return other.id

    def _pin(self, resolution='1920x1080', fps=59.94):
        """This recording's format pin: its first segment probed."""
        seg = RecordingSegment.query.filter_by(recording_id=self.rid).first()
        seg.probe_resolution, seg.probe_fps = resolution, fps
        db.session.commit()

    def _tested(self, channel_id, resolution, fps):
        seed.make_channel_test(db.session.get(Channel, channel_id), all_null=False,
                               status=TEST_STATUS_COMPLETED, resolution=resolution, fps=fps)
        db.session.commit()


class SwitchChoicesTests(_LiveGroupCase):
    """What the dialog lists, and why each row can or cannot be picked."""

    def test_the_current_member_is_listed_and_not_selectable(self):
        c = self._choices()[self.ch_tv_id]
        self.assertFalse(c.ok)
        self.assertIn('Recording from it now', c.why)

    def test_a_recording_off_member_is_not_listed_at_all(self):
        self.assertNotIn(self.ch_off_id, self._choices())

    def test_a_free_member_is_selectable(self):
        c = self._choices()[self.ch_spare_id]
        self.assertTrue(c.ok)
        self.assertFalse(c.warn)

    def test_a_member_that_failed_this_run_is_not_selectable(self):
        self.state.failed_member_ids.add(self.ch_spare_id)
        c = self._choices()[self.ch_spare_id]
        self.assertFalse(c.ok)
        self.assertIn('died earlier in this recording', c.why)

    def test_a_member_on_a_blocked_account_is_not_selectable(self):
        blocks.set_recording_blocks(self.rid, {self.spare_id: None})
        c = self._choices()[self.ch_spare_id]
        self.assertFalse(c.ok)
        self.assertIn('blocked for the rest of this recording', c.why)

    def test_a_member_on_an_account_at_its_limit_is_not_selectable(self):
        self._busy_third()
        c = self._choices()[self.ch_third_id]
        self.assertFalse(c.ok)
        self.assertIn('connection limit', c.why)

    def test_a_different_format_is_selectable_and_warned(self):
        self._pin('1920x1080', 59.94)
        self._tested(self.ch_spare_id, '1280x720', 59.94)
        self._tested(self.ch_third_id, '1920x1080', 59.94)
        spare, third = self._choices()[self.ch_spare_id], self._choices()[self.ch_third_id]
        self.assertTrue(spare.ok and spare.warn, spare)
        self.assertIn('changes format', spare.why)
        self.assertTrue(third.ok and not third.warn, third)
        self.assertIn('Same format', third.why)


class ManualMoveTests(_LiveGroupCase):
    """failover_group_member(target_channel_id=...): the user's pick, no ranking."""

    def test_the_pick_moves_the_recording_and_scores_nothing(self):
        before = db.session.get(Channel, self.ch_tv_id).health_score
        self.assertTrue(recorder.failover_group_member(
            self.t.app, self.rid, 'you asked for it', target_channel_id=self.ch_third_id))
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).channel_id, self.ch_third_id,
                         'the pick wins over the better-scoring spare')
        self.assertEqual(db.session.get(Channel, self.ch_tv_id).health_score, before)
        self.assertEqual(ChannelEvent.query.filter_by(channel_id=self.ch_tv_id).count(), 0)
        self.assertNotIn(self.ch_tv_id, self.state.failed_member_ids)
        self.assertNotIn(self.ch_tv_id, self.state.demoted_member_ids)
        ev = _events(self.rid, GROUP_FAILOVER)[0]
        extra = json.loads(ev.extra_data)
        self.assertTrue(extra['manual'])
        self.assertFalse(extra['format_mismatch'])
        self.assertIn('you asked to move from "TV Feed" to "Third Feed"', ev.detail)
        self.assertIn('not scored for the move', ev.detail)
        self.assertNotIn(('recording', self.rid), connlim._holders.get(self.tv_id, []),
                         'the seat moves with the recording')

    def test_a_pick_that_died_since_the_dialog_is_refused_and_said(self):
        self.state.failed_member_ids.add(self.ch_spare_id)
        self.assertFalse(recorder.failover_group_member(
            self.t.app, self.rid, 'you asked for it', target_channel_id=self.ch_spare_id))
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).channel_id, self.ch_tv_id)
        ev = _events(self.rid, DIAGNOSTICS)[-1]
        self.assertEqual(json.loads(ev.extra_data)['kind'], 'switch_refused')
        self.assertIn('its feed already died', ev.detail)

    def test_a_format_change_is_named_not_refused(self):
        self._pin('1920x1080', 59.94)
        self._tested(self.ch_spare_id, '1280x720', 59.94)
        self.assertTrue(recorder.failover_group_member(
            self.t.app, self.rid, 'you asked for it', target_channel_id=self.ch_spare_id))
        ev = _events(self.rid, GROUP_FAILOVER)[0]
        self.assertTrue(json.loads(ev.extra_data)['format_mismatch'])
        self.assertIn('changes format here rather than refusing your pick', ev.detail)


class WatchdogMoveRequestTests(_LiveGroupCase):
    """app/watchdog.py::_check_move_request - the request is carried out on the watchdog."""

    def _wd(self):
        from app.watchdog import WatchdogThread
        return WatchdogThread(self.rid, self.state, self.t.app)

    def test_a_switch_request_kills_closes_and_launches_on_the_watchdog(self):
        req = recorder.MoveRequest(target_channel_id=self.ch_spare_id)
        self.state.move_request = req
        wd = self._wd()
        with mock.patch('app.watchdog.terminate_or_kill') as kill, \
             mock.patch.object(recorder, '_close_active_segment') as close, \
             mock.patch.object(recorder, '_launch_segment') as launch:
            self.assertTrue(wd._check_move_request(1))
        kill.assert_called_once()
        close.assert_called_once_with(self.t.app, self.rid, exit_reason='CHANNEL_SWITCHED')
        launch.assert_called_once_with(self.t.app, self.rid, 2)
        self.assertTrue(req.done.is_set() and req.moved)
        self.assertIsNone(self.state.move_request, 'consumed, never retried')
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).channel_id, self.ch_spare_id)

    def test_a_refused_switch_answers_without_touching_the_capture(self):
        self.state.failed_member_ids.add(self.ch_spare_id)
        req = recorder.MoveRequest(target_channel_id=self.ch_spare_id)
        self.state.move_request = req
        with mock.patch('app.watchdog.terminate_or_kill') as kill, \
             mock.patch.object(recorder, '_launch_segment') as launch:
            self.assertFalse(self._wd()._check_move_request(1))
        self.assertFalse(kill.called)
        self.assertFalse(launch.called)
        self.assertTrue(req.done.is_set())
        self.assertFalse(req.moved)

    def test_a_block_request_runs_the_block_check_now(self):
        blocks.set_recording_blocks(self.rid, {self.tv_id: None})
        req = recorder.MoveRequest()
        self.state.move_request = req
        wd = self._wd()
        wd._block_check_at = time.monotonic() + 999   # not due for a long time
        with mock.patch('app.watchdog.terminate_or_kill'), \
             mock.patch.object(recorder, '_close_active_segment') as close, \
             mock.patch.object(recorder, '_launch_segment'):
            self.assertTrue(wd._check_move_request(1))
        close.assert_called_once_with(self.t.app, self.rid, exit_reason='ACCOUNT_BLOCKED')
        self.assertTrue(req.done.is_set() and req.moved)


class _RouteCase(_LiveGroupCase):
    """Routes that leave a request for the watchdog: a stand-in watchdog answers it."""

    def _answer_requests(self, outcome=True):
        """A thread that plays the watchdog: waits for the request and answers it the way
        _check_move_request does, through the real move with the capture stubbed."""
        from app.watchdog import WatchdogThread
        wd = WatchdogThread(self.rid, self.state, self.t.app)

        def _run():
            deadline = time.monotonic() + 5
            while self.state.move_request is None and time.monotonic() < deadline:
                time.sleep(0.02)
            with self.t.app.app_context():
                with mock.patch('app.watchdog.terminate_or_kill'), \
                     mock.patch.object(recorder, '_close_active_segment'), \
                     mock.patch.object(recorder, '_launch_segment'):
                    wd._check_move_request(1)
        th = threading.Thread(target=_run, daemon=True)
        th.start()
        return th


class SwitchRouteTests(_RouteCase):

    def test_options_list_what_the_dialog_shows(self):
        resp = self.t.client.get(f'/api/recordings/{self.rid}/switch-options')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertEqual(body['current']['name'], 'TV Feed')
        names = {c['name']: c for c in body['choices']}
        self.assertNotIn('Off Feed', names)
        self.assertFalse(names['TV Feed']['ok'])
        self.assertTrue(names['Spare Feed']['ok'])
        self.assertEqual(names['Spare Feed']['account']['name'], 'Spare Account')
        self.assertEqual(names['Spare Feed']['score'], 80)

    def test_the_score_is_the_chips_whole_number(self):
        ch = db.session.get(Channel, self.ch_spare_id)
        ch.health_score = 84.48853940520299
        db.session.commit()
        body = self.t.client.get(f'/api/recordings/{self.rid}/switch-options').get_json()
        row = next(c for c in body['choices'] if c['name'] == 'Spare Feed')
        self.assertEqual(row['score'], 84)
        self.assertIsInstance(row['score'], int)

    def test_options_refuse_a_recording_that_is_not_capturing(self):
        rec = db.session.get(Recording, self.rid)
        rec.status = REC_STATUS_SCHEDULED
        db.session.commit()
        resp = self.t.client.get(f'/api/recordings/{self.rid}/switch-options')
        self.assertEqual(resp.status_code, 400)

    def test_the_route_refuses_what_the_dialog_would_not_offer(self):
        for body, needle in (({'channel_id': self.ch_off_id}, 'not one this group records from'),
                             ({'channel_id': self.ch_tv_id}, 'Recording from it now'),
                             ({'channel_id': self.ch_spare_id, 'leave': 'sideways'}, 'leave must be'),
                             ({}, 'Pick the channel')):
            resp = self.t.client.post(f'/api/recordings/{self.rid}/switch-channel', json=body)
            self.assertEqual(resp.status_code, 400, body)
            self.assertIn(needle, resp.get_json()['error'])
        self.assertIsNone(self.state.move_request, 'nothing was asked of the watchdog')

    def test_a_switch_is_made_by_the_watchdog_and_reported(self):
        th = self._answer_requests()
        resp = self.t.client.post(f'/api/recordings/{self.rid}/switch-channel',
                                  json={'channel_id': self.ch_spare_id, 'leave': 'keep'})
        th.join(timeout=10)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertTrue(body['moved'])
        self.assertEqual((body['from'], body['to']), ('TV Feed', 'Spare Feed'))
        self.assertIn('you asked to move from "TV Feed" to "Spare Feed"', body['message'])
        self.assertEqual(body['left'], '')
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).channel_id, self.ch_spare_id)
        m = ChannelGroupMember.query.filter_by(group_id=self.group_id, channel_id=self.ch_tv_id).one()
        self.assertTrue(m.recording_enabled, 'keep leaves the switch alone')

    def test_leaving_with_recording_off_goes_through_the_participation_writer(self):
        th = self._answer_requests()
        resp = self.t.client.post(f'/api/recordings/{self.rid}/switch-channel',
                                  json={'channel_id': self.ch_spare_id, 'leave': 'off'})
        th.join(timeout=10)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertIn('Recording is now off for "TV Feed"', resp.get_json()['left'])
        db.session.expire_all()
        m = ChannelGroupMember.query.filter_by(group_id=self.group_id, channel_id=self.ch_tv_id).one()
        self.assertFalse(m.recording_enabled)
        ev = ChannelGroupEvent.query.filter_by(group_id=self.group_id, channel_id=self.ch_tv_id,
                                               event_type=GROUP_MEMBER_PARTICIPATION).one()
        self.assertEqual(json.loads(ev.extra_data)['surface'], 'recording_page')
        self.assertIn("Switch channel dialog", ev.detail)

    def test_leaving_with_a_block_goes_through_the_block_writer(self):
        th = self._answer_requests()
        resp = self.t.client.post(f'/api/recordings/{self.rid}/switch-channel',
                                  json={'channel_id': self.ch_spare_id, 'leave': 'block'})
        th.join(timeout=10)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertIn('"TV Account" is blocked', resp.get_json()['left'])
        self.assertEqual(blocks.recording_blocks(self.rid), {self.tv_id: None})
        self.assertTrue(any('Blocks account use' in e.detail for e in _events(self.rid, RECORDING_EDITED)))

    def test_a_watchdog_that_does_not_answer_is_said_not_guessed(self):
        with mock.patch.object(rec_routes, '_MOVE_WAIT_SECONDS', 0.2):
            resp = self.t.client.post(f'/api/recordings/{self.rid}/switch-channel',
                                      json={'channel_id': self.ch_spare_id})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertIsNone(body['moved'])
        self.assertIn('next check', body['message'])
        self.assertIsNotNone(self.state.move_request, 'still waiting for the watchdog')
        m = ChannelGroupMember.query.filter_by(group_id=self.group_id, channel_id=self.ch_tv_id).one()
        self.assertTrue(m.recording_enabled, 'nothing else changes until the move is confirmed')


class BlockPreviewTests(_RouteCase):

    def _preview(self, *account_ids):
        q = json.dumps([{'account_id': a, 'slots': None} for a in account_ids])
        resp = self.t.client.get(f'/api/recordings/{self.rid}/block-preview?blocks={q}')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()

    def test_a_block_elsewhere_does_not_touch_the_recording(self):
        self.assertFalse(self._preview(self.spare_id)['affects'])

    def test_a_block_on_the_live_account_names_where_the_recording_goes(self):
        p = self._preview(self.tv_id)
        self.assertTrue(p['affects'])
        self.assertEqual(p['to']['name'], 'Spare Feed')
        self.assertFalse(p['held'])
        self.assertEqual(p['current']['name'], 'TV Feed')

    def test_a_candidate_on_a_full_account_is_held(self):
        self._busy_third()
        p = self._preview(self.tv_id, self.spare_id)
        self.assertTrue(p['affects'])
        self.assertEqual(p['to']['name'], 'Third Feed')
        self.assertTrue(p['held'])

    def test_nowhere_to_go_is_said(self):
        p = self._preview(self.tv_id, self.spare_id, self.third_id)
        self.assertTrue(p['affects'])
        self.assertIsNone(p['to'])

    def test_a_block_from_the_page_is_carried_out_at_once(self):
        th = self._answer_requests()
        resp = self.t.client.post(f'/api/recordings/{self.rid}/account-blocks',
                                  json={'blocks': [{'account_id': self.tv_id, 'slots': None}]})
        th.join(timeout=10)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertTrue(body['moved'])
        self.assertEqual((body['from'], body['to']), ('TV Feed', 'Spare Feed'))
        self.assertIn('not scored for the move', body['message'])
        self.assertEqual(AccountBlock.query.filter_by(recording_id=self.rid).count(), 1)


if __name__ == '__main__':
    unittest.main()
