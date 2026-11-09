"""Matching event channels by their names (app/sports_match.py, app/event_names.py, design 6.8, 9).

An event channel has no real guide listing, so its name is the only one it has. Every name is a shape
seen on prod's accounts (2026-11-04 and 2026-11-07) with made-up leagues and teams: nothing received
from TheSportsDB ships (design 13). Throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_name_match
"""
import json
import os
import re
import sys
import threading
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_epg_entry  # noqa: E402

from app import db, sports_match  # noqa: E402
from app.accounts import _do_sync  # noqa: E402
from app.database import (  # noqa: E402
    Channel, M3uAccount, SPORTS_AIRING_FIRST, SPORTS_AIRING_REPLAY, SPORTS_AIRING_UNKNOWN, SPORTS_MATCH_KIND_CHANNEL_NAME,
    SPORTS_MATCH_KIND_GUIDE, SPORTS_MATCH_STRONG, SPORTS_MATCH_WEAK, SportsEvent, SportsLeague,
    SportsLeagueCategory, SportsMatch, SportsTeam,
)
from sim import load_world, serve_for_test  # noqa: E402

SMALL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sim', 'worlds', 'small.yaml')

NOW = datetime(2099, 11, 7, 12, 0)
#: 1pm Eastern on Sunday Nov 8, 2099.
FACEOFF = datetime(2099, 11, 8, 18, 0)


class NameCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.account = make_account()
        self.football = self.league(9001, ['Pretend Football League', 'PFL'])
        self.hockey = self.league(9002, ['Pretend Hockey League', 'PHL'])
        self.teams = {}
        for tid, full, nick in ((1, 'Harborview Gulls', 'Gulls'), (2, 'Ironvale Bears', 'Bears'),
                                (3, 'Northgate Panthers', 'Panthers'), (4, 'Metro City Giants', 'Giants'),
                                (11, 'Harborview Lightning', 'Lightning'), (12, 'Ironvale Blackhawks', 'Blackhawks'),
                                (13, 'Northgate Owls', 'Owls'), (14, 'Saint-Rémy Comets', 'Comets')):
            self.teams[tid] = full
            db.session.add(SportsTeam(tsdb_id=tid, full_name=full, names=json.dumps([full, nick]),
                                      city=full[:-len(nick)].strip(), last_looked_up=NOW))
        db.session.commit()

    def league(self, tsdb_id, terms, **kw):
        league = SportsLeague(tsdb_id=tsdb_id, name=terms[0], match_terms=json.dumps(terms), **kw)
        db.session.add(league)
        db.session.commit()
        return league

    def game(self, home, away, start=FACEOFF, league=None):
        league = league or self.hockey
        event = SportsEvent(tsdb_id=SportsEvent.query.count() + 100, league_id=league.id,
                            name=f'{self.teams[home]} vs {self.teams[away]}', home_team_id=home, away_team_id=away,
                            home_name=self.teams[home], away_name=self.teams[away], start_at=start,
                            local_date=start.date())
        db.session.add(event)
        db.session.commit()
        return event

    def race(self, name, start, league):
        event = SportsEvent(tsdb_id=SportsEvent.query.count() + 500, league_id=league.id, name=name, start_at=start,
                            local_date=start.date())
        db.session.add(event)
        db.session.commit()
        return event

    def channel(self, name, category=None):
        ch = make_channel(self.account, name=name, category_name=category)
        db.session.commit()
        return ch

    def tie(self, league, category, replay=False):
        db.session.add(SportsLeagueCategory(league_id=league.id, category_name=category, is_replay=replay))
        db.session.commit()

    def rename(self, channel, name):
        db.session.get(Channel, channel.id).name = name
        db.session.commit()

    def run_match(self, now=NOW):
        note = sports_match.run(now=now, pause=0)
        db.session.expire_all()
        return note

    def named(self, event, **kw):
        return SportsMatch.query.filter_by(event_id=event.id, kind=SPORTS_MATCH_KIND_CHANNEL_NAME, **kw).all()

    def trust(self, league):
        league.trust_sample, league.trust_agreed = 30, 30
        db.session.commit()


