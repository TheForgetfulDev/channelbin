"""Automated recordings of a game follow the time it goes by (app/sports_follow.py,
dev/docs/DESIGN-sports-data.md §16.1, dev/changelog/1631): a rule's recording and one a league page's
Record made move with TheSportsDB's time by the league's answer, a TV-guide or hand-made one and a
window a person set never move, and Cover both past its limit keeps a second recording at TheSportsDB's
time. Made-up league and teams; throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_follow
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_epg_entry, make_recording  # noqa: E402

from app import db, sports_follow, sports_match, sports_times  # noqa: E402
from app.database import (  # noqa: E402
    RECORDING_SPORTS_RETIMED, RECORDING_SPORTS_SECOND, RECORDING_SPORTS_TIME_KEPT, RECORDING_START_REARMED,
    RULE_MATCH_SCHEDULED,
    AutoRule, Recording, RecordingEvent, RecordingProfile, RecordingSports, RuleMatch, SportsEvent,
    SportsFollow, SportsLeague, SportsMatch,
)
from app.program_titles import title_key  # noqa: E402
from app.tz_utils import local_input_value  # noqa: E402

TITLE = 'PFL Football'
SUB = 'Harborview Gulls at Ironvale Bears'
PRE, POST = timedelta(minutes=5), timedelta(minutes=10)


def now_utc():
    return datetime.utcnow().replace(second=0, microsecond=0)


class Case(unittest.TestCase):
    """A followed league and one game: the guide lists it three hours out, TheSportsDB has it
    `early` before that, and the guide has not followed the move."""

    early = timedelta(minutes=75)
    hours = 3
    ahead = timedelta(hours=3)

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.t.sandbox_config({'display': {'timezone': 'America/New_York', 'time_format': '12h'}})
        for target in ('app.routes.recordings.schedule_recording', 'app.routes.recordings.unschedule_recording',
                       'app.routes.recordings.end_capture_wait'):
            patcher = mock.patch(target)
            setattr(self, target.rsplit('.', 1)[1], patcher.start())
            self.addCleanup(patcher.stop)
        self.now = now_utc()
        self.listed = self.now + self.ahead
        self.league = SportsLeague(tsdb_id=9001, name='Pretend Football League', sport='Pretend Ball',
                                   current_season='2099', match_terms=json.dumps(['PFL']))
        db.session.add(self.league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=self.league.id, team_tsdb_id=0, rule_id=0))
        self.event = SportsEvent(tsdb_id=7001, league_id=self.league.id, name='Harborview Gulls vs Ironvale Bears',
                                 home_team_id=1, away_team_id=2, home_name='Ironvale Bears',
                                 away_name='Harborview Gulls', start_at=self.listed - self.early,
                                 guide_start=self.listed, local_date=self.listed.date())
        db.session.add(self.event)
        self.acc = make_account()
        self.ch = make_channel(self.acc, name='Local 1')
        self.profile = RecordingProfile(name='Sports', pre_padding_minutes=5, post_padding_minutes=10)
        db.session.add(self.profile)
        db.session.flush()
        self.entry = make_epg_entry(self.ch, title=TITLE, sub_title=SUB, start_time=self.listed,
                                    duration_minutes=self.hours * 60)
        db.session.add(SportsMatch(
            event_id=self.event.id, channel_id=self.ch.id, kind='guide', program_start_time=self.listed,
            program_stop_time=self.stop, title_key=title_key(TITLE), episode_key=title_key(SUB),
            program_title=TITLE, program_sub_title=SUB, strength='strong', airing='first', reason='x'))
        db.session.commit()

    @property
    def stop(self):
        return self.listed + timedelta(hours=self.hours)

    @property
    def tsdb(self):
        return self.listed - self.early

    def recording(self, window=None, kind='record', owned=True, linked=True, profile=True, status='SCHEDULED'):
        """A SCHEDULED recording of the listing at `window` (default: the listing, padded). A Record one
        carries the mark and, when `owned`, ChannelBin's window; a rule one has its ledger row."""
        start, stop = window or (self.listed - PRE, self.stop + POST)
        kw = {}
        if kind == 'record':
            kw['sports_record_tsdb_id'] = 7001
            if owned:
                kw.update(sports_window_start=start, sports_window_stop=stop)
        elif kind == 'rule':
            rule = AutoRule(name='PFL games', search_params='q=PFL')
            db.session.add(rule)
            db.session.flush()
            kw['rule_id'] = rule.id
        rec = make_recording(status=status, name='PFL Football', channel_id=self.ch.id, start_time=start,
                             stop_time=stop, program_start_time=self.listed, program_stop_time=self.stop,
                             program_title=TITLE, program_sub_title=SUB,
                             profile_id=self.profile.id if profile else None, **kw)
        if kind == 'rule':
            off = timedelta(minutes=1) if not owned else timedelta(0)
            db.session.add(RuleMatch(rule_id=kw['rule_id'], title_key=title_key(TITLE), episode_key=title_key(SUB),
                                     program_start_time=self.listed, program_stop_time=self.stop,
                                     action_taken=RULE_MATCH_SCHEDULED, recording_id=rec.id,
                                     rec_start_time=start + off, rec_stop_time=stop))
        if linked:
            db.session.add(RecordingSports(recording_id=rec.id, tsdb_event_id=7001, league_name=self.league.name,
                                           event_name=self.event.name, strength='strong', airing='first',
                                           reason='x', linked_at=self.now, updated_at=self.now))
        db.session.commit()
        return rec.id

    def follow(self):
        out = sports_follow.follow(now=self.now)
        db.session.expire_all()
        return out

    def rec(self, rid):
        db.session.expire_all()
        return db.session.get(Recording, rid)

    def window(self, rid):
        r = self.rec(rid)
        return r.start_time, r.stop_time

    def events(self, rid, kind):
        return RecordingEvent.query.filter_by(recording_id=rid, event_type=kind).all()

    def move_tsdb(self, start):
        ev = db.session.get(SportsEvent, self.event.id)
        ev.start_at = start
        db.session.commit()


