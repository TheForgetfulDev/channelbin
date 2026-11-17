"""A single-channel recording whose account has no connection free moves to another channel with
the same program, behind a three-level setting (app/seat_moves.py, dev/changelog/1611).

  - at start: a full account moves the recording to a free one, the event says from where, to
    where and why, and the seat is held on the new account;
  - channels whose latest check passed go first, then never-checked ones, then ones whose
    latest check failed; a failing streak, a hidden channel, a login sibling, a channel with a
    live recording and another episode or start time are never taken;
  - global, profile and recording levels, the narrowest winning; off waits as before;
  - a rule's schedule-time pick follows the setting and the same tier order;
  - the record dialog stores the per-recording answer, an edit that could not show it leaves
    it alone, and the connection warning says a move will be tried;
  - the recording's page says what it will do.

Throwaway temp SQLite DB through make_test_app(); ffmpeg is never launched.

    python3 -m unittest tests.test_seat_moves
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
from tests.support.clock import tomorrow_local_noon  # noqa: E402
from app import account_links, admission, auto_rules, db, recorder  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.database import (  # noqa: E402
    AccountLogin, Recording, RecordingEvent, RecordingProfile, RuleMatch,
    RECORDING_MOVED_FOR_CONNECTION, REC_STATUS_IN_PROGRESS, REC_STATUS_SCHEDULED,
)
from app.tz_utils import local_input_value  # noqa: E402

TITLE = 'Cup Race One'


class _StartCase(unittest.TestCase):
    """Account Full's one connection is held by another recording; the program is also on
    channels of other accounts. A recording on Full's channel is started."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.configure()
        self.full = seed.make_account(name='Full', max_connections=1)
        self.free = seed.make_account(name='Free', max_connections=1)
        self.other = seed.make_account(name='Other', max_connections=1)
        self.mine = seed.make_channel(self.full, name='Mine HD', health_score=90)
        db.session.commit()
        self.program_start = (datetime.utcnow() - timedelta(minutes=1)).replace(microsecond=0)
        self.listing(self.mine)
        connlim._holders.clear()
        connlim.try_acquire(self.full.id, 'recording', 999999)

    def tearDown(self):
        connlim._holders.clear()
        self.t.cleanup()

    def configure(self, **recording):
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False}, **recording}})

    def listing(self, channel, title=TITLE, sub_title=None, start=None):
        seed.make_epg_entry(channel, title=title, sub_title=sub_title,
                            start_time=start or self.program_start, duration_minutes=60)
        db.session.commit()

    def channel(self, account, name, tested=None, **kw):
        ch = seed.make_channel(account, name=name, **kw)
        if tested is not None:
            seed.make_channel_test(ch, all_null=False, status=tested)
        db.session.commit()
        self.listing(ch)
        return ch

    def scheduled(self, **kw):
        now = datetime.utcnow()
        rec = seed.make_recording(
            status=REC_STATUS_SCHEDULED, name='race', channel_id=self.mine.id,
            start_time=now - timedelta(seconds=30), stop_time=now + timedelta(hours=1),
            program_title=TITLE, program_start_time=self.program_start, **kw)
        db.session.commit()
        return rec.id

    def start(self, rid):
        with mock.patch('app.scheduler.reschedule_recording_start') as rearm, \
                mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rid)
        db.session.expire_all()
        return db.session.get(Recording, rid), rearm

    def moved_event(self, rid):
        return RecordingEvent.query.filter_by(
            recording_id=rid, event_type=RECORDING_MOVED_FOR_CONNECTION).first()


