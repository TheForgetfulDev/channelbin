"""Account blocks - a stretch of time during which ChannelBin keeps off an account, so a TV
can watch live on it while ChannelBin records from the others (dev/changelog/1151).

The properties guarded here, each of which fails in its own way:

  * a block covers everything that opens a stream: the slot acquire refuses (so a health
    check is skipped and never scored, and a single-channel start waits), and every place a
    group member is chosen drops members on a blocked account;
  * a block set on a recording takes that recording's window, read live - it starts when the
    recording starts and ends the moment the recording stops capturing;
  * a recording with nowhere unblocked to go waits for the block to end, and when the block
    outlasts its window it ends FAILED with its own reason and no alert;
  * a block added while a recording is running moves a group recording to a member on
    another account, and the member it left is neither scored, demoted nor burned; with no
    alternative the recording stays and says so, once;
  * the routes set, bound and end blocks, and a recording or account delete takes its blocks
    with it.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_account_blocks
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, recorder  # noqa: E402
from app import account_blocks as blocks  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.database import (  # noqa: E402
    AccountBlock, Alert, Channel, ChannelEvent, ChannelGroupMember, ChannelTest, Recording,
    RecordingEvent, DIAGNOSTICS, GROUP_FAILOVER, GROUP_MEMBER_SELECTED, RECORDING_EDITED,
    RECORDING_START_DEFERRED, REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS,
    REC_STATUS_COMPLETED, REC_STATUS_FAILED, FAILURE_ACCOUNT_BLOCKED,
)
from app.tz_utils import local_input_value  # noqa: E402


def _events(recording_id, event_type):
    return RecordingEvent.query.filter_by(recording_id=recording_id,
                                          event_type=event_type).order_by(RecordingEvent.id).all()


def _block_deferrals(recording_id):
    return [e for e in _events(recording_id, RECORDING_START_DEFERRED)
            if json.loads(e.extra_data or '{}').get('kind') == 'account_block']


class _Case(unittest.TestCase):
    """Two single-connection accounts - the TV's and a spare - with one channel each in a
    group. The TV account's member is the better one on the ranking, so any preference for
    the spare has to come from the block."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False},
        }})
        self.tv = seed.make_account(name='TV Account', max_connections=1)
        self.spare = seed.make_account(name='Spare Account', max_connections=1)
        self.ch_tv = seed.make_channel(self.tv, name='TV Feed', health_score=100)
        self.ch_spare = seed.make_channel(self.spare, name='Spare Feed', health_score=80)
        self.group = seed.make_group(name='The Game', members=[self.ch_tv, self.ch_spare])
        db.session.commit()
        self.tv_id, self.spare_id = self.tv.id, self.spare.id
        self.ch_tv_id, self.ch_spare_id = self.ch_tv.id, self.ch_spare.id
        self.group_id = self.group.id
        connlim._holders.clear()

    def tearDown(self):
        connlim._holders.clear()
        self.t.cleanup()

    def _block(self, account_id, minutes=60, start_offset=-1, slots=None):
        now = datetime.utcnow()
        return blocks.add_account_block(account_id, now + timedelta(minutes=minutes),
                                        start=now + timedelta(minutes=start_offset),
                                        slots=slots)

    def _scheduled(self, channel_id=None, group_id=None, start_offset=-30, minutes=60):
        now = datetime.utcnow()
        start = now + timedelta(seconds=start_offset)
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='the game',
                                  channel_id=channel_id, group_id=group_id,
                                  start_time=start, stop_time=start + timedelta(minutes=minutes))
        db.session.commit()
        return rec.id