class FollowTests(Case):
    def test_a_record_recording_moves_to_thesportsdbs_time(self):
        """Under the default, Go by TheSportsDB: the start moves to TheSportsDB's 75 minutes earlier,
        padded by the profile; the end stays with the listing, never earlier than listed."""
        rid = self.recording()
        self.assertEqual(self.follow()['moved'], 1)
        self.assertEqual(self.window(rid), (self.tsdb - PRE, self.stop + POST))
        r = self.rec(rid)
        self.assertEqual((r.sports_window_start, r.sports_window_stop), (self.tsdb - PRE, self.stop + POST))
        self.assertEqual((r.scheduled_start_time, r.program_start_time), (self.tsdb - PRE, self.listed))
        self.assertEqual(len(self.events(rid, RECORDING_SPORTS_RETIMED)), 1)
        self.schedule_recording.assert_called()

    def test_it_goes_back_to_the_listing_when_the_two_agree_again(self):
        rid = self.recording()
        self.follow()
        self.move_tsdb(self.listed)
        self.follow()
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))
        self.assertEqual(len(self.events(rid, RECORDING_SPORTS_RETIMED)), 2)

    def test_a_second_pass_with_nothing_changed_writes_nothing(self):
        rid = self.recording()
        self.follow()
        self.assertEqual(self.follow()['moved'], 0)
        self.assertEqual(len(self.events(rid, RECORDING_SPORTS_RETIMED)), 1)

    def test_a_rule_recording_moves_and_its_ledger_row_moves_with_it(self):
        """Retrack reads RuleMatch.rec_* as the rule's window: left behind, it would read ChannelBin's
        move as a person's edit."""
        rid = self.recording(kind='rule')
        self.follow()
        self.assertEqual(self.window(rid), (self.tsdb - PRE, self.stop + POST))
        m = RuleMatch.query.filter_by(recording_id=rid).one()
        self.assertEqual((m.rec_start_time, m.rec_stop_time), (self.tsdb - PRE, self.stop + POST))
        self.assertEqual(m.program_start_time, self.listed)

    def test_a_tv_guide_recording_linked_to_the_game_never_moves(self):
        """A recording made from the TV Guide works as scheduled (dev/changelog/1624); its
        recording_sports link is not a reason to follow."""
        rid = self.recording(kind='guide')
        self.assertEqual(self.follow()['moved'], 0)
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))
        self.assertEqual(self.events(rid, RECORDING_SPORTS_RETIMED), [])
        self.assertEqual(self.events(rid, RECORDING_SPORTS_TIME_KEPT), [], 'it is not followed, so nothing is kept from it')

    def test_times_a_person_set_at_record_are_kept_and_said_once(self):
        rid = self.recording(owned=False)
        self.follow()
        self.follow()
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))
        kept = self.events(rid, RECORDING_SPORTS_TIME_KEPT)
        self.assertEqual(len(kept), 1)
        self.assertIn('left as it is', kept[0].detail)

    def test_a_rule_recording_a_person_edited_is_kept(self):
        rid = self.recording(kind='rule', owned=False)
        self.follow()
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))
        self.assertEqual(len(self.events(rid, RECORDING_SPORTS_TIME_KEPT)), 1)

    def test_go_by_the_guide_keeps_the_listing(self):
        sports_times.set_league(9001, {'go_by': sports_times.GO_GUIDE})
        rid = self.recording()
        self.follow()
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))

    def test_further_off_than_the_wait_keeps_the_listing(self):
        sports_times.set_league(9001, {'wait_hours': 1})
        rid = self.recording()
        self.follow()
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))

    def test_a_rule_recording_with_no_profile_moves_once_and_keeps_its_padding(self):
        """With no profile the padding is the one ChannelBin gave it, never the gap to its program
        once it has moved, which would move it again by the same amount on every pass."""
        rid = self.recording(kind='rule', profile=False)
        self.follow()
        self.follow()
        self.follow()
        self.assertEqual(self.window(rid), (self.tsdb - PRE, self.stop + POST))
        self.assertEqual(len(self.events(rid, RECORDING_SPORTS_RETIMED)), 1)

    def test_a_record_recording_with_no_profile_has_no_padding(self):
        """Record pads by the chosen profile, and with none by nothing: saved at TheSportsDB's time, it is
        already where it belongs, and the gap to its program is not padding."""
        rid = self.recording(window=(self.tsdb, self.stop), profile=False)
        self.follow()
        self.assertEqual(self.window(rid), (self.tsdb, self.stop))
        self.assertEqual(self.events(rid, RECORDING_SPORTS_RETIMED), [])

    def test_a_move_back_past_the_matchers_reach_says_so(self):
        """TheSportsDB postponing the game past the matcher's window puts the recording back on its
        listing, and the event does not say it went by TheSportsDB."""
        rid = self.recording()
        self.follow()
        self.move_tsdb(self.listed + timedelta(hours=14))
        self.follow()
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))
        last = self.events(rid, RECORDING_SPORTS_RETIMED)[-1].detail
        self.assertIn("so it records the listing's times", last)
        self.assertNotIn('goes by TheSportsDB', last)

    def test_a_recording_from_no_listing_of_the_game_is_left_alone(self):
        """An event channel's recording keeps the window its pick gave it."""
        SportsMatch.query.delete()
        db.session.commit()
        rid = self.recording()
        self.follow()
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))