class ReadingTests(NameCase):
    def test_a_viaplay_name_with_a_timezone(self):
        """League in the text and both teams by city is weak by the 6.3 table; the time in the name
        agrees, and a NEXT slot is the live feed."""
        event = self.game(11, 12)
        ch = self.channel('NEXT | PHL HARBORVIEW - IRONVALE | Sun 08 Nov 13:00 EST (US) | 8K EXCLUSIVE | US: PPV 2')
        note = self.run_match()
        (m,) = self.named(event)
        self.assertEqual((m.channel_id, m.strength, m.airing), (ch.id, SPORTS_MATCH_WEAK, SPORTS_AIRING_FIRST))
        self.assertEqual(m.program_start_time, FACEOFF)
        self.assertTrue(m.reason.startswith("Read from the channel's name, not a guide listing: "))
        self.assertIn('the teams by city', m.reason)
        self.assertIn('08 Nov 13:00 EST', m.reason)
        self.assertEqual(m.program_title, ch.name)
        self.assertIn('named by 1 event channel, read from their names', note)

    def test_a_tied_category_supplies_the_league_and_a_name_with_no_date(self):
        """`US| NHL PPV` names do not say NHL. With no date, the game is the only one between these
        teams within a week, and doubt about the airing is unknown."""
        event = self.game(11, 12)
        self.tie(self.hockey, 'US| PHL PPV')
        self.channel('PHL 01: 1pm Blackhawks @ Lightning', category='US| PHL PPV')
        self.channel('PPV 02: 1pm Blackhawks @ Lightning', category='US| PHL PPV')
        self.run_match()
        found = self.named(event)
        self.assertEqual(len(found), 2)
        for m in found:
            self.assertEqual((m.strength, m.airing), (SPORTS_MATCH_STRONG, SPORTS_AIRING_UNKNOWN))
            self.assertIsNone(m.program_start_time)
            self.assertIn('its name gives no date, so this is the only game', m.reason)

    def test_a_clock_with_no_timezone_counts_on_its_date(self):
        """`@ Nov 7 12:30 PM :Sportsnet+` is local to somewhere unknown, so only its date is used, and
        it picks the game on that date over a later one of the same pair."""
        sunday = self.game(11, 12)
        later = self.game(12, 11, start=FACEOFF + timedelta(days=3))
        self.channel('PHL: Harborview Lightning vs Ironvale Blackhawks @ Nov 11 7:00 PM :Sportsnet+  08')
        self.run_match()
        self.assertEqual(self.named(sunday), [])
        (m,) = self.named(later)
        self.assertIn('the date in its name, which gives no timezone', m.reason)
        self.assertEqual(m.strength, SPORTS_MATCH_STRONG)

    def test_a_date_with_no_timezone_and_games_on_consecutive_days_is_not_matched(self):
        """`Nov 8 01:00 :Telia FI` is Finland's time for a Nov 7 evening game in North America, so
        with the rematch on Nov 8 the date cannot tell the two apart, and neither is picked."""
        first = self.game(11, 12, start=datetime(2099, 11, 8, 0, 0))
        first.local_date = date(2099, 11, 7)
        db.session.commit()
        rematch = self.game(12, 11, start=datetime(2099, 11, 9, 0, 0))
        self.channel('PHL: Lightning vs Blackhawks @ Nov 8 01:00 :Telia FI  02')
        self.run_match()
        self.assertEqual(self.named(first) + self.named(rematch), [])

    def test_a_channel_its_provider_no_longer_lists_is_not_read(self):
        """A dropped channel keeps its last name; that name must not match the pair's next game."""
        event = self.game(11, 12)
        seen = NOW - timedelta(hours=2)
        dropped = self.channel('PHL 01: 7pm Blackhawks @ Lightning')
        listed = self.channel('PHL 02: 7pm Blackhawks @ Lightning')
        db.session.get(Channel, dropped.id).last_seen_at = seen - timedelta(hours=12)
        db.session.get(Channel, listed.id).last_seen_at = seen
        db.session.commit()
        self.run_match()
        self.assertEqual([m.channel_id for m in self.named(event)], [listed.id])

    def test_a_channel_with_guide_rows_is_not_read_by_its_name(self):
        event = self.game(11, 12)
        ch = self.channel('NEXT | PHL HARBORVIEW - IRONVALE | Sun 08 Nov 13:00 EST (US)')
        make_epg_entry(ch, title='Pretend Hockey Tonight', start_time=NOW, duration_minutes=60)
        db.session.commit()
        self.run_match()
        self.assertEqual(self.named(event), [])

    def filler(self, ch, start=FACEOFF - timedelta(hours=6), blocks=6, **kw):
        """A guide source's filler: the channel's own name in 2-hour blocks, as on two of prod's three
        accounts on 2026-11-08 (about 10,700 channels)."""
        for i in range(blocks):
            make_epg_entry(ch, title=ch.name, start_time=start + timedelta(hours=2 * i),
                           duration_minutes=120, **kw)
        db.session.commit()

    def test_a_channel_whose_guide_only_repeats_its_name_is_read_by_its_name(self):
        """dev/docs/BUGS.md 2026-11-08 10:07: filler hid an event channel from name matching."""
        event = self.game(11, 12)
        ch = self.channel('PHL  | 06 - 1pm Blackhawks at Lightning')
        self.filler(ch)
        make_epg_entry(ch, title='  phl  | 06 - 1pm blackhawks at lightning ', start_time=FACEOFF + timedelta(hours=12),
                       duration_minutes=120, sub_title=' ')
        db.session.commit()
        self.run_match()
        (m,) = self.named(event)
        self.assertEqual((m.channel_id, m.strength, m.airing), (ch.id, SPORTS_MATCH_STRONG, SPORTS_AIRING_UNKNOWN))

    def test_filler_is_never_a_guide_match(self):
        """dev/docs/BUGS.md 2026-11-08 10:07: the block at the start read as the live airing and the
        blocks after it, during and after the game, as its replays."""
        event = self.game(11, 12)
        self.filler(self.channel('PHL  | 06 - 1pm Blackhawks at Lightning'))
        self.run_match()
        self.assertEqual(SportsMatch.query.filter_by(event_id=event.id, kind=SPORTS_MATCH_KIND_GUIDE).count(), 0)

    def test_a_listing_titled_like_its_channel_with_more_to_say_is_a_listing(self):
        """Only a bare repeat of the name is filler: a description makes it a listing like any other."""
        event = self.game(11, 12)
        ch = self.channel('PHL: Blackhawks at Lightning')
        make_epg_entry(ch, title='PHL: Blackhawks at Lightning', start_time=FACEOFF, duration_minutes=180,
                       description='The Lightning host the Blackhawks.')
        db.session.commit()
        self.run_match()
        self.assertEqual(self.named(event), [])
        (m,) = SportsMatch.query.filter_by(event_id=event.id, kind=SPORTS_MATCH_KIND_GUIDE).all()
        self.assertEqual(m.program_start_time, FACEOFF)

    def test_a_listing_titled_like_its_channel_with_a_sub_title_is_a_listing(self):
        """A sub-title alone, with no description, also makes it a listing."""
        event = self.game(11, 12)
        ch = self.channel('PHL Tonight')
        make_epg_entry(ch, title='PHL Tonight', sub_title='Blackhawks at Lightning', start_time=FACEOFF,
                       duration_minutes=180)
        db.session.commit()
        self.run_match()
        self.assertEqual(SportsMatch.query.filter_by(event_id=event.id, kind=SPORTS_MATCH_KIND_GUIDE).count(), 1)

    def test_an_empty_slot_and_a_team_channel_are_not_events(self):
        event = self.game(11, 12)
        for name in ('PHL | 05 -', 'PHL 05: No Game Today', '- NO EVENT STREAMING - | 8K EXCLUSIVE',
                     'PHL TEAM| HARBORVIEW LIGHTNING'):
            self.channel(name, category='US| PHL PPV')
        self.tie(self.hockey, 'US| PHL PPV')
        self.run_match()
        self.assertEqual(self.named(event), [])

    def test_a_name_naming_several_games_is_not_matched(self):
        """ESPN+ `NHL: MTL vs. TOR • NJ vs. NYI` is a schedule, not one slot."""
        a, b = self.game(11, 12), self.game(13, 14)
        self.channel('US (ESPN+ 420) | PHL: Lightning vs. Blackhawks • Owls vs. Comets (2099-11-08 12:04:05)')
        self.run_match()
        self.assertEqual(self.named(a) + self.named(b), [])

    def test_a_highlights_name_is_not_a_game(self):
        event = self.game(11, 12)
        self.channel('PHL Highlights: Lightning @ Blackhawks Nov 8')
        self.run_match()
        self.assertEqual(self.named(event), [])