class BlockWindowTests(_Case):
    """What counts as blocked, and when."""

    def test_an_account_block_covers_its_own_window_only(self):
        self._block(self.tv_id, minutes=60, start_offset=30)
        self.assertEqual(blocks.blocked_account_ids([self.tv_id]), set(),
                         'a block that has not started yet blocks nothing')
        later = datetime.utcnow() + timedelta(minutes=45)
        self.assertEqual(blocks.blocked_account_ids([self.tv_id], at=later), {self.tv_id})

    def test_a_recording_block_follows_the_recording_and_ends_when_capture_does(self):
        rid = self._scheduled(channel_id=self.ch_spare_id, start_offset=600)
        blocks.set_recording_blocks(rid, {self.tv_id: None})
        self.assertEqual(blocks.blocked_account_ids([self.tv_id]), set(),
                         'the recording has not started, so neither has its block')

        rec = db.session.get(Recording, rid)
        rec.start_time = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()
        self.assertEqual(blocks.blocked_account_ids([self.tv_id]), {self.tv_id},
                         'moving the recording moves its block - no copied times')

        rec.status = REC_STATUS_COMPLETED
        db.session.commit()
        self.assertEqual(blocks.blocked_account_ids([self.tv_id]), set(),
                         'a recording that stopped capturing frees the account at once')

    def test_a_partial_block_leaves_the_rest_of_the_limit(self):
        roomy = seed.make_account(name='Roomy', max_connections=2)
        db.session.commit()
        self._block(roomy.id, slots=1)
        self.assertEqual(blocks.blocked_account_ids([roomy.id]), set(),
                         'one of two connections blocked leaves the account usable')
        self.assertTrue(connlim.try_acquire(roomy.id, 'recording', 1))
        self.assertFalse(connlim.try_acquire(roomy.id, 'recording', 2),
                         'the blocked connection is not handed out')

    def test_free_at_names_when_the_block_ends(self):
        self._block(self.tv_id, minutes=90)
        until = blocks.free_at(self.tv_id)
        self.assertIsNotNone(until)
        self.assertAlmostEqual((until - datetime.utcnow()).total_seconds(), 90 * 60, delta=5)
        self.assertIsNone(blocks.free_at(self.spare_id))


class SlotRefusalTests(_Case):
    """Where a connection is taken, a blocked account has no slot."""

    def test_the_acquire_refuses_on_a_blocked_account(self):
        self._block(self.tv_id)
        self.assertFalse(connlim.try_acquire(self.tv_id, 'test', self.ch_tv_id))
        self.assertTrue(connlim.at_limit(self.tv_id))
        self.assertTrue(connlim.try_acquire(self.spare_id, 'test', self.ch_spare_id))

    def test_a_health_check_on_a_blocked_account_is_skipped_and_never_scored(self):
        from app import channel_tester
        self._block(self.tv_id)
        before = db.session.get(Channel, self.ch_tv_id).health_score
        self.assertIsNone(channel_tester.run_channel_test(self.t.app, self.ch_tv_id))
        db.session.expire_all()
        self.assertEqual(ChannelTest.query.filter_by(channel_id=self.ch_tv_id).count(), 0)
        self.assertEqual(db.session.get(Channel, self.ch_tv_id).health_score, before)


class RecordStartTests(_Case):

    def test_a_group_skips_the_blocked_member_even_though_it_ranks_first(self):
        self._block(self.tv_id)
        rid = self._scheduled(group_id=self.group_id)
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.channel_id, self.ch_spare_id)
        self.assertEqual(rec.status, REC_STATUS_IN_PROGRESS)
        ev = _events(rid, GROUP_MEMBER_SELECTED)[0]
        self.assertIn('blocked account', ev.detail)
        self.assertIn('TV Feed', ev.detail)
        self.assertEqual(json.loads(ev.extra_data)['skipped_blocked_channel_ids'],
                         [self.ch_tv_id])

    def test_every_member_blocked_waits_says_so_once_and_raises_no_alert(self):
        self._block(self.tv_id)
        self._block(self.spare_id)
        rid = self._scheduled(group_id=self.group_id)
        with mock.patch('app.scheduler.reschedule_recording_start') as resched, \
             mock.patch.object(recorder, '_launch_segment') as launch:
            recorder.start_recording(self.t.app, rid)
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.status, REC_STATUS_SCHEDULED)
        self.assertFalse(launch.called)
        self.assertTrue(resched.called)
        self.assertEqual(len(_block_deferrals(rid)), 1)
        self.assertEqual(rec.start_deferred_for, 'the block on "TV Account" to end')
        self.assertEqual(Alert.query.count(), 0, 'the user set the block - nothing to alert')
        self.assertEqual(len(_events(rid, GROUP_MEMBER_SELECTED)), 1,
                         'a retry that picks the same member does not write it again')

    def test_the_wait_retries_when_the_block_ends_not_every_poll(self):
        self._block(self.ch_tv.account_id, minutes=3)
        rid = self._scheduled(channel_id=self.ch_tv_id)
        with mock.patch('app.scheduler.reschedule_recording_start') as resched, \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rid)
        retry_at = resched.call_args[0][1]
        self.assertAlmostEqual((retry_at - datetime.utcnow()).total_seconds(), 180, delta=5)

    def test_a_block_that_outlasts_the_window_ends_failed_with_its_own_reason(self):
        self._block(self.tv_id, minutes=120)
        rid = self._scheduled(channel_id=self.ch_tv_id, start_offset=-3600, minutes=59)
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rid)

        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(rec.status, REC_STATUS_FAILED)
        self.assertEqual(rec.failure_reason, FAILURE_ACCOUNT_BLOCKED)
        self.assertEqual(Alert.query.count(), 0)

    def test_a_block_set_on_the_recording_itself_is_honored_at_its_start(self):
        rid = self._scheduled(group_id=self.group_id)
        blocks.set_recording_blocks(rid, {self.tv_id: None})
        with mock.patch('app.scheduler.reschedule_recording_start'), \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rid)
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).channel_id, self.ch_spare_id)