class CoverBothTests(Case):
    """Cover both, TheSportsDB two hours before a one-hour listing: past the 90-minute limit, and the two
    windows do not touch, so a second recording is kept at TheSportsDB's time."""

    early = timedelta(hours=2)
    hours = 1

    def setUp(self):
        super().setUp()
        sports_times.set_league(9001, {'go_by': sports_times.GO_BOTH})

    def seconds(self, rid):
        db.session.expire_all()
        return Recording.query.filter_by(sports_cover_for_id=rid).all()

    def test_a_second_recording_is_scheduled_at_thesportsdbs_time(self):
        rid = self.recording()
        self.assertEqual(self.follow()['added'], 1)
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))
        (second,) = self.seconds(rid)
        self.assertEqual((second.start_time, second.stop_time),
                         (self.tsdb - PRE, self.stop - self.early + POST))
        self.assertEqual((second.sports_window_start, second.sports_window_stop),
                         (second.start_time, second.stop_time))
        self.assertIsNone(second.rule_id)
        self.assertIsNone(second.sports_record_tsdb_id)
        self.assertEqual(second.program_start_time, self.listed)
        self.assertIn('TheSportsDB time', second.name)
        self.assertEqual(len(self.events(rid, RECORDING_SPORTS_SECOND)), 1)
        self.assertEqual(len(self.events(second.id, RECORDING_SPORTS_SECOND)), 1)

    def test_a_second_pass_makes_no_third_recording(self):
        rid = self.recording()
        self.follow()
        self.follow()
        self.assertEqual(len(self.seconds(rid)), 1)

    def test_the_second_is_removed_when_the_gap_closes(self):
        rid = self.recording()
        self.follow()
        self.move_tsdb(self.listed - timedelta(minutes=30))
        self.assertEqual(self.follow()['removed'], 1)
        self.assertEqual(self.seconds(rid), [])
        said = [json.loads(e.extra_data)['kind'] for e in self.events(rid, RECORDING_SPORTS_SECOND)]
        self.assertEqual(said, ['added', 'removed'])

    def test_the_second_moves_with_thesportsdb(self):
        rid = self.recording()
        self.follow()
        later = self.tsdb + timedelta(minutes=15)
        self.move_tsdb(later)
        self.follow()
        (second,) = self.seconds(rid)
        self.assertEqual(second.start_time, later - PRE)

    def test_a_second_a_person_removed_is_not_made_again(self):
        rid = self.recording()
        self.follow()
        (second,) = self.seconds(rid)
        r = self.client_delete(second.id)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.follow()
        self.assertEqual(self.seconds(rid), [])

    def test_cancelling_the_first_removes_the_second(self):
        rid = self.recording()
        self.follow()
        r = self.client_delete(rid)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        db.session.expire_all()
        self.assertEqual(Recording.query.filter(Recording.sports_cover_for_id.isnot(None)).all(), [])

    def test_the_recording_pages_name_each_other(self):
        rid = self.recording()
        self.follow()
        (second,) = self.seconds(rid)
        first = self.t.client.get(f'/recordings/{rid}').get_data(as_text=True)
        self.assertIn(f'/recordings/{second.id}"', first)
        other = self.t.client.get(f'/recordings/{second.id}').get_data(as_text=True)
        self.assertIn('second recording of this game', other)
        self.assertIn(f'/recordings/{rid}"', other)

    def test_a_failed_removal_leaves_the_second_and_says_nothing(self):
        """The delete and the first one's "removed" event are one commit: a removal that fails
        part way never leaves a gone second that reads as a person's."""
        rid = self.recording()
        self.follow()
        (second,) = self.seconds(rid)
        self.move_tsdb(self.listed - timedelta(minutes=30))
        real = sports_follow.add_recording_event

        def fail_on_removed(rec_id, kind, **kw):
            if (kw.get('extra') or {}).get('kind') == sports_follow.SECOND_REMOVED:
                raise RuntimeError('stopped between the delete and its event')
            return real(rec_id, kind, **kw)
        with mock.patch('app.sports_follow.add_recording_event', side_effect=fail_on_removed), \
                self.assertLogs('app.sports_follow', 'ERROR'):
            self.follow()
        self.assertEqual([s.id for s in self.seconds(rid)], [second.id])

    def test_deleting_the_rule_removes_the_second(self):
        from app import auto_rules
        rid = self.recording(kind='rule')
        self.follow()
        self.assertEqual(len(self.seconds(rid)), 1)
        rule_id = self.rec(rid).rule_id
        auto_rules.delete_rule(rule_id, cancel_scheduled=True)
        db.session.expire_all()
        self.assertEqual(Recording.query.filter(Recording.sports_cover_for_id.isnot(None)).all(), [])

    def test_padding_never_makes_the_two_overlap(self):
        """Apart before padding, the two would overlap after it, and a handoff on one channel cuts the
        earlier one short: the second's padding is trimmed instead."""
        self.profile.post_padding_minutes = 90
        db.session.commit()
        rid = self.recording(window=(self.listed - PRE, self.stop + timedelta(minutes=90)))
        self.follow()
        (second,) = self.seconds(rid)
        self.assertEqual((second.start_time, second.stop_time), (self.tsdb - PRE, self.listed - PRE))

    def client_delete(self, rid):
        db.session.commit()
        return self.t.client.post(f'/recordings/{rid}/cancel-json')


class CoverBothNothingLeftTests(Case):
    """Cover both never writes a second recording that ends before it starts: when nothing of
    TheSportsDB's window is left before the first recording (it has passed, or what is left lies inside
    the first's own window), no second is made, or the one there is removed, and the first recording
    says why once (dev/docs/BUGS.md 2026-11-25 @ 10:44:23 PM)."""

    early = timedelta(hours=2)
    hours = 1

    def setUp(self):
        super().setUp()
        sports_times.set_league(9001, {'go_by': sports_times.GO_BOTH})

    def seconds(self, rid):
        db.session.expire_all()
        return Recording.query.filter_by(sports_cover_for_id=rid).all()

    def said(self, rid):
        return [json.loads(e.extra_data) for e in self.events(rid, RECORDING_SPORTS_SECOND)]

    def test_no_second_is_made_when_thesportsdbs_time_has_passed(self):
        self.move_tsdb(self.listed - timedelta(hours=5))
        rid = self.recording()
        self.assertEqual(self.follow()['added'], 0)
        self.assertEqual(self.seconds(rid), [])
        (note,) = self.said(rid)
        self.assertEqual(note['kind'], sports_follow.SECOND_NOT_MADE)
        (ev,) = self.events(rid, RECORDING_SPORTS_SECOND)
        self.assertIn('already passed', ev.detail)
        self.follow()
        self.assertEqual(len(self.said(rid)), 1, 'said once, not on every pass')

    def test_a_second_is_still_made_once_the_time_opens_again(self):
        self.move_tsdb(self.listed - timedelta(hours=5))
        rid = self.recording()
        self.follow()
        self.move_tsdb(self.tsdb)
        self.assertEqual(self.follow()['added'], 1)
        self.assertEqual(len(self.seconds(rid)), 1)

    def test_a_second_whose_time_has_passed_is_removed(self):
        rid = self.recording()
        self.follow()
        (second,) = self.seconds(rid)
        self.move_tsdb(self.listed - timedelta(hours=5))
        self.assertEqual(self.follow()['removed'], 1)
        self.assertEqual(self.seconds(rid), [])
        last = self.said(rid)[-1]
        self.assertEqual((last['kind'], last['reason']), (sports_follow.SECOND_REMOVED,
                                                          sports_follow.REMOVED_NOTHING_LEFT))
        self.follow()
        self.assertEqual(len(self.said(rid)), 2, 'the removal already said why; no note follows it')
        self.assertEqual(self.seconds(rid), [])

    def test_no_second_when_what_is_left_lies_inside_the_first_recordings_window(self):
        """TheSportsDB's padded end is still ahead, but the first recording has started its window."""
        self.profile.post_padding_minutes = 90
        db.session.commit()
        later = self.listed - PRE + timedelta(minutes=1)
        rid = self.recording(window=(self.listed - PRE, self.stop + timedelta(minutes=90)))
        out = sports_follow.follow(now=later)
        self.assertEqual(out['added'], 0)
        self.assertEqual(self.seconds(rid), [])
        (ev,) = self.events(rid, RECORDING_SPORTS_SECOND)
        self.assertIn("inside this recording's own window", ev.detail)

    def test_no_recording_anywhere_ends_before_it_starts(self):
        self.move_tsdb(self.listed - timedelta(hours=5))
        self.recording()
        self.follow()
        db.session.expire_all()
        self.assertEqual([r.id for r in Recording.query.all() if r.stop_time <= r.start_time], [])


