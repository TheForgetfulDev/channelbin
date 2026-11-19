"""Matching TheSportsDB's events to guide rows (app/sports_match.py, dev/docs/DESIGN-sports-data.md 6, 7, 9).

Every case is a shape measured in the real guide (dev/docs/sports-data-investigation.md and design 6.3,
6.7), with made-up leagues and teams: nothing received from TheSportsDB ships (design 13). Throwaway temp
SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_match
"""
import json
import os
import sys
import unittest
from unittest import mock
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_epg_entry  # noqa: E402

from app import admission, db, sports_data, sports_match, sports_skips, sports_times  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (  # noqa: E402
    EPGEntry, SPORTS_AIRING_FIRST, SPORTS_AIRING_REPLAY, SPORTS_AIRING_UNKNOWN, SPORTS_MATCH_STRONG, SPORTS_MATCH_WEAK,
    SportsEvent, SportsLeague, SportsLeagueCategory, SportsMatch, SportsState, SportsTeam,
)

NOW = datetime(2099, 11, 8, 12, 0)
KICKOFF = datetime(2099, 11, 8, 18, 0)


class MatchCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.account = make_account()
        self.football = self.league(9001, 'Pretend Football League', ['Pretend Football League', 'PFL'])
        self.hockey = self.league(9002, 'Pretend Hockey League', ['Pretend Hockey League', 'PHL'])
        self.teams = {}
        for tid, full, nick in ((1, 'Harborview Gulls', 'Gulls'), (2, 'Ironvale Bears', 'Bears'),
                                (3, 'Northgate Panthers', 'Panthers'), (4, 'Saint-Rémy Comets', 'Comets'),
                                (5, 'Metro City Giants', 'Giants'), (6, 'Metro City Jets', 'Jets'),
                                (11, 'Harborview Lightning', 'Lightning'), (12, 'Ironvale Blackhawks', 'Blackhawks'),
                                (13, 'Northgate Panthers', 'Panthers')):
            self.teams[tid] = full
            city = full[:-len(nick)].strip()
            db.session.add(SportsTeam(tsdb_id=tid, full_name=full, names=json.dumps([full, nick]), city=city,
                                      last_looked_up=NOW))
        db.session.commit()
        self._channels = 0

    def league(self, tsdb_id, name, terms, **kw):
        league = SportsLeague(tsdb_id=tsdb_id, name=name, match_terms=json.dumps(terms), **kw)
        db.session.add(league)
        db.session.commit()
        return league

    def game(self, home, away, start=KICKOFF, league=None, **kw):
        league = league or self.football
        event = SportsEvent(tsdb_id=kw.pop('tsdb_id', None) or (SportsEvent.query.count() + 100),
                            league_id=league.id, name=f'{self.teams[home]} vs {self.teams[away]}',
                            home_team_id=home, away_team_id=away, home_name=self.teams[home],
                            away_name=self.teams[away], start_at=start,
                            local_date=kw.pop('local_date', (start or KICKOFF).date()), **kw)
        db.session.add(event)
        db.session.commit()
        return event

    def race(self, name, start, local_date, league):
        event = SportsEvent(tsdb_id=SportsEvent.query.count() + 500, league_id=league.id, name=name,
                            start_at=start, local_date=local_date)
        db.session.add(event)
        db.session.commit()
        return event

    def channel(self, name=None, category=None):
        self._channels += 1
        return make_channel(self.account, name=name or f'Local {self._channels}', category_name=category)

    def row(self, title, start=KICKOFF, sub_title=None, minutes=180, channel=None, description=None, **kw):
        channel = channel or self.channel()
        entry = make_epg_entry(channel, title=title, sub_title=sub_title, start_time=start,
                               duration_minutes=minutes, description=description, **kw)
        db.session.commit()
        return channel, entry

    def run_match(self, now=NOW):
        note = sports_match.run(now=now, pause=0)
        db.session.expire_all()
        return note

    def matches(self, event, **kw):
        return SportsMatch.query.filter_by(event_id=event.id, **kw).order_by(SportsMatch.program_start_time).all()

    def trust(self, league):
        league.trust_sample, league.trust_agreed = 30, 30
        db.session.commit()


