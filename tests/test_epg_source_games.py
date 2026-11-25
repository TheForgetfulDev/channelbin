"""Which followed sports games each EPG source would match (app/sports_match.py::source_games(),
app/epg_scorecard.py, dev/changelog/1726).

For an account reading two or more guides, the EPG check runs the sports matcher's own judge over
each source's listings alone, as a dry run, and stores per source how many followed games still
to come it lists, on how many channels and how many no other source lists. The games read from
channel names belong to no source and are counted apart. These tests pin:

  - DryRunTests: a source's listings are read from both tables, only for this account's
    channels, only followed games still to come count, a listing marked a wrong match does not,
    a hidden channel does not, and nothing is written to sports_matches.
  - ParityTests: one source's dry run finds the games a real matcher run stores.
  - ScoreTests: score_source_games() counts through each source's reach day, and "only here".
  - CheckTests: check_source_games() stores rows for an account with two sources, none for one,
    and run_check() clears an account that reads nothing.
  - TeardownTests: stopping, deleting a source and deleting the account take the rows; a row for
    a source no longer read is never shown.
  - NameTests and PageTests: the channel-name count, and both lines on the account page.

Made-up leagues and teams, a throwaway SQLite DB, no network.
  python3 -m unittest tests.test_epg_source_games
"""
import json
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, sports_match  # noqa: E402
from app.database import (  # noqa: E402
    EpgAlternateEntry, EpgSource, EpgSourceGames, EpgSourceSubscription, EPG_SOURCE_URL,
    SPORTS_AIRING_FIRST, SPORTS_MATCH_KIND_CHANNEL_NAME, SPORTS_MATCH_KIND_GUIDE, SPORTS_MATCH_STRONG,
    SportsEvent, SportsFollow, SportsLeague, SportsMatch, SportsTeam,
)
from app.epg_scorecard import (SourceGamesScore, check_source_games, games_text,  # noqa: E402
                               name_games_text, run_check, score_source_games,
                               stored_source_games)
from app.epg_sources import delete_sources_for_account, delete_url_source, stop_using_source  # noqa: E402
from app.sports_match import Listed, SourceGames, name_games, source_games  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_epg_entry, make_epg_source  # noqa: E402

NOW = datetime(2099, 11, 8, 12, 0)
KICKOFF = datetime(2099, 11, 8, 18, 0)


class GamesCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.account = make_account()
        self.first = make_epg_source(self.account, EPG_SOURCE_URL, name='First guide')
        self.second = self.source(self.account, 'Second guide')
        self.league = SportsLeague(tsdb_id=9001, name='Pretend Football League', sport='American Football',
                                   match_terms=json.dumps(['Pretend Football League', 'PFL']))
        db.session.add(self.league)
        db.session.flush()
        self.teams = {}
        for tid, full, nick in ((1, 'Harborview Gulls', 'Gulls'), (2, 'Ironvale Bears', 'Bears'),
                                (3, 'Northgate Panthers', 'Panthers'), (4, 'Metro City Giants', 'Giants')):
            self.teams[tid] = full
            db.session.add(SportsTeam(tsdb_id=tid, full_name=full, names=json.dumps([full, nick]),
                                      city=full[:-len(nick)].strip(), last_looked_up=NOW))
        self.follow(0)
        db.session.commit()
        self._channels = 0

    def source(self, account, name):
        src = EpgSource(kind=EPG_SOURCE_URL, owner_account_id=account.id, name=name,
                        url=f'http://example.test/{name}.xml')
        db.session.add(src)
        db.session.flush()
        n = EpgSourceSubscription.query.filter_by(account_id=account.id).count()
        db.session.add(EpgSourceSubscription(source_id=src.id, account_id=account.id, priority=n + 1))
        db.session.flush()
        return src

    def follow(self, team, league=None):
        db.session.add(SportsFollow(league_id=(league or self.league).id, team_tsdb_id=team))
        db.session.flush()

    def game(self, home, away, start=KICKOFF, tsdb_id=None):
        event = SportsEvent(tsdb_id=tsdb_id or (SportsEvent.query.count() + 100), league_id=self.league.id,
                            name=f'{self.teams[home]} vs {self.teams[away]}', home_team_id=home,
                            away_team_id=away, home_name=self.teams[home], away_name=self.teams[away],
                            start_at=start, local_date=start.date())
        db.session.add(event)
        db.session.commit()
        return event

    def channel(self, account=None, **kw):
        self._channels += 1
        return make_channel(account or self.account, name=f'Local {self._channels}', **kw)

    def listing(self, event, source, channel=None, start=None, alternate=False):
        """A listing of `event` from `source` on `channel`: in the guide, or in the alternates."""
        channel = channel or self.channel()
        title, sub = 'PFL Football', f'{self.teams[event.away_team_id]} at {self.teams[event.home_team_id]}'
        start = start or event.start_at
        if alternate:
            db.session.add(EpgAlternateEntry(source_id=source.id, channel_id=channel.id, title=title,
                                             sub_title=sub, start_time=start,
                                             stop_time=start + timedelta(hours=3)))
            db.session.flush()
        else:
            make_epg_entry(channel, title=title, sub_title=sub, start_time=start, duration_minutes=180,
                           source_id=source.id)
        db.session.commit()
        return channel

    def dry(self, account=None, sources=None):
        account = account or self.account
        return source_games(account.id, [s.id for s in (sources or (self.first, self.second))], NOW)