class RecordSaveTests(Case):
    """A league page's Record save: at the times Record worked out, the recording is ChannelBin's to
    move; at times the person set, it is theirs."""

    def save(self, start, stop):
        r = self.t.client.post('/recordings/new-json', data={
            'name': 'PFL Football', 'url': 'http://example.test/live/1',
            'start_time': local_input_value(start), 'stop_time': local_input_value(stop),
            'channel_id': str(self.ch.id), 'source_epg_id': str(self.entry.id),
            'sports_event_id': str(self.event.id), 'profile_id': str(self.profile.id), 'force': '1'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return self.rec(r.get_json()['id'])

    def test_saved_at_records_times_it_follows(self):
        rec = self.save(self.tsdb - PRE, self.stop + POST)
        self.assertEqual((rec.sports_window_start, rec.sports_window_stop), (self.tsdb - PRE, self.stop + POST))

    def test_saved_after_its_start_it_still_follows(self):
        """The create path moves a start already passed to now; the window it keeps is that one."""
        db.session.get(SportsEvent, self.event.id).start_at = self.now - timedelta(minutes=10)
        db.session.commit()
        rec = self.save(self.now - timedelta(minutes=10) - PRE, self.stop + POST)
        self.assertGreaterEqual(rec.start_time, self.now)
        self.assertEqual((rec.sports_window_start, rec.sports_window_stop), (rec.start_time, rec.stop_time))

    def test_saved_at_other_times_it_does_not(self):
        rec = self.save(self.listed - PRE, self.stop + POST)
        self.assertIsNone(rec.sports_window_start)
        self.assertEqual(rec.sports_record_tsdb_id, 7001)


class RunningTests(Case):
    """A recording already under way when TheSportsDB moves the game (dev/changelog/1632): the F1
    rain delay, the start moved 90 minutes later at the green flag. Its start stays, its end moves
    later by the move under TheSportsDB and Cover both, and nothing else changes it."""

    early = timedelta(0)
    ahead = timedelta(minutes=-30)
    late = timedelta(minutes=90)

    def setUp(self):
        super().setUp()
        p = mock.patch('app.scheduler.reschedule_recording_stop')
        self.rearm_stop = p.start()
        self.addCleanup(p.stop)

    def delay(self, by=None, seen=None):
        ev = db.session.get(SportsEvent, self.event.id)
        ev.previous_start, ev.start_at = ev.start_at, self.listed + (self.late if by is None else by)
        ev.start_changed_at = seen or self.now - timedelta(minutes=5)
        db.session.commit()

    def running(self, **kw):
        return self.recording(status='IN_PROGRESS', **kw)

    def test_its_end_moves_later_by_the_move_and_its_start_stays(self):
        rid = self.running()
        self.delay()
        self.assertEqual(self.follow()['extended'], 1)
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + self.late + POST))
        r = self.rec(rid)
        self.assertEqual((r.scheduled_start_time, r.scheduled_stop_time), (self.listed - PRE, self.stop + self.late + POST))
        self.assertEqual(r.sports_window_stop, self.stop + self.late + POST)
        (ev,) = self.events(rid, RECORDING_SPORTS_RETIMED)
        self.assertIn('after it had started', ev.detail)
        self.assertIn('90 minutes later', ev.detail)
        self.assertEqual(json.loads(ev.extra_data)['kind'], sports_follow.RETIMED_EXTENDED)
        self.rearm_stop.assert_called_once_with(rid, self.stop + self.late + POST)
        self.schedule_recording.assert_not_called()

    def test_a_second_pass_writes_nothing(self):
        rid = self.running()
        self.delay()
        self.follow()
        self.assertEqual(self.follow()['extended'], 0)
        self.assertEqual(len(self.events(rid, RECORDING_SPORTS_RETIMED)), 1)

    def test_a_rule_recording_moves_its_ledger_end_with_it_even_after_a_late_start(self):
        """The recorder moves start_time to a late start's moment; the window is read from
        scheduled_start_time, so the recording is still ChannelBin's."""
        rid = self.running(kind='rule')
        r = db.session.get(Recording, rid)
        r.start_time = self.listed + timedelta(minutes=3)
        db.session.commit()
        self.delay()
        self.follow()
        self.assertEqual(self.rec(rid).stop_time, self.stop + self.late + POST)
        m = RuleMatch.query.filter_by(recording_id=rid).one()
        self.assertEqual((m.rec_start_time, m.rec_stop_time), (self.listed - PRE, self.stop + self.late + POST))

    def test_cover_both_ends_later_too_and_the_guide_never(self):
        sports_times.set_league(9001, {'go_by': sports_times.GO_BOTH})
        both = self.running()
        self.delay()
        self.follow()
        self.assertEqual(self.rec(both).stop_time, self.stop + self.late + POST)
        sports_times.set_league(9001, {'go_by': sports_times.GO_GUIDE})
        guide = self.running()
        self.follow()
        self.assertEqual(self.rec(guide).stop_time, self.stop + POST)

    def test_an_end_a_person_changed_is_left_and_said_once(self):
        rid = self.running()
        r = db.session.get(Recording, rid)
        r.stop_time = self.stop + timedelta(minutes=30)
        db.session.commit()
        self.delay()
        self.follow()
        self.follow()
        self.assertEqual(self.rec(rid).stop_time, self.stop + timedelta(minutes=30))
        self.assertEqual(len(self.events(rid, RECORDING_SPORTS_TIME_KEPT)), 1)
        self.rearm_stop.assert_not_called()

    def test_a_move_to_another_day_is_a_new_date_not_a_delay(self):
        """A race rained out to the next day: the recording is not stretched a day, and the game is not
        held at the start it began at (sports_match.MAX_DELAY)."""
        rid = self.running()
        self.delay(by=timedelta(hours=26))
        self.assertFalse(sports_match.kept_start(db.session.get(SportsEvent, self.event.id)))
        self.follow()
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))

    def test_a_postponed_game_is_not_running_late(self):
        rid = self.running()
        self.delay()
        ev = db.session.get(SportsEvent, self.event.id)
        ev.postponed = True
        db.session.commit()
        self.follow()
        self.assertEqual(self.rec(rid).stop_time, self.stop + POST)

    def test_a_recording_on_the_same_channel_inside_the_later_end_is_named(self):
        """Its start hands off and stops this one, so the event says the later end holds only until then."""
        rid = self.running()
        nxt = make_recording(status='SCHEDULED', name='Next show', channel_id=self.ch.id, start_time=self.stop + POST,
                             stop_time=self.stop + 3 * timedelta(hours=1))
        db.session.commit()
        self.delay()
        self.follow()
        (ev,) = self.events(rid, RECORDING_SPORTS_RETIMED)
        self.assertIn(f'Recording #{nxt.id} on the same channel starts at', ev.detail)
        self.assertIn('takes over from this one', ev.detail)

    def test_a_tv_guide_recording_under_way_never_changes(self):
        rid = self.running(kind='guide')
        self.delay()
        self.follow()
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))
        self.assertEqual(self.events(rid, RECORDING_SPORTS_RETIMED), [])

    def test_an_earlier_start_changes_nothing_and_says_so_once(self):
        rid = self.running()
        self.delay(by=-timedelta(minutes=30))
        self.follow()
        self.follow()
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))
        (kept,) = self.events(rid, RECORDING_SPORTS_TIME_KEPT)
        self.assertIn('already recording, so nothing changes', kept.detail)
        self.assertEqual(json.loads(kept.extra_data)['reason'], sports_follow.KEPT_UNDER_WAY)

    def test_a_move_later_seen_before_the_start_still_ends_later_while_it_records_its_padding(self):
        """Not a started game: under TheSportsDB the listing goes to the new start, which a recording in
        its padding cannot move to, so only its end follows."""
        rid = self.running()
        self.delay(by=timedelta(minutes=60), seen=self.listed - timedelta(minutes=20))
        self.follow()
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + timedelta(minutes=60) + POST))

    def test_a_running_second_recording_ends_later_too(self):
        """Cover both's second can be the one holding the game."""
        sports_times.set_league(9001, {'go_by': sports_times.GO_BOTH})
        first = self.recording(status='COMPLETED')
        start, stop = self.listed - PRE, self.stop + POST
        second = make_recording(status='IN_PROGRESS', name='PFL Football (TheSportsDB time)', channel_id=self.ch.id,
                                start_time=start, stop_time=stop, program_start_time=self.listed,
                                program_stop_time=self.stop, program_title=TITLE, program_sub_title=SUB,
                                profile_id=self.profile.id, sports_cover_for_id=first,
                                sports_window_start=start, sports_window_stop=stop)
        db.session.add(RecordingSports(recording_id=second.id, tsdb_event_id=7001, league_name=self.league.name,
                                       event_name=self.event.name, strength='strong', airing='first',
                                       reason='x', linked_at=self.now, updated_at=self.now))
        db.session.commit()
        self.delay()
        self.follow()
        self.assertEqual(self.rec(second.id).stop_time, self.stop + self.late + POST)