class FalseMatchTests(NameCase):
    def test_another_leagues_cities_do_not_match(self):
        """`NHL TAMPA BAY - CHICAGO` matched the NFL's Buccaneers at Bears in the investigation."""
        football = self.game(1, 2, league=self.football)
        hockey = self.game(11, 12)
        self.channel('NEXT | PHL HARBORVIEW - IRONVALE | Sun 08 Nov 13:00 EST (US) | 8K EXCLUSIVE')
        self.run_match()
        self.assertEqual(self.named(football), [])
        self.assertEqual(len(self.named(hockey)), 1)

    def test_no_league_in_the_name_or_category(self):
        event = self.game(11, 12)
        self.channel('NEXT | HARBORVIEW - IRONVALE | Sun 08 Nov 13:00 EST (US)')
        self.run_match()
        self.assertEqual(self.named(event), [])

    def test_a_team_channel_naming_its_markets_teams_is_not_a_game(self):
        """dev/docs/BUGS.md 2026-11-08 01:18: a provider's team channels name their market's teams with
        nothing between them, and with only filler in their guide they were read as the game."""
        event = self.game(11, 12)
        three = self.channel('PHL TEAMS| CBS LIGHTNING BLACKHAWKS OWLS HARBORVIEW')
        two = self.channel('PHL TEAMS| CBS LIGHTNING BLACKHAWKS ST. LOUIS MO')
        slot = self.channel('PHL  | 06 - 1pm Blackhawks at Lightning')
        for ch in (three, two, slot):
            make_epg_entry(ch, title=ch.name, start_time=FACEOFF, duration_minutes=120)
        db.session.commit()
        self.run_match()
        self.assertEqual([m.channel_id for m in self.named(event)], [slot.id])

    def test_two_teams_joined_by_a_bare_hyphen_are_a_game(self):
        """`Atlanta Falcons-Cincinnati Bengals @ 2026-11-08 15:20:00 :Tv2Play DK` on prod."""
        event = self.game(11, 12)
        ch = self.channel('PHL: Harborview Lightning-Ironvale Blackhawks @ 2099-11-08 18:00:00 :Tv2Play DK  12')
        self.run_match()
        self.assertEqual([m.channel_id for m in self.named(event)], [ch.id])