class DryRunTests(GamesCase):
    def test_each_source_is_judged_on_its_own_listings_from_either_table(self):
        both, guide_only, alt_only = self.game(1, 2), self.game(3, 4), self.game(2, 3, KICKOFF + timedelta(days=1))
        a, b = self.channel(), self.channel()
        self.listing(both, self.first, a)
        self.listing(both, self.second, a, alternate=True)
        self.listing(guide_only, self.first, b)
        self.listing(alt_only, self.second, b, alternate=True)
        got = self.dry()
        self.assertEqual(set(got.followed), {both.id, guide_only.id, alt_only.id})
        self.assertEqual(set(got.listed[self.first.id]), {both.id, guide_only.id})
        self.assertEqual(set(got.listed[self.second.id]), {both.id, alt_only.id})
        self.assertEqual(got.listed[self.first.id][both.id].channels, {a.id})
        self.assertTrue(got.listed[self.first.id][both.id].live)

    def test_another_accounts_channels_on_a_shared_source_do_not_count(self):
        other = make_account(name='Other')
        db.session.add(EpgSourceSubscription(source_id=self.first.id, account_id=other.id, priority=1))
        db.session.commit()
        event = self.game(1, 2)
        self.listing(event, self.first, self.channel(account=other))
        self.assertEqual(self.dry().listed[self.first.id], {})
        self.assertEqual(set(self.dry(account=other, sources=[self.first]).listed[self.first.id]), {event.id})

    def test_only_followed_games_still_to_come_count(self):
        SportsFollow.query.delete()
        self.follow(1)
        db.session.commit()
        mine, theirs = self.game(1, 2), self.game(3, 4)
        over = self.game(1, 3, NOW - timedelta(hours=1))
        for e in (mine, theirs, over):
            self.listing(e, self.first)
        got = self.dry()
        self.assertEqual(set(got.followed), {mine.id})
        self.assertEqual(set(got.listed[self.first.id]), {mine.id})

    def test_nothing_followed_counts_nothing(self):
        SportsFollow.query.delete()
        db.session.commit()
        self.listing(self.game(1, 2), self.first)
        got = self.dry()
        self.assertEqual((got.followed, got.listed[self.first.id]), ({}, {}))

    def test_a_listing_marked_a_wrong_match_does_not_count(self):
        event = self.game(1, 2)
        ch = self.listing(event, self.first)
        db.session.add(SportsMatch(event_id=event.id, channel_id=ch.id, kind=SPORTS_MATCH_KIND_GUIDE,
                                   program_start_time=event.start_at, title_key='pfl football',
                                   strength=SPORTS_MATCH_STRONG, airing=SPORTS_AIRING_FIRST, reason='x',
                                   rejected_at=NOW))
        db.session.commit()
        self.assertEqual(self.dry().listed[self.first.id], {})

    def test_a_hidden_channel_does_not_count(self):
        event = self.game(1, 2)
        self.listing(event, self.first, self.channel(hidden=True))
        self.assertEqual(self.dry().listed[self.first.id], {})

    def test_a_dry_run_writes_no_match(self):
        self.listing(self.game(1, 2), self.first)
        self.dry()
        db.session.expire_all()
        self.assertEqual(SportsMatch.query.count(), 0)


