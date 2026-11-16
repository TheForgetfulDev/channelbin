"""A team's look for a sports channel's waiting screen: its colors, short name and badge
(app/sports_data.py::_team_look, app/sports_screen.py::side_colors, app/sports_images.py's badges,
migration 127), dev/changelog/1532.

The shapes are lookupteam's as measured on 2026-11-13 (DESIGN-sports-data.md 2), served by
tests/support/fake_sportsdb.py over real HTTP with made-up teams and images. Throwaway temp SQLite
DB and images folder - never the live dvr.db.
  python3 -m unittest tests.test_sports_team_look
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, migrations, sports_data, sports_feed, sports_images  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (  # noqa: E402
    SPORTS_FETCH_OK, SPORTS_FETCH_REFUSED, SportsEvent, SportsFollow, SportsImage, SportsLeague, SportsTeam,
)
from app.sports_screen import (  # noqa: E402
    NEUTRAL_AWAY, NEUTRAL_HOME, NO_SECOND, side_colors, short_code, team_colors,
)
from app.storage_dirs import SPORTS_IMAGES, image_dir  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.fake_sportsdb import LEAGUE_ID, FakeSportsDB, game, team_row  # noqa: E402
from tests.support.images import png_bytes  # noqa: E402

NOW = datetime(2099, 3, 2, 12, 0)
BADGE = png_bytes(64, 64)


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


class LookParseTests(unittest.TestCase):
    """Each measured shape of lookupteam's color, short name and badge fields."""

    def test_colors_in_either_case_are_kept_lowercase(self):
        look = sports_data._team_look(team_row(1, 'Arsenal', colors=('#EF0107', '#fbffff', '#013373'),
                                               short='ARS', badge='https://r2.thesportsdb.com/b/x.png'))
        self.assertEqual((look['color1'], look['color2'], look['color3']), ('#ef0107', '#fbffff', '#013373'))
        self.assertEqual(look['short_name'], 'ARS')
        self.assertEqual(look['badge_url'], 'https://r2.thesportsdb.com/b/x.png')

    def test_empty_strings_are_unknown(self):
        """Forfar Athletic's answer: '' for every color and for the short name."""
        look = sports_data._team_look(team_row(2, 'Forfar Athletic', short=''))
        self.assertEqual(look, {'color1': None, 'color2': None, 'color3': None, 'short_name': None,
                                'badge_url': None})

    def test_absent_and_null_keys_are_unknown(self):
        look = sports_data._team_look({'idTeam': '3', 'strTeam': 'Nobody', 'strColour1': None, 'strBadge': ''})
        self.assertEqual(set(look.values()), {None})

    def test_a_color_that_is_not_rrggbb_is_unknown_and_logged(self):
        row = team_row(4, 'Oddballs', colors=('Red', '#12345', '#00ff00'))
        with self.assertLogs('app.sports_data', 'WARNING') as logs:
            look = sports_data._team_look(row)
        self.assertEqual((look['color1'], look['color2'], look['color3']), (None, None, '#00ff00'))
        self.assertIn("'Red'", logs.output[0])
        self.assertIn('Oddballs', logs.output[0])