class LocalStationTests(MatchCase):
    """How NBC, CBS and FOX stations list a regional game, measured 2026-11-07 (design 6.7)."""

    def test_a_game_named_in_the_sub_title_matches_strong(self):
        event = self.game(2, 1)
        self.row('PFL Football ᴸᶦᵛᵉ', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        (m,) = self.matches(event)
        self.assertEqual(m.strength, SPORTS_MATCH_STRONG)
        self.assertEqual(m.airing, SPORTS_AIRING_FIRST)
        self.assertIn('both teams by name', m.reason)
        self.assertEqual(m.program_title, 'PFL Football')

    def test_a_generic_row_does_not_match(self):
        event = self.game(2, 1)
        self.row('PFL Football ᴸᶦᵛᵉ', description='Football action from around the Pretend Football League.')
        self.run_match()
        self.assertEqual(self.matches(event), [])

    def test_the_description_is_never_read(self):
        """Description text gave 775 rows for one game in the investigation."""
        event = self.game(2, 1)
        self.row('PFL Football', description='The Harborview Gulls visit the Ironvale Bears.')
        self.run_match()
        self.assertEqual(self.matches(event), [])

    def test_emoji_and_tags_inside_the_team_names(self):
        event = self.game(2, 1)
        self.row('PFL Football [NEW] ᴺᵉʷ', sub_title='Harborview 🏈 [PFL] Gulls at Ironvale 🏈 [PFL] Bears')
        self.run_match()
        (m,) = self.matches(event)
        self.assertEqual(m.strength, SPORTS_MATCH_STRONG)

    def test_home_first_order_accents_and_live_prefix(self):
        event = self.game(1, 4)
        self.row('Live: PFL: Harborview Gulls - Saint-Remy Comets')
        self.row('PFL Football', sub_title='Saint-Rémy Comets at Harborview Gulls')
        self.run_match()
        self.assertEqual(len(self.matches(event)), 2)

    def test_postgame_and_short_rows_are_not_games(self):
        event = self.game(2, 1)
        self.row('PFL Postgame', sub_title='Harborview Gulls at Ironvale Bears', start=KICKOFF + timedelta(hours=3))
        self.row('PFL Gulls @ Bears Highlights')
        self.row('PFL: Harborview Gulls at Ironvale Bears', minutes=30)
        self.run_match()
        self.assertEqual(self.matches(event), [])

    def test_a_betting_show_about_the_game_is_not_the_game(self):
        """A betting channel's `Illinois vs. UCLA College Football In-Game Live Gameday`, an hour
        before kickoff and three hours long, names the league and both teams. It was never a pick
        or a candidate in 23,483 audit observations (dev/changelog/1577); this holds it there."""
        event = self.game(2, 1)
        self.row('Harborview Gulls vs. Ironvale Bears PFL Football In-Game Live Gameday ᴸᶦᵛᵉ',
                 start=KICKOFF - timedelta(hours=1))
        self.run_match()
        self.assertEqual(self.matches(event), [])

    def test_one_channels_game_is_never_another_channels(self):
        one = self.game(2, 1)
        two = self.game(4, 3)
        ch_a, _ = self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        ch_b, _ = self.row('PFL Football', sub_title='Saint-Rémy Comets at Northgate Panthers')
        self.run_match()
        self.assertEqual([m.channel_id for m in self.matches(one)], [ch_a.id])
        self.assertEqual([m.channel_id for m in self.matches(two)], [ch_b.id])


class FalseMatchTests(MatchCase):
    """Every false match in the investigation came from city names with no league in the text."""

    def test_city_only_row_of_another_league_does_not_match(self):
        """`NHL TAMPA BAY - CHICAGO` matched the NFL's Buccaneers at Bears."""
        football = self.game(2, 1)
        hockey = self.game(12, 11, league=self.hockey)
        self.row('PHL HARBORVIEW - IRONVALE')
        self.run_match()
        self.assertEqual(self.matches(football), [])
        (m,) = self.matches(hockey)
        self.assertEqual(m.strength, SPORTS_MATCH_WEAK)
        self.assertIn('by city', m.reason)

    def test_a_nickname_in_another_league_does_not_match(self):
        """Nicknames repeat across leagues: Panthers, Jets, Kings, Rangers, Cardinals, Giants."""
        football = self.game(2, 3)
        self.row('PHL Hockey', sub_title='Northgate Panthers at Ironvale Bears')
        self.run_match()
        self.assertEqual(self.matches(football), [])

    def test_no_league_in_the_text_does_not_match(self):
        """`AFC TORONTO VS. MONTREAL ROSES FC` matched Canadiens at Maple Leafs."""
        event = self.game(2, 1)
        self.row('HARBORVIEW GULLS VS. IRONVALE BEARS FC')
        self.run_match()
        self.assertEqual(self.matches(event), [])

    def test_a_row_naming_another_league_in_this_leagues_category(self):
        """A tied category never proves a game by itself: `NFL Sunday Ticket` also holds other things."""
        event = self.game(2, 3)
        db.session.add(SportsLeagueCategory(league_id=self.football.id, category_name='PFL Sunday Ticket'))
        db.session.commit()
        self.row('PHL: Northgate Panthers at Ironvale Bears', channel=self.channel(category='PFL Sunday Ticket'))
        self.run_match()
        self.assertEqual(self.matches(event), [])

    def test_an_accent_in_a_city(self):
        """`Montréal` has an accent, and listings drop it."""
        event = self.game(1, 4)
        self.row('PFL: Saint-Remy at Harborview')
        self.run_match()
        (m,) = self.matches(event)
        self.assertEqual(m.strength, SPORTS_MATCH_WEAK)

    def test_a_term_with_an_accent_hyphen_or_curly_apostrophe_reaches_the_check(self):
        """The SQL narrows on raw text, so a folded term alone would never reach the Python check."""
        event = self.game(2, 1)
        self.football.match_terms = json.dumps(['Série PF', 'Pretend-Ball', 'O’Pretend'])
        db.session.commit()
        self.row('Serie PF: Harborview Gulls at Ironvale Bears')
        self.row('Série PF: Harborview Gulls at Ironvale Bears', start=KICKOFF + timedelta(minutes=1))
        self.row('Pretend Ball: Harborview Gulls at Ironvale Bears', start=KICKOFF + timedelta(minutes=2))
        self.row("O'Pretend: Harborview Gulls at Ironvale Bears", start=KICKOFF + timedelta(minutes=3))
        self.run_match()
        self.assertEqual(len(self.matches(event)), 4)

    def test_a_shared_city_reads_as_neither_before_the_other_teams_lookup(self):
        """`New York` is not the Giants' city while the Jets' lookup is still queued."""
        giants = db.session.query(SportsTeam).filter_by(tsdb_id=5).one()
        giants.last_looked_up = None
        db.session.commit()
        event = self.game(1, 6)
        self.game(5, 2, start=KICKOFF + timedelta(days=7))
        self.row('PFL: Metro City at Harborview')
        self.run_match()
        self.assertEqual(self.matches(event), [])

    def test_a_city_two_teams_share_reads_as_neither(self):
        event = self.game(1, 6)
        self.game(5, 2, start=KICKOFF + timedelta(days=7))   # the other Metro City team, a week on
        self.row('PFL: Metro City at Harborview')
        self.run_match()
        self.assertEqual(self.matches(event), [])


class TimeTests(MatchCase):
    """The time sets strength and airing, never which game a row is (design 6.3, 2026-11-07)."""

    def test_placeholders_listed_for_days_before_do_not_match(self):
        event = self.game(2, 1)
        package = self.channel('PFL Package 09', category='PFL Package')
        for hours in range(2, 14, 2):
            self.row('PFL | 09 - 1pm Gulls at Bears', start=KICKOFF - timedelta(hours=hours), minutes=120,
                     channel=package)
        ch, _ = self.row('PFL | 09 - 1pm Gulls at Bears', channel=package, minutes=120)
        self.run_match()
        self.assertEqual([m.program_start_time for m in self.matches(event)], [KICKOFF])

    def test_a_twelve_hour_typo_still_matches_and_shows_both_times(self):
        """TheSportsDB listed the Baptist Health 200 at 11:30am UTC for 11:30pm."""
        wrong = datetime(2099, 11, 8, 11, 30)
        real = wrong + timedelta(hours=12)
        event = self.game(2, 1, start=wrong)
        self.trust(self.football)
        for _ in range(3):
            self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=real)
        # The window is around TheSportsDB's time, so a replay 4 hours after the real start is past it.
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=real + timedelta(hours=4))
        self.run_match(now=wrong)
        found = self.matches(event)
        self.assertEqual([m.program_start_time for m in found], [real] * 3)
        self.assertEqual(event.guide_start, real)
        self.assertTrue(sports_match.time_disagrees(event))
        self.assertTrue(all(m.airing == SPORTS_AIRING_FIRST for m in found))
        self.assertIn('the time the guide lists', found[0].reason)

    def test_a_disagreement_names_both_times(self):
        self.t.sandbox_config({'display': {'timezone': 'America/New_York', 'time_format': '12h'}})
        wrong = datetime(2099, 11, 8, 11, 30)
        event = self.game(2, 1, start=wrong)
        for _ in range(2):
            self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=wrong + timedelta(hours=12))
        self.run_match(now=wrong)
        reason = self.matches(event)[0].reason
        self.assertIn('at 6:30 PM, the time the guide lists', reason)
        self.assertIn('TheSportsDB lists 6:30 AM', reason)

    def test_a_start_that_has_aired_is_kept_when_its_rows_leave_the_guide(self):
        """An import drops listings that have aired; the replays left must not become the game's start,
        call its time wrong, or spend the league's trust."""
        event = self.game(2, 1)
        live = [self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')[1] for _ in range(3)]
        for _ in range(2):
            self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=KICKOFF + timedelta(hours=6))
        self.run_match(now=KICKOFF - timedelta(hours=1))
        self.assertEqual(event.guide_start, KICKOFF)
        for entry in live:
            db.session.delete(db.session.get(EPGEntry, entry.id))
        db.session.commit()
        self.run_match(now=KICKOFF + timedelta(hours=8))
        self.assertEqual(event.guide_start, KICKOFF)
        self.assertFalse(sports_match.time_disagrees(event))
        replays = [m for m in self.matches(event) if m.program_start_time == KICKOFF + timedelta(hours=6)]
        self.assertEqual({m.airing for m in replays}, {SPORTS_AIRING_REPLAY})
        league = db.session.get(SportsLeague, self.football.id)
        self.assertEqual((league.trust_sample, league.trust_agreed), (1, 1))

    def test_nothing_past_thirteen_hours(self):
        event = self.game(2, 1)
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=KICKOFF + timedelta(hours=14))
        self.run_match()
        self.assertEqual([m.program_start_time for m in self.matches(event)], [KICKOFF])

    def test_a_replay_in_a_trusted_league(self):
        event = self.game(2, 1)
        self.trust(self.football)
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=KICKOFF + timedelta(hours=12))
        self.run_match()
        self.assertEqual([m.airing for m in self.matches(event)], [SPORTS_AIRING_FIRST, SPORTS_AIRING_REPLAY])

    def test_seventy_five_minutes_early_is_weak_in_a_trusted_league(self):
        """Sky's `NFL Jaguars @ Ravens` started 75 minutes early."""
        event = self.game(2, 1)
        self.trust(self.football)
        for _ in range(3):
            self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.row('PFL Gulls @ Bears', start=KICKOFF - timedelta(minutes=75))
        self.run_match()
        early = self.matches(event)[0]
        self.assertEqual(early.strength, SPORTS_MATCH_WEAK)
        self.assertIn('75 minutes early', early.reason)

    def test_an_untrusted_league_says_the_time_was_not_checked(self):
        event = self.game(2, 1)
        self.row('PFL Gulls @ Bears', start=KICKOFF - timedelta(minutes=75))
        self.run_match()
        (m,) = self.matches(event)
        self.assertEqual(m.strength, SPORTS_MATCH_STRONG)
        self.assertIn('not checked', m.reason)

    def test_two_games_of_one_pair_inside_the_window_go_to_the_nearer(self):
        """A doubleheader puts two games of one pair a few hours apart."""
        early = self.game(2, 1)
        late = self.game(2, 1, start=KICKOFF + timedelta(hours=5))
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=KICKOFF + timedelta(hours=5))
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        self.assertEqual([m.program_start_time for m in self.matches(early)], [KICKOFF])
        self.assertEqual([m.program_start_time for m in self.matches(late)], [KICKOFF + timedelta(hours=5)])

    def test_a_home_and_home_series_goes_to_the_nearer_game(self):
        first = self.game(2, 1)
        second = self.game(1, 2, start=KICKOFF + timedelta(days=1))
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.row('PFL Football', sub_title='Ironvale Bears at Harborview Gulls', start=KICKOFF + timedelta(days=1))
        self.run_match()
        self.assertEqual([m.program_start_time for m in self.matches(first)], [KICKOFF])
        self.assertEqual([m.program_start_time for m in self.matches(second)], [KICKOFF + timedelta(days=1)])