class NamedRunningTests(Case):
    """A rule's recording from an event channel's name, already under way when TheSportsDB moves the
    game later (dev/changelog/1643): no listing to recompute a window from and no guide start for
    kept_start(), so its end moves by TheSportsDB's start less the recording's program start, the
    pick's, from the pick's own end. The same answer on every pass, so the delay is never added
    twice, and a delay entered in steps adds up."""

    early = timedelta(0)
    ahead = timedelta(minutes=-30)

    def setUp(self):
        super().setUp()
        p = mock.patch('app.scheduler.reschedule_recording_stop')
        self.rearm_stop = p.start()
        self.addCleanup(p.stop)
        SportsMatch.query.delete()
        db.session.get(SportsEvent, self.event.id).guide_start = None
        db.session.commit()

    def named_rule_recording(self, kind='rule'):
        rid = self.recording(kind=kind, status='IN_PROGRESS')
        if kind == 'rule':
            RuleMatch.query.filter_by(recording_id=rid).update({'sports_event_tsdb_id': 7001})
            db.session.commit()
        return rid

    def move(self, by, seen=None):
        ev = db.session.get(SportsEvent, self.event.id)
        ev.previous_start, ev.start_at = ev.start_at, self.listed + by
        ev.start_changed_at = seen or self.now - timedelta(minutes=5)
        db.session.commit()

    def test_its_end_moves_later_by_the_move_from_the_picks_end_and_its_ledger_with_it(self):
        rid = self.named_rule_recording()
        self.move(timedelta(minutes=90))
        self.assertEqual(self.follow()['extended'], 1)
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + timedelta(minutes=90) + POST))
        m = RuleMatch.query.filter_by(recording_id=rid).one()
        self.assertEqual(m.rec_stop_time, self.stop + timedelta(minutes=90) + POST)
        (ev,) = self.events(rid, RECORDING_SPORTS_RETIMED)
        self.assertIn('the recording started from, and it changed that after the recording had started', ev.detail)
        self.assertIn('90 minutes later', ev.detail)
        self.rearm_stop.assert_called_once_with(rid, self.stop + timedelta(minutes=90) + POST)

    def test_a_second_pass_never_adds_the_delay_twice(self):
        rid = self.named_rule_recording()
        self.move(timedelta(minutes=90))
        self.follow()
        self.assertEqual(self.follow()['extended'], 0)
        self.assertEqual(self.window(rid)[1], self.stop + timedelta(minutes=90) + POST)
        self.assertEqual(len(self.events(rid, RECORDING_SPORTS_RETIMED)), 1)

    def test_a_delay_entered_in_steps_adds_up(self):
        """previous_start is overwritten at each step for a game no guide lists; the recording's own
        start is the anchor, so the second step lands on the whole delay."""
        rid = self.named_rule_recording()
        self.move(timedelta(minutes=30))
        self.follow()
        self.move(timedelta(minutes=75), seen=self.now - timedelta(minutes=1))
        self.assertEqual(self.follow()['extended'], 1)
        self.assertEqual(self.window(rid)[1], self.stop + timedelta(minutes=75) + POST)

    def test_with_no_profile_it_keeps_its_padding_and_a_second_pass_adds_nothing(self):
        """No profile: the padding is the gap to the program, then the one the event stored."""
        rid = self.recording(kind='rule', status='IN_PROGRESS', profile=False)
        RuleMatch.query.filter_by(recording_id=rid).update({'sports_event_tsdb_id': 7001})
        db.session.commit()
        self.move(timedelta(minutes=45))
        self.assertEqual(self.follow()['extended'], 1)
        self.assertEqual(self.follow()['extended'], 0)
        self.assertEqual(self.window(rid)[1], self.stop + timedelta(minutes=45) + POST)

    def test_a_move_read_before_its_start_changes_nothing(self):
        rid = self.named_rule_recording()
        self.move(timedelta(minutes=90), seen=self.listed - timedelta(hours=1))
        self.assertEqual(self.follow()['extended'], 0)
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))

    def test_a_move_to_another_day_is_a_new_date_not_a_delay(self):
        rid = self.named_rule_recording()
        self.move(timedelta(days=1))
        self.follow()
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))

    def test_a_record_recording_from_a_name_keeps_the_persons_window(self):
        rid = self.named_rule_recording(kind='record')
        self.move(timedelta(minutes=90))
        self.follow()
        self.assertEqual(self.window(rid), (self.listed - PRE, self.stop + POST))


