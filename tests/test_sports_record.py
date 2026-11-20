"""Record and Search programs on a Sports Schedules league page's game (mockup 54 round 2,
dev/changelog/1539): the pick (app/auto_rules.py::game_pick, in an auto-record rule's own order),
the record endpoint, saving a game only an event channel names, the `event` Programs filter, the
rule refusal, and what the row shows - Record, a greyed Record and why, a recording's pill and
Manage recording. Made-up leagues and teams; throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_record
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_epg_entry, make_recording  # noqa: E402

from app import db, sports_match, sports_times  # noqa: E402
from app.database import (  # noqa: E402
    AutoRule, Recording, RecordingSports, SportsEvent, SportsFollow, SportsLeague, SportsMatch,
)
from app.program_titles import title_key  # noqa: E402
from app.tz_utils import local_input_value  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'sports_record_page.mjs')
DIALOG_HARNESS = os.path.join(REPO, 'tests', 'support', 'sports_record_dialog.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
TITLE = 'PFL Football'
SUB = 'Harborview Gulls at Ironvale Bears'


def now_utc():
    return datetime.utcnow().replace(second=0, microsecond=0)


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


class Rig:
    """A followed league, one game three hours out, and channels to list it on."""

    def __init__(self, start=None, trusted=True):
        self.now = now_utc()
        self.start = start or (self.now + timedelta(hours=3))
        self.league = SportsLeague(tsdb_id=9001, name='Pretend Football League', sport='Pretend Ball',
                                   current_season='2099', match_terms=json.dumps(['PFL']),
                                   trust_sample=12 if trusted else 0, trust_agreed=12 if trusted else 0)
        db.session.add(self.league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=self.league.id, team_tsdb_id=0, rule_id=0))
        self.event = SportsEvent(tsdb_id=7001, league_id=self.league.id, name='Harborview Gulls vs Ironvale Bears',
                                 home_team_id=1, away_team_id=2, home_name='Ironvale Bears',
                                 away_name='Harborview Gulls', start_at=self.start, local_date=self.start.date())
        db.session.add(self.event)
        self.acc = make_account()
        db.session.flush()
        self.league_id, self.event_id = self.league.id, self.event.id
        self.n = 0

    def channel(self, name=None, **kw):
        self.n += 1
        ch = make_channel(self.acc, name=name or f'Local {self.n}', **kw)
        db.session.flush()
        return ch

    def game(self, tsdb_id, start, home, away):
        """Another game in the league."""
        ev = SportsEvent(tsdb_id=tsdb_id, league_id=self.league.id, name=f'{away} vs {home}', home_team_id=tsdb_id,
                         away_team_id=tsdb_id + 1, home_name=home, away_name=away, start_at=start,
                         local_date=start.date())
        db.session.add(ev)
        db.session.flush()
        return ev

    def listing(self, ch, start=None, hours=3, airing='first', title=TITLE, sub=SUB, strength='strong', event=None):
        """A guide listing and the match the matcher would have stored for it."""
        event = event or self.event
        start = start or event.start_at
        entry = make_epg_entry(ch, title=title, sub_title=sub, start_time=start, duration_minutes=hours * 60)
        db.session.add(SportsMatch(
            event_id=event.id, channel_id=ch.id, kind='guide', program_start_time=start,
            program_stop_time=start + timedelta(hours=hours), title_key=title_key(title),
            episode_key=title_key(sub) if sub else '', program_title=title, program_sub_title=sub,
            strength=strength, airing=airing, reason='Matched on the league named in the listing.'))
        db.session.flush()
        return entry

    def name(self, ch, start=None):
        """An event channel naming the game, its clock read from the name when `start` is given."""
        db.session.add(SportsMatch(
            event_id=self.event.id, channel_id=ch.id, kind='channel_name', program_start_time=start,
            title_key=ch.name.lower(), episode_key='', program_title=ch.name, strength='strong', airing='unknown',
            reason=f'{sports_match._NAME_PREFIX}: matched on the league and both teams in it.'))
        db.session.flush()

    def hold(self, status='SCHEDULED', airing='first', ch=None, start=None, rule_id=None, name='PFL Football',
             event=None):
        """A recording linked to the game, as sports_data.link_recording() leaves it."""
        event = event or self.event
        start = start or event.start_at
        rec = make_recording(status=status, name=name, channel_id=ch.id if ch else None, start_time=start,
                             stop_time=start + timedelta(hours=3), rule_id=rule_id)
        db.session.flush()
        db.session.add(RecordingSports(
            recording_id=rec.id, tsdb_event_id=event.tsdb_id, league_name=self.league.name,
            event_name=event.name, strength='strong', airing=airing, reason='x', linked_at=self.now,
            updated_at=self.now))
        db.session.flush()
        return rec


class Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.t.sandbox_config({'display': {'timezone': 'America/New_York', 'time_format': '12h'}})
        self.client = self.t.client

    def pick(self, rig):
        db.session.commit()
        db.session.expire_all()
        return self.client.get(f'/api/sports/events/{rig.event_id}/record')

    def page(self, rig):
        db.session.commit()
        db.session.expire_all()
        r = self.client.get(f'/sports/{rig.league_id}')
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)


class PickTests(Case):
    def test_the_pick_is_the_listing_most_channels_carry_then_the_earliest(self):
        """keepers()'s order, run over the Programs search narrowed to the game."""
        rig = Rig()
        a, b, c, d, e = (rig.channel() for _ in range(5))
        rig.listing(a, title='PFL Early Show', start=rig.start - timedelta(minutes=30))
        rig.listing(b, title='PFL Early Show', start=rig.start - timedelta(minutes=30))
        wide = [rig.listing(ch) for ch in (c, d, e)]
        r = self.pick(rig)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body['kind'], 'live')
        self.assertIn(body['epg_id'], [x.id for x in wide])
        self.assertEqual(body['notice']['strong'], 'ChannelBin picked this listing.')
        self.assertIn('most of your channels carry (3 of the 5 channels with this game)', body['notice']['text'])
        self.assertIn('f.event=7001', body['search_url'])

    def moved(self, rig, believed=True):
        """2026-11-17's Mavericks-Warriors shape: one channel at the game's start, three at a time
        95 minutes later, where the guide still lists the game TheSportsDB moved."""
        if believed:
            rig.event.guide_start = rig.start
        near = rig.listing(rig.channel('KFAA'))
        far = [rig.listing(rig.channel(f'Wide {i}'), start=rig.start + timedelta(minutes=95)) for i in range(3)]
        return near, far

    def test_a_listing_at_the_believed_start_beats_more_channels_95_minutes_off(self):
        """dev/changelog/1625: Record picked ESPN's 14 channels at 9:35 over KFAA at TheSportsDB's
        8:00, which the matcher believed, and would have missed 90 minutes of the game."""
        rig = Rig()
        near, _far = self.moved(rig)
        body = self.pick(rig).get_json()
        self.assertEqual((body['kind'], body['epg_id']), ('live', near.id))
        text = body['notice']['text']
        self.assertIn("the live airing at the game's start, on 1 of the 4 channels with this game, recorded from KFAA.",
                      text)
        self.assertRegex(text, r'3 channels list it at \d+:\d\d [AP]M instead, too far from \d+:\d\d [AP]M, when '
                               r'ChannelBin believes it starts\.')
        self.assertNotIn('most of your channels carry', text)

    def test_the_record_button_ranks_the_same_way(self):
        """The row's label comes from sports_view._preview_pick(): a game that started at its
        believed start offers Record now from that listing, not Record from the later one."""
        rig = Rig(start=now_utc() - timedelta(minutes=30))
        self.moved(rig)
        self.assertRegex(self.page(rig), r'data-act="record" data-event="\d+">.*?Record now</button>')

    def test_with_no_believed_start_the_most_channels_still_win(self):
        rig = Rig()
        _near, far = self.moved(rig, believed=False)
        body = self.pick(rig).get_json()
        self.assertIn(body['epg_id'], [x.id for x in far])
        self.assertIn('most of your channels carry (3 of the 4 channels with this game)', body['notice']['text'])

    def test_only_replays_in_the_guide_records_the_replay_and_says_so(self):
        rig = Rig()
        entry = rig.listing(rig.channel(), start=rig.start + timedelta(hours=6), airing='replay')
        body = self.pick(rig).get_json()
        self.assertEqual(body['kind'], 'replay')
        self.assertEqual(body['epg_id'], entry.id)
        self.assertEqual(body['notice']['strong'], 'This is a replay.')
        self.assertIn('Only replays of this game are in your guide', body['notice']['text'])
        self.assertIn('An auto-record rule would not record a replay here.', body['notice']['text'])

    def test_an_ended_listing_is_never_picked_and_its_replay_is(self):
        rig = Rig(start=now_utc() - timedelta(hours=4))
        rig.listing(rig.channel(), hours=3)
        later = rig.listing(rig.channel(), start=now_utc() + timedelta(hours=2), airing='replay')
        body = self.pick(rig).get_json()
        self.assertEqual((body['kind'], body['epg_id']), ('replay', later.id))
        self.assertIn('The live airing of this game is over', body['notice']['text'])

    def test_every_listing_over_is_a_409_saying_so(self):
        rig = Rig(start=now_utc() - timedelta(hours=4))
        rig.listing(rig.channel(), hours=3)
        r = self.pick(rig)
        self.assertEqual(r.status_code, 409)
        self.assertIn('has ended, so there is nothing left to record', r.get_json()['error'])

    def test_a_game_on_now_is_picked_and_its_button_says_record_now(self):
        rig = Rig(start=now_utc() - timedelta(minutes=30))
        entry = rig.listing(rig.channel())
        body = self.pick(rig).get_json()
        self.assertEqual((body['kind'], body['epg_id']), ('live', entry.id))
        self.assertIn('data-act="record"', self.page(rig))
        self.assertRegex(self.page(rig), r'data-act="record" data-event="\d+">.*?Record now</button>')

    def test_a_game_already_recorded_is_a_409_not_a_second_recording(self):
        rig = Rig()
        ch = rig.channel()
        rig.listing(ch)
        rig.hold(ch=ch)
        r = self.pick(rig)
        self.assertEqual(r.status_code, 409)
        self.assertIn('already has a recording scheduled', r.get_json()['error'])

    def test_a_failed_recording_frees_record_again(self):
        rig = Rig()
        ch = rig.channel()
        rig.listing(ch)
        rig.hold(status='FAILED', ch=ch)
        self.assertEqual(self.pick(rig).status_code, 200)

    def test_a_weak_match_alone_is_not_picked_for_a_person(self):
        """A weak match must never decide a recording (design 10), here as for a rule."""
        rig = Rig()
        rig.listing(rig.channel(), strength='weak')
        r = self.pick(rig)
        self.assertEqual(r.status_code, 409)
        self.assertIn('not sure enough which listing', r.get_json()['error'])