class GuideStartTests(MatchCase):
    """Which start a game is given when the guide lists it at several (dev/docs/BUGS.md 2026-11-17 @ 05:10:28 PM)."""

    def listing(self, start, n, live=False):
        title = 'PFL Football ᴸᶦᵛᵉ' if live else 'PFL Football'
        return [self.row(title, sub_title='Harborview Gulls at Ironvale Bears', start=start)[0] for _ in range(n)]

    def test_a_replay_on_more_channels_does_not_move_the_game_off_its_live_listings(self):
        """104 NFL Network feeds listing a 12:30 AM replay outvoted 48 channels carrying the 8:15 PM game live,
        and the live listings were then dropped as too early."""
        event = self.game(2, 1)
        self.listing(KICKOFF - timedelta(minutes=75), 3, live=True)
        self.listing(KICKOFF - timedelta(minutes=15), 2, live=True)
        self.listing(KICKOFF + timedelta(hours=4, minutes=15), 8)
        self.run_match()
        self.assertEqual(event.guide_start, KICKOFF - timedelta(minutes=75))
        self.assertFalse(sports_match.time_disagrees(event))
        live = [m for m in self.matches(event) if m.program_start_time <= KICKOFF]
        self.assertEqual(len(live), 5)
        self.assertEqual({m.airing for m in live}, {SPORTS_AIRING_FIRST})
        playing = sports_match.airing_now([event.id], KICKOFF + timedelta(minutes=30))
        self.assertEqual(len({channel for _e, channel, _s, _t in playing}), 5)

    def test_the_leagues_answer_decides_when_a_sports_channel_plays_each_listing(self):
        """Texans at Colts on Nov 18 (dev/changelog/1630): listed 7:00 PM on most channels, 8:00 PM on a
        few, TheSportsDB 8:15 PM. Going by TheSportsDB each listing starts at 8:15 PM; the replay stays."""
        event = self.game(2, 1)
        self.listing(KICKOFF - timedelta(minutes=75), 3, live=True)
        self.listing(KICKOFF - timedelta(minutes=15), 2, live=True)
        self.listing(KICKOFF + timedelta(hours=4, minutes=15), 8)
        self.run_match()

        def starts(now):
            return sorted({s for _c, _a, s, _t in sports_match.listing_windows([event.id], now)[event.id]})
        self.assertEqual(starts(NOW), [KICKOFF])  # a replay is never played (_playable)
        sports_times.set_league(9001, {'go_by': sports_times.GO_GUIDE})
        self.assertEqual(starts(NOW), [KICKOFF - timedelta(minutes=75), KICKOFF - timedelta(minutes=15)])
        sports_times.set_league(9001, {'go_by': sports_times.GO_BOTH})
        self.assertEqual(starts(NOW), [KICKOFF - timedelta(minutes=75), KICKOFF - timedelta(minutes=15)])
        sports_times.set_league(9001, {'go_by': None})
        # Further off than the 24-hour wait, every listing is as listed.
        self.assertEqual(starts(KICKOFF - timedelta(hours=26)),
                         [KICKOFF - timedelta(minutes=75), KICKOFF - timedelta(minutes=15)])
        on = sports_match.airing_now([event.id], KICKOFF - timedelta(minutes=30))
        self.assertEqual(on, [], 'going by TheSportsDB, nothing plays before 8:15 PM')

    def test_a_listing_moved_later_plays_past_its_listed_end(self):
        """Listed 90 minutes, ending 15 minutes after TheSportsDB's start: asked 15 minutes after that, the
        moved listing is still on, which airing_now's query has to reach back for (review of 1630)."""
        event = self.game(2, 1)
        early = KICKOFF - timedelta(minutes=75)
        self.row('PFL Football ᴸᶦᵛᵉ', sub_title='Harborview Gulls at Ironvale Bears', start=early, minutes=90)
        self.run_match()
        self.assertEqual(event.guide_start, early)
        on = sports_match.airing_now([event.id], KICKOFF + timedelta(minutes=30))
        self.assertEqual([(s, t) for _e, _c, s, t in on], [(KICKOFF, KICKOFF + timedelta(minutes=90))])

    def test_a_start_marked_live_beats_one_on_more_channels(self):
        event = self.game(2, 1)
        self.listing(KICKOFF - timedelta(minutes=60), 4)
        self.listing(KICKOFF, 1, live=True)
        self.run_match()
        self.assertEqual(event.guide_start, KICKOFF)

    def test_an_earlier_start_on_more_channels_stays_matched_as_a_first_airing(self):
        """397 FOX stations listed a game at 3:30 PM and 44 at TheSportsDB's 7:00 PM. TheSportsDB's time is
        the game's start, and the earlier listing on more channels is never dropped as a placeholder."""
        event = self.game(2, 1)
        self.listing(KICKOFF - timedelta(hours=3, minutes=30), 4)
        self.listing(KICKOFF, 2)
        self.run_match(now=KICKOFF - timedelta(hours=6))
        self.assertEqual(event.guide_start, KICKOFF)
        found = self.matches(event)
        self.assertEqual([m.program_start_time for m in found], [KICKOFF - timedelta(hours=3, minutes=30)] * 4 + [KICKOFF] * 2)
        self.assertEqual({m.airing for m in found if m.program_start_time == KICKOFF}, {SPORTS_AIRING_FIRST})

    def test_a_lone_unmarked_listing_hours_off_is_not_the_start(self):
        """ESPN+ listed Kent State vs Miami (OH) at 9 AM on one event channel for a 7 PM game: the guide
        showed the game that morning and nothing at night."""
        event = self.game(2, 1)
        self.listing(KICKOFF - timedelta(hours=10), 1)
        self.run_match(now=KICKOFF - timedelta(hours=12))
        self.assertIsNone(event.guide_start)
        self.assertEqual(self.matches(event), [])

    def test_a_far_start_marked_live_is_believed_when_nothing_is_near(self):
        event = self.game(2, 1)
        self.listing(KICKOFF + timedelta(hours=3), 1, live=True)
        self.run_match()
        self.assertEqual(event.guide_start, KICKOFF + timedelta(hours=3))
        self.assertTrue(sports_match.time_disagrees(event))

    def test_a_stored_start_that_has_passed_is_measured_again_before_the_game(self):
        """The 9 AM placeholder's start had passed by the afternoon and was kept as if the game had aired."""
        event = self.game(2, 1)
        event.guide_start, event.guide_channels = KICKOFF - timedelta(hours=10), 1
        db.session.commit()
        self.listing(KICKOFF, 2)
        self.run_match(now=KICKOFF - timedelta(hours=4))
        self.assertEqual((event.guide_start, event.guide_channels), (KICKOFF, 2))

    def test_a_start_the_guide_no_longer_gives_is_cleared_before_the_game(self):
        event = self.game(2, 1)
        event.guide_start, event.guide_channels = KICKOFF - timedelta(hours=10), 1
        db.session.commit()
        self.run_match(now=KICKOFF - timedelta(hours=4))
        self.assertEqual((event.guide_start, event.guide_channels), (None, None))


class MovedGameTests(MatchCase):
    """A game TheSportsDB moved while the guide still lists the old time (dev/docs/BUGS.md 2026-11-17 @ 06:55:07 PM).
    Mavericks-Warriors moved from 9:30 to 8:00 PM ET; ESPN and ESPN Deportes went on listing 9:35 and 9:30,
    and the Dallas station alone moved to 8:00."""

    OLD = KICKOFF + timedelta(minutes=90)

    def listing(self, start, n, minutes=150):
        return [self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=start,
                         minutes=minutes)[0] for _ in range(n)]

    def moved(self):
        return self.game(2, 1, previous_start=self.OLD, start_changed_at=NOW)

    def test_one_listing_of_the_new_time_beats_more_at_the_old_one(self):
        event = self.moved()
        self.listing(KICKOFF, 1)
        self.listing(self.OLD, 3)
        self.run_match()
        self.assertEqual(event.guide_start, KICKOFF)
        self.assertIsNone(event.guide_behind_start)

    def test_a_guide_still_at_the_old_time_goes_by_thesportsdb(self):
        event = self.moved()
        self.listing(self.OLD + timedelta(minutes=5), 3)
        self.listing(self.OLD, 1)
        note = self.run_match()
        self.assertEqual(event.guide_start, KICKOFF)
        self.assertEqual(event.guide_behind_start, self.OLD + timedelta(minutes=5))
        self.assertEqual(event.guide_channels, 3)
        self.assertFalse(sports_match.time_disagrees(event))
        self.assertIn('still listed at the old time', note)
        found = self.matches(event)
        self.assertEqual({m.airing for m in found}, {SPORTS_AIRING_FIRST})
        self.assertTrue(all('where the guide still lists it' in m.reason for m in found))

    def test_a_sports_channel_plays_the_old_listings_from_the_new_time(self):
        event = self.moved()
        self.listing(self.OLD + timedelta(minutes=5), 3)
        self.listing(self.OLD, 1)
        self.run_match()
        playing = sports_match.airing_now([event.id], KICKOFF + timedelta(minutes=10))
        self.assertEqual(len({channel for _e, channel, _s, _t in playing}), 4)
        # Each listing moves by its own gap to TheSportsDB's start, not the game's (dev/changelog/1630).
        self.assertEqual({s for _e, _c, s, _t in playing}, {KICKOFF})
        windows = sports_match.listing_windows([event.id], KICKOFF + timedelta(minutes=10))[event.id]
        self.assertEqual({s for _c, _a, s, _t in windows}, {KICKOFF})

    def test_a_moved_listing_ends_no_earlier_than_listed(self):
        """A game moved earlier that runs long is not cut at the moved end of the network's slot."""
        event = self.moved()
        self.listing(self.OLD, 2, minutes=150)
        self.run_match()
        stops = {t for _e, _c, _s, t in sports_match.airing_now([event.id], KICKOFF + timedelta(minutes=10))}
        self.assertEqual(stops, {self.OLD + timedelta(minutes=150)})

    def test_a_replay_hours_later_stays_a_replay_and_is_not_moved(self):
        event = self.moved()
        self.listing(self.OLD, 2)
        self.listing(self.OLD + timedelta(hours=6), 2)
        self.run_match()
        later = [m for m in self.matches(event) if m.program_start_time > self.OLD]
        self.assertEqual({m.airing for m in later}, {SPORTS_AIRING_REPLAY})
        start = self.OLD + timedelta(hours=6)
        rule = sports_times.Rules().of(0)
        self.assertEqual(sports_times.event_windows(start, start + timedelta(hours=2), event, rule, KICKOFF),
                         [(start, start + timedelta(hours=2))])

    def test_the_mark_clears_once_the_guide_follows(self):
        event = self.moved()
        self.listing(self.OLD, 2)
        self.run_match()
        self.assertIsNotNone(event.guide_behind_start)
        self.listing(KICKOFF, 1)
        self.run_match()
        self.assertEqual(event.guide_start, KICKOFF)
        self.assertIsNone(event.guide_behind_start)

    def test_a_game_the_guide_is_behind_on_is_left_out_of_the_trust_sample(self):
        event = self.moved()
        self.listing(self.OLD, 2)
        self.run_match()
        self.assertIsNotNone(event.guide_behind_start)
        self.assertEqual(db.session.get(SportsLeague, self.football.id).trust_sample, 0)

    def test_the_league_page_row_says_the_guide_is_behind(self):
        from app import sports_view
        from app.tz_utils import get_display_tz
        event = self.moved()
        self.listing(self.OLD, 2)
        self.run_match()
        rules = sports_times.Rules()
        notes = sports_view._game_row(event, rules.of(9001), NOW, get_display_tz(), False)['notes']
        self.assertTrue(any(n.startswith('The guide still lists it at the old time') and 'on 2 channels' in n
                            and n.endswith("Sports channels and Record on this page go by TheSportsDB's time")
                            for n in notes), notes)
        guide = sports_times.Rule(go_by=sports_times.GO_GUIDE, cover_both_minutes=90, wait_hours=24)
        notes = sports_view._game_row(event, guide, NOW, get_display_tz(), False)['notes']
        self.assertTrue(any(n.endswith('go by the listing') for n in notes), notes)

    def test_an_unmoved_game_is_never_marked(self):
        event = self.game(2, 1)
        self.listing(self.OLD, 2)
        self.run_match()
        self.assertIsNone(event.guide_behind_start)
        self.assertEqual(event.guide_start, self.OLD)


