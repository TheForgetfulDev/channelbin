"""A channel group of local stations shows a different game in different cities, and a game is
taken only from members whose own guide does not show something else (dev/docs/BUGS.md
2026-11-22 @ 06:56:06 PM, dev/changelog/1690).

On prod a group of 210 CBS stations had 26 members listing one NFL game and 155 another at the
same time. The search's group collapse kept the group as a source of the first game because some
members listed it, and the play then took the group's healthiest member, which showed the other
game: three tune-ins in a row played the wrong game. A group recording of a game had the same gap
at record start and at failover.

Made-up league and teams; a throwaway temp SQLite database.

  python3 -m unittest tests.test_sports_group_regional
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (make_account, make_channel, make_epg_entry, make_group,  # noqa: E402
                                make_guide_match, make_recording)

import app.output_feed as feed  # noqa: E402
from app import admission, auto_rules, db, recorder, sports_data, sports_feed  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app import migrations as M  # noqa: E402
from app.database import (DIAGNOSTICS, GROUP_MEMBER_SELECTED, REC_STATUS_IN_PROGRESS,  # noqa: E402
                          REC_STATUS_SCHEDULED, Recording, RecordingEvent,
                          RecordingSports, SportsEvent, SportsFeedChannel, SportsFollow, SportsLeague)

TITLE = 'PFL Football'


class _Regional(unittest.TestCase):
    """Two games of one league at one time, `self.event` (the one asked about) and `self.other`."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.addCleanup(admission.reset_for_tests)
        self.addCleanup(feed.stop_all)
        self.t.sandbox_config({'integrations': {'output_feed': {'enabled': True, 'username': 'u', 'password': 'p',
                                                                'mode': 'redirect'}},
                               'display': {'timezone': 'America/New_York', 'time_format': '12h'},
                               'recording': {'live_thumbnail': {'enabled': False}}})
        self.now = datetime.utcnow().replace(second=0, microsecond=0)
        self.start = self.now - timedelta(minutes=30)
        league = SportsLeague(tsdb_id=9001, name='Pretend League', sport='Pretend Ball', current_season='2099',
                              match_terms=json.dumps(['PFL']))
        db.session.add(league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0, rule_id=0))
        self.event = SportsEvent(tsdb_id=7001, league_id=league.id, name='Ironvale Bears vs Harborview Gulls',
                                 home_team_id=101, away_team_id=102, home_name='Ironvale Bears',
                                 away_name='Harborview Gulls', start_at=self.start, guide_start=self.start,
                                 local_date=self.start.date())
        self.other = SportsEvent(tsdb_id=7002, league_id=league.id, name='Eastport Owls vs Northgate Panthers',
                                 home_team_id=103, away_team_id=104, home_name='Eastport Owls',
                                 away_name='Northgate Panthers', start_at=self.start, guide_start=self.start,
                                 local_date=self.start.date())
        db.session.add_all([self.event, self.other])
        db.session.commit()
        self.league_id = league.id
        self._n = 0
        connlim._holders.clear()
        self.addCleanup(connlim._holders.clear)

    def channel(self, score, name=None):
        self._n += 1
        ch = make_channel(make_account(f'Account {self._n}', max_connections=1), stream_id=500 + self._n,
                          name=name or f'Station {self._n}')
        ch.health_score = score
        db.session.flush()
        return ch

    def shows(self, event, *channels):
        for ch in channels:
            make_guide_match(event, ch, self.start, title=TITLE)
        db.session.commit()


