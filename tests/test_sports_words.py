"""The word for one event of a league: game, match, race or event (app/sports_words.py,
dev/changelog/1663).

  - NounTests: the word comes from TheSportsDB's sport and the event's shape, for every motorsport
    and every sport that says match, never from a named league.
  - MotorsportPageTests: a motorsport league's page, its row on Sports Schedules and a rule about
    it never call a race a game (dev/docs/BUGS.md 2026-11-20).
  - MatchPageTests: a soccer league's page, rule page and waiting screen say match.

Made-up leagues and events; throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_words
"""
import html
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

from app import (admission, auto_rules, db, sports_feed, sports_screen, sports_skips, sports_view,  # noqa: E402
                 sports_words)
from app.alerts import AUTO_RULE_GAME_UNLISTED  # noqa: E402
from app.database import (  # noqa: E402
    SPORTS_FETCH_SHRUNK, SPORTS_KIND_ROUND, Alert, SportsEvent, SportsFetch, SportsFollow, SportsLeague,
    SportsLeagueCategory,
)

_TAGS = re.compile(r'<(script|style)\b.*?</\1>', re.S)
_ATTR_TEXT = re.compile(r'\b(?:data-tip|aria-label|title|placeholder|data-label)="([^"]*)"')
_GAME = re.compile(r'\bgames?\b', re.I)
#: A sentence about naming both teams is about a game whatever the league holds.
_TEAM_LINE = re.compile(r'both teams', re.I)


def said(page: str) -> list:
    """Every piece of text a person can read on a page: text nodes and the attributes shown as
    text. Markup, class names and scripts are left out."""
    body = _TAGS.sub(' ', page)
    out = [html.unescape(v) for v in _ATTR_TEXT.findall(body)]
    out += [html.unescape(t).strip() for t in re.split(r'<[^>]+>', body)]
    return [t for t in out if t]


def stray_games(page: str) -> list:
    return [t for t in said(page) if _GAME.search(t) and not _TEAM_LINE.search(t)]


class NounTests(unittest.TestCase):
    def test_the_word_comes_from_the_sport_and_the_shape(self):
        for sport, matchup, want in (
                ('American Football', True, 'game'), ('Ice Hockey', None, 'game'),
                ('Soccer', True, 'match'), ('Soccer', None, 'match'), ('Tennis', True, 'match'),
                ('Rugby', True, 'match'), ('Cricket', True, 'match'),
                ('Motorsport', False, 'race'), ('Motorsport', None, 'race'),
                ('Golf', False, 'event'), ('Tennis', False, 'event'), (None, None, 'game'), (None, False, 'event')):
            self.assertEqual(sports_words.noun(sport, matchup)[0], want, (sport, matchup))

    def test_a_plural_is_the_words_own(self):
        self.assertEqual([sports_words.many(w) for w in ('game', 'match', 'race', 'event')],
                         ['games', 'matches', 'races', 'events'])
        self.assertEqual((sports_words.a('event'), sports_words.a('match')), ('an event', 'a match'))

    def test_the_waiting_screen_never_adds_an_s(self):
        words = sports_screen.words_for(sports_screen.STATE_NONE, sports_screen.MODE_PROXY, 'match', carries='x')
        self.assertEqual(words.kicker, 'No matches scheduled')
        self.assertNotIn('matchs', words.note)
        self.assertEqual(sports_screen.carries(None, 'Pretend Premier', 1, 'match'), 'Pretend Premier matches')


class _PageCase(unittest.TestCase):
    SPORT = 'Motorsport'
    TEAMS = False

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.addCleanup(admission.reset_for_tests)
        self.t.sandbox_config({'display': {'timezone': 'America/New_York', 'time_format': '12h'}})
        now = datetime.utcnow().replace(second=0, microsecond=0)
        self.league = SportsLeague(tsdb_id=9301, name='Pretend Series', sport=self.SPORT, current_season='2099',
                                   match_terms=json.dumps(['Pretend Series']))
        db.session.add(self.league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=self.league.id, team_tsdb_id=0, rule_id=0, created_at=now))
        for i in range(2):
            start = now + timedelta(days=1 + i, hours=3)
            extra = (dict(home_team_id=11 + i, away_team_id=21 + i, home_name=f'Home {i}', away_name=f'Away {i}')
                     if self.TEAMS else {})
            db.session.add(SportsEvent(tsdb_id=8300 + i, league_id=self.league.id, name=f'Harbor {400 + i}',
                                       start_at=start, local_date=start.date(), **extra))
        db.session.add(SportsLeagueCategory(league_id=self.league.id, category_name='Pretend Series PPV'))
        db.session.add(SportsFetch(league_id=self.league.id, kind=SPORTS_KIND_ROUND,
                                   target=f'round:{self.league.tsdb_id}:2099:1', outcome=SPORTS_FETCH_SHRUNK,
                                   last_at=now, reason='Came back with fewer than were stored.'))
        db.session.commit()

    def page(self, url):
        r = self.t.client.get(url)
        self.assertEqual(r.status_code, 200, url)
        return r.get_data(as_text=True)