class StartedGameMovedTests(MatchCase):
    """A game TheSportsDB moves after it has started keeps its start (dev/docs/BUGS.md 2026-11-18 @ 07:52:09 AM).
    A rain delay entered mid-game moved the start later; the matcher read the guide's listing at the old
    start as behind and moved it later by the move, and a sports channel showed its waiting screen over
    the game on the air."""

    def setUp(self):
        super().setUp()
        db.session.add(SportsState(id=1))
        self.event = self.game(2, 1)
        for _ in range(3):
            self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=KICKOFF, minutes=150)
        self.run_match(now=KICKOFF - timedelta(hours=1))
        self.assertEqual(self.event.guide_start, KICKOFF)

    def move(self, to, seen=KICKOFF + timedelta(minutes=15)):
        event = db.session.get(SportsEvent, self.event.id)
        event.previous_start, event.start_at, event.start_changed_at = event.start_at, to, seen
        db.session.commit()

    def test_a_sports_channel_keeps_playing_a_game_moved_later_mid_game(self):
        self.move(KICKOFF + timedelta(minutes=90))
        self.run_match(now=KICKOFF + timedelta(minutes=20))
        self.assertEqual(self.event.guide_start, KICKOFF)
        self.assertIsNone(self.event.guide_behind_start)
        playing = sports_match.airing_now([self.event.id], KICKOFF + timedelta(minutes=20), before=timedelta(hours=1))
        self.assertEqual(len({channel for _e, channel, _s, _t in playing}), 3)
        self.assertEqual({s for _e, _c, s, _t in playing}, {KICKOFF})

    def test_its_listings_stay_strong_and_live_in_a_trusted_league(self):
        """Checked against the moved start, a listing exactly 90 minutes off would turn weak, and a
        sports channel plays strong listings only."""
        self.trust(self.football)
        self.move(KICKOFF + timedelta(minutes=90))
        self.run_match(now=KICKOFF + timedelta(minutes=20))
        found = self.matches(self.event)
        self.assertEqual({(m.strength, m.airing) for m in found}, {(SPORTS_MATCH_STRONG, SPORTS_AIRING_FIRST)})
        self.assertTrue(all('a game that has started keeps its start' in m.reason for m in found), found[0].reason)

    def test_a_move_earlier_mid_game_changes_nothing(self):
        self.move(KICKOFF - timedelta(minutes=30))
        self.run_match(now=KICKOFF + timedelta(minutes=20))
        self.assertEqual(self.event.guide_start, KICKOFF)
        self.assertIsNone(self.event.guide_behind_start)

    def test_a_move_seen_before_kickoff_still_goes_by_thesportsdb(self):
        """The rule of dev/changelog/1623 is unchanged for a game that has not started."""
        self.move(KICKOFF + timedelta(minutes=90), seen=KICKOFF - timedelta(minutes=50))
        self.run_match(now=KICKOFF - timedelta(minutes=45))
        self.assertEqual(self.event.guide_start, KICKOFF + timedelta(minutes=90))
        self.assertEqual(self.event.guide_behind_start, KICKOFF)

    def test_a_passed_placeholder_start_is_not_a_started_game(self):
        event = SportsEvent(guide_start=KICKOFF - timedelta(hours=4), previous_start=KICKOFF,
                            start_at=KICKOFF + timedelta(minutes=30), start_changed_at=KICKOFF - timedelta(hours=3))
        self.assertFalse(sports_match.kept_start(event))

    def test_it_is_left_out_of_the_trust_sample(self):
        self.move(KICKOFF + timedelta(minutes=90))
        self.run_match(now=KICKOFF + timedelta(minutes=20))
        self.assertEqual(db.session.get(SportsLeague, self.football.id).trust_sample, 0)

    def test_the_league_page_row_says_it_kept_its_start(self):
        from app import sports_view
        from app.tz_utils import get_display_tz
        self.move(KICKOFF + timedelta(minutes=90))
        self.run_match(now=KICKOFF + timedelta(minutes=20))
        notes = sports_view._game_row(self.event, sports_times.Rules().of(9001), KICKOFF + timedelta(minutes=20),
                                      get_display_tz(), False)['notes']
        self.assertTrue(any(n.startswith('It had already started when TheSportsDB moved it') for n in notes), notes)
        self.assertFalse(any(n.startswith('The guide still lists it') for n in notes), notes)
        # A move later says how much later it runs (dev/changelog/1632), except under Go by the guide.
        self.assertTrue(any('run 90 minutes later to match' in n for n in notes), notes)
        guide = sports_times.Rule(go_by=sports_times.GO_GUIDE, cover_both_minutes=90, wait_hours=24)
        notes = sports_view._game_row(self.event, guide, KICKOFF + timedelta(minutes=20), get_display_tz(), False)['notes']
        self.assertFalse(any('later to match' in n for n in notes), notes)

    def test_a_delay_entered_in_steps_stays_started_and_ends_later_by_all_of_it(self):
        """dev/docs/BUGS.md 2026-11-18 @ 03:25:38 PM: read every 30 minutes, a rain delay arrives as
        30-minute steps. Each move
        used to overwrite previous_start, so after 90 minutes of steps the game stopped reading as
        started and was shifted forward over the game on the air."""
        from app.sports_data import _store_parsed
        league = db.session.get(SportsLeague, self.football.id)
        for step in range(1, 5):
            seen = KICKOFF + timedelta(minutes=15 + 30 * (step - 1))
            _store_parsed(league, [{'tsdb_id': self.event.tsdb_id, 'start_at': KICKOFF + timedelta(minutes=30 * step)}], seen)
            db.session.commit()
            self.run_match(now=seen + timedelta(minutes=5))
        event = db.session.get(SportsEvent, self.event.id)
        self.assertEqual((event.previous_start, event.guide_start), (KICKOFF, KICKOFF))
        self.assertTrue(sports_match.kept_start(event))
        self.assertEqual(sports_times.running_late(event), timedelta(hours=2))
        now = KICKOFF + timedelta(minutes=110)
        playing = sports_match.airing_now([self.event.id], now)
        self.assertEqual(len({channel for _e, channel, _s, _t in playing}), 3)
        self.assertEqual({(s, t) for _e, _c, s, t in playing},
                         {(KICKOFF, KICKOFF + timedelta(minutes=150) + timedelta(hours=2))})

    def test_a_sports_channel_holds_a_game_moved_later_mid_game_past_its_listing(self):
        """The live channel and the recording go by the same reader (dev/changelog/1632): 90 minutes
        later at the restart, the listing's channel plays on for 90 minutes past the listing."""
        self.move(KICKOFF + timedelta(minutes=90))
        self.run_match(now=KICKOFF + timedelta(minutes=20))
        after_listing = KICKOFF + timedelta(minutes=170)
        self.assertEqual(len(sports_match.airing_now([self.event.id], after_listing)), 3)

    def test_the_run_says_so_and_logs_the_move_once(self):
        self.move(KICKOFF + timedelta(minutes=90))
        with self.assertLogs('app.sports_match', level='WARNING') as logs:
            note = self.run_match(now=KICKOFF + timedelta(minutes=20))
        self.assertIn('1 game TheSportsDB moved after it had started keeps its start', note)
        self.assertTrue(any('after it had started' in line for line in logs.output), logs.output)
        with self.assertNoLogs('app.sports_match', level='WARNING'):
            self.run_match(now=KICKOFF + timedelta(minutes=40))