class TimeTests(NameCase):
    def test_a_names_time_far_off_in_a_trusted_league_is_weak_and_says_how_far(self):
        self.trust(self.hockey)
        event = self.game(11, 12)
        self.channel('Live | Lightning @ Blackhawks | PHL.TV | Sun 08 Nov 15:00 EST (US)')
        self.run_match()
        (m,) = self.named(event)
        self.assertEqual(m.strength, SPORTS_MATCH_WEAK)
        self.assertIn('is 120 minutes after the listed start', m.reason)

    def test_within_45_minutes_agrees(self):
        self.trust(self.hockey)
        event = self.game(11, 12)
        self.channel('PHL: Lightning @ Blackhawks | Sun 08 Nov 13:30 EST (US)')
        self.run_match()
        (m,) = self.named(event)
        self.assertEqual((m.strength, m.airing), (SPORTS_MATCH_STRONG, SPORTS_AIRING_FIRST))

    def test_a_name_with_no_date_and_a_home_and_home_series_goes_to_the_nearer_and_is_weak(self):
        tonight = self.game(11, 12, start=NOW + timedelta(hours=8))
        tomorrow = self.game(12, 11, start=NOW + timedelta(hours=32))
        self.channel('PHL 03: 7pm Blackhawks @ Lightning')
        self.run_match()
        self.assertEqual(self.named(tomorrow), [])
        (m,) = self.named(tonight)
        self.assertEqual(m.strength, SPORTS_MATCH_WEAK)
        self.assertIn('2 games between these teams are within a week', m.reason)

    def test_a_game_more_than_a_week_out_is_not_named_by_a_name_with_no_date(self):
        event = self.game(11, 12, start=NOW + timedelta(days=8))
        self.channel('PHL 03: 7pm Blackhawks @ Lightning')
        self.run_match()
        self.assertEqual(self.named(event), [])

    def test_a_replays_category_makes_a_replay(self):
        event = self.game(11, 12)
        self.tie(self.hockey, 'US| PHL REPLAY', replay=True)
        self.channel('PHL 03: Blackhawks @ Lightning', category='US| PHL REPLAY')
        self.run_match()
        (m,) = self.named(event)
        self.assertEqual(m.airing, SPORTS_AIRING_REPLAY)