class SportsChannelPlayTests(_Regional):
    """The sports channel on the IPTV output (output_feed._sports_member over game_sources())."""

    def setUp(self):
        super().setUp()
        self.assertTrue(sports_feed.set_team_channel(self.league_id, 101, True))
        self.feed_id = feed.feed_id_for_sports(SportsFeedChannel.query.one().id)

    def plays(self, exclude=()):
        db.session.expire_all()
        play = feed.resolve_play(self.feed_id, exclude_ids=frozenset(exclude), now=self.now)
        self.assertIsNotNone(play.member, play.why_none)
        return play.member.id

    def test_a_member_showing_another_game_is_never_played(self):
        """The healthiest member shows the other game; the member showing this one plays."""
        elsewhere, here = self.channel(95), self.channel(50)
        make_group('CBS Stations', members=[elsewhere, here])
        self.shows(self.other, elsewhere)
        self.shows(self.event, here)
        self.assertEqual(self.plays(), here.id)
        # Moved off it by hand (the play's failover), it does not land on the other game either.
        db.session.expire_all()
        play = feed.resolve_play(self.feed_id, exclude_ids=frozenset({here.id}), now=self.now)
        self.assertIsNone(play.member)

    def test_a_group_with_only_members_showing_another_game_says_so(self):
        off, elsewhere = self.channel(80), self.channel(95)
        make_group('CBS Stations', members=[off, elsewhere], disabled={off.id})
        self.shows(self.other, elsewhere)
        self.shows(self.event, off)
        db.session.expire_all()
        play = feed.resolve_play(self.feed_id, now=self.now)
        self.assertIsNone(play.member)
        self.assertIn('has its channels with Recording on showing something else at that time', play.why_none)

    def test_a_member_with_no_guide_comes_after_the_members_showing_the_game(self):
        """A station with no guide may show either game, so it is taken only once every member
        whose guide shows this game has failed the play."""
        blind, here, elsewhere = self.channel(99), self.channel(40), self.channel(90)
        make_group('CBS Stations', members=[blind, here, elsewhere])
        self.shows(self.other, elsewhere)
        self.shows(self.event, here)
        self.assertEqual(self.plays(), here.id)
        self.assertEqual(self.plays(exclude={here.id}), blind.id)

    def test_a_group_whose_members_all_show_another_game_is_walked_past(self):
        """The only member listing this game has Recording off, so the group has nothing to play
        and the play takes the next channel listing the game instead of the other game."""
        off, elsewhere = self.channel(80), self.channel(95)
        single = self.channel(30)
        make_group('CBS Stations', members=[off, elsewhere], disabled={off.id})
        self.shows(self.other, elsewhere)
        self.shows(self.event, off, single)
        self.assertEqual(self.plays(), single.id)

    def test_why_this_channel_says_how_many_members_were_left_out(self):
        elsewhere, here, single = self.channel(95), self.channel(50), self.channel(20)
        make_group('CBS Stations', members=[elsewhere, here])
        self.shows(self.other, elsewhere)
        self.shows(self.event, here, single)
        found = auto_rules.game_sources(self.event.id, self.now)
        self.assertIn('your channel group "CBS Stations" goes first', found.why.text)
        self.assertIn('1 of its channels shows something else at that time and is left out.', found.why.text)