class CategoryTests(MatchCase):
    """A category a person tied to a league supplies the league (design 5.2)."""

    def test_a_tied_category_supplies_the_league(self):
        event = self.game(2, 1)
        db.session.add(SportsLeagueCategory(league_id=self.football.id, category_name='PFL Sunday Ticket'))
        db.session.commit()
        ch = self.channel(category='PFL Sunday Ticket')
        self.row('Football: Harborview at Ironvale', channel=ch)
        self.run_match()
        (m,) = self.matches(event)
        self.assertEqual(m.strength, SPORTS_MATCH_STRONG)
        self.assertIn('its category, PFL Sunday Ticket', m.reason)

    def test_a_replays_category_makes_a_replay(self):
        event = self.game(2, 1)
        db.session.add(SportsLeagueCategory(league_id=self.football.id, category_name='PFL REPLAY', is_replay=True))
        db.session.commit()
        self.row('Harborview Gulls at Ironvale Bears', channel=self.channel(category='PFL REPLAY'))
        self.run_match()
        (m,) = self.matches(event)
        self.assertEqual(m.airing, SPORTS_AIRING_REPLAY)

    def test_another_leagues_category_is_not_this_league(self):
        event = self.game(2, 3)
        db.session.add(SportsLeagueCategory(league_id=self.hockey.id, category_name='PHL Center Ice'))
        db.session.commit()
        self.row('Northgate Panthers at Ironvale Bears', channel=self.channel(category='PHL Center Ice'))
        self.run_match()
        self.assertEqual(self.matches(event), [])


class FieldEventTests(MatchCase):
    """Races: the series and the local date, with the name telling strong from weak (design 6.4)."""

    def setUp(self):
        super().setUp()
        self.series = self.league(9003, 'Pretend Stock Car Series', ['Pretend Stock Car Series'],
                                  sport='Motorsport')

    def test_the_local_date_not_the_utc_date(self):
        """`UNOH 200` is Sep 17 and its guide row is 00:00 UTC Sep 18."""
        event = self.race('Lakeside 200', datetime(2099, 9, 17, 23, 30), date(2099, 9, 17), self.series)
        self.row('Pretend Stock Car Series ᴸᶦᵛᵉ', sub_title='Lakeside 200', start=datetime(2099, 9, 18, 0, 0))
        self.run_match(now=datetime(2099, 9, 17, 12))
        (m,) = self.matches(event)
        self.assertEqual(m.strength, SPORTS_MATCH_STRONG)

    def test_practice_qualifying_classics_and_short_rows_do_not_match(self):
        event = self.race('Lakeside 200', datetime(2099, 9, 17, 23, 30), date(2099, 9, 17), self.series)
        start = datetime(2099, 9, 17, 16)
        self.row('Pretend Stock Car Series', sub_title='Championship Race, Free Practice', start=start)
        self.row('Pretend Stock Car Series Qualifying', start=start)
        self.row('Pretend Stock Car Series Classics', sub_title='Lakeside 200', start=start, minutes=187)
        self.row('Pretend Stock Car Series', sub_title='Lakeside 200', start=start, minutes=60)
        self.run_match(now=datetime(2099, 9, 17, 12))
        self.assertEqual(self.matches(event), [])

    def test_a_different_race_name_is_weak_and_names_both(self):
        event = self.race('Lakeside 200', datetime(2099, 9, 17, 23, 30), date(2099, 9, 17), self.series)
        self.row('Pretend Stock Car Series', sub_title='Lakeside 250', start=datetime(2099, 9, 17, 23, 30))
        self.run_match(now=datetime(2099, 9, 17, 12))
        (m,) = self.matches(event)
        self.assertEqual(m.strength, SPORTS_MATCH_WEAK)
        self.assertIn('"Lakeside 250"', m.reason)
        self.assertIn('"Lakeside 200"', m.reason)

    def test_replays_with_one_title_tell_first_from_later(self):
        """The Truck finale airs at 00:30 UTC and again at 07:00, 11:00 and 14:00 with one title."""
        event = self.race('Harbor 200', datetime(2099, 11, 6, 11, 30), date(2099, 11, 6), self.series)
        for hhmm in ((0, 30), (7, 0), (11, 0)):
            self.row('Pretend Stock Car Series', sub_title='Harbor 200', start=datetime(2099, 11, 7, *hhmm))
        self.run_match(now=datetime(2099, 11, 6, 12))
        self.assertEqual([m.airing for m in self.matches(event)],
                         [SPORTS_AIRING_FIRST, SPORTS_AIRING_REPLAY, SPORTS_AIRING_REPLAY])

    def grand_prix(self):
        """The Las Vegas Grand Prix's shape in prod's guide on 2026-11-18: a support series at 6:00 PM
        ET, then the race at 10:55 and 11:00 PM ET, TheSportsDB's 04:00 UTC, and a replay at 3:25 AM."""
        event = self.race('Harbor Grand Prix', datetime(2099, 11, 22, 4, 0), date(2099, 11, 21), self.series)
        for _ in range(2):
            self.row('Pretend Stock Car Series Academy', sub_title='Harbor', start=datetime(2099, 11, 21, 23, 0),
                     minutes=120)
        for _ in range(4):
            self.row('Live Harbor Pretend Stock Car Series Grand Prix', start=datetime(2099, 11, 22, 3, 55),
                     minutes=125)
        self.row('Pretend Stock Car Series - Harbor Grand Prix ᴸᶦᵛᵉ', start=datetime(2099, 11, 22, 4, 0), minutes=120)
        for _ in range(3):
            self.row('Harbor Pretend Stock Car Series Grand Prix', start=datetime(2099, 11, 22, 8, 25), minutes=125)
        return event

    def test_a_weak_listing_from_another_series_never_sets_the_start(self):
        """dev/docs/BUGS.md 2026-11-18 @ 09:04:05 AM: an F1 Academy listing at 6:00 PM ET set the Las
        Vegas Grand Prix's start, and the 82 channels carrying the race live became replays."""
        event = self.grand_prix()
        self.run_match(now=datetime(2099, 11, 21, 12))
        event = db.session.get(SportsEvent, event.id)
        self.assertEqual(event.guide_start, datetime(2099, 11, 22, 4, 0))
        by_title = {}
        for m in self.matches(event):
            by_title.setdefault(m.program_title, set()).add((m.strength, m.airing))
        self.assertEqual(by_title['Live Harbor Pretend Stock Car Series Grand Prix'],
                         {(SPORTS_MATCH_STRONG, SPORTS_AIRING_FIRST)})
        self.assertEqual(by_title['Pretend Stock Car Series - Harbor Grand Prix'],
                         {(SPORTS_MATCH_STRONG, SPORTS_AIRING_FIRST)})
        self.assertEqual(by_title['Harbor Pretend Stock Car Series Grand Prix'],
                         {(SPORTS_MATCH_STRONG, SPORTS_AIRING_REPLAY)})
        self.assertEqual({a for s, a in by_title['Pretend Stock Car Series Academy']}, {SPORTS_AIRING_UNKNOWN})

    def test_the_start_near_thesportsdb_beats_more_channels_further_off(self):
        """The race's starts are ranked as a game's are (dev/changelog/1620): an earlier strong listing on
        more channels does not outrank one at TheSportsDB's time."""
        event = self.race('Harbor Grand Prix', datetime(2099, 11, 22, 4, 0), date(2099, 11, 21), self.series)
        for _ in range(3):
            self.row('Pretend Stock Car Series', sub_title='Harbor Grand Prix', start=datetime(2099, 11, 21, 20, 0))
        self.row('Pretend Stock Car Series', sub_title='Harbor Grand Prix', start=datetime(2099, 11, 22, 4, 0))
        self.run_match(now=datetime(2099, 11, 21, 12))
        event = db.session.get(SportsEvent, event.id)
        self.assertEqual((event.guide_start, event.guide_channels), (datetime(2099, 11, 22, 4, 0), 1))

    def test_a_race_with_only_weak_listings_still_gets_a_start(self):
        """Characterization: weak listings set the start when there is nothing stronger."""
        event = self.race('Harbor Grand Prix', datetime(2099, 11, 22, 4, 0), date(2099, 11, 21), self.series)
        self.row('Pretend Stock Car Series', sub_title='Ironvale 300', start=datetime(2099, 11, 22, 4, 0))
        self.run_match(now=datetime(2099, 11, 21, 12))
        event = db.session.get(SportsEvent, event.id)
        self.assertEqual(event.guide_start, datetime(2099, 11, 22, 4, 0))

    def test_a_race_moved_later_after_it_started_keeps_its_start(self):
        """dev/docs/BUGS.md 2026-11-18 @ 03:25:38 PM, the F1 rain delay: moved two hours later at the green flag, the race
        kept a start only matchups held, the start went to the re-air nearer the new time, and the
        channels carrying the race live lost it."""
        event = self.race('Harbor Grand Prix', datetime(2099, 11, 22, 4, 0), date(2099, 11, 21), self.series)
        for _ in range(4):
            self.row('Live Harbor Pretend Stock Car Series Grand Prix', start=datetime(2099, 11, 22, 3, 55),
                     minutes=125)
        self.row('Harbor Pretend Stock Car Series Grand Prix', start=datetime(2099, 11, 22, 6, 0), minutes=125)
        self.run_match(now=datetime(2099, 11, 21, 12))
        event = db.session.get(SportsEvent, event.id)
        self.assertEqual(event.guide_start, datetime(2099, 11, 22, 3, 55))
        event.previous_start, event.start_at = event.start_at, datetime(2099, 11, 22, 6, 0)
        event.start_changed_at = datetime(2099, 11, 22, 4, 15)
        db.session.commit()
        now = datetime(2099, 11, 22, 4, 20)
        self.run_match(now=now)
        event = db.session.get(SportsEvent, event.id)
        self.assertEqual(event.guide_start, datetime(2099, 11, 22, 3, 55))
        playing = sports_match.airing_now([event.id], now)
        self.assertEqual(len(playing), 4)
        self.assertEqual({t for _e, _c, _s, t in playing}, {datetime(2099, 11, 22, 8, 0)})

    def test_onboard_driver_and_pit_lane_feeds_are_not_the_race(self):
        """The main feed is what is recorded and played (dev/docs/BUGS.md 2026-11-18 @ 09:04:05 AM)."""
        event = self.race('Harbor Grand Prix', datetime(2099, 11, 22, 4, 0), date(2099, 11, 21), self.series)
        start = datetime(2099, 11, 22, 4, 0)
        self.row('Pretend Stock Car Series Onboard', sub_title='Harbor Grand Prix', start=start)
        self.row('Pretend Stock Car Series: Driver Cam', sub_title='Harbor Grand Prix', start=start)
        self.row('Pretend Stock Car Series Pit Lane', sub_title='Harbor Grand Prix', start=start)
        self.row('Pretend Stock Car Series Data Channel', sub_title='Harbor Grand Prix', start=start)
        self.run_match(now=datetime(2099, 11, 21, 12))
        self.assertEqual(self.matches(event), [])