class RenameTests(NameCase):
    def test_a_rename_to_the_next_game_marks_the_old_one_gone(self):
        first = self.game(11, 12)
        second = self.game(13, 14, start=FACEOFF + timedelta(days=1))
        ch = self.channel('PHL 01: 1pm Blackhawks @ Lightning')
        self.run_match()
        self.assertEqual(len(self.named(first)), 1)
        self.rename(ch, 'PHL 01: 7pm Comets @ Owls')
        self.run_match()
        (old,) = self.named(first)
        self.assertIsNotNone(old.gone_since)
        (new,) = self.named(second)
        self.assertIsNone(new.gone_since)

    def test_a_name_moving_on_after_the_game_is_left_alone(self):
        event = self.game(11, 12, start=NOW - timedelta(hours=10))
        ch = self.channel('End | PHL: Blackhawks @ Lightning')
        self.run_match()
        self.assertEqual(len(self.named(event)), 1)
        self.rename(ch, 'PHL | 01 -')
        self.run_match()
        (m,) = self.named(event)
        self.assertIsNone(m.gone_since)

    def test_a_rejection_survives_a_rename_that_still_names_the_game(self):
        event = self.game(11, 12)
        ch = self.channel('NEXT | PHL: Blackhawks @ Lightning | Sun 08 Nov 13:00 EST (US)')
        self.run_match()
        (m,) = self.named(event)
        sports_match.mark_wrong(event.id, [m.id], True)
        self.rename(ch, 'LIVE | PHL: Blackhawks @ Lightning | Sun 08 Nov 13:00 EST (US)')
        self.run_match()
        (m,) = self.named(event)
        self.assertIsNotNone(m.rejected_at)
        self.assertTrue(m.program_title.startswith('LIVE'))

    def test_a_channel_given_guide_rows_stops_being_read_by_its_name(self):
        event = self.game(11, 12)
        ch = self.channel('PHL 01: 1pm Blackhawks @ Lightning')
        self.run_match()
        make_epg_entry(ch, title='Pretend Hockey Tonight', start_time=NOW, duration_minutes=60)
        db.session.commit()
        self.run_match()
        (m,) = self.named(event)
        self.assertIsNotNone(m.gone_since)