class HeldStartTests(Case):
    """A recording still SCHEDULED after its start (the recorder is holding it for a connection) is
    not moved to "now" by each pass (dev/docs/BUGS.md 2026-11-23 @ 11:12:32 PM): its start
    stays, so its wait and its place among the waiters stay."""

    ahead = timedelta(minutes=-10)
    early = timedelta(0)

    def assert_left(self, rid):
        before = self.window(rid)
        out = self.follow()
        self.assertEqual(out['moved'], 0)
        self.assertEqual(self.window(rid), before)
        self.assertEqual(self.events(rid, RECORDING_SPORTS_RETIMED), [])
        self.end_capture_wait.assert_not_called()

    def test_a_held_rule_recording_keeps_its_start(self):
        rid = self.recording(kind='rule')
        self.assert_left(rid)
        m = RuleMatch.query.filter_by(recording_id=rid).one()
        self.assertEqual(m.rec_start_time, self.listed - PRE)

    def test_a_held_record_recording_keeps_its_start(self):
        self.assert_left(self.recording())

    def test_an_earlier_time_already_passed_does_not_move_a_held_start(self):
        self.move_tsdb(self.listed - timedelta(minutes=30))
        self.assert_left(self.recording())


class PassedGameFutureStartTests(Case):
    """The other half of _start_from_now (dev/docs/BUGS.md 2026-11-23 @ 11:12:32 PM): a game whose
    start moved to before now, while its recording's start is still ahead, starts now - keeping
    the later start would miss the start of the game."""

    ahead = timedelta(minutes=20)
    early = timedelta(hours=1)

    def test_a_future_start_for_a_game_already_under_way_moves_to_now(self):
        rid = self.recording()
        self.assertGreater(self.window(rid)[0], self.now)
        out = self.follow()
        self.assertEqual(out['moved'], 1)
        self.assertEqual(self.window(rid)[0], self.now)


class HeldSecondTests(Case):
    """Cover both's second recording, held past its start, is not moved on each pass either
    (dev/docs/BUGS.md 2026-11-23 @ 11:12:32 PM)."""

    early = timedelta(hours=2)
    hours = 1
    ahead = timedelta(minutes=110)

    def test_a_held_second_keeps_its_start(self):
        sports_times.set_league(9001, {'go_by': sports_times.GO_BOTH})
        rid = self.recording()
        self.assertEqual(self.follow()['added'], 1)
        db.session.expire_all()
        (second,) = Recording.query.filter_by(sports_cover_for_id=rid).all()
        sid, before = second.id, (second.start_time, second.stop_time)
        out = sports_follow.follow(now=self.now + timedelta(minutes=10))
        self.assertEqual(out['moved'], 0)
        self.assertEqual(self.window(sid), before)
        self.assertEqual(self.events(sid, RECORDING_SPORTS_RETIMED), [])

    def test_a_held_second_with_no_job_is_armed_again(self):
        sports_times.set_league(9001, {'go_by': sports_times.GO_BOTH})
        rid = self.recording()
        self.follow()
        db.session.expire_all()
        (second,) = Recording.query.filter_by(sports_cover_for_id=rid).all()
        sid, before = second.id, (second.start_time, second.stop_time)
        self.schedule_recording.reset_mock()
        with mock.patch('app.scheduler.missing_starts', side_effect=lambda got: {sid} & set(got)):
            out = sports_follow.follow(now=self.now + timedelta(minutes=10))
        self.assertEqual((out['moved'], out['armed']), (0, 1))
        self.assertEqual(self.schedule_recording.call_args.args[1:], (sid, *before))
        self.assertEqual(len(self.events(sid, RECORDING_START_REARMED)), 1)