class SkipWordsTests(MatchCase):
    """The matcher reads each league's own words and lengths, once per run."""

    def setUp(self):
        super().setUp()
        self.series = self.league(9003, 'Pretend Stock Car Series', ['Pretend Stock Car Series'], sport='Motorsport')
        queued = mock.patch('app.scheduler.queue_sports_match', return_value=True)
        queued.start()
        self.addCleanup(queued.stop)
        self.race_event = self.race('Lakeside 200', datetime(2099, 9, 17, 23, 30), date(2099, 9, 17), self.series)
        self.when = datetime(2099, 9, 17, 23, 30)

    def titles(self, event):
        self.run_match(now=datetime(2099, 9, 17, 12))
        return sorted(m.program_title for m in self.matches(event, gone_since=None))

    def test_a_removed_word_lets_a_listing_count_and_an_added_one_keeps_it_out(self):
        self.row('Pretend Stock Car Series Classics', sub_title='Lakeside 200', start=self.when)
        self.row('Pretend Stock Car Series Chequered Flag', sub_title='Lakeside 200', start=self.when)
        self.assertEqual(self.titles(self.race_event), ['Pretend Stock Car Series Chequered Flag'])
        sports_skips.remove_word(9003, sports_skips.KIND_RACE, 'classics')
        sports_skips.add_word(9003, sports_skips.KIND_RACE, 'chequered flag')
        self.assertEqual(self.titles(self.race_event), ['Pretend Stock Car Series Classics'])

    def test_the_race_length_is_the_leagues(self):
        self.row('Pretend Stock Car Series', sub_title='Lakeside 200', start=self.when, minutes=60)
        self.assertEqual(self.titles(self.race_event), [])
        sports_skips.set_minutes(9003, sports_skips.LENGTH_RACE, 60)
        self.assertEqual(self.titles(self.race_event), ['Pretend Stock Car Series'])

    def test_a_game_reads_the_game_list_and_length(self):
        game = self.game(1, 2)
        self.row('Pretend Football League Gameday: Ironvale Bears at Harborview Gulls', start=KICKOFF)
        self.row('PFL: Ironvale Bears at Harborview Gulls', start=KICKOFF + timedelta(hours=4), minutes=60)
        self.run_match()
        self.assertEqual(len(self.matches(game)), 1)
        sports_skips.add_word(9001, sports_skips.KIND_GAME, 'gameday')
        sports_skips.set_minutes(9001, sports_skips.LENGTH_GAME, 60)
        self.run_match()
        self.assertEqual([m.program_title for m in self.matches(game, gone_since=None)],
                         ['PFL: Ironvale Bears at Harborview Gulls'])

    def test_one_config_read_per_run_whatever_the_leagues(self):
        self.game(1, 2)
        self.game(11, 12, league=self.hockey)
        with mock.patch('app.sports_skips.load_config', wraps=load_config) as read:
            self.run_match()
        self.assertEqual(read.call_count, 1)


class SkippedListingsTests(MatchCase):
    """The listings dialog's Skipped section, worked out when it opens."""

    def setUp(self):
        super().setUp()
        self.series = self.league(9003, 'Pretend Stock Car Series', ['Pretend Stock Car Series', 'PSCS'],
                                  sport='Motorsport')
        queued = mock.patch('app.scheduler.queue_sports_match', return_value=True)
        queued.start()
        self.addCleanup(queued.stop)

    def skipped(self, event):
        r = self.t.client.get(f'/api/sports/events/{event.id}/listings')
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_a_race_names_its_skipped_listings_grouped_with_every_reason(self):
        race = self.race('Harbor Grand Prix', datetime(2099, 11, 22, 4, 0), date(2099, 11, 21), self.series)
        self.race('Harbor Grand Prix Qualifying', datetime(2099, 11, 21, 4, 0), date(2099, 11, 20), self.series)
        for _ in range(3):
            self.row('Harbor PSCS GP Highlights', start=datetime(2099, 11, 22, 7, 0), minutes=60)
        self.row('PSCS Chequered Flag - Harbor Grand Prix', start=datetime(2099, 11, 22, 6, 0), minutes=60)
        self.row('Harbor PSCS GP: Qualifying Hlts', start=datetime(2099, 11, 22, 1, 25), minutes=60)
        self.row('Live Harbor PSCS Grand Prix', start=datetime(2099, 11, 22, 3, 55), minutes=125)
        self.row('Harbor Highlights', start=datetime(2099, 11, 22, 8, 0), minutes=60)
        data = self.skipped(race)
        got = [(g['title'], g['channels'], g['why']) for g in data['skipped']]
        self.assertEqual(got, [
            ('PSCS Chequered Flag - Harbor Grand Prix', 1,
             'It is 60 minutes long, shorter than the 120 a race listing needs.'),
            ('Harbor PSCS GP Highlights', 3,
             'It says "highlights", and it is 60 minutes long, shorter than the 120 a race listing needs.'),
        ], "the qualifying's highlights are its own, the race listing counts, and a listing not naming the series is not shown")
        self.assertEqual((data['league'], data['matchup']), ('Pretend Stock Car Series', False))

    def test_a_game_shows_only_listings_naming_both_teams(self):
        game = self.game(1, 2)
        self.row('Pretend Football League In-Game Live: Ironvale Bears at Harborview Gulls', start=KICKOFF, minutes=60)
        self.row('PFL Pregame: Harborview Gulls', start=KICKOFF - timedelta(hours=1), minutes=60)
        self.row('PFL Highlights Factory', start=KICKOFF, minutes=30)
        data = self.skipped(game)
        self.assertEqual([(g['title'], g['why']) for g in data['skipped']], [
            ('Pretend Football League In-Game Live: Ironvale Bears at Harborview Gulls',
             'It says "in game", and it is 60 minutes long, shorter than the 90 a game listing needs.')])
        self.assertTrue(data['matchup'])