class ParityTests(GamesCase):
    def test_one_source_finds_the_games_a_run_stores(self):
        games = [self.game(1, 2), self.game(3, 4, KICKOFF + timedelta(days=1)),
                 self.game(2, 4, KICKOFF + timedelta(days=2))]
        self.listing(games[0], self.first)
        self.listing(games[1], self.first)
        self.listing(games[0], self.first, start=KICKOFF + timedelta(hours=8))   # a replay
        with mock.patch('app.sports_data.link_unfinished_recordings', return_value=0), \
                mock.patch('app.sports_follow.follow_guarded', return_value={}), \
                mock.patch('app.sports_feed.place_all_guarded'):
            sports_match.run(now=NOW, pause=0)
        db.session.expire_all()
        stored = {m.event_id for m in SportsMatch.query.filter_by(kind=SPORTS_MATCH_KIND_GUIDE)}
        self.assertEqual(stored, {games[0].id, games[1].id})
        self.assertEqual(set(self.dry(sources=[self.first]).listed[self.first.id]), stored)


class ScoreTests(unittest.TestCase):
    def test_every_number_on_the_line_is_through_the_reach_day(self):
        """A game after the reach day is listed_later and nothing else, so "N of M, on C
        channels, K only here" is one window and K is never more than N."""
        day = date(2099, 11, 8)
        days = {1: date(2099, 11, 8), 2: date(2099, 11, 9), 3: date(2099, 11, 10)}
        games = SourceGames(days, {
            10: {1: Listed({100}, True), 2: Listed({100, 101}, False)},
            20: {3: Listed({200}, True)},
            30: {1: Listed({300}, False)}})
        scores = {s.source_id: s for s in score_source_games(games, {10: day, 20: None, 30: None})}
        self.assertEqual(scores[10], SourceGamesScore(10, day, 1, 1, 1, 1, 1, 0, (1, 2)))
        self.assertEqual(scores[20], SourceGamesScore(20, None, 3, 1, 0, 1, 1, 1, (3,)))
        self.assertEqual(scores[30], SourceGamesScore(30, None, 3, 1, 0, 0, 1, 0, (1,)))

    def test_a_race_with_only_its_day_is_counted_on_that_day(self):
        """A race's 00:00 placeholder is the evening before in US time; its day is its own date."""
        from types import SimpleNamespace
        from zoneinfo import ZoneInfo
        from app.sports_data import game_day
        race = SimpleNamespace(start_at=datetime(2099, 11, 14, 0, 0), local_date=date(2099, 11, 14),
                               home_team_id=None, away_team_id=None)
        game = SimpleNamespace(start_at=datetime(2099, 11, 14, 2, 0), local_date=date(2099, 11, 14),
                               home_team_id=1, away_team_id=2)
        et = ZoneInfo('America/New_York')
        self.assertEqual(game_day(race, et), date(2099, 11, 14))
        self.assertEqual(game_day(game, et), date(2099, 11, 13))

    def test_the_lines_say_what_was_counted(self):
        s = SourceGamesScore(10, date(2099, 11, 9), 31, 22, 1, 20, 249, 19, ())
        line, tip = games_text(s, lambda d: 'Fri Nov 9', 'Nov 8, 9:40 AM', ('game', 'games'))
        self.assertEqual(line, 'Lists 22 of the 31 followed games through Fri Nov 9, on 249 channels, 19 only here.')
        self.assertIn('20 of the 22 games have a live airing here', tip)
        self.assertIn('It also lists 1 game after Fri Nov 9, not counted above.', tip)
        self.assertIn('Counted Nov 8, 9:40 AM.', tip)
        replays = SourceGamesScore(10, None, 3, 1, 0, 0, 1, 1, ())
        self.assertIn('Every one of them is a replay; none airs live here.',
                      games_text(replays, str, 'now', ('game', 'games'))[1])
        none = SourceGamesScore(10, None, 3, 0, 0, 0, 0, 0, ())
        self.assertEqual(games_text(none, str, 'now', ('race', 'races'))[0],
                         'Lists 0 of the 3 followed races still to come.')
        self.assertIsNone(name_games_text({}, set(), ('game', 'games')))
        line, _tip = name_games_text({1: {5, 6}, 2: {6}}, {1}, ('game', 'games'))
        self.assertEqual(line, 'Read from channel names: 2 followed games on 2 channels, 1 that no guide here lists.')