class SideColorTests(unittest.TestCase):
    """The mockup's sideColors(): dev/mockups/55-page.js."""

    def test_two_known_teams_keep_their_first_colors(self):
        sides = side_colors(['#ef0107', '#fbffff'], ['#004c54', '#000000'])
        self.assertEqual((sides.away, sides.home, sides.swapped),
                         (('#ef0107', '#fbffff'), ('#004c54', '#000000'), False))

    def test_a_team_with_one_color_is_edged_in_white(self):
        self.assertEqual(side_colors(['#ef0107'], ['#004c54']).away, ('#ef0107', NO_SECOND))

    def test_one_missing_is_graphite_on_its_own_side(self):
        sides = side_colors(['#ef0107', '#fbffff'], [])
        self.assertEqual((sides.away, sides.home), (('#ef0107', '#fbffff'), NEUTRAL_HOME))
        sides = side_colors([], ['#004c54'])
        self.assertEqual((sides.away, sides.home), (NEUTRAL_AWAY, ('#004c54', NO_SECOND)))

    def test_both_missing_still_shows_two_sides(self):
        sides = side_colors([], [])
        self.assertEqual((sides.away, sides.home), (NEUTRAL_AWAY, NEUTRAL_HOME))
        self.assertNotEqual(sides.away[0], sides.home[0])

    def test_close_first_colors_put_home_in_its_second(self):
        """Nottingham Forest's red and Manchester United's."""
        sides = side_colors(['#dd0000', '#ffffff'], ['#da291c', '#fbe122'])
        self.assertTrue(sides.swapped)
        self.assertEqual(sides.home, ('#fbe122', '#da291c'))

    def test_no_swap_when_home_has_no_second_or_it_is_close_too(self):
        self.assertFalse(side_colors(['#dd0000'], ['#da291c']).swapped)
        self.assertFalse(side_colors(['#dd0000'], ['#da291c', '#cc1111']).swapped)

    def test_team_colors_skips_the_unknown_ones(self):
        team = SportsTeam(tsdb_id=1, full_name='x', color1=None, color2='#123456', color3='#abcdef')
        self.assertEqual(team_colors(team), ['#123456', '#abcdef'])
        self.assertEqual(team_colors(None), [])

    def test_the_short_code_falls_back_to_the_name(self):
        self.assertEqual(short_code('Forfar Athletic', None), 'FOR')
        self.assertEqual(short_code('St. Louis Blues', ''), 'STL')
        self.assertEqual(short_code('Arsenal', 'ars'), 'ARS')
        self.assertEqual(short_code('', None), '?')


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.fake = FakeSportsDB()
        self.addCleanup(self.fake.stop)
        p = patch.object(sports_data, 'BASE_URL', self.fake.base_url)
        p.start()
        self.addCleanup(p.stop)

    def configure(self, feed=True, download=True):
        self.t.sandbox_config({'integrations': {'output_feed': {'enabled': feed, 'username': 'tv',
                                                                'password': 'tv-secret'}},
                               'sports_data': {'download_images': download}})

    def tick(self, now):
        db.session.expire_all()
        out = sports_data.tick(now=now)
        db.session.expire_all()
        return out


class WorkerLookTests(_Case):
    """The look comes in on the same once-a-season lookupteam call as the names, and no other."""

    def test_a_followed_leagues_teams_get_their_look(self):
        start = NOW + timedelta(days=1)
        self.fake.next_event[LEAGUE_ID] = [game(1, 'Reds', 'Blues', iso(start), home_id=11, away_id=12)]
        self.fake.rounds[(LEAGUE_ID, '2099', 9)] = [game(1, 'Reds', 'Blues', iso(start), home_id=11, away_id=12)]
        self.fake.teams[11] = team_row(11, 'Pretendville Reds', 'Reds', colors=('#C8102E', '#FFFFFF', ''),
                                       short='PVR', badge='https://r2.thesportsdb.com/images/media/team/badge/r.png')
        self.fake.teams[12] = team_row(12, 'Pretendville Blues', short='')
        sports_data.follow_league({'tsdb_id': LEAGUE_ID, 'name': 'Pretend League', 'sport': 'Pretend Ball',
                                   'season': '2099', 'terms': ['Pretend League']}, None)
        now = NOW
        for _ in range(30):
            if self.tick(now) == 'idle':
                break
            now += timedelta(minutes=2)
        reds = SportsTeam.query.filter_by(tsdb_id=11).one()
        self.assertEqual((reds.color1, reds.color2, reds.color3, reds.short_name),
                         ('#c8102e', '#ffffff', None, 'PVR'))
        self.assertEqual(reds.badge_url, 'https://r2.thesportsdb.com/images/media/team/badge/r.png')
        blues = SportsTeam.query.filter_by(tsdb_id=12).one()
        self.assertEqual((blues.color1, blues.short_name, blues.badge_url), (None, None, None))
        self.assertEqual(len(self.fake.calls_to('lookupteam')), 2)