class StartMoveTests(_StartCase):

    def test_a_full_account_moves_the_recording_to_the_same_program_on_a_free_one(self):
        there = self.channel(self.free, 'There')
        rid = self.scheduled()
        rec, rearm = self.start(rid)
        self.assertEqual(rec.channel_id, there.id)
        self.assertEqual(rec.status, REC_STATUS_IN_PROGRESS, 'it starts now instead of waiting')
        self.assertFalse(rearm.called)
        self.assertIn(('recording', rid), connlim._holders.get(self.free.id, []))
        ev = self.moved_event(rid)
        self.assertIn('Moved from "Mine HD" (Full) to "There" (Free)', ev.detail)
        self.assertIn('it has not been health checked', ev.detail)
        self.assertIn('the global setting is to move', ev.detail)
        extra = json.loads(ev.extra_data)
        self.assertEqual((extra['from_channel_id'], extra['to_channel_id']),
                         (self.mine.id, there.id))

    def test_a_passed_check_beats_an_untested_channel_which_beats_a_failed_one(self):
        failed = self.channel(self.free, 'Failed', tested='FAILED', health_score=100)
        untested = self.channel(self.other, 'Untested', health_score=95)
        third = seed.make_account(name='Third', max_connections=1)
        passed = self.channel(third, 'Passed', tested='COMPLETED', health_score=40)
        rid = self.scheduled()
        rec, _ = self.start(rid)
        self.assertEqual(rec.channel_id, passed.id)
        self.assertIn('its latest health check passed', self.moved_event(rid).detail)
        db.session.delete(passed)  # hidden-recompute-ok: a test fixture's channel
        db.session.commit()
        connlim._holders.clear()
        connlim.try_acquire(self.full.id, 'recording', 999999)
        rid = self.scheduled()
        rec, _ = self.start(rid)
        self.assertEqual(rec.channel_id, untested.id,
                         'an unchecked channel beats one whose latest check failed')
        self.assertNotEqual(rec.channel_id, failed.id)

    def test_a_failed_check_is_still_taken_when_nothing_better_is_free(self):
        failed = self.channel(self.free, 'Failed', tested='FAILED')
        rec, _ = self.start(self.scheduled())
        self.assertEqual(rec.channel_id, failed.id)

    def test_channels_it_must_never_take(self):
        streak = self.channel(self.free, 'Streaking', tested='FAILED')
        streak.consecutive_test_failures = 99
        self.channel(self.other, 'Hidden', hidden=True)
        # The sibling lists a free login of its own ahead of the shared one, so it has a seat
        # now: only the login-sibling rule keeps it out.
        sibling_acct = seed.make_account(name='Sibling', max_connections=1)
        main = account_links.add_login(self.full.id, 'main', 'main-user', 'pw', 1).id
        account_links.add_login(sibling_acct.id, 'own', 'own-user', 'pw', 1)
        db.session.add(AccountLogin(account_id=sibling_acct.id, login_id=main, position=1))
        db.session.commit()
        connlim._holders.clear()
        connlim.try_acquire(self.full.id, 'recording', 999999)
        self.channel(sibling_acct, 'On the same login')
        live_acct = seed.make_account(name='Live', max_connections=2)
        live = self.channel(live_acct, 'Already recording')
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='other', channel_id=live.id)
        episode_acct = seed.make_account(name='Episode', max_connections=1)
        other_ep = seed.make_channel(episode_acct, name='Other episode')
        db.session.commit()
        self.listing(other_ep, sub_title='Part Two')
        later_acct = seed.make_account(name='Later', max_connections=1)
        later = seed.make_channel(later_acct, name='Later showing')
        db.session.commit()
        self.listing(later, start=self.program_start + timedelta(hours=3))
        rid = self.scheduled(program_sub_title='Part One')
        rec, rearm = self.start(rid)
        self.assertEqual(rec.channel_id, self.mine.id, 'nothing qualifies, so it waits')
        self.assertEqual(rec.status, REC_STATUS_SCHEDULED)
        self.assertTrue(rearm.called)
        self.assertIsNone(self.moved_event(rid))

    def test_a_check_still_running_counts_as_unchecked(self):
        """A test row is written FAILED until it ends; mid-check it says nothing yet."""
        running = self.channel(self.free, 'Being checked', health_score=95)
        seed.make_channel_test(running, all_null=False, status='FAILED', test_ended_at=None)
        failed = self.channel(self.other, 'Failed', tested='FAILED', health_score=100)
        db.session.commit()
        rec, _ = self.start(self.scheduled())
        self.assertEqual(rec.channel_id, running.id)
        self.assertNotEqual(rec.channel_id, failed.id)

    def test_a_wait_says_why_it_did_not_move(self):
        from app.database import RECORDING_START_DEFERRED
        rid = self.scheduled()
        self.start(rid)
        ev = RecordingEvent.query.filter_by(recording_id=rid,
                                            event_type=RECORDING_START_DEFERRED).one()
        self.assertIn('No other channel showing this program is on an account with a '
                      'connection free', ev.detail)

    def test_a_move_decided_against_a_stale_row_gives_its_seat_back(self):
        """dev/changelog/987: two starts of one recording can run at once. The one that finds
        the row already moved or claimed must not repoint it, and must not keep the seat."""
        there = self.channel(self.free, 'There')
        rid = self.scheduled()
        real = recorder._try_acquire_slot_with_preemption

        def acquire_then_lose_the_race(app, recording_id, account_id, acquire=None):
            got = real(app, recording_id, account_id, acquire)
            if got and account_id == self.free.id:
                db.session.execute(db.text('UPDATE recordings SET status = :s WHERE id = :i'),
                                   {'s': REC_STATUS_IN_PROGRESS, 'i': recording_id})
                db.session.commit()
            return got

        with mock.patch.object(recorder, '_try_acquire_slot_with_preemption',
                               side_effect=acquire_then_lose_the_race):
            rec, _ = self.start(rid)
        self.assertEqual(rec.channel_id, self.mine.id, 'the row is left as the winner wrote it')
        self.assertNotIn(('recording', rid), connlim._holders.get(self.free.id, []))
        self.assertIsNone(self.moved_event(rid))
        self.assertNotEqual(rec.channel_id, there.id)

    def test_no_program_snapshot_means_no_move(self):
        self.channel(self.free, 'There')
        now = datetime.utcnow()
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='by url',
                                  channel_id=self.mine.id, start_time=now - timedelta(seconds=30),
                                  stop_time=now + timedelta(hours=1))
        db.session.commit()
        rec, _ = self.start(rec.id)
        self.assertEqual(rec.channel_id, self.mine.id)
        from app.database import RECORDING_START_DEFERRED
        ev = RecordingEvent.query.filter_by(recording_id=rec.id,
                                            event_type=RECORDING_START_DEFERRED).one()
        self.assertIn('it has no guide listing', ev.detail)