class GroupRecordingTests(_Regional):
    """A group recording of a game: record start, failover and its TheSportsDB link."""

    def setUp(self):
        super().setUp()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.t.sandbox_config({'recording': {'dvr_output_dir': self.dvr,
                                             'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
                                             'live_thumbnail': {'enabled': False}}})

    def recording(self, group, source, status=REC_STATUS_SCHEDULED, channel_id=None):
        rec = make_recording(status=status, name='game', group_id=group.id, channel_id=channel_id,
                             start_time=self.now - timedelta(seconds=30), stop_time=self.now + timedelta(hours=3),
                             program_start_time=self.start, program_stop_time=self.start + timedelta(hours=3),
                             program_title=TITLE, source_channel_id=source.id if source else None)
        db.session.commit()
        return rec.id

    def begin(self, rid):
        with mock.patch('app.scheduler.reschedule_recording_start'), \
                mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rid)
        db.session.expire_all()
        return db.session.get(Recording, rid)

    def test_record_start_takes_the_member_showing_the_game(self):
        elsewhere, here = self.channel(100, 'Green Bay'), self.channel(50, 'San Diego')
        group = make_group('CBS Stations', members=[elsewhere, here])
        self.shows(self.other, elsewhere)
        self.shows(self.event, here)
        rec = self.begin(self.recording(group, here))
        self.assertEqual(rec.channel_id, here.id)
        ev = RecordingEvent.query.filter_by(recording_id=rec.id, event_type=GROUP_MEMBER_SELECTED).one()
        self.assertIn('skipped 1 member whose guide shows something other than Harborview Gulls at Ironvale '
                      'Bears at that time: "Green Bay" (Northgate Panthers at Eastport Owls)', ev.detail)

    def test_a_recording_of_no_game_is_chosen_as_before(self):
        """Not tied to a game (its listing has no match): the healthiest member, as always."""
        best, other = self.channel(100), self.channel(50)
        group = make_group('Locals', members=[best, other])
        make_epg_entry(best, title='Evening News', start_time=self.start, duration_minutes=180)
        make_epg_entry(other, title='Something Else', start_time=self.start, duration_minutes=180)
        db.session.commit()
        self.assertEqual(self.begin(self.recording(group, other)).channel_id, best.id)

    def test_no_member_showing_the_game_starts_anyway_and_says_so(self):
        """Every member with Recording on shows the other game: the start goes ahead on the best
        of them, as the format lock's zero-survivors override does, and the recording says so."""
        off, elsewhere = self.channel(90), self.channel(60, 'Green Bay')
        group = make_group('CBS Stations', members=[off, elsewhere], disabled={off.id})
        self.shows(self.other, elsewhere)
        self.shows(self.event, off)
        rec = self.begin(self.recording(group, off))
        self.assertEqual(rec.channel_id, elsewhere.id)
        ev = RecordingEvent.query.filter_by(recording_id=rec.id, event_type=DIAGNOSTICS).one()
        self.assertEqual(json.loads(ev.extra_data)['kind'], recorder.DIAGNOSTIC_KIND_OTHER_PROGRAM)
        self.assertIn('whose guide shows Northgate Panthers at Eastport Owls at this time', ev.detail)

    def test_failover_never_moves_onto_a_member_showing_another_game(self):
        here, elsewhere, also = self.channel(80), self.channel(100), self.channel(30)
        group = make_group('CBS Stations', members=[here, elsewhere, also])
        self.shows(self.other, elsewhere)
        self.shows(self.event, here, also)
        rid = self.recording(group, here, status=REC_STATUS_IN_PROGRESS, channel_id=here.id)
        recorder._active[rid] = recorder.RecordingState(current_segment_num=1)
        self.addCleanup(recorder._active.pop, rid, None)
        self.assertTrue(connlim.try_acquire(here.account_id, 'recording', rid))
        self.assertTrue(recorder.failover_group_member(self.t.app, rid, 'stream died'))
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).channel_id, also.id)
        # With that one gone too, nothing is left: it does not splice the other game in.
        self.assertFalse(recorder.failover_group_member(self.t.app, rid, 'stream died'))

    def test_the_switch_dialog_marks_a_member_showing_another_game(self):
        here, elsewhere = self.channel(80), self.channel(100, 'Green Bay')
        group = make_group('CBS Stations', members=[here, elsewhere])
        self.shows(self.other, elsewhere)
        self.shows(self.event, here)
        rid = self.recording(group, here, status=REC_STATUS_IN_PROGRESS, channel_id=here.id)
        recorder._active[rid] = recorder.RecordingState(current_segment_num=1)
        self.addCleanup(recorder._active.pop, rid, None)
        choice = next(c for c in recorder.switch_choices(rid) if c.channel.id == elsewhere.id)
        self.assertTrue(choice.ok)
        self.assertTrue(choice.warn)
        self.assertIn('Its guide shows Northgate Panthers at Eastport Owls at this time', choice.why)

    def test_the_link_is_the_game_its_listing_was_matched_to(self):
        """Every member's listing has the same title, so reading all of them is two games and no
        link. The listing it was made from says which."""
        elsewhere, here = self.channel(100), self.channel(50)
        group = make_group('CBS Stations', members=[elsewhere, here])
        self.shows(self.other, elsewhere)
        self.shows(self.event, here)
        rid = self.recording(group, here)
        self.assertEqual(sports_data.link_recording(rid), sports_data.LINK_LINKED)
        self.assertEqual(db.session.get(RecordingSports, rid).tsdb_event_id, self.event.tsdb_id)

    def test_the_create_path_records_the_listing_it_was_made_from(self):
        from app.config import load_config
        from app.database import EPGEntry
        from app.routes import recordings as rec_routes
        elsewhere, here = self.channel(100), self.channel(50)
        group = make_group('CBS Stations', members=[elsewhere, here])
        self.shows(self.event, here)
        entry = EPGEntry.query.filter_by(channel_id=here.id).one()
        with mock.patch('app.routes.recordings.schedule_recording'):
            rec = rec_routes._create_scheduled_recording(
                load_config(), name='game', url='http://example.test/live/1', start_utc=self.now + timedelta(hours=1),
                stop_utc=self.now + timedelta(hours=4), channel_id=elsewhere.id, group_id=group.id, profile_id=None,
                entry=entry)
        self.assertEqual(rec.source_channel_id, here.id)