class SessionOfTests(unittest.TestCase):
    """What a field event or a listing names (dev/changelog/1633), on TheSportsDB's and prod's guide
    spellings, with the series' own names swapped for made-up ones."""

    def test_thesportsdbs_names(self):
        S = sports_match.Session
        for name, want in (
                ('Harbor Grand Prix', S(sports_match.SESSION_RACE)),
                ('Harbor Grand Prix Practice 1', S(sports_match.SESSION_PRACTICE, 1)),
                ('Harbor Grand Prix Practice 3', S(sports_match.SESSION_PRACTICE, 3)),
                ('Harbor Grand Prix Qualifying', S(sports_match.SESSION_QUALIFYING)),
                ('Harbor Grand Prix Sprint', S(sports_match.SESSION_SPRINT)),
                ('Harbor Grand Prix Sprint Qualifying', S(sports_match.SESSION_SPRINT_QUALIFYING)),
                ('Harbor Grand Final Qualifying Race 35', S(sports_match.SESSION_QUALIFYING, 35)),
                ('Harbor Grand Final Top 10 Shootout Race 36', S(sports_match.SESSION_SHOOTOUT, 36)),
                ('Harbor Grand Final Race 37', S(sports_match.SESSION_RACE)),
                ('The Duel at Harbor', S(sports_match.SESSION_RACE))):
            self.assertEqual(sports_match.session_of(sports_match.normalize(name)), want, name)

    def test_the_guides_spellings(self):
        S = sports_match.Session
        for text, want in (
                ('Live Harbor F1 GP: Practice 1', S(sports_match.SESSION_PRACTICE, 1)),
                ('Harbor Grand Prix Practice-2 ᴸᶦᵛᵉ', S(sports_match.SESSION_PRACTICE, 2)),
                ('Live: Formula 1 Harbor Grand Prix: 1st Practice', S(sports_match.SESSION_PRACTICE, 1)),
                ('Harbor Grand Prix Qualification ᴸᶦᵛᵉ', S(sports_match.SESSION_QUALIFYING)),
                ('Pretend Sprint Cup Series: Harbor 400', S(sports_match.SESSION_RACE))):
            self.assertEqual(sports_match.session_of(sports_match.normalize(text)), want, text)


class SessionTests(MatchCase):
    """Practice, qualifying, shootouts and sprints match as their own events (dev/changelog/1633), in
    the shape of prod's Las Vegas and Adelaide weekends of 2026-11-18."""

    def setUp(self):
        super().setUp()
        self.series = self.league(9003, 'Pretend Stock Car Series', ['Pretend Stock Car Series', 'PSCS'],
                                  sport='Motorsport')

    def weekend(self):
        ev = {}
        for key, name, start, local in (
                ('p1', 'Harbor Grand Prix Practice 1', datetime(2099, 11, 20, 0, 30), date(2099, 11, 19)),
                ('p2', 'Harbor Grand Prix Practice 2', datetime(2099, 11, 20, 4, 0), date(2099, 11, 19)),
                ('p3', 'Harbor Grand Prix Practice 3', datetime(2099, 11, 21, 0, 30), date(2099, 11, 20)),
                ('q', 'Harbor Grand Prix Qualifying', datetime(2099, 11, 21, 4, 0), date(2099, 11, 20)),
                ('race', 'Harbor Grand Prix', datetime(2099, 11, 22, 4, 0), date(2099, 11, 21))):
            ev[key] = self.race(name, start, local, self.series)
        return ev

    def titles(self, event):
        return {(m.program_title, m.strength, m.airing) for m in self.matches(event)}

    def test_each_session_matches_its_own_event_and_never_the_race(self):
        ev = self.weekend()
        self.row('Live Harbor PSCS GP: Practice 1', start=datetime(2099, 11, 20, 0, 0), minutes=115)
        for _ in range(2):
            self.row('Live Harbor PSCS GP: Practice 2', start=datetime(2099, 11, 20, 3, 45), minutes=100)
        self.row('Harbor PSCS GP: Practice 2', start=datetime(2099, 11, 20, 10, 15), minutes=75)
        self.row('PSCS Racing - Harbor Grand Prix, Practice 3 ᴸᶦᵛᵉ', start=datetime(2099, 11, 21, 0, 15), minutes=115)
        self.row('Live Harbor PSCS GP: Qualifying', start=datetime(2099, 11, 21, 3, 0), minutes=180)
        self.row('Live Harbor PSCS Grand Prix', start=datetime(2099, 11, 22, 3, 55), minutes=125)
        self.run_match(now=datetime(2099, 11, 19, 12))
        first, replay = SPORTS_AIRING_FIRST, SPORTS_AIRING_REPLAY
        self.assertEqual(self.titles(ev['p1']), {('Live Harbor PSCS GP: Practice 1', SPORTS_MATCH_STRONG, first)})
        self.assertEqual(self.titles(ev['p2']), {('Live Harbor PSCS GP: Practice 2', SPORTS_MATCH_STRONG, first),
                                                 ('Harbor PSCS GP: Practice 2', SPORTS_MATCH_STRONG, replay)})
        self.assertEqual({t for t, _s, _a in self.titles(ev['p3'])}, {'PSCS Racing - Harbor Grand Prix, Practice 3'})
        self.assertEqual(self.titles(ev['q']), {('Live Harbor PSCS GP: Qualifying', SPORTS_MATCH_STRONG, first)})
        self.assertEqual(self.titles(ev['race']), {('Live Harbor PSCS Grand Prix', SPORTS_MATCH_STRONG, first)})
        race, quali = (db.session.get(SportsEvent, ev[k].id) for k in ('race', 'q'))
        self.assertEqual(race.guide_start, datetime(2099, 11, 22, 3, 55), "a session's listing never sets the race's")
        self.assertEqual(quali.guide_start, datetime(2099, 11, 21, 3, 0))
        self.assertIn('the qualifying, and the listing names this race weekend', self.matches(ev['q'])[0].reason)

    def test_matched_sessions_count_toward_the_leagues_trust(self):
        """Question 1(b) of dev/changelog/1629: F1 reaches 10 measured events in two weekends."""
        ev = self.weekend()
        for key, title in (('p1', 'Practice 1'), ('p2', 'Practice 2'), ('p3', 'Practice 3'), ('q', 'Qualifying')):
            self.row(f'Live Harbor PSCS GP: {title}', start=ev[key].start_at, minutes=90)
        self.row('Live Harbor PSCS Grand Prix', start=ev['race'].start_at, minutes=125)
        self.run_match(now=datetime(2099, 11, 19, 12))
        self.assertEqual(db.session.get(SportsLeague, self.series.id).trust_sample, 5)

    def test_a_listing_naming_no_race_on_a_session_day_matches_nothing(self):
        """`Formula 1` on qualifying day is as likely the qualifying, so it is matched to the race only
        on a day the race is the one event (dev/changelog/1633)."""
        ev = self.weekend()
        self.row('Live PSCS', start=datetime(2099, 11, 21, 3, 0), minutes=180)
        self.row('PSCS Racing', start=datetime(2099, 11, 21, 3, 5), minutes=180)
        self.row('PSCS Racing', start=datetime(2099, 11, 22, 3, 55), minutes=180)
        self.run_match(now=datetime(2099, 11, 19, 12))
        self.assertEqual([m.program_start_time for m in self.matches(ev['race'])], [datetime(2099, 11, 22, 3, 55)])
        for key in ('p1', 'p2', 'p3', 'q'):
            self.assertEqual(self.matches(ev[key]), [], key)

    def test_a_race_replay_on_practice_day_is_not_a_weak_practice_match(self):
        """Prod's Las Vegas Practice 1, the only event on its day, held 84 weak matches from another
        race's replay and the year before's."""
        p1 = self.race('Harbor Grand Prix Practice 1', datetime(2099, 11, 20, 0, 30), date(2099, 11, 19), self.series)
        self.row('PSCS', sub_title='Ironvale Grand Prix', start=datetime(2099, 11, 19, 20, 0), minutes=150)
        self.row('Harbor PSCS Grand Prix 2098', start=datetime(2099, 11, 19, 22, 0), minutes=150)
        self.run_match(now=datetime(2099, 11, 19, 12))
        self.assertEqual(self.matches(p1), [])

    def test_placeholders_pre_shows_highlights_and_short_listings_are_not_sessions(self):
        ev = self.weekend()
        self.row('Next EVENT: PSCS Harbor Grand Prix Practice-1 on 2099-11-19 at 07:30PM EST',
                 start=datetime(2099, 11, 19, 20, 0), minutes=222)
        self.row('PSCS Pre Qualifying - Harbor Grand Prix', start=datetime(2099, 11, 21, 3, 0), minutes=55)
        self.row('Harbor PSCS GP: Qualifying Hlts', start=datetime(2099, 11, 21, 8, 10), minutes=60)
        self.row('Live Harbor PSCS GP: Qualifying', start=datetime(2099, 11, 21, 3, 50), minutes=20)
        self.run_match(now=datetime(2099, 11, 19, 12))
        for key in ev:
            self.assertEqual(self.matches(ev[key]), [], key)

    def test_another_series_practice_with_several_practices_that_day_matches_none(self):
        """`F1 Academy: Practice` beside the weekend's three practices."""
        ev = self.weekend()
        self.row('Live PSCS Academy: Practice', start=datetime(2099, 11, 20, 1, 55), minutes=55)
        self.run_match(now=datetime(2099, 11, 19, 12))
        for key in ('p1', 'p2', 'p3'):
            self.assertEqual(self.matches(ev[key]), [], key)

    def test_a_sprint_and_its_qualifying_are_their_own_events(self):
        sq = self.race('Harbor Grand Prix Sprint Qualifying', datetime(2099, 11, 20, 20, 30), date(2099, 11, 20),
                       self.series)
        sprint = self.race('Harbor Grand Prix Sprint', datetime(2099, 11, 21, 16, 0), date(2099, 11, 21), self.series)
        race = self.race('Harbor Grand Prix', datetime(2099, 11, 22, 18, 0), date(2099, 11, 22), self.series)
        self.row('Live Harbor PSCS GP: Sprint Qualifying', start=datetime(2099, 11, 20, 20, 15), minutes=75)
        self.row('Live Harbor PSCS GP: Sprint', start=datetime(2099, 11, 21, 15, 45), minutes=60)
        self.run_match(now=datetime(2099, 11, 19, 12))
        self.assertEqual({t for t, _s, _a in self.titles(sq)}, {'Live Harbor PSCS GP: Sprint Qualifying'})
        self.assertEqual({t for t, _s, _a in self.titles(sprint)}, {'Live Harbor PSCS GP: Sprint'})
        self.assertEqual(self.matches(race), [])

    def test_three_qualifyings_in_one_band_go_by_number_then_by_the_nearest_start(self):
        """Adelaide's shape: a qualifying and a race each of three days, so a day's band holds two or
        three of each kind, and a session is rarely the only one of its day (dev/changelog/1633)."""
        ev = {}
        for key, name, start in (
                ('q35', 'Harbor Grand Final Qualifying Race 35', datetime(2099, 11, 27, 2, 40)),
                ('r35', 'Harbor Grand Final Race 35', datetime(2099, 11, 27, 5, 50)),
                ('q36', 'Harbor Grand Final Qualifying Race 36', datetime(2099, 11, 28, 1, 15)),
                ('s36', 'Harbor Grand Final Top 10 Shootout Race 36', datetime(2099, 11, 28, 2, 5)),
                ('r36', 'Harbor Grand Final Race 36', datetime(2099, 11, 28, 4, 50)),
                ('q37', 'Harbor Grand Final Qualifying Race 37', datetime(2099, 11, 29, 1, 15)),
                ('r37', 'Harbor Grand Final Race 37', datetime(2099, 11, 29, 4, 45))):
            ev[key] = self.race(name, start, start.date(), self.series)
        self.row('PSCS: Qualifying Race 36', start=datetime(2099, 11, 28, 1, 10), minutes=60)
        self.row('PSCS Qualifying', start=datetime(2099, 11, 29, 1, 15), minutes=45)
        self.row('PSCS Qualifying', start=datetime(2099, 11, 28, 13, 0), minutes=45)
        self.row('PSCS Top 10 Shootout', start=datetime(2099, 11, 28, 2, 0), minutes=40)
        self.run_match(now=datetime(2099, 11, 26, 12))
        starts = {k: [m.program_start_time for m in self.matches(e)] for k, e in ev.items()}
        self.assertEqual(starts['q36'], [datetime(2099, 11, 28, 1, 10)])
        self.assertEqual(starts['q37'], [datetime(2099, 11, 29, 1, 15)])
        self.assertEqual(starts['s36'], [datetime(2099, 11, 28, 2, 0)])
        self.assertEqual(starts['q35'], [])
        self.assertIn('the one starting nearest it', self.matches(ev['q37'])[0].reason)