class _LiveCase(_Case):
    """A group recording running on the TV account's member when the block arrives."""

    def setUp(self):
        super().setUp()
        now = datetime.utcnow()
        rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='live',
                                  channel_id=self.ch_tv_id, group_id=self.group_id,
                                  start_time=now - timedelta(minutes=10),
                                  stop_time=now + timedelta(hours=1))
        db.session.commit()
        self.rid = rec.id
        self.assertTrue(connlim.try_acquire(self.tv_id, 'recording', self.rid))
        self.state = recorder.RecordingState(current_segment_num=1)
        recorder._active[self.rid] = self.state

    def tearDown(self):
        recorder._active.pop(self.rid, None)
        super().tearDown()


class FailoverTests(_LiveCase):

    def test_a_block_move_lands_on_another_account_and_scores_nothing(self):
        self._block(self.tv_id)
        before = db.session.get(Channel, self.ch_tv_id).health_score
        self.assertTrue(recorder.failover_group_member(
            self.t.app, self.rid, 'Account "TV Account" is blocked', block_move=True))

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).channel_id, self.ch_spare_id)
        self.assertEqual(db.session.get(Channel, self.ch_tv_id).health_score, before)
        self.assertEqual(ChannelEvent.query.filter_by(channel_id=self.ch_tv_id).count(), 0,
                         'a block move writes no health observation')
        self.assertNotIn(self.ch_tv_id, self.state.failed_member_ids)
        self.assertNotIn(self.ch_tv_id, self.state.demoted_member_ids)
        ev = _events(self.rid, GROUP_FAILOVER)[0]
        self.assertTrue(json.loads(ev.extra_data)['block_move'])
        self.assertNotIn(('recording', self.rid), connlim._holders.get(self.tv_id, []),
                         'the slot on the blocked account is handed back')

    def test_a_dead_feed_failover_passes_over_a_blocked_account(self):
        """The better-ranked spare is blocked; a third, weaker member on an open account is
        what the failover must reach. Ranking the blocked one first would walk into the
        slot refusal and end the move with a member still available."""
        third_acct = seed.make_account(name='Third Account', max_connections=1)
        third = seed.make_channel(third_acct, name='Third Feed', health_score=40)
        db.session.add(ChannelGroupMember(group_id=self.group_id, channel_id=third.id,
                                          position=2, recording_enabled=True,
                                          test_enabled=True))
        db.session.commit()
        third_id = third.id
        self._block(self.spare_id)
        self.assertTrue(recorder.failover_group_member(self.t.app, self.rid, 'stream died'))
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).channel_id, third_id)
        ev = _events(self.rid, GROUP_FAILOVER)[0]
        self.assertIn('blocked account', ev.detail)