class ScheduledStampTests(_Regional):
    """The member a scheduled group recording names before it starts
    (routes/recordings.py::_resolve_group_member) is one showing its game, the answer record
    start gives (dev/docs/BUGS.md 2026-11-26, dev/changelog/1758)."""

    def stamp(self, group, supplied, listing):
        from app.database import EPGEntry
        from app.routes import recordings as rec_routes
        entry = EPGEntry.query.filter_by(channel_id=listing.id).one() if listing is not None else None
        return rec_routes._resolve_group_member(
            group, supplied.id, self.now + timedelta(hours=1), self.now + timedelta(hours=4), 3,
            listing=rec_routes._listing_of(entry) if entry is not None else None)

    def test_a_supplied_member_showing_another_game_gives_way_to_one_showing_this_one(self):
        elsewhere, here = self.channel(100, 'Green Bay'), self.channel(50, 'San Diego')
        group = make_group('CBS Stations', members=[elsewhere, here])
        self.shows(self.other, elsewhere)
        self.shows(self.event, here)
        resolved, reason = self.stamp(group, elsewhere, here)
        self.assertEqual(resolved, here.id)
        self.assertEqual(reason, 'its guide shows Northgate Panthers at Eastport Owls at this time, not '
                                 'Harborview Gulls at Ironvale Bears')

    def test_a_supplied_member_showing_the_game_is_kept(self):
        elsewhere, here = self.channel(100), self.channel(50)
        group = make_group('CBS Stations', members=[elsewhere, here])
        self.shows(self.other, elsewhere)
        self.shows(self.event, here)
        self.assertEqual(self.stamp(group, here, here), (here.id, None))

    def test_a_member_with_no_guide_gives_way_to_one_whose_guide_shows_the_game(self):
        blind, here = self.channel(99), self.channel(40)
        group = make_group('CBS Stations', members=[blind, here])
        self.shows(self.event, here)
        resolved, reason = self.stamp(group, blind, here)
        self.assertEqual(resolved, here.id)
        self.assertIn('does not say what it shows', reason)

    def test_a_blocked_member_showing_the_game_leaves_the_one_record_start_would_take(self):
        """Record start drops a blocked member before it prefers the members whose guide shows
        the game, so with the one carrier blocked it records from the member with no guide.
        The stamp names that member too."""
        from app.account_blocks import add_account_block
        blind, here = self.channel(40), self.channel(99)
        group = make_group('CBS Stations', members=[blind, here])
        self.shows(self.event, here)
        add_account_block(here.account_id, self.now + timedelta(hours=8))
        db.session.commit()
        resolved, reason = self.stamp(group, here, here)
        self.assertEqual(resolved, blind.id)
        self.assertEqual(reason, 'its account is blocked in this window')

    def test_no_member_showing_the_game_is_stamped_as_if_no_game_were_known(self):
        """An override, never a skip: the supplied member stays, and record start says so."""
        off, elsewhere = self.channel(90), self.channel(60)
        group = make_group('CBS Stations', members=[off, elsewhere], disabled={off.id})
        self.shows(self.other, elsewhere)
        self.shows(self.event, off)
        self.assertEqual(self.stamp(group, elsewhere, off), (elsewhere.id, None))

    def test_a_listing_of_no_game_and_no_listing_at_all_change_nothing(self):
        best, other = self.channel(100), self.channel(50)
        group = make_group('Locals', members=[best, other])
        make_epg_entry(best, title='Evening News', start_time=self.start, duration_minutes=180)
        make_epg_entry(other, title='Something Else', start_time=self.start, duration_minutes=180)
        db.session.commit()
        self.assertEqual(self.stamp(group, other, other), (other.id, None))
        self.assertEqual(self.stamp(group, other, None), (other.id, None))

    def test_an_edit_asks_about_the_recording_s_own_listing(self):
        from app.routes import recordings as rec_routes
        elsewhere, here = self.channel(100), self.channel(50)
        group = make_group('CBS Stations', members=[elsewhere, here])
        self.shows(self.other, elsewhere)
        self.shows(self.event, here)
        rec = make_recording(status=REC_STATUS_SCHEDULED, name='game', group_id=group.id,
                             channel_id=elsewhere.id, start_time=self.now + timedelta(hours=1),
                             stop_time=self.now + timedelta(hours=4), program_start_time=self.start,
                             program_title=TITLE, source_channel_id=here.id)
        db.session.commit()
        resolved, _reason = rec_routes._resolve_group_member(
            group, rec.channel_id, rec.start_time, rec.stop_time, 3, exclude_recording_id=rec.id,
            listing=rec_routes._listing_of(rec))
        self.assertEqual(resolved, here.id)

    def test_the_record_dialog_s_create_stamps_the_member_showing_the_game(self):
        from app.database import EPGEntry
        from app.tz_utils import local_input_value
        elsewhere, here = self.channel(100), self.channel(50)
        group = make_group('CBS Stations', members=[elsewhere, here])
        self.shows(self.other, elsewhere)
        self.shows(self.event, here)
        entry = EPGEntry.query.filter_by(channel_id=here.id).one()
        start, stop = self.now + timedelta(hours=1), self.now + timedelta(hours=4)
        with mock.patch('app.routes.recordings.schedule_recording'):
            resp = self.t.client.post('/recordings/new-json', data={
                'name': 'game', 'url': 'http://example.test/live/1', 'channel_id': elsewhere.id,
                'group_id': group.id, 'source_epg_id': entry.id, 'force': '1',
                'start_time': local_input_value(start), 'stop_time': local_input_value(stop)})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(Recording.query.one().channel_id, here.id)