class StorageTests(MatchCase):
    """A match survives an import, is marked gone rather than deleted, and a rejection sticks (design 9)."""

    def test_a_reimport_keeps_the_same_match(self):
        event = self.game(2, 1)
        ch, entry = self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        (before,) = self.matches(event)
        db.session.delete(db.session.get(EPGEntry, entry.id))
        db.session.commit()
        make_epg_entry(ch, title='PFL Football ᴸᶦᵛᵉ', sub_title='Harborview Gulls at Ironvale Bears',
                       start_time=KICKOFF, duration_minutes=180)
        db.session.commit()
        self.run_match()
        (after,) = self.matches(event)
        self.assertEqual(after.id, before.id)
        self.assertIsNone(after.gone_since)

    def test_a_row_no_longer_listed_is_marked_gone_not_deleted(self):
        event = self.game(2, 1)
        _, entry = self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        db.session.delete(db.session.get(EPGEntry, entry.id))
        db.session.commit()
        self.run_match()
        (m,) = self.matches(event)
        self.assertIsNotNone(m.gone_since)

    def test_a_rejected_match_stays_rejected(self):
        event = self.game(2, 1)
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        (m,) = self.matches(event)
        m.rejected_at = NOW
        db.session.commit()
        self.run_match()
        (m,) = self.matches(event)
        self.assertIsNotNone(m.rejected_at)


class TeardownTests(MatchCase):
    """Channel ids are re-issued and foreign keys are off, so a match left behind would be inherited."""

    def test_a_channel_deleted_during_a_run_gets_no_match(self):
        event = self.game(2, 1)
        ch, entry = self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        row = sports_match.Row(ch.id, None, entry.title, entry.sub_title, entry.start_time, entry.stop_time, False)
        found = [sports_match.Found(row, SPORTS_MATCH_STRONG, 'x')]
        db.session.delete(db.session.get(EPGEntry, entry.id))
        db.session.delete(ch)
        db.session.commit()
        sports_match._store_event(event.id, found, KICKOFF, 1, NOW)
        self.assertEqual(SportsMatch.query.count(), 0)

    def test_deleting_the_account_deletes_its_channels_matches(self):
        from app.routes.accounts import _delete_account_and_jobs
        event = self.game(2, 1)
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        self.assertEqual(len(self.matches(event)), 1)
        deleted, _ = _delete_account_and_jobs(self.account.id)
        self.assertTrue(deleted)
        db.session.expire_all()
        self.assertEqual(SportsMatch.query.count(), 0)

    def test_the_missing_channel_delete_deletes_its_matches(self):
        event = self.game(2, 1)
        ch, _ = self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        self.assertEqual(len(self.matches(event)), 1)
        now = datetime.utcnow()
        ch.last_seen_at = now - timedelta(days=30)
        self.account.last_sync_at = now - timedelta(days=1)
        db.session.commit()
        resp = self.t.client.post('/channels/missing-delete', json={'account_id': self.account.id})
        self.assertEqual(resp.get_json().get('deleted_count'), 1, resp.get_data(as_text=True))
        db.session.expire_all()
        self.assertEqual(SportsMatch.query.count(), 0)

    def test_removing_the_league_deletes_its_matches_and_categories(self):
        event = self.game(2, 1)
        db.session.add(SportsLeagueCategory(league_id=self.football.id, category_name='PFL Package'))
        db.session.commit()
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        self.assertEqual(len(self.matches(event)), 1)
        self.assertTrue(sports_data.delete_league(self.football.id))
        self.assertEqual(SportsMatch.query.count(), 0)
        self.assertEqual(SportsLeagueCategory.query.count(), 0)

    def test_pruning_an_event_deletes_its_matches(self):
        event = self.game(2, 1)
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        self.run_match()
        self.assertEqual(len(self.matches(event)), 1)
        db.session.add(SportsState(id=1))
        db.session.commit()
        sports_data._prune({'history_keep_days': 1}, KICKOFF + timedelta(days=5))
        db.session.expire_all()
        self.assertEqual(SportsMatch.query.count(), 0)


class TrustAndRunTests(MatchCase):
    def test_trust_counts_events_the_guide_identified(self):
        for day in range(12):
            start = KICKOFF - timedelta(days=day)
            event = self.game(2, 1, start=start, tsdb_id=1000 + day)
            offset = timedelta(hours=2) if day == 0 else timedelta(0)
            # Two channels: one unmarked listing that far off is not believed as the start at all.
            for _ in range(2 if day == 0 else 1):
                self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start=start + offset)
            self.assertIsNotNone(event)
        self.run_match(now=KICKOFF)
        sports_match.match_league(self.football.id, KICKOFF - timedelta(days=11), pause=0)
        db.session.expire_all()
        league = db.session.get(SportsLeague, self.football.id)
        self.assertEqual((league.trust_sample, league.trust_agreed), (12, 11))
        self.assertTrue(sports_match.league_trusted(league))

    def test_a_refused_run_says_what_it_waits_for(self):
        db.session.add(SportsState(id=1))
        db.session.commit()
        self.game(2, 1)
        ticket = admission.try_start(admission.KIND_SYNC, 'Account 1')
        self.addCleanup(admission.release, ticket)
        note = self.run_match()
        self.assertIn('Waiting: an account sync', note)
        self.assertEqual(db.session.get(SportsState, 1).match_note, note)

    def test_a_run_that_stops_says_so_instead_of_the_last_good_one(self):
        from unittest.mock import patch
        db.session.add(SportsState(id=1, match_note='Found 3 events of 3 in the guide.'))
        db.session.commit()
        self.game(2, 1)
        with patch.object(sports_match, 'match_league', side_effect=RuntimeError('boom')), \
                self.assertLogs('app.sports_match', 'ERROR'):
            with self.assertRaises(RuntimeError):
                sports_match.run(now=NOW, pause=0)
        db.session.expire_all()
        self.assertIn('stopped with an error (RuntimeError)', db.session.get(SportsState, 1).match_note)
        self.assertEqual(admission.active_kinds(), set())

    def test_a_run_names_what_it_found(self):
        db.session.add(SportsState(id=1))
        db.session.commit()
        self.game(2, 1)
        self.game(4, 3)
        self.row('PFL Football', sub_title='Harborview Gulls at Ironvale Bears')
        note = self.run_match()
        self.assertIn('Found 1 event of 2 in the guide, on 1 listing', note)
        self.assertIsNotNone(db.session.get(SportsState, 1).last_match_at)


if __name__ == '__main__':
    unittest.main()