class WatchdogBlockCheckTests(_LiveCase):
    """app/watchdog.py::_check_account_block - the block-after-start path."""

    def _check(self):
        from app.watchdog import WatchdogThread
        wd = WatchdogThread(self.rid, self.state, self.t.app)
        return wd

    def test_a_block_added_mid_recording_moves_the_group_to_another_account(self):
        wd = self._check()
        self.assertFalse(wd._check_account_block(1), 'nothing is blocked yet')
        self._block(self.tv_id)
        wd._block_check_at = 0
        with mock.patch('app.watchdog.terminate_or_kill') as kill, \
             mock.patch.object(recorder, '_close_active_segment') as close, \
             mock.patch.object(recorder, '_launch_segment') as launch:
            self.assertTrue(wd._check_account_block(1))
        kill.assert_called_once()
        close.assert_called_once_with(self.t.app, self.rid, exit_reason='ACCOUNT_BLOCKED')
        launch.assert_called_once_with(self.t.app, self.rid, 2)
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).channel_id, self.ch_spare_id)

    def test_with_nowhere_to_go_it_stays_and_says_so_once(self):
        self._block(self.tv_id)
        self._block(self.spare_id)
        wd = self._check()
        with mock.patch('app.watchdog.terminate_or_kill') as kill, \
             mock.patch.object(recorder, '_launch_segment') as launch:
            self.assertFalse(wd._check_account_block(1))
            wd._block_check_at = 0
            self.assertFalse(wd._check_account_block(1))
        self.assertFalse(kill.called, 'a live capture is never stopped by a block')
        self.assertFalse(launch.called)
        stays = [e for e in _events(self.rid, DIAGNOSTICS)
                 if json.loads(e.extra_data)['kind'] == 'account_block_stayed']
        self.assertEqual(len(stays), 1)
        self.assertIn('keeps recording', stays[0].detail)


class ServingMemberTests(_Case):

    def test_the_guide_row_names_the_member_a_start_would_use(self):
        from app.channel_groups import blocked_account_ids_now, serving_member
        self.assertEqual(serving_member(self.group).member.id, self.ch_tv_id)
        self._block(self.tv_id)
        self.assertEqual(serving_member(self.group, blocked_account_ids=blocked_account_ids_now())
                         .member.id, self.ch_spare_id)


class RouteTests(_Case):

    def test_blocking_an_account_from_its_page(self):
        resp = self.t.client.post(f'/api/accounts/{self.tv_id}/blocks',
                                  json={'minutes': 120})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertIn('blocked until', resp.get_json()['message'])
        self.assertEqual(blocks.blocked_account_ids([self.tv_id]), {self.tv_id})

    def test_a_block_is_bounded(self):
        resp = self.t.client.post(f'/api/accounts/{self.tv_id}/blocks',
                                  json={'minutes': 25 * 60})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(AccountBlock.query.count(), 0)

    def test_unblock_ends_it_and_rearms_waiting_starts(self):
        bid = self._block(self.tv_id)
        with mock.patch('app.account_blocks.rearm_waiting_starts') as rearm, \
             mock.patch('app.routes.accounts.rearm_waiting_starts', rearm):
            resp = self.t.client.delete(f'/api/accounts/{self.tv_id}/blocks/{bid}')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue(rearm.called)
        self.assertEqual(blocks.blocked_account_ids([self.tv_id]), set())

    def _post_new(self, **extra):
        start = datetime.utcnow() + timedelta(hours=2)
        data = {
            'name': 'Big Game', 'url': 'http://example.test/live/1',
            'start_time': local_input_value(start),
            'stop_time': local_input_value(start + timedelta(hours=1)),
            'channel_id': str(self.ch_tv_id), 'group_id': str(self.group_id),
            'block_accounts_loaded': '1',
        }
        data.update(extra)
        with mock.patch('app.routes.recordings.schedule_recording'):
            return self.t.client.post('/recordings/new-json', data=data)

    def test_scheduling_with_a_block_saves_it_and_restamps_the_group_member(self):
        resp = self._post_new(block_account_ids=str(self.tv_id))
        self.assertEqual(resp.status_code, 200, resp.get_json())
        rec = Recording.query.filter_by(name='Big Game').first()
        self.assertEqual(blocks.recording_blocks(rec.id), {self.tv_id: None})
        self.assertEqual(rec.channel_id, self.ch_spare_id,
                         'the stamp skips the member on the account being blocked')
        ev = _events(rec.id, RECORDING_EDITED)[0]
        self.assertIn('TV Account', ev.detail)

    def test_blocking_the_only_account_warns_before_saving(self):
        resp = self._post_new(group_id='', block_account_ids=str(self.tv_id))
        body = resp.get_json()
        self.assertIs(body['success'], False)
        self.assertIn('will not record anything', body['account_block_warning']['message'])
        self.assertEqual(Recording.query.filter_by(name='Big Game').count(), 0)

    def test_an_edit_whose_picker_never_loaded_leaves_blocks_alone(self):
        rid = self._scheduled(channel_id=self.ch_spare_id, start_offset=7200)
        blocks.set_recording_blocks(rid, {self.tv_id: None})
        rec = db.session.get(Recording, rid)
        data = {'name': rec.name, 'url': rec.url,
                'start_time': local_input_value(rec.start_time),
                'stop_time': local_input_value(rec.stop_time)}
        with mock.patch('app.routes.recordings._apply_edit_and_reschedule'):
            resp = self.t.client.post(f'/recordings/{rid}/edit-json', data=data)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(blocks.recording_blocks(rid), {self.tv_id: None})

    def test_the_in_progress_picker_replaces_a_running_recordings_blocks(self):
        rid = self._scheduled(channel_id=self.ch_spare_id)
        rec = db.session.get(Recording, rid)
        rec.status = REC_STATUS_IN_PROGRESS
        db.session.commit()
        resp = self.t.client.post(f'/api/recordings/{rid}/account-blocks',
                                  json={'blocks': [{'account_id': self.tv_id, 'slots': None}]})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(blocks.recording_blocks(rid), {self.tv_id: None})