class CheckTests(GamesCase):
    def orders(self):
        return {self.account.id: [self.first.id, self.second.id]}

    def test_an_account_with_two_sources_gets_a_row_per_source(self):
        event = self.game(1, 2)
        self.listing(event, self.first)
        self.assertEqual(check_source_games(self.orders(), NOW), 1)
        rows = {g.source_id: g for g, _at in stored_source_games(self.account.id)}
        self.assertEqual(set(rows), {self.first.id, self.second.id})
        self.assertEqual((rows[self.first.id].listed, rows[self.first.id].only_here,
                          rows[self.first.id].event_ids), (1, 1, (event.id,)))
        self.assertEqual(rows[self.second.id].listed, 0)

    def test_an_account_with_one_source_keeps_none(self):
        self.listing(self.game(1, 2), self.first)
        check_source_games(self.orders(), NOW)
        check_source_games({self.account.id: [self.first.id]}, NOW)
        self.assertEqual(EpgSourceGames.query.count(), 0)

    def test_a_check_clears_an_account_that_reads_nothing(self):
        self.listing(self.game(1, 2), self.first)
        check_source_games(self.orders(), NOW)
        EpgSourceSubscription.query.delete()
        db.session.commit()
        with mock.patch('app.epg_scorecard.check_findings', return_value=0):
            run_check(NOW)
        db.session.expire_all()
        self.assertEqual(EpgSourceGames.query.count(), 0)


class TeardownTests(GamesCase):
    def setUp(self):
        super().setUp()
        self.listing(self.game(1, 2), self.first)
        check_source_games({self.account.id: [self.first.id, self.second.id]}, NOW)
        self.assertEqual(EpgSourceGames.query.count(), 2)

    def remaining(self):
        db.session.expire_all()
        return {(r.account_id, r.source_id) for r in EpgSourceGames.query}

    def test_stopping_a_source_takes_its_row(self):
        stop_using_source(self.account.id, self.second.id, False)
        self.assertEqual(self.remaining(), {(self.account.id, self.first.id)})

    def test_deleting_a_source_takes_its_row(self):
        delete_url_source(self.second.id, False)
        self.assertEqual(self.remaining(), {(self.account.id, self.first.id)})

    def test_deleting_the_account_takes_its_rows(self):
        # Its second source belongs to another account, so only the account's own delete takes
        # that row.
        self.second.owner_account_id = make_account(name='Owner').id
        db.session.commit()
        delete_sources_for_account(self.account.id)
        db.session.commit()
        self.assertEqual(self.remaining(), set())

    def test_a_row_for_a_source_no_longer_read_is_never_shown(self):
        EpgSourceSubscription.query.filter_by(source_id=self.second.id).delete()
        db.session.commit()
        self.assertEqual([g.source_id for g, _at in stored_source_games(self.account.id)], [self.first.id])