class StartSettingTests(_StartCase):

    def test_the_global_setting_off_waits(self):
        from app.database import RECORDING_START_DEFERRED
        self.configure(move_when_account_full=False)
        self.channel(self.free, 'There')
        rid = self.scheduled()
        rec, rearm = self.start(rid)
        self.assertEqual(rec.channel_id, self.mine.id)
        self.assertTrue(rearm.called)
        ev = RecordingEvent.query.filter_by(recording_id=rid,
                                            event_type=RECORDING_START_DEFERRED).one()
        self.assertIn('Moving to another channel when the account is full is off (from the '
                      'global setting)', ev.detail)

    def test_a_profile_off_beats_the_global_on_and_a_recording_on_beats_the_profile(self):
        self.channel(self.free, 'There')
        profile = RecordingProfile(name='Stay', move_when_account_full=False)
        db.session.add(profile)
        db.session.commit()
        rec, _ = self.start(self.scheduled(profile_id=profile.id))
        self.assertEqual(rec.channel_id, self.mine.id)
        rec, _ = self.start(self.scheduled(profile_id=profile.id, move_when_account_full=True))
        self.assertNotEqual(rec.channel_id, self.mine.id)
        self.assertIn('this recording is set to move', self.moved_event(rec.id).detail)

    def test_a_profile_on_beats_the_global_off(self):
        self.configure(move_when_account_full=False)
        there = self.channel(self.free, 'There')
        profile = RecordingProfile(name='Move', move_when_account_full=True)
        db.session.add(profile)
        db.session.commit()
        rec, _ = self.start(self.scheduled(profile_id=profile.id))
        self.assertEqual(rec.channel_id, there.id)
        self.assertIn('its profile is set to move', self.moved_event(rec.id).detail)


class RuleSeatChoiceTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        mock.patch('app.routes.recordings.schedule_recording').start()
        self.addCleanup(mock.patch.stopall)
        self.start = tomorrow_local_noon()
        self.one = seed.make_account(name='Account One', max_connections=1)
        self.two = seed.make_account(name='Account Two', max_connections=1)
        self.three = seed.make_account(name='Account Three', max_connections=1)
        db.session.commit()
        self.first = self.channel(self.one, 'Sports One HD', health_score=90)
        busy = seed.make_channel(self.one, name='Busy channel')
        db.session.commit()
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='Busy', channel_id=busy.id,
                            start_time=self.start - timedelta(minutes=30),
                            stop_time=self.start + timedelta(minutes=90))
        db.session.commit()

    def tearDown(self):
        admission.reset_for_tests()
        self.t.cleanup()

    def channel(self, account, name, tested=None, **kw):
        ch = seed.make_channel(account, name=name, **kw)
        if tested is not None:
            seed.make_channel_test(ch, all_null=False, status=tested)
        db.session.commit()
        seed.make_epg_entry(ch, title=TITLE, start_time=self.start, duration_minutes=60)
        db.session.commit()
        return ch

    def run_rule(self, profile_id=None):
        form = {'name': 'Test rule', 'search': 'grain=airings&q=Cup+Race'}
        if profile_id is not None:
            form['profile_id'] = str(profile_id)
        rid = auto_rules.create_rule(auto_rules.read_rule_values(form))
        auto_rules.evaluate_now(rid)
        db.session.expire_all()
        rec = Recording.query.filter_by(rule_id=rid).one()
        return rec, RuleMatch.query.filter_by(rule_id=rid, recording_id=rec.id).one()

    def test_a_passed_check_is_tried_before_an_untested_channel_with_a_better_score(self):
        self.channel(self.two, 'Sports One Untested', health_score=80)
        passed = self.channel(self.three, 'Sports One Checked', tested='COMPLETED',
                              health_score=40)
        rec, row = self.run_rule()
        self.assertEqual(rec.channel_id, passed.id)
        self.assertIn('Chosen because its latest health check passed.', row.detail)

    def test_an_untested_channel_is_taken_when_nothing_checked_is_free(self):
        untested = self.channel(self.two, 'Sports One Untested')
        rec, row = self.run_rule()
        self.assertEqual(rec.channel_id, untested.id)
        self.assertIn('Chosen because it has not been health checked.', row.detail)

    def test_a_profile_set_to_wait_keeps_the_first_choice_and_says_why(self):
        self.channel(self.two, 'Sports One Free', tested='COMPLETED')
        profile = RecordingProfile(name='Stay', move_when_account_full=False)
        db.session.add(profile)
        db.session.commit()
        rec, row = self.run_rule(profile_id=profile.id)
        self.assertEqual(rec.channel_id, self.first.id)
        self.assertIn('Moving to another channel with this listing when the account is full is '
                      'turned off', row.detail)


class RecordDialogTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.start = tomorrow_local_noon()
        self.stop = self.start + timedelta(hours=1)
        self.acct = seed.make_account(name='Account', max_connections=1)
        self.ch = seed.make_channel(self.acct, name='Mine')
        busy = seed.make_channel(self.acct, name='Busy')
        db.session.commit()
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='Busy', channel_id=busy.id,
                            start_time=self.start, stop_time=self.stop)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def post(self, path, **extra):
        data = {'name': 'Mine show', 'url': 'http://example.test/live/9',
                'channel_id': str(self.ch.id),
                'start_time': local_input_value(self.start),
                'stop_time': local_input_value(self.stop), **extra}
        with mock.patch('app.routes.recordings.schedule_recording'):
            return self.t.client.post(path, data=data).get_json()

    def test_the_warning_says_a_move_will_be_tried_only_when_it_will(self):
        entry = seed.make_epg_entry(self.ch, title=TITLE, start_time=self.start,
                                    duration_minutes=60)
        db.session.commit()
        listed = {'source_epg_id': str(entry.id)}
        body = self.post('/recordings/new-json', move_when_full='', move_when_full_loaded='1',
                         **listed)
        self.assertIn('the recording moves to another channel with this program',
                      body['connection_limit_warning']['message'])
        body = self.post('/recordings/new-json', move_when_full='0', move_when_full_loaded='1',
                         **listed)
        self.assertNotIn('moves to another channel', body['connection_limit_warning']['message'])
        body = self.post('/recordings/new-json', move_when_full='1', move_when_full_loaded='1')
        self.assertNotIn('moves to another channel', body['connection_limit_warning']['message'],
                         'a recording with no guide listing has nothing to move to')

    def test_create_stores_the_answer_and_edit_leaves_it_unless_it_was_shown(self):
        body = self.post('/recordings/new-json', force='1', move_when_full='0',
                         move_when_full_loaded='1')
        rid = body['id']
        db.session.expire_all()
        self.assertIs(db.session.get(Recording, rid).move_when_account_full, False)
        self.post(f'/recordings/{rid}/edit-json', force='1', move_when_full='')
        db.session.expire_all()
        self.assertIs(db.session.get(Recording, rid).move_when_account_full, False,
                      'an edit that could not show the stored answer leaves it alone')
        self.post(f'/recordings/{rid}/edit-json', force='1', move_when_full='',
                  move_when_full_loaded='1')
        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, rid).move_when_account_full)
        self.assertIn('error', self.post('/recordings/new-json', move_when_full='maybe',
                                         move_when_full_loaded='1'))

    def test_the_recording_page_says_what_it_will_do(self):
        body = self.post('/recordings/new-json', force='1', move_when_full='0',
                         move_when_full_loaded='1')
        html = self.t.client.get(f'/recordings/{body["id"]}').get_data(as_text=True)
        self.assertIn('If the account is full', html)
        self.assertIn('Waits for a connection on this channel (set on this recording)', html)


if __name__ == '__main__':
    unittest.main()