class PageTests(_Case):
    """Every surface that shows a block says until when, and offers the way out."""

    def test_the_account_page_names_the_block_and_offers_unblock(self):
        bid = self._block(self.tv_id, minutes=90)
        html = self.t.client.get(f'/accounts/{self.tv_id}').get_data(as_text=True)
        self.assertIn('Account use is blocked', html)
        self.assertIn(f'data-block-id="{bid}"', html)
        self.assertIn('Blocked until', html)

    def test_the_accounts_list_badges_only_the_blocked_account(self):
        self._block(self.tv_id)
        html = self.t.client.get('/accounts').get_data(as_text=True)
        self.assertEqual(html.count('Blocked until'), 1)
        self.assertIn('data-act="block"', html)

    def test_the_recording_page_names_the_accounts_it_blocks(self):
        rid = self._scheduled(channel_id=self.ch_spare_id, start_offset=7200)
        blocks.set_recording_blocks(rid, {self.tv_id: None})
        html = self.t.client.get(f'/recordings/{rid}').get_data(as_text=True)
        self.assertIn('Blocks account use', html)
        self.assertIn('&#34;TV Account&#34;', html)

    def test_a_recording_that_never_started_for_a_block_says_so(self):
        rid = self._scheduled(channel_id=self.ch_tv_id)
        rec = db.session.get(Recording, rid)
        rec.status, rec.failure_reason = REC_STATUS_FAILED, FAILURE_ACCOUNT_BLOCKED
        rec.completed_at = datetime.utcnow()
        db.session.commit()
        html = self.t.client.get(f'/recordings/{rid}').get_data(as_text=True)
        self.assertIn('<b>blocked</b>', html)


class TeardownTests(_Case):

    def test_deleting_a_recording_takes_its_blocks(self):
        rid = self._scheduled(channel_id=self.ch_spare_id, start_offset=7200)
        blocks.set_recording_blocks(rid, {self.tv_id: None})
        with mock.patch('app.routes.recordings.unschedule_recording'):
            resp = self.t.client.post(f'/recordings/{rid}/delete-json', json={})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(AccountBlock.query.filter_by(recording_id=rid).count(), 0)

    def test_deleting_an_account_takes_its_blocks(self):
        self._block(self.tv_id)
        resp = self.t.client.delete(f'/api/accounts/{self.tv_id}')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(AccountBlock.query.filter_by(account_id=self.tv_id).count(), 0)


class PreCheckTests(_Case):

    def test_a_pre_check_whose_target_is_blocked_at_start_is_skipped(self):
        from app import channel_tester
        rid = self._scheduled(channel_id=self.ch_tv_id, start_offset=3600)
        blocks.set_recording_blocks(rid, {self.tv_id: None})
        cfg = {'channel_testing': {'pre_check': {'enabled': True}}}
        with mock.patch('app.config.load_config',
                        side_effect=lambda *a, **k: _merged(cfg)), \
             mock.patch.object(channel_tester, '_pre_check_skip') as skip, \
             mock.patch.object(channel_tester, 'run_channel_test') as run:
            channel_tester.run_pre_check(self.t.app, rid)
        self.assertFalse(run.called, 'no connection is opened on a blocked account')
        self.assertIn('blocked', skip.call_args[0][3])


def _merged(overrides):
    from app.config import _DEFAULTS
    import copy
    cfg = copy.deepcopy(_DEFAULTS)
    for k, v in overrides.items():
        cfg.setdefault(k, {}).update(v)
    return cfg


if __name__ == '__main__':
    unittest.main()