class MigrationTests(unittest.TestCase):
    """Migration 127 asks again for every team looked up before the look was stored."""

    def test_teams_with_no_look_are_asked_again_and_the_step_reruns_cleanly(self):
        tmp = tempfile.mkdtemp()
        conn = sqlite3.connect(os.path.join(tmp, 'm.db'))
        cur = conn.cursor()
        cur.execute('CREATE TABLE sports_teams (id INTEGER PRIMARY KEY, tsdb_id INTEGER, full_name TEXT, '
                    'names TEXT, city TEXT, last_looked_up DATETIME)')
        cur.execute('CREATE TABLE sports_fetches (id INTEGER PRIMARY KEY, target TEXT, kind TEXT, '
                    'league_id INTEGER, next_due_at DATETIME)')
        cur.execute("INSERT INTO sports_teams VALUES (1, 11, 'Reds', '[]', NULL, '2099-01-01 00:00:00')")
        cur.execute("INSERT INTO sports_teams VALUES (2, 12, 'Blues', '[]', NULL, NULL)")
        cur.execute("INSERT INTO sports_fetches VALUES (1, 'team:11', 'team', 1, NULL)")
        cur.execute("INSERT INTO sports_fetches VALUES (2, 'league:5', 'league', 1, NULL)")
        conn.commit()
        migrations._m127_sports_team_look(conn, cur)
        cols = {r[1] for r in cur.execute('PRAGMA table_info(sports_teams)')}
        self.assertTrue({'color1', 'color2', 'color3', 'short_name', 'badge_url'} <= cols)
        self.assertEqual(cur.execute('SELECT last_looked_up FROM sports_teams WHERE id = 1').fetchone(), (None,))
        due = dict(cur.execute('SELECT target, next_due_at FROM sports_fetches').fetchall())
        self.assertIsNotNone(due['team:11'])
        self.assertIsNone(due['league:5'])
        # A team that got its look is left alone by a second run.
        cur.execute("UPDATE sports_teams SET color1 = '#aabbcc', last_looked_up = '2099-02-01 00:00:00' WHERE id = 1")
        cur.execute("UPDATE sports_fetches SET next_due_at = NULL WHERE target = 'team:11'")
        conn.commit()
        migrations._m127_sports_team_look(conn, cur)
        self.assertIsNotNone(cur.execute('SELECT last_looked_up FROM sports_teams WHERE id = 1').fetchone()[0])
        self.assertIsNone(cur.execute("SELECT next_due_at FROM sports_fetches WHERE target = 'team:11'").fetchone()[0])
        conn.close()


BEARS, GULLS, PANTHERS, COMETS = 101, 102, 103, 104