class MembershipShapeTests(_Regional):
    """sports_match.members_for_game() itself."""

    def test_a_listing_a_few_minutes_late_is_still_the_game(self):
        from app.sports_match import members_for_game
        late = self.channel(50)
        make_guide_match(self.event, late, self.start + timedelta(minutes=5), title=TITLE)
        db.session.commit()
        found = members_for_game([late.id], self.event.id, self.start, TITLE.lower())
        self.assertEqual(found.keep, {late.id})
        self.assertEqual(found.carry, {late.id})

    def test_an_unmatched_listing_under_another_title_is_left_out(self):
        from app.program_titles import title_key
        from app.sports_match import members_for_game
        news, same = self.channel(50), self.channel(50)
        make_epg_entry(news, title='Evening News', start_time=self.start, duration_minutes=180)
        make_epg_entry(same, title=TITLE, start_time=self.start, duration_minutes=180)
        db.session.commit()
        found = members_for_game([news.id, same.id], self.event.id, self.start, title_key(TITLE))
        self.assertEqual(found.other, {news.id: 'Evening News'})
        self.assertEqual(found.keep, {same.id}, 'the same title cannot be told apart, so it stays')
        self.assertEqual(found.carry, frozenset())


class M144StepTests(unittest.TestCase):
    """Migration 144 adds recordings.source_channel_id (dev/changelog/1690)."""

    def test_adds_the_column_and_is_rerunnable(self):
        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
            cur = conn.cursor()
            cur.execute('CREATE TABLE recordings (id INTEGER PRIMARY KEY, name VARCHAR(255))')
            conn.commit()
            M._m144_recording_source_channel(conn, cur)
            M._m144_recording_source_channel(conn, cur)
            self.assertIn('source_channel_id', {r[1] for r in cur.execute('PRAGMA table_info(recordings)')})
            conn.close()

    def test_registered_as_migration_144(self):
        self.assertIn((144, M._m144_recording_source_channel), [(v, fn) for v, _d, fn in M.SCHEMA_MIGRATIONS])


if __name__ == '__main__':
    unittest.main()