class MotorsportPageTests(_PageCase):
    def test_the_league_page_never_calls_a_race_a_game(self):
        """dev/docs/BUGS.md 2026-11-20: the Fewer games badge, the categories note and more wrote
        "game" on a motorsport league's page."""
        page = self.page(f'/sports/{self.league.id}')
        self.assertIn('Upcoming races', page)
        self.assertIn('Fewer races', page)
        self.assertEqual(stray_games(page), [])
        self.assertIn('"many": "races"', page)

    def test_the_list_sorts_by_next_event_and_its_row_says_races(self):
        page = self.page('/sports')
        self.assertIn('Next event', page)
        self.assertNotIn('Next game', page)
        row = page[page.index('Pretend Series'):]
        self.assertIn('<span class="rl-sum">All races</span>', row[:3000])

    def test_a_rule_about_it_expects_races_and_its_alert_names_a_race(self):
        ch = seed.make_channel(seed.make_account(), name='Somewhere')
        seed.make_epg_entry(ch, title='Unrelated Show', start_time=datetime.utcnow() + timedelta(hours=2))
        soon = datetime.utcnow().replace(second=0, microsecond=0) + timedelta(hours=3)
        db.session.add(SportsEvent(tsdb_id=8399, league_id=self.league.id, name='Ironvale 300', start_at=soon,
                                   local_date=soon.date()))
        db.session.commit()
        with patch('app.routes.recordings.schedule_recording'):
            rid = auto_rules.create_rule(auto_rules.read_rule_values(
                {'name': 'Pretend races', 'search': 'grain=airings&q=zzz', 'sports_league_id': self.league.id}))
            auto_rules.evaluate_now(rid)
        db.session.expire_all()
        page = self.page(f'/rules/{rid}')
        self.assertIn('Expected races', page)
        self.assertIn('<th>Race</th>', page)
        self.assertNotIn('Expected games', page)
        self.assertIn('"games": "Expected races"', page)
        alert = Alert.query.filter_by(alert_type=AUTO_RULE_GAME_UNLISTED, dismissed_at=None).one()
        self.assertIn('has a race with nothing to record it from', alert.title)
        self.assertNotRegex(alert.body, _GAME)

    def test_a_sports_channels_words_are_the_leagues(self):
        self.assertEqual(sports_feed.event_word(self.league), 'race')
        game = SimpleNamespace(home_team_id=0, away_team_id=0)
        self.assertEqual(sports_feed.event_word(self.league, [game]), 'race')


class MatchPageTests(_PageCase):
    SPORT = 'Soccer'
    TEAMS = True

    def test_a_soccer_leagues_page_says_match(self):
        page = self.page(f'/sports/{self.league.id}')
        self.assertIn('Upcoming matches', page)
        self.assertIn('for a match, the names of both teams', page)
        self.assertIn('A match still has to name both teams', page)
        self.assertNotIn('matchs', page)
        # The skip words are what listings say (`pre game`), not the page's own word for a match.
        words = set(sports_skips.BUILTIN.of(self.league.tsdb_id).words(sports_skips.KIND_GAME))
        # Nor is the card's example of them, `a pre-game show`.
        own = [t for t in said(page) if _GAME.search(t) and t.removeprefix('Remove ') not in words
               and 'pre-game show' not in t]
        self.assertEqual(own, [])

    def test_the_skip_words_card_is_still_the_matchup_one(self):
        """The card's list was chosen by comparing the word to "game", which a match is not."""
        from app.config import load_config
        ctx = sports_view.skips_context(db.session.get(SportsLeague, self.league.id), load_config(), 'match')
        self.assertTrue(ctx['game'])
        self.assertEqual(ctx['lengths'][0]['label'], 'Shortest match listing (minutes)')
        # The examples stay the built-in skip words, which are spelled for a game (sports_skips).
        self.assertIn('a pre-game show', ctx['shows'])


if __name__ == '__main__':
    unittest.main()