class RaceTests(NameCase):
    def setUp(self):
        super().setUp()
        self.cup = self.league(9003, ['Pretend Cup Series'])
        self.final = self.race('Pretend Cup Series Championship Race', FACEOFF + timedelta(hours=1), self.cup)

    def test_a_race_name_with_a_date(self):
        self.channel('Pretend: Pretend Cup Series Championship Race @ Nov 8 14:30 PM :MAX US 04')
        self.run_match()
        (m,) = self.named(self.final)
        self.assertEqual(m.strength, SPORTS_MATCH_STRONG)
        self.assertIn('the only race of the series that day', m.reason)

    def test_a_race_whose_name_the_channel_gives(self):
        named = self.race('Harborview 500', FACEOFF + timedelta(days=5), self.cup)
        self.channel('Pretend Cup Series: Harborview 500 @ Nov 13 2:00 PM :MAX US 05')
        self.run_match()
        (m,) = self.named(named)
        self.assertEqual(m.strength, SPORTS_MATCH_STRONG)
        self.assertIn('it names this race', m.reason)

    def test_a_race_the_channel_names_differently_is_weak_and_says_both(self):
        named = self.race('Harborview 500', FACEOFF + timedelta(days=5), self.cup)
        self.channel('Pretend Cup Series: Ironvale 400 @ Nov 13 2:00 PM :MAX US 05')
        self.run_match()
        (m,) = self.named(named)
        self.assertEqual(m.strength, SPORTS_MATCH_WEAK)
        self.assertIn('TheSportsDB calls the race "Harborview 500"', m.reason)

    def test_qualifying_is_not_the_race(self):
        self.channel('Pretend Cup Series Qualifying: Championship Race @ Nov 8 14:30 :TSN+  58')
        self.run_match()
        self.assertEqual(self.named(self.final), [])

    def test_a_race_name_with_no_date_is_not_matched(self):
        self.channel('Pretend Cup Series Championship Race | 8K EXCLUSIVE')
        self.run_match()
        self.assertEqual(self.named(self.final), [])


class GuideCountsTests(NameCase):
    def test_guide_readers_do_not_count_name_matches(self):
        """The league page's guide column, a recording's link and the trust measurement read guide
        matches only; a name match carries no listing a recording could be made from."""
        event = self.game(11, 12)
        self.channel('NEXT | PHL HARBORVIEW - IRONVALE | Sun 08 Nov 13:00 EST (US)')
        self.run_match()
        self.assertEqual(len(self.named(event)), 1)
        self.assertEqual(sports_match.game_counts([event.id]), {})
        self.assertEqual(SportsMatch.query.filter_by(kind=SPORTS_MATCH_KIND_GUIDE).count(), 0)
        db.session.expire_all()
        self.assertIsNone(db.session.get(SportsEvent, event.id).guide_start)


class HiddenTests(NameCase):
    """dev/docs/BUGS.md 2026-11-08 11:47: a hidden channel carries no guide rows, so it read as an event
    channel with only its name and was offered on the league page."""

    def hide(self, ch, hidden=True):
        db.session.get(Channel, ch.id).hidden = hidden
        db.session.commit()

    def listing(self, ch):
        make_epg_entry(ch, title='PHL Hockey', sub_title='Ironvale Blackhawks at Harborview Lightning',
                       start_time=FACEOFF, duration_minutes=180)
        db.session.commit()

    def test_a_hidden_event_channel_is_not_read_by_its_name(self):
        event = self.game(11, 12)
        hidden = self.channel('NEXT | PHL HARBORVIEW - IRONVALE | Sun 08 Nov 13:00 EST (SE) | SE: PPV 28')
        shown = self.channel('NEXT | PHL HARBORVIEW - IRONVALE | Sun 08 Nov 13:00 EST (US) | US: PPV 2')
        self.hide(hidden)
        self.run_match()
        self.assertEqual([m.channel_id for m in self.named(event)], [shown.id])

    def test_a_channel_hidden_after_it_was_named_is_gone_and_back_when_shown(self):
        event = self.game(11, 12)
        ch = self.channel('NEXT | PHL HARBORVIEW - IRONVALE | Sun 08 Nov 13:00 EST (US) | US: PPV 2')
        self.run_match()
        self.hide(ch)
        self.run_match()
        (m,) = self.named(event)
        self.assertEqual(m.gone_since, NOW)
        self.hide(ch, False)
        self.run_match()
        (m,) = self.named(event)
        self.assertIsNone(m.gone_since)

    def test_a_hidden_channels_listing_is_not_a_guide_match(self):
        """Hiding deletes a channel's rows; one left behind is still not an offer."""
        event = self.game(11, 12)
        hidden, shown = self.channel('PHL Network'), self.channel('PHL Network 2')
        self.listing(hidden)
        self.listing(shown)
        self.hide(hidden)
        self.run_match()
        found = SportsMatch.query.filter_by(event_id=event.id, kind=SPORTS_MATCH_KIND_GUIDE).all()
        self.assertEqual([m.channel_id for m in found], [shown.id])
        self.assertEqual(db.session.get(SportsEvent, event.id).guide_channels, 1)

    def test_the_league_page_drops_a_channel_the_moment_it_is_hidden(self):
        """The matcher runs only after a sync, a guide import or a schedule update, so the readers
        filter too: a hide must not wait for the next sync to leave the page."""
        event = self.game(11, 12)
        hidden, shown = self.channel('PHL Network'), self.channel('PHL Network 2')
        self.listing(hidden)
        self.listing(shown)
        self.run_match()
        self.assertEqual(sports_match.game_counts([event.id])[event.id]['channels'], 2)
        self.hide(hidden)
        self.assertEqual(sports_match.game_counts([event.id])[event.id]['channels'], 1)
        (group,) = sports_match.listing_groups(event.id, None, False)
        self.assertEqual([c['channel_id'] for c in group['channels']], [shown.id])