class NameTests(GamesCase):
    def name_match(self, event, channel, **kw):
        db.session.add(SportsMatch(event_id=event.id, channel_id=channel.id, kind=SPORTS_MATCH_KIND_CHANNEL_NAME,
                                   strength=SPORTS_MATCH_STRONG, airing=SPORTS_AIRING_FIRST, reason='x', **kw))
        db.session.commit()

    def test_the_accounts_current_name_matches_of_followed_games_to_come(self):
        SportsFollow.query.delete()
        self.follow(1)
        db.session.commit()
        mine, theirs, over = self.game(1, 2), self.game(3, 4), self.game(1, 3, NOW - timedelta(hours=1))
        a, b = self.channel(), self.channel()
        self.name_match(mine, a)
        self.name_match(mine, b)
        self.name_match(theirs, a)
        self.name_match(over, a)
        self.name_match(mine, self.channel(account=make_account(name='Other')))
        self.name_match(mine, self.channel(hidden=True))
        self.name_match(self.game(1, 4), a, gone_since=NOW)
        self.name_match(self.game(2, 1), a, rejected_at=NOW)
        self.assertEqual(name_games(self.account.id, NOW), {mine.id: {a.id, b.id}})


class PageTests(GamesCase):
    def test_the_sources_card_shows_each_sources_line_and_the_name_line(self):
        event, named = self.game(1, 2), self.game(3, 4)
        self.listing(event, self.first)
        ch = self.channel()
        db.session.add(SportsMatch(event_id=named.id, channel_id=ch.id, kind=SPORTS_MATCH_KIND_CHANNEL_NAME,
                                   strength=SPORTS_MATCH_STRONG, airing=SPORTS_AIRING_FIRST, reason='x'))
        db.session.commit()
        check_source_games({self.account.id: [self.first.id, self.second.id]}, NOW)
        # The page reads the name matches at the real time, and 2099's games are still to come.
        html = self.t.client.get(f'/accounts/{self.account.id}').get_data(as_text=True)
        # The first source reaches as far as the listing it holds; the second holds none.
        self.assertIn('Lists 1 of the 2 followed games through Nov 8, 2099, on 1 channel, 1 only here.', html)
        self.assertIn('Lists 0 of the 2 followed games still to come.', html)
        self.assertIn('Read from channel names: 1 followed game on 1 channel, 1 that no guide here lists.', html)

    def test_the_scorecard_api_carries_the_numbers(self):
        event, named = self.game(1, 2), self.game(3, 4)
        self.listing(event, self.first)
        db.session.add(SportsMatch(event_id=named.id, channel_id=self.channel().id,
                                   kind=SPORTS_MATCH_KIND_CHANNEL_NAME, strength=SPORTS_MATCH_STRONG,
                                   airing=SPORTS_AIRING_FIRST, reason='x'))
        db.session.commit()
        self.assertIsNone(self.t.client.get(f'/api/accounts/{self.account.id}/epg-scorecard').get_json()['sports'])
        check_source_games({self.account.id: [self.first.id, self.second.id]}, NOW)
        sports = self.t.client.get(f'/api/accounts/{self.account.id}/epg-scorecard').get_json()['sports']
        first = next(s for s in sports['sources'] if s['source_id'] == self.first.id)
        self.assertEqual((first['followed'], first['listed'], first['event_ids']), (2, 1, [event.id]))
        self.assertEqual(sports['by_channel_name'],
                         {'games': 1, 'channels': 1, 'only_by_name': 1, 'event_ids': [named.id]})

    def test_an_account_with_no_counts_shows_no_line(self):
        html = self.t.client.get(f'/accounts/{self.account.id}').get_data(as_text=True)
        self.assertNotIn('followed game', html)
        self.assertNotIn('acct-sports-names', html)


if __name__ == '__main__':
    unittest.main()