class PickReasonTests(Case):
    """Why a game's channel was picked, from the order that picked it (auto_rules.pick_reason(),
    dev/changelog/1661), and that a recording says so."""

    def why(self, rig):
        from app import auto_rules
        db.session.commit()
        return auto_rules.game_sources(rig.event_id).why

    def test_the_widest_listing_and_a_tie_no_health_check_has_broken(self):
        rig = Rig()
        a, b, c, d, e = (rig.channel() for _ in range(5))
        rig.listing(a, title='PFL Early Show', start=rig.start - timedelta(minutes=30))
        rig.listing(b, title='PFL Early Show', start=rig.start - timedelta(minutes=30))
        for ch in (c, d, e):
            rig.listing(ch)
        why = self.why(rig)
        self.assertEqual((why.listing, why.channel, why.listing_channels, why.game_channels, why.tied),
                         ('widest', 'tie', 3, 5, 3))
        self.assertIn('the one most channels carry: 3 of the 5 that list it, across 2 listings', why.text)
        self.assertIn('3 share the best health score (50)', why.text)
        self.assertIn('no health check yet', why.text)
        self.assertEqual(why.extra()['picked']['score'], 50)

    def test_the_best_health_score_alone(self):
        rig = Rig()
        rig.listing(rig.channel())
        rig.listing(rig.channel(health_score=90.0))
        why = self.why(rig)
        self.assertEqual((why.listing, why.channel, why.score, why.tied), ('only', 'health', 90, 1))
        self.assertIn('It is the only listing of it in your guide, on 2 channels.', why.text)
        self.assertIn('this one has the best health score (90)', why.text)

    def test_a_listing_at_the_start_with_a_wider_one_too_far_off(self):
        rig = Rig()
        rig.event.guide_start = rig.start
        rig.listing(rig.channel('KFAA'))
        for i in range(3):
            rig.listing(rig.channel(f'Wide {i}'), start=rig.start + timedelta(minutes=95))
        why = self.why(rig)
        self.assertEqual((why.listing, why.channel), ('near', 'only'))
        self.assertIn('on 1 of the 4 channels that list it. A listing on 3 channels starts too far', why.text)

    def test_an_event_channel_pick_counts_the_names(self):
        rig = Rig()
        rig.name(rig.channel(name='1pm Gulls at Bears'))
        rig.name(rig.channel(name='PFL 05: Gulls @ Bears 1:00 PM ET'), start=rig.start)
        why = self.why(rig)
        self.assertEqual((why.channel, why.game_channels), ('name', 2))
        self.assertIn('a name that gives a time zone goes first', why.text)

    def save(self, rig, entry):
        db.session.commit()
        with mock.patch('app.routes.recordings.schedule_recording'):
            r = self.client.post('/recordings/new-json', data={
                'name': 'PFL Football', 'url': 'http://example.test/live/1',
                'start_time': local_input_value(entry.start_time), 'stop_time': local_input_value(entry.stop_time),
                'channel_id': str(entry.channel_id), 'source_epg_id': str(entry.id),
                'sports_event_id': str(rig.event_id), 'force': '1'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        from app.database import RECORDING_CREATED, RecordingEvent
        return RecordingEvent.query.filter_by(recording_id=r.get_json()['id'], event_type=RECORDING_CREATED).one()

    def test_a_save_from_record_says_why_on_the_recording(self):
        rig = Rig()
        rig.listing(rig.channel())
        best = rig.listing(rig.channel(health_score=90.0))
        ev = self.save(rig, best)
        self.assertIn('Record picked this channel.', ev.detail)
        self.assertIn('this one has the best health score (90)', ev.detail)
        self.assertEqual(json.loads(ev.extra_data)['picked'],
                         {'listing': 'only', 'channel': 'health', 'listing_channels': 2, 'game_channels': 2,
                          'tied': 1, 'score': 90})

    def test_a_channel_the_person_chose_instead_gets_no_reason_of_channelbins(self):
        rig = Rig()
        rig.listing(rig.channel(health_score=90.0))
        rig.listing(rig.channel(), start=rig.start + timedelta(hours=6), airing='replay')
        other = rig.listing(rig.channel('Elsewhere'), title='Some Other Show', strength='weak')
        ev = self.save(rig, other)
        self.assertNotIn('Record picked this channel', ev.detail)
        self.assertNotIn('picked', json.loads(ev.extra_data))


class SeatTests(Case):
    """Record on a league page gives the answer a rule's scheduling step gives when the picked
    listing's first channel is on an account with no connection in the window
    (auto_rules.seat_choice, dev/changelog/1602)."""

    def test_a_full_account_opens_the_dialog_on_the_same_listing_on_a_free_one(self):
        rig = Rig()
        first = rig.channel('Sports One HD', health_score=90)
        other = make_channel(make_account(name='Account Two'), name='Sports One', stream_id=900,
                             health_score=60)
        rig.listing(first)
        moved_to = rig.listing(other)
        make_recording(status='SCHEDULED', name='by hand', channel_id=rig.channel('Busy').id,
                       start_time=rig.start - timedelta(minutes=30), stop_time=rig.start + timedelta(hours=1))
        r = self.pick(rig)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body['epg_id'], moved_to.id)
        text = body['notice']['text']
        self.assertIn('recorded from Sports One.', text)
        self.assertNotIn('the healthiest of them', text)
        self.assertIn('Moved to the same listing on an account with a connection free', text)
        self.assertIn('Passed over "Sports One HD"', text)

    def test_a_first_choice_with_a_connection_is_left_alone(self):
        rig = Rig()
        first = rig.channel('Sports One HD', health_score=90)
        other = make_channel(make_account(name='Account Two'), name='Sports One', stream_id=900,
                             health_score=60)
        kept = rig.listing(first)
        rig.listing(other)
        body = self.pick(rig).get_json()
        self.assertEqual(body['epg_id'], kept.id)
        self.assertIn('Sports One HD, the healthiest of them', body['notice']['text'])
        self.assertNotIn('Passed over', body['notice']['text'])


class EventChannelTests(Case):
    def test_an_event_channel_with_a_clock_in_its_name_is_recorded_from(self):
        rig = Rig()
        ch = rig.channel(name='PFL 05: Gulls @ Bears 1:00 PM ET')
        rig.name(ch, start=rig.start)
        body = self.pick(rig).get_json()
        self.assertEqual(body['kind'], 'event')
        self.assertEqual(body['program']['channel_id'], ch.id)
        self.assertEqual(body['program']['start_time'], rig.start.strftime('%Y-%m-%dT%H:%M:%S'))
        self.assertTrue(body['program']['stream_url'])
        self.assertEqual(body['sports_event_id'], rig.event_id)
        self.assertIn("From an event channel's name.", body['notice']['strong'])
        self.assertIn('Its name gives the start', body['notice']['text'])
        self.assertIn('4 hours', body['notice']['text'])

    def test_no_time_zone_in_a_league_that_goes_by_thesportsdb_records_from_its_start(self):
        """Untrusted on purpose: the league's answer decides, not its measured record (dev/changelog/1630)."""
        rig = Rig(trusted=False)
        ch = rig.channel(name='1pm Gulls at Bears')
        rig.name(ch)
        body = self.pick(rig).get_json()
        self.assertEqual(body['kind'], 'event')
        self.assertEqual(body['program']['start_time'], rig.start.strftime('%Y-%m-%dT%H:%M:%S'))
        self.assertIn("so the start is TheSportsDB's", body['notice']['text'])
        self.assertIn('which Pretend Football League goes by', body['notice']['text'])

    def test_no_time_zone_in_a_league_that_goes_by_the_guide_greys_record_with_the_reason(self):
        rig = Rig()
        sports_times.set_league(9001, {'go_by': sports_times.GO_GUIDE})
        ch = rig.channel(name='1pm Gulls at Bears')
        rig.name(ch)
        r = self.pick(rig)
        self.assertEqual(r.status_code, 409)
        why = r.get_json()['error']
        self.assertIn('this league goes by your guide, which has no listing of it', why)
        # dev/docs/BUGS.md 2026-11-20: the one cause, never a list of what it might be.
        self.assertNotIn('TheSportsDB gives no time', why)
        self.assertNotIn(' or ', why)
        text = self.page(rig)
        self.assertRegex(text, r'class="btn btn-sm disabled" data-act="record" data-event="\d+" aria-disabled="true" '
                               r'data-why="[^"]*this league goes by your guide, which has no listing of it')

    def test_no_time_at_all_greys_record_naming_that_cause(self):
        rig = Rig()
        rig.event.start_at = None
        ch = rig.channel(name='1pm Gulls at Bears')
        rig.name(ch)
        why = self.pick(rig).get_json()['error']
        self.assertIn('TheSportsDB gives no time for it', why)
        self.assertNotIn('goes by your guide', why)

    def test_a_game_further_off_than_the_wait_with_no_listing_is_offered_on_thesportsdbs_time(self):
        """dev/docs/BUGS.md 2026-11-20: UTEP at Air Force, greyed two days out with only event channels."""
        rig = Rig(start=now_utc() + timedelta(hours=50))
        ch = rig.channel(name='PFL 493 | Gulls vs. Bears (2099-01-01 20:00:05)')
        rig.name(ch)
        body = self.pick(rig).get_json()
        self.assertEqual(body['kind'], 'event')
        self.assertEqual(body['program']['start_time'], rig.start.strftime('%Y-%m-%dT%H:%M:%S'))
        self.assertIn('the recording moves with it, unless you change its times below', body['notice']['text'])
        self.assertNotIn('aria-disabled="true" data-why', self.page(rig).split('data-act="record"')[1][:200])

    def save(self, rig, ch, start, stop):
        with mock.patch('app.routes.recordings.schedule_recording'):
            r = self.client.post('/recordings/new-json', data={
                'name': 'Harborview Gulls at Ironvale Bears', 'url': 'http://example.test/live/1',
                'start_time': local_input_value(start), 'stop_time': local_input_value(stop),
                'channel_id': str(ch.id), 'sports_event_id': str(rig.event_id), 'force': '1'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        db.session.expire_all()
        return db.session.get(Recording, r.get_json()['id'])

    def moved(self, rig, rec):
        from app import sports_follow
        ev = db.session.get(SportsEvent, rig.event_id)
        ev.previous_start, ev.start_at = ev.start_at, ev.start_at + timedelta(hours=1)
        ev.start_changed_at = datetime.utcnow()
        db.session.commit()
        with mock.patch('app.routes.recordings._rearm_replanned'):
            out = sports_follow.follow()
        db.session.expire_all()
        return out, db.session.get(Recording, rec.id)

    def test_saved_at_records_times_on_thesportsdbs_time_it_moves_with_thesportsdb(self):
        rig = Rig(start=now_utc() + timedelta(hours=50))
        ch = rig.channel(name='1pm Gulls at Bears')
        rig.name(ch)
        db.session.commit()
        rec = self.save(rig, ch, rig.start, rig.start + timedelta(hours=4))
        self.assertEqual(rec.sports_start_from, 'tsdb')
        self.assertEqual((rec.sports_window_start, rec.sports_window_stop), (rec.start_time, rec.stop_time))
        out, rec = self.moved(rig, rec)
        self.assertEqual(out['moved'], 1)
        hour = timedelta(hours=1)
        self.assertEqual((rec.start_time, rec.stop_time, rec.program_start_time),
                         (rig.start + hour, rig.start + timedelta(hours=5), rig.start + hour))
        self.assertEqual((rec.sports_window_start, rec.sports_window_stop), (rec.start_time, rec.stop_time))

    def test_saved_at_times_a_person_chose_it_keeps_them(self):
        rig = Rig(start=now_utc() + timedelta(hours=50))
        ch = rig.channel(name='1pm Gulls at Bears')
        rig.name(ch)
        db.session.commit()
        rec = self.save(rig, ch, rig.start - timedelta(minutes=30), rig.start + timedelta(hours=4))
        self.assertEqual(rec.sports_start_from, 'tsdb')
        self.assertIsNone(rec.sports_window_start)
        out, rec = self.moved(rig, rec)
        self.assertEqual((out['moved'], out['kept']), (0, 1))
        self.assertEqual(rec.program_start_time, rig.start)

    def test_saved_on_the_clock_its_channels_name_gives_it_never_follows_thesportsdb(self):
        rig = Rig()
        ch = rig.channel(name='PFL 05: Gulls @ Bears 1:00 PM ET')
        rig.name(ch, start=rig.start)
        db.session.commit()
        rec = self.save(rig, ch, rig.start, rig.start + timedelta(hours=4))
        self.assertEqual(rec.sports_start_from, 'name')
        self.assertIsNone(rec.sports_window_start)
        out, rec = self.moved(rig, rec)
        self.assertEqual((out['moved'], out['kept']), (0, 0))
        self.assertEqual(rec.program_start_time, rig.start)

    def test_saving_it_records_the_pick_and_links_the_game(self):
        rig = Rig()
        ch = rig.channel(name='PFL 05: Gulls @ Bears 1:00 PM ET')
        rig.name(ch, start=rig.start)
        db.session.commit()
        start, stop = rig.start - timedelta(minutes=5), rig.start + timedelta(hours=4)
        with mock.patch('app.routes.recordings.schedule_recording'):
            r = self.client.post('/recordings/new-json', data={
                'name': 'Harborview Gulls at Ironvale Bears', 'url': 'http://example.test/live/1',
                'start_time': local_input_value(start), 'stop_time': local_input_value(stop),
                'channel_id': str(ch.id), 'sports_event_id': str(rig.event_id), 'force': '1'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        db.session.expire_all()
        rec = db.session.get(Recording, r.get_json()['id'])
        self.assertEqual(rec.program_start_time, rig.start)
        self.assertEqual(rec.program_stop_time, rig.start + timedelta(hours=4))
        self.assertEqual(rec.program_title, 'Harborview Gulls at Ironvale Bears')
        link = db.session.get(RecordingSports, rec.id)
        self.assertIsNotNone(link)
        self.assertEqual(link.tsdb_event_id, 7001)

    def test_saving_on_a_channel_that_no_longer_names_it_is_refused(self):
        rig = Rig()
        ch = rig.channel(name='PFL 05: Gulls @ Bears 1:00 PM ET')
        rig.name(ch, start=rig.start)
        other = rig.channel(name='Something else')
        db.session.commit()
        r = self.client.post('/recordings/new-json', data={
            'name': 'x', 'url': 'http://example.test/live/9', 'start_time': local_input_value(rig.start),
            'stop_time': local_input_value(rig.start + timedelta(hours=4)), 'channel_id': str(other.id), 'sports_event_id': str(rig.event_id), 'force': '1'})
        self.assertEqual(r.status_code, 409)
        self.assertIn('changed since the dialog opened', r.get_json()['error'])
        self.assertEqual(Recording.query.count(), 0)


class SearchTests(Case):
    def test_the_event_filter_finds_the_games_listings_and_nothing_else(self):
        rig = Rig()
        a, b = rig.channel(), rig.channel()
        mine = rig.listing(a)
        make_epg_entry(b, title=TITLE, sub_title='Northgate Panthers at Saint-Remy Comets', start_time=rig.start)
        rig.listing(rig.channel(), airing='replay', start=rig.start + timedelta(hours=5))
        gone = rig.listing(rig.channel(), start=rig.start + timedelta(hours=8))
        SportsMatch.query.filter_by(program_start_time=rig.start + timedelta(hours=8)).update(
            {'rejected_at': rig.now})
        db.session.commit()
        r = self.client.get('/api/channels/search?grain=airings&f.event=7001')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ids = {row['id'] for row in r.get_json()['rows']}
        self.assertIn(mine.id, ids)
        self.assertNotIn(gone.id, ids)
        self.assertEqual(len(ids), 2)

    def test_a_rule_cannot_be_made_from_a_one_game_search(self):
        from app.auto_rules import RuleError, clean_search
        with self.assertRaises(RuleError) as caught:
            clean_search('grain=airings&f.event=7001')
        self.assertIn('cannot be about one game', str(caught.exception))
        r = self.client.post('/api/auto-rules', json={'name': 'One game', 'search': 'grain=airings&f.event=7001'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(AutoRule.query.count(), 0)
        # Leaving one game out is an ordinary rule.
        clean_search('grain=airings&q=pfl&x.event=7001')

    def test_the_search_page_names_the_game_from_the_database(self):
        Rig()
        db.session.commit()
        text = self.client.get('/channels?grain=airings&f.event=7001').get_data(as_text=True)
        self.assertIn('"7001": {"name": "Harborview Gulls at Ironvale Bears", "noun": "game"}', text)

    def test_a_digit_int_refuses_in_the_event_filter_is_not_a_500(self):
        """dev/docs/BUGS.md 2026-11-15: str.isdigit() is true of a superscript two and int()
        raises on it, so the page that names the game crashed on such a link."""
        Rig()
        db.session.commit()
        r = self.client.get('/channels?grain=airings&f.event=\u00b2&x.event=7001')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        self.assertIn('"7001"', r.get_data(as_text=True))


class RowTests(Case):
    def test_a_game_with_listings_offers_record_and_search_programs(self):
        rig = Rig()
        rig.listing(rig.channel())
        text = self.page(rig)
        self.assertRegex(text, r'<button class="btn btn-sm" data-act="record" data-event="\d+">'
                               r'<svg[^>]*data-icon="record".*?Record</button>')
        self.assertRegex(text, r'href="/channels\?[^"]*f\.event=7001[^"]*">Search programs')
        self.assertIn('View on TheSportsDB', text)
        self.assertNotIn('Edit on TheSportsDB', text)
        self.assertNotIn('Manage recording', text)

    def test_record_is_on_the_row_and_in_no_menu(self):
        rig = Rig()
        rig.listing(rig.channel())
        text = self.page(rig)
        menus = [chunk[:chunk.index('</span>')] for chunk in text.split('<div class="menu pop-left">')[1:]]
        self.assertEqual(len(menus), 2)
        for menu in menus:
            self.assertNotIn('data-act="record"', menu)

    def test_a_scheduled_recording_is_a_red_pill_and_manage_recording(self):
        rig = Rig()
        ch = rig.channel()
        rig.listing(ch)
        rule = AutoRule(name='Bears games', search_params='grain=airings', enabled=True)
        db.session.add(rule)
        db.session.flush()
        rec = rig.hold(ch=ch, rule_id=rule.id)
        text = self.page(rig)
        self.assertRegex(text, r'<span class="sp-held"><span class="badge b-live"><svg[^>]*data-icon="record"[^>]*>.*?'
                               r'</svg>Recording scheduled</span></span><span class="s">On Local 1, by the Bears games rule')
        self.assertIn('Manage recording', text)
        self.assertNotIn('data-act="record"', text)
        self.assertIn(f'data-act="edit-rec" data-rec="{rec.id}"', text)
        self.assertIn(f'href="/recordings/{rec.id}">Open recording', text)
        self.assertIn('>Rule: Bears games', text)
        self.assertIn('will not schedule this game again', text)
        self.assertNotIn('data-act="stop-rec"', text)

    def test_a_recording_under_way_pulses_and_offers_stop(self):
        rig = Rig(start=now_utc() - timedelta(minutes=40))
        ch = rig.channel()
        rig.listing(ch)
        rig.hold(status='IN_PROGRESS', ch=ch)
        text = self.page(rig)
        self.assertIn('<span class="badge b-live"><span class="pulse"></span>Recording now</span>', text)
        self.assertIn('data-act="stop-rec"', text)
        self.assertNotIn('data-act="edit-rec"', text)
        self.assertNotIn('data-act="cancel-rec"', text)

    def test_a_scheduled_replay_keeps_record_for_the_live_airing(self):
        rig = Rig()
        ch = rig.channel()
        rig.listing(ch)
        rec = rig.hold(airing='replay', ch=ch, start=rig.start + timedelta(hours=6))
        text = self.page(rig)
        self.assertIn('Replay scheduled', text)
        self.assertIn('data-act="record"', text)
        self.assertIn(f'href="/recordings/{rec.id}">Replay recording', text)

    def test_a_replay_already_scheduled_is_the_games_recording_when_the_replay_is_all_that_is_left(self):
        """Record would pick the very replay the game has a recording of: no second one."""
        rig = Rig(start=now_utc() - timedelta(hours=4))
        ch = rig.channel()
        rig.listing(ch, hours=3)
        later = now_utc() + timedelta(hours=2)
        rig.listing(rig.channel(), start=later, airing='replay')
        rig.hold(airing='replay', ch=ch, start=later)
        text = self.page(rig)
        self.assertIn('Replay scheduled', text)
        self.assertIn('Manage recording', text)
        self.assertNotIn('data-act="record"', text)
        r = self.pick(rig)
        self.assertEqual(r.status_code, 409)
        self.assertIn('already has its replay scheduled', r.get_json()['error'])

    def test_a_failed_recording_still_shows_and_record_is_offered_again(self):
        """Principle 1: what happened stays on the row, and Record comes back."""
        rig = Rig()
        ch = rig.channel()
        rig.listing(ch)
        rec = rig.hold(status='FAILED', ch=ch)
        text = self.page(rig)
        self.assertIn('<span class="badge b-fail">Recording failed</span>', text)
        self.assertIn('Open recording says what happened.', text)
        self.assertIn('data-act="record"', text)
        self.assertIn(f'href="/recordings/{rec.id}">Failed recording', text)

    def test_a_game_that_started_this_morning_stays_on_the_page(self):
        """The league page keeps a game for its day, so its recording can be managed there."""
        from app.tz_utils import to_local
        from zoneinfo import ZoneInfo
        tz = ZoneInfo('America/New_York')
        now = now_utc()
        local_midnight = to_local(now, tz).replace(hour=0, minute=0)
        start = max(local_midnight.astimezone(ZoneInfo('UTC')).replace(tzinfo=None) + timedelta(minutes=5),
                    now - timedelta(hours=20))
        if start >= now:
            self.skipTest('just after local midnight: no earlier moment today to start a game at')
        rig = Rig(start=start)
        rig.listing(rig.channel())
        self.assertIn('Harborview Gulls at Ironvale Bears', self.page(rig))

    def test_a_game_not_in_the_guide_greys_record_and_search_with_why(self):
        rig = Rig()
        text = self.page(rig)
        self.assertRegex(text, r'data-act="record" data-event="\d+" aria-disabled="true" data-why="This game is not '
                               r'in your guide yet, so there is nothing to record.')
        self.assertIn('data-act="search-why"', text)


@unittest.skipIf(shutil.which('node') is None or not os.path.isdir(JSDOM), 'node or jsdom not installed')
class RowScriptTests(unittest.TestCase):
    """static/js/sports.js's Record and Manage recording, over the page the app rendered."""

    @classmethod
    def setUpClass(cls):
        t = make_test_app(csrf=False)
        try:
            t.sandbox_config({'display': {'timezone': 'America/New_York', 'time_format': '12h'}})
            rig = Rig()
            rig.listing(rig.channel())                                           # Record
            rig.game(7002, rig.start + timedelta(days=2), 'Northgate Panthers', 'Saint-Remy Comets')  # greyed
            held = rig.game(7003, rig.start + timedelta(days=1), 'Ashford Owls', 'Bellmere Foxes')
            ch = rig.channel()
            rig.listing(ch, event=held)
            rule = AutoRule(name='Owls games', search_params='grain=airings', enabled=True)
            db.session.add(rule)
            db.session.flush()
            rig.hold(ch=ch, event=held, rule_id=rule.id, name='Bellmere Foxes at Ashford Owls')
            on = rig.game(7004, now_utc() - timedelta(minutes=30), 'Cold Harbor Elks', 'Dunmore Hawks')
            ch2 = rig.channel()
            rig.listing(ch2, event=on)
            rig.hold(status='IN_PROGRESS', ch=ch2, event=on, name='Dunmore Hawks at Cold Harbor Elks')
            db.session.commit()
            cls.league_id = rig.league_id
            html = t.client.get(f'/sports/{rig.league_id}').get_data(as_text=True)
        finally:
            t.cleanup()
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'league.html'), 'w', encoding='utf-8') as fh:
                fh.write(html)
            proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO], capture_output=True, text=True,
                                  cwd=REPO, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
        cls.obs = json.loads(proc.stdout)

    def test_no_script_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_a_greyed_record_says_why_and_asks_the_server_nothing(self):
        self.assertIn('is not in your guide yet', self.obs['greyed_toast'])
        self.assertEqual(self.obs['greyed_requests'], 0)

    def test_record_asks_for_the_pick_then_opens_the_shared_dialog_on_it(self):
        self.assertEqual(len(self.obs['record_requests']), 2)
        self.assertRegex(self.obs['record_requests'][0], r'^GET /api/sports/events/\d+/record$')
        self.assertEqual(self.obs['record_requests'][1], 'GET /api/channels/airings/55/record-context?group=9')
        opened = self.obs['opened']
        self.assertEqual(opened['prog']['id'], 55)
        self.assertEqual(opened['opts']['notice']['strong'], 'ChannelBin picked this listing.')
        self.assertEqual(opened['opts']['notice']['searchUrl'], '/channels?grain=airings&f.event=7001')

    def test_search_programs_greyed_says_why(self):
        self.assertIn('a search would find nothing', self.obs['search_toast'])

    def test_edit_recording_opens_the_dialog_in_edit_mode(self):
        prog = self.obs['edit']['prog']
        self.assertTrue(prog['has_recording'])
        self.assertEqual(prog['recording_status'], 'SCHEDULED')
        self.assertEqual(prog['title'], 'Bellmere Foxes at Ashford Owls')

    def test_cancel_uses_the_recordings_lists_confirm_and_route(self):
        self.assertEqual(self.obs['cancel_title'], 'Cancel scheduled recording')
        self.assertIn('"Bellmere Foxes at Ashford Owls" will be removed.', self.obs['cancel_body'])
        self.assertIn('The Owls games rule made it, and will not schedule this game again.', self.obs['cancel_body'])
        self.assertEqual(len(self.obs['cancel_requests']), 1)
        self.assertRegex(self.obs['cancel_requests'][0], r'^POST /recordings/\d+/cancel-json$')
        self.assertGreaterEqual(self.obs['cancel_swaps'], 1)

    def test_stop_uses_the_recordings_lists_confirm_and_route(self):
        self.assertEqual(self.obs['stop_title'], 'Stop recording')
        self.assertRegex(self.obs['stop_requests'][0], r'^POST /recordings/\d+/stop-json$')
        self.assertGreaterEqual(self.obs['stop_swaps'], 1)

    def test_a_save_from_the_dialog_swaps_the_page(self):
        self.assertGreaterEqual(self.obs['saved_swaps'], 1)


class GameTimesTests(Case):
    """Record's times by the league's answer to which time a game goes by (app/sports_times.py,
    dev/changelog/1630). TheSportsDB has the game three hours out; the guide lists it earlier."""

    def rig(self, early, hours=5):
        rig = Rig()
        listed = rig.start - early
        entry = rig.listing(rig.channel(), start=listed, hours=hours)
        rig.event.guide_start = listed
        return rig, entry, listed

    def times(self, body):
        return body['times']['start_time'], body['times']['stop_time']

    def test_go_by_thesportsdb_starts_the_dialog_at_its_time_and_ends_that_much_later(self):
        rig, _entry, listed = self.rig(timedelta(minutes=75))
        body = self.pick(rig).get_json()
        self.assertEqual(self.times(body), (iso(rig.start), iso(listed + timedelta(hours=5, minutes=75))))
        self.assertIn('the default for every league, so the recording starts at', body['notice']['text'])

    def test_your_guide_keeps_the_listings_times(self):
        rig, _entry, listed = self.rig(timedelta(minutes=75))
        sports_times.set_league(9001, {'go_by': sports_times.GO_GUIDE})
        body = self.pick(rig).get_json()
        self.assertEqual(self.times(body), (iso(listed), iso(listed + timedelta(hours=5))))
        self.assertIn("goes by your guide, so the recording keeps the listing's times", body['notice']['text'])

    def test_cover_both_past_the_limit_keeps_the_listing_and_names_the_second_time(self):
        rig, _entry, listed = self.rig(timedelta(hours=4), hours=2)
        sports_times.set_league(9001, {'go_by': sports_times.GO_BOTH})
        body = self.pick(rig).get_json()
        self.assertEqual(self.times(body), (iso(listed), iso(listed + timedelta(hours=2))))
        self.assertIn('more than the 90-minute limit', body['notice']['text'])
        self.assertIn('ChannelBin schedules a second one at', body['notice']['text'])

    def test_further_off_than_the_wait_keeps_the_listing_and_says_why(self):
        rig, _entry, listed = self.rig(timedelta(minutes=75))
        sports_times.set_league(9001, {'wait_hours': 1})
        body = self.pick(rig).get_json()
        self.assertEqual(self.times(body), (iso(listed), iso(listed + timedelta(hours=5))))
        self.assertIn("only from 1 hour before a game", body['notice']['text'])

    def save(self, entry, event_id):
        db.session.commit()
        with mock.patch('app.routes.recordings.schedule_recording'):
            r = self.client.post('/recordings/new-json', data={
                'name': 'PFL Football', 'url': 'http://example.test/live/1',
                'start_time': local_input_value(entry.start_time), 'stop_time': local_input_value(entry.stop_time),
                'channel_id': str(entry.channel_id), 'source_epg_id': str(entry.id),
                'sports_event_id': str(event_id), 'force': '1'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        db.session.expire_all()
        return db.session.get(Recording, r.get_json()['id'])

    def test_a_save_from_record_marks_the_recording_as_the_games(self):
        rig, entry, _listed = self.rig(timedelta(minutes=75))
        self.assertEqual(self.save(entry, rig.event_id).sports_record_tsdb_id, 7001)

    def test_a_listing_that_is_not_the_games_is_never_marked(self):
        """The page's event id is checked against the matcher's listings, never taken on trust."""
        rig, entry, _listed = self.rig(timedelta(minutes=75))
        other = rig.game(7100, rig.start + timedelta(days=1), 'Saltmarsh Otters', 'Harborview Gulls')
        self.assertIsNone(self.save(entry, other.id).sports_record_tsdb_id)


@unittest.skipIf(shutil.which('node') is None or not os.path.isdir(JSDOM), 'node or jsdom not installed')
class RecordDialogScriptTests(unittest.TestCase):
    """The shared record dialog (static/js/guide.js::openModal, templates/_record_modal.html) on the
    league page: Record's notice and event field are drawn, and cleared when any other page opens it."""

    @classmethod
    def setUpClass(cls):
        t = make_test_app(csrf=False)
        try:
            t.sandbox_config({'display': {'timezone': 'America/New_York', 'time_format': '12h'}})
            rig = Rig()
            entry = rig.listing(rig.channel())
            named = rig.channel(name='PFL 05: Gulls @ Bears 1:00 PM ET')
            db.session.commit()
            html = t.client.get(f'/sports/{rig.league_id}').get_data(as_text=True)
            live = t.client.get(f'/api/sports/events/{rig.event_id}/record').get_data(as_text=True)
            cls.event_id = rig.event_id
            context = t.client.get(f'/api/channels/airings/{entry.id}/record-context').get_data(as_text=True)
            rig.name(named, start=rig.start)
            SportsMatch.query.filter_by(kind='guide').delete()
            db.session.commit()
            event = t.client.get(f'/api/sports/events/{rig.event_id}/record').get_data(as_text=True)
        finally:
            t.cleanup()
        with tempfile.TemporaryDirectory() as tmp:
            for name, body in (('league.html', html), ('live.json', live), ('context.json', context),
                               ('event.json', event)):
                with open(os.path.join(tmp, name), 'w', encoding='utf-8') as fh:
                    fh.write(body)
            proc = subprocess.run([shutil.which('node'), DIALOG_HARNESS, tmp, REPO], capture_output=True,
                                  text=True, cwd=REPO, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
        cls.obs = json.loads(proc.stdout)

    def test_guide_js_runs_on_the_league_page_without_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_a_listing_pick_draws_its_notice_and_carries_its_event(self):
        """The event id rides along so the save can mark the recording as this game's, once the
        server has checked the listing is one of its own (dev/changelog/1630)."""
        live = self.obs['live']
        self.assertTrue(live['open'])
        self.assertTrue(live['note_shown'])
        self.assertIn('notice-info', live['note_class'])
        self.assertTrue(live['note_text'].startswith('ChannelBin picked this listing.'))
        self.assertIn('f.event=7001', live['note_link'])
        self.assertEqual(live['sports_event'], str(self.event_id))
        self.assertNotEqual(live['epg_id'], '')

    def test_an_event_channel_pick_carries_the_event_and_draws_its_head(self):
        ev = self.obs['event']
        self.assertIn('notice-warn', ev['note_class'])
        self.assertTrue(ev['note_text'].startswith("From an event channel's name."))
        self.assertNotEqual(ev['sports_event'], '')
        self.assertEqual(ev['epg_id'], '')
        self.assertIn('Harborview Gulls at Ironvale Bears', ev['head'])
        self.assertIn('PFL 05: Gulls @ Bears 1:00 PM ET', ev['head'])

    def test_any_other_open_clears_both(self):
        plain = self.obs['plain']
        self.assertTrue(plain['open'])
        self.assertFalse(plain['note_shown'])
        self.assertEqual(plain['note_text'], '')
        self.assertEqual(plain['sports_event'], '')
        self.assertEqual(plain['epg_id'], '77')


if __name__ == '__main__':
    unittest.main()