class SyncTriggerTests(NameCase):
    def test_a_sync_that_imports_no_guide_still_queues_a_match(self):
        """Names change at a sync, and an account with no guide source imports none, so the
        import's own request never comes."""
        self.game(11, 12)
        sim = serve_for_test(self, load_world(SMALL))
        view = sim.account('meadow-m3u')
        account = M3uAccount(name='Meadow', m3u_url=view.m3u_url, status='OK')
        db.session.add(account)
        db.session.commit()
        with mock.patch('app.scheduler.queue_sports_match') as queued:
            _do_sync(account.id, threading.Event())
        reasons = [c.kwargs.get('reason') for c in queued.call_args_list]
        self.assertIn(f'account #{account.id} synced its channels', reasons)

    def test_a_simulated_providers_event_slots_are_read_after_a_real_sync(self):
        """The simulator's event slots (`<sport> GAME 03: Away vs Home 19:30`, a clock with no date,
        no guide rows) come through the real sync and are matched by name to a stored game."""
        sim = serve_for_test(self, load_world(SMALL))
        view = sim.account('meadow-m3u')
        account = M3uAccount(name='Meadow', m3u_url=view.m3u_url, status='OK')
        db.session.add(account)
        db.session.commit()
        _do_sync(account.id, threading.Event())
        db.session.expire_all()
        slot = re.compile(r'\b([A-Za-z]+) (?:EVENT|GAME|MATCH|PPV) \d+: ([A-Za-z]+) vs ([A-Za-z]+)\b', re.I)
        found = next(((ch, m) for ch in Channel.query.filter_by(account_id=account.id).all()
                      if (m := slot.search(ch.name))), None)
        self.assertIsNotNone(found, 'the small world carries no timed event slot')
        ch, m = found
        sport, away, home = m.groups()
        league = self.league(9100, [sport])
        for tid, team in ((901, home), (902, away)):
            db.session.add(SportsTeam(tsdb_id=tid, full_name=team, names=json.dumps([team]), city='',
                                      last_looked_up=NOW))
        self.teams.update({901: home, 902: away})
        db.session.commit()
        event = self.game(901, 902, start=datetime.utcnow() + timedelta(hours=6), league=league)
        sports_match.run(pause=0)
        db.session.expire_all()
        names = {x.channel_id for x in self.named(event)}
        self.assertIn(ch.id, names)

    def test_nothing_is_queued_with_nothing_stored(self):
        sim = serve_for_test(self, load_world(SMALL))
        view = sim.account('meadow-m3u')
        account = M3uAccount(name='Meadow', m3u_url=view.m3u_url, status='OK')
        db.session.add(account)
        db.session.commit()
        with mock.patch('app.scheduler.queue_sports_match') as queued:
            _do_sync(account.id, threading.Event())
        queued.assert_not_called()


if __name__ == '__main__':
    unittest.main()