class BadgeTests(_Case):
    """A badge is downloaded only for a team a served channel's waiting screen would draw."""

    def setUp(self):
        super().setUp()
        self.configure()
        # The fake is plain http on loopback; the real check is the last test's subject.
        self.loopback_ok = patch.object(sports_images, 'address_refused', lambda url: '')
        self.loopback_ok.start()
        self.addCleanup(self.loopback_ok.stop)
        self.league = SportsLeague(tsdb_id=9001, name='Pretend League', sport='Pretend Ball',
                                   current_season='2099', match_terms=json.dumps(['PFL']))
        db.session.add(self.league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=self.league.id, team_tsdb_id=0, rule_id=0))
        self.badges = {}
        for tsdb_id, name in ((BEARS, 'Bears'), (GULLS, 'Gulls'), (PANTHERS, 'Panthers'), (COMETS, 'Comets')):
            self.badges[tsdb_id] = self.fake.badge(f'{name.lower()}.png', BADGE)
            db.session.add(SportsTeam(tsdb_id=tsdb_id, full_name=name, names='[]', badge_url=self.badges[tsdb_id]))
        db.session.commit()
        self._tsdb = 7000

    def event(self, home, away, start):
        self._tsdb += 1
        db.session.add(SportsEvent(tsdb_id=self._tsdb, league_id=self.league.id, name=f'{away} vs {home}',
                                   home_team_id=home, away_team_id=away, start_at=start, local_date=start.date()))
        db.session.commit()

    def bears_channel(self):
        self.assertTrue(sports_feed.set_team_channel(self.league.id, BEARS, True))

    def downloaded(self):
        db.session.expire_all()
        return {i.url for i in SportsImage.query.filter(SportsImage.file_name.isnot(None))}

    def drain(self, now=NOW):
        for _ in range(12):
            if self.tick(now) == 'idle':
                return now
            now += timedelta(minutes=5)
        self.fail('the worker never ran out of badges')

    def test_the_next_games_teams_are_downloaded(self):
        self.bears_channel()
        self.event(BEARS, GULLS, NOW + timedelta(days=1))
        self.event(PANTHERS, COMETS, NOW + timedelta(days=1))  # not on the Bears' channel
        self.assertEqual(self.tick(NOW), SPORTS_FETCH_OK)
        self.drain(NOW + timedelta(minutes=5))
        self.assertEqual(self.downloaded(), {self.badges[BEARS], self.badges[GULLS]})
        path = sports_images.badge_path(load_config(), self.badges[GULLS])
        with open(path, 'rb') as fh:
            self.assertEqual(fh.read(), BADGE)
        self.assertIsNone(sports_images.badge_path(load_config(), self.badges[PANTHERS]))

    def test_only_the_games_a_screen_draws(self):
        """The next SCREEN_GAMES games of each channel, and none that is over."""
        self.bears_channel()
        self.event(BEARS, COMETS, NOW - timedelta(hours=8))  # over
        for day in range(1, sports_feed.SCREEN_GAMES + 1):
            self.event(BEARS, GULLS, NOW + timedelta(days=day))
        self.event(BEARS, PANTHERS, NOW + timedelta(days=9))  # the fifth coming game
        self.assertEqual(sports_feed.screen_team_ids(NOW), {BEARS, GULLS})

    def test_no_served_channel_downloads_nothing(self):
        self.event(BEARS, GULLS, NOW + timedelta(days=1))
        self.assertEqual(self.tick(NOW), 'idle')
        self.assertEqual(self.fake.calls_to('image'), [])

    def test_the_feed_off_downloads_nothing(self):
        self.configure(feed=False)
        self.bears_channel()
        self.event(BEARS, GULLS, NOW + timedelta(days=1))
        self.assertEqual(self.tick(NOW), 'idle')
        self.assertEqual(self.fake.calls_to('image'), [])

    def test_downloads_switched_off_downloads_nothing(self):
        self.configure(download=False)
        self.bears_channel()
        self.event(BEARS, GULLS, NOW + timedelta(days=1))
        self.assertEqual(self.tick(NOW), 'idle')
        self.assertEqual(self.fake.calls_to('image'), [])

    def test_a_badge_kept_while_its_team_names_it_and_pruned_after(self):
        self.bears_channel()
        self.event(BEARS, GULLS, NOW + timedelta(days=1))
        self.drain()
        folder = image_dir(load_config(), SPORTS_IMAGES)
        gulls = SportsImage.query.filter_by(url=self.badges[GULLS]).one().file_name
        sports_images.prune(load_config())
        self.assertTrue(os.path.exists(os.path.join(folder, gulls)))
        team = SportsTeam.query.filter_by(tsdb_id=GULLS).one()
        team.badge_url = self.fake.badge('gulls-new.png', BADGE)
        db.session.commit()
        sports_images.prune(load_config())
        self.assertFalse(os.path.exists(os.path.join(folder, gulls)))
        self.assertIsNone(SportsImage.query.filter_by(url=self.badges[GULLS]).first())

    def test_an_address_off_their_host_is_never_fetched(self):
        self.bears_channel()
        self.event(BEARS, GULLS, NOW + timedelta(days=1))
        self.loopback_ok.stop()
        self.assertEqual(self.tick(NOW), SPORTS_FETCH_REFUSED)
        self.assertEqual(self.fake.calls_to('image'), [])
        row = SportsImage.query.filter_by(url=self.badges[BEARS]).one()
        self.assertIsNone(row.next_try_at)
        self.assertIn('https', row.reason)


if __name__ == '__main__':
    unittest.main()