class UnarmedTests(Case):
    """A followed recording left SCHEDULED with no start job (its re-arm failed twice) is armed again
    by a later pass at its own times (dev/docs/BUGS.md 2026-11-25 @ 10:42:07 PM): since a start already
    passed keeps its time, no move does it by the way."""

    ahead = timedelta(minutes=-10)
    early = timedelta(0)

    def missing(self, ids):
        p = mock.patch('app.scheduler.missing_starts', side_effect=lambda got: set(got) & set(ids))
        p.start()
        self.addCleanup(p.stop)

    def test_a_held_start_with_no_job_is_armed_again_at_its_own_times(self):
        rid = self.recording()
        before = self.window(rid)
        self.missing([rid])
        out = self.follow()
        self.assertEqual((out['moved'], out['armed']), (0, 1))
        self.assertEqual(self.window(rid), before)
        self.schedule_recording.assert_called_once()
        self.assertEqual(self.schedule_recording.call_args.args[1:], (rid, *before))
        (ev,) = self.events(rid, RECORDING_START_REARMED)
        self.assertIn('nothing armed to start it', ev.detail)
        self.assertIn('starts now', ev.detail)

    def test_a_rule_recording_with_no_job_is_armed_again(self):
        rid = self.recording(kind='rule')
        self.missing([rid])
        self.assertEqual(self.follow()['armed'], 1)
        self.assertEqual(self.schedule_recording.call_args.args[1], rid)

    def test_one_still_armed_is_left_alone(self):
        rid = self.recording()
        self.missing([])
        self.assertEqual(self.follow()['armed'], 0)
        self.schedule_recording.assert_not_called()
        self.assertEqual(self.events(rid, RECORDING_START_REARMED), [])

    def test_a_failed_arm_says_nothing_and_the_next_pass_tries_again(self):
        rid = self.recording()
        self.missing([rid])
        self.schedule_recording.side_effect = [RuntimeError('jobstore down'), None]
        with self.assertLogs('app.sports_follow', 'WARNING'):
            self.assertEqual(self.follow()['armed'], 0)
        self.assertEqual(self.events(rid, RECORDING_START_REARMED), [])
        self.assertEqual(self.follow()['armed'], 1)
        self.assertEqual(len(self.events(rid, RECORDING_START_REARMED)), 1)

    def test_a_jobstore_that_cannot_be_read_does_not_stop_the_moves(self):
        self.move_tsdb(self.listed + timedelta(hours=1))
        rid = self.recording()
        with mock.patch('app.scheduler.missing_starts', side_effect=RuntimeError('unpickle failed')), \
                self.assertLogs('app.sports_follow', 'WARNING'):
            out = self.follow()
        self.assertEqual((out['moved'], out['armed']), (1, 0))
        self.assertEqual(self.window(rid)[0], self.listed + timedelta(hours=1) - PRE)

    def test_one_armed_again_since_the_pass_began_is_left_alone(self):
        """The pass asks once before its moves; a start that fired or an edit that re-armed meanwhile
        has a job again by the time the repair runs."""
        rid = self.recording()
        answers = iter([{rid}, set()])
        with mock.patch('app.scheduler.missing_starts', side_effect=lambda got: next(answers)):
            self.assertEqual(self.follow()['armed'], 0)
        self.schedule_recording.assert_not_called()
        self.assertEqual(self.events(rid, RECORDING_START_REARMED), [])

    def test_a_recording_this_pass_moved_is_not_armed_twice(self):
        """The move's own re-arm is the arm: the repair is for what no move reaches."""
        self.move_tsdb(self.listed + timedelta(hours=1))
        rid = self.recording()
        self.missing([rid])
        out = self.follow()
        self.assertEqual((out['moved'], out['armed']), (1, 0))
        self.assertEqual(self.schedule_recording.call_count, 1)


class MissingStartsTests(unittest.TestCase):
    """scheduler.missing_starts(): the ids with no start_<id> job, and nothing when no scheduler is
    live to arm one (dev/docs/BUGS.md 2026-11-25 @ 10:42:07 PM)."""

    def test_names_only_the_ids_with_no_start_job(self):
        from app import scheduler
        fake = mock.Mock()
        fake.get_job.side_effect = lambda job_id: None if job_id == 'start_2' else object()
        with mock.patch.object(scheduler, '_scheduler', fake), \
                mock.patch.object(scheduler, 'scheduler_is_live', return_value=True):
            self.assertEqual(scheduler.missing_starts([1, 2, 3]), {2})

    def test_nothing_when_the_scheduler_is_not_live(self):
        from app import scheduler
        with mock.patch.object(scheduler, 'scheduler_is_live', return_value=False):
            self.assertEqual(scheduler.missing_starts([1, 2]), set())


class HookTests(Case):
    def test_a_matcher_run_follows_the_games_times(self):
        from app import sports_match
        with mock.patch('app.sports_follow.follow_guarded', return_value={}) as follow:
            sports_match.run(now=self.now, pause=0)
        follow.assert_called_once()

    def test_a_rule_check_follows_the_games_times(self):
        from app import auto_rules
        db.session.add(AutoRule(name='PFL games', search_params='q=PFL'))
        db.session.commit()
        with mock.patch('app.sports_follow.follow_guarded', return_value={}) as follow:
            auto_rules._run(auto_rules.TRIGGER_CHECK, None)
        follow.assert_called_once()


if __name__ == '__main__':
    unittest.main()


class EditRepadTests(Case):
    """Editing a recording's profile on the record dialog (app/edit_window.py, dev/changelog/1652): a
    window ChannelBin gave it moves to the new profile's padding, the stored fact moves with it, and the
    follow pass then has nothing to do - before, the edit kept the old times and the next pass moved
    them unannounced, with an event blaming the game's time (dev/docs/BUGS.md 2026-11-19 @ 09:14:30 PM ET)."""

    def setUp(self):
        super().setUp()
        self.wide = RecordingProfile(name='Wide', pre_padding_minutes=30, post_padding_minutes=60)
        db.session.add(self.wide)
        db.session.commit()
        self.wide_id = self.wide.id

    def edit(self, rid, start, stop, profile_id):
        r = self.t.client.post(f'/recordings/{rid}/edit-json', data={
            'name': 'PFL Football', 'url': 'http://example.test/live/1',
            'start_time': local_input_value(start), 'stop_time': local_input_value(stop),
            'profile_id': '' if profile_id is None else str(profile_id), 'force': '1'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        db.session.expire_all()

    def window_answer(self, rid):
        r = self.t.client.get(f'/api/recordings/{rid}/edit-window')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def edited_events(self, rid):
        from app.database import RECORDING_EDITED
        return self.events(rid, RECORDING_EDITED)

    def test_a_followed_record_recording_pads_from_the_games_time(self):
        rid = self.recording()
        self.follow()
        got = self.window_answer(rid)
        self.assertEqual((got['owned'], got['from_game']), (True, True))
        self.assertEqual((got['start_time'], got['stop_time']),
                         (self.tsdb.strftime('%Y-%m-%dT%H:%M:%S'), self.stop.strftime('%Y-%m-%dT%H:%M:%S')))

    def test_saved_at_the_new_padding_it_stays_channelbins_and_the_pass_moves_nothing(self):
        rid = self.recording()
        self.follow()
        want = (self.tsdb - timedelta(minutes=30), self.stop + timedelta(minutes=60))
        self.edit(rid, *want, self.wide_id)
        r = self.rec(rid)
        self.assertEqual((r.start_time, r.stop_time), want)
        self.assertEqual((r.sports_window_start, r.sports_window_stop), want)
        (edited,) = self.edited_events(rid)
        self.assertIn('padding of profile "Wide"', edited.detail)
        self.assertIn('the game', edited.detail)
        retimed = len(self.events(rid, RECORDING_SPORTS_RETIMED))
        self.assertEqual(self.follow()['moved'], 0)
        self.assertEqual(self.window(rid), want)
        self.assertEqual(len(self.events(rid, RECORDING_SPORTS_RETIMED)), retimed)

    def test_a_profile_change_at_the_old_times_is_the_persons_and_never_moved_later(self):
        """The hidden second effect: the times left equal to the stored window, so the next pass read
        them as ChannelBin's and moved them to the new padding."""
        rid = self.recording()
        self.follow()
        old = self.window(rid)
        self.edit(rid, *old, self.wide_id)
        self.assertEqual(self.follow()['moved'], 0)
        self.assertEqual(self.window(rid), old)
        self.assertFalse(self.window_answer(rid)['owned'])

    def test_a_rule_recordings_ledger_moves_with_the_padding(self):
        rid = self.recording(kind='rule')
        self.follow()
        want = (self.tsdb - timedelta(minutes=30), self.stop + timedelta(minutes=60))
        self.edit(rid, *want, self.wide_id)
        m = RuleMatch.query.filter_by(recording_id=rid).one()
        self.assertEqual((m.rec_start_time, m.rec_stop_time), want)
        self.assertEqual(self.follow()['moved'], 0)
        self.assertEqual(self.window(rid), want)

    def test_moved_to_no_profile_it_keeps_no_padding(self):
        """With no profile the pass pads by the last padding ChannelBin gave the window, and the edit's
        is the last: none."""
        rid = self.recording()
        self.follow()
        self.edit(rid, self.tsdb, self.stop, None)
        self.follow()
        self.assertEqual(self.window(rid), (self.tsdb, self.stop))
        r = self.rec(rid)
        self.assertEqual((r.sports_window_start, r.sports_window_stop), (self.tsdb, self.stop))

    def test_a_tv_guide_recording_pads_from_its_listing_while_its_times_are_the_profiles(self):
        rid = self.recording(kind='guide')
        got = self.window_answer(rid)
        self.assertEqual((got['owned'], got['from_game']), (True, False))
        self.assertEqual(got['start_time'], self.listed.strftime('%Y-%m-%dT%H:%M:%S'))
        moved = self.recording(kind='guide', window=(self.listed - PRE - timedelta(minutes=7), self.stop + POST))
        got = self.window_answer(moved)
        self.assertFalse(got['owned'])
        self.assertIn('set by hand', got['why'])

    def test_a_recording_from_no_listing_says_so(self):
        rec = make_recording(status='SCHEDULED', name='By hand', channel_id=self.ch.id,
                             start_time=self.listed, stop_time=self.stop)
        db.session.commit()
        got = self.window_answer(rec.id)
        self.assertFalse(got['owned'])
        self.assertIn('not made from a guide listing', got['why'])

    def test_the_edit_events_name_times_in_the_display_timezone(self):
        """The recording page shows an event's detail as written, and the edit wrote naive UTC
        datetimes with no zone among Eastern times (dev/docs/BUGS.md 2026-11-19 @ 09:45:31 PM ET)."""
        from app.tz_utils import format_local
        rid = self.recording(kind='guide')
        start, stop = self.listed - timedelta(minutes=30), self.stop + timedelta(minutes=60)
        self.edit(rid, start, stop, self.wide_id)
        later = start + timedelta(minutes=10)
        self.edit(rid, later, stop, self.wide_id)
        repadded, typed = self.edited_events(rid)
        self.assertIn(f'→ {format_local(start)}', repadded.detail)
        self.assertIn('Start/stop time edited:', typed.detail)
        self.assertIn(f'→ {format_local(later)}', typed.detail)
        for e in (repadded, typed):
            self.assertNotIn(str(start), e.detail)
            self.assertNotIn(str(later), e.detail)


class CoverBothRepadTests(Case):
    """An edit that re-pads a followed recording to a new profile moves its Cover both second to the
    same padding in the same save, saying it was the padding; before, the second stayed at the old
    padding and the next pass moved it with an event blaming the game's time (dev/docs/BUGS.md 2026-11-25 @ 10:46:52 PM)."""

    early = timedelta(hours=2)
    hours = 1

    def setUp(self):
        super().setUp()
        sports_times.set_league(9001, {'go_by': sports_times.GO_BOTH})
        self.wide = RecordingProfile(name='Wide', pre_padding_minutes=30, post_padding_minutes=60)
        db.session.add(self.wide)
        db.session.commit()
        self.wide_id = self.wide.id

    edit = EditRepadTests.edit

    def second(self, rid):
        db.session.expire_all()
        (second,) = Recording.query.filter_by(sports_cover_for_id=rid).all()
        return second

    def test_the_second_moves_with_the_edit_and_the_pass_moves_nothing(self):
        rid = self.recording()
        self.follow()
        sid = self.second(rid).id
        want = (self.listed - timedelta(minutes=30), self.stop + timedelta(minutes=60))
        self.schedule_recording.reset_mock()
        self.edit(rid, *want, self.wide_id)
        s = self.second(rid)
        moved = (self.tsdb - timedelta(minutes=30), want[0])
        self.assertEqual((s.start_time, s.stop_time), moved)
        self.assertEqual((s.sports_window_start, s.sports_window_stop), moved)
        (ev,) = self.events(sid, RECORDING_SPORTS_RETIMED)
        self.assertIn('padding of profile "Wide"', ev.detail)
        self.assertNotIn('TheSportsDB has', ev.detail)
        extra = json.loads(ev.extra_data)
        self.assertEqual((extra['reason'], extra['pre_minutes'], extra['post_minutes']),
                         (sports_follow.RETIMED_PADDING, 30, 60))
        self.assertIn(sid, [c.args[1] for c in self.schedule_recording.call_args_list])
        self.assertEqual(self.follow()['moved'], 0)
        self.assertEqual(self.window(sid), moved)
        self.assertEqual(len(self.events(sid, RECORDING_SPORTS_RETIMED)), 1)

    def test_a_typed_window_beside_the_profile_change_leaves_the_second(self):
        rid = self.recording()
        self.follow()
        s = self.second(rid)
        before = (s.start_time, s.stop_time)
        self.edit(rid, self.listed - timedelta(minutes=12), self.stop + timedelta(minutes=60), self.wide_id)
        self.assertEqual(self.window(s.id), before)
        self.assertEqual(self.events(s.id, RECORDING_SPORTS_RETIMED), [])
