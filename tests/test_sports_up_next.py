"""The Up next channels on the output feed (app/sports_up_next.py, dev/changelog/1614): ChannelBin's
own picture of the coming games, one channel for everything followed and one per league, each a
page every 10 s drawn in the waiting screen's look. What is served and its switch, what the pages
show, where they sit in the catalog, the clip made on tune-in and pruned, and a play of one through the
relay in every mode, holding no seat and counted against nothing.

The relay tests replace sports_screen.render_clip with a copy of a small synthesized TS file, as
tests/test_sports_waiting.py does; RotationClipTests makes a real one, small, to hold the ffmpeg
fix the multi-page clip needed.

  python3 -m unittest tests.test_sports_up_next
"""
import os
import shutil
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import clips  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel  # noqa: E402

import app.connection_limits as connlim  # noqa: E402
import app.output_feed as feed  # noqa: E402
import app.sports_screen as ss  # noqa: E402
import app.sports_screen_draw as draw  # noqa: E402
from app import db, sports_feed, sports_up_next as un  # noqa: E402
from app.database import (OutputFeedPlay, SportsEvent, SportsFollow, SportsLeague, SportsMatch,  # noqa: E402
                          SportsTeam, SportsUpNextChannel)

_HAVE_FFMPEG = shutil.which('ffmpeg') is not None
USER, PASS = 'tv', 'tv-secret'


def _cfg(**overrides):
    block = {'enabled': True, 'username': USER, 'password': PASS, 'reserve_seconds': 0}
    block.update(overrides)
    return {'integrations': {'output_feed': block}}


class _Leagues:
    """Two followed leagues with games, and helpers to add more."""

    def seed(self):
        self.now = datetime.utcnow()
        self.nhl = self.league(4380, 'NHL')
        self.f1 = self.league(4370, 'Formula 1', sport='Motorsport')
        db.session.add(SportsTeam(tsdb_id=101, full_name='Ironvale Bears', names='[]', color1='#c8102e',
                                  color2='#ffffff'))
        db.session.add(SportsTeam(tsdb_id=102, full_name='Harborview Gulls', names='[]', color1='#0038a8'))
        db.session.commit()

    def league(self, tsdb_id, name, sport='Ice Hockey', follow=True):
        lg = SportsLeague(tsdb_id=tsdb_id, name=name, sport=sport, match_terms='[]')
        db.session.add(lg)
        db.session.flush()
        if follow:
            db.session.add(SportsFollow(league_id=lg.id, team_tsdb_id=0, rule_id=0))
        db.session.commit()
        return lg

    def game(self, league, hours, n, field=False):
        start = self.now + timedelta(hours=hours)
        ev = SportsEvent(tsdb_id=70000 + n, league_id=league.id, name=f'Grand Prix {n}',
                         home_team_id=0 if field else 101, away_team_id=0 if field else 102,
                         home_name=None if field else 'Ironvale Bears',
                         away_name=None if field else 'Harborview Gulls',
                         start_at=start, local_date=start.date())
        db.session.add(ev)
        db.session.commit()
        return ev

    def listed(self, event, name, start=None):
        ch = make_channel(make_account(f'{name} acct', max_connections=1), name=name)
        db.session.flush()
        start = start or event.start_at
        db.session.add(SportsMatch(event_id=event.id, channel_id=ch.id, kind='guide', strength='strong',
                                   airing='first', program_start_time=start,
                                   program_stop_time=start + timedelta(hours=3), title_key='g', reason='r'))
        db.session.commit()
        return ch

    def rotation(self, league_id):
        number = un.set_up_next(league_id, True)
        ch = un.channels(number=number)[0]
        return un.rotations([ch], datetime.utcnow(), {})[number]


class ServedAndSwitchTests(_Leagues, unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.seed()

    def test_off_until_a_person_turns_one_on(self):
        self.assertEqual(un.channels(), [])
        listed = un.channels(everything=True)
        self.assertEqual([c.name for c in listed], ['Up next'])
        self.assertFalse(listed[0].enabled)

    def test_the_one_for_everything_is_offered_only_once_something_is_followed(self):
        SportsFollow.query.delete()
        db.session.commit()
        self.assertEqual(un.channels(everything=True), [])

    def test_a_switch_keeps_its_row_and_feed_id(self):
        number = un.set_up_next(self.nhl.id, True)
        self.assertGreater(number, 0)
        self.assertEqual([c.name for c in un.channels()], ['NHL up next'])
        self.assertEqual(un.set_up_next(self.nhl.id, False), number)
        self.assertEqual(un.channels(), [])
        self.assertEqual(un.set_up_next(self.nhl.id, True), number)
        self.assertEqual(SportsUpNextChannel.query.count(), 1)
        self.assertEqual(un.set_up_next(un.EVERYTHING, True), un.EVERYTHING)
        self.assertEqual([c.number for c in un.channels()], [un.EVERYTHING, number])

    def test_a_league_nothing_follows_cannot_be_turned_on_and_is_left_out_when_unfollowed(self):
        other = self.league(4391, 'NFL', follow=False)
        with self.assertRaises(un.NotFollowed):
            un.set_up_next(other.id, True)
        number = un.set_up_next(self.nhl.id, True)
        SportsFollow.query.filter_by(league_id=self.nhl.id).delete()
        db.session.commit()
        self.assertEqual(un.channels(), [])
        left = [c for c in un.channels(everything=True) if c.number == number][0]
        self.assertTrue(left.enabled)
        self.assertEqual(left.left_out, un.LEFT_OUT_UNFOLLOWED)

    def test_a_league_deleted_takes_its_channel_and_its_id_is_never_issued_again(self):
        from app import sports_data
        gone = un.set_up_next(self.nhl.id, True)
        self.assertTrue(sports_data.delete_league(self.nhl.id))
        db.session.commit()
        self.assertEqual(SportsUpNextChannel.query.filter_by(league_id=self.nhl.id).count(), 0)
        self.assertGreater(un.set_up_next(self.f1.id, True), gone)

    def test_the_route_switches_and_refuses_by_name(self):
        r = self.t.client.put('/api/sports/up-next', json={'league_id': self.nhl.id, 'enabled': True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()['name'], 'NHL up next')
        r = self.t.client.put('/api/sports/up-next', json={'league_id': 0, 'enabled': True})
        self.assertEqual(r.get_json()['name'], 'Up next')
        other = self.league(4391, 'NFL', follow=False)
        r = self.t.client.put('/api/sports/up-next', json={'league_id': other.id, 'enabled': True})
        self.assertEqual(r.status_code, 409)
        self.assertIn('Follow the league', r.get_json()['error'])
        r = self.t.client.put('/api/sports/up-next', json={'league_id': 99999, 'enabled': True})
        self.assertEqual(r.status_code, 404)
        r = self.t.client.put('/api/sports/up-next', json={'league_id': 0, 'enabled': 'yes'})
        self.assertEqual(r.status_code, 400)

    def test_the_iptv_output_row_switch_reaches_an_up_next_channel(self):
        r = self.t.client.post(f'/api/iptv-output/rows/{feed.feed_id_for_up_next(0)}', json={'on': True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(un.channels()[0].everything)
        r = self.t.client.post(f'/api/iptv-output/rows/{feed.feed_id_for_up_next(4242)}', json={'on': True})
        self.assertEqual(r.status_code, 404)


class PagesTests(_Leagues, unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.seed()

    def test_a_league_pages_its_games_four_at_a_time_up_to_three_pages(self):
        for n in range(14):
            self.game(self.nhl, 2 + n, n)
        rot = self.rotation(self.nhl.id)
        self.assertEqual(len(rot.pages), un.LEAGUE_PAGES)
        self.assertEqual([1 + len(p.later) for p in rot.pages], [4, 4, 4])
        self.assertEqual(rot.pages[0].words.kicker, 'Next game')
        self.assertEqual(rot.pages[1].words.kicker, 'Coming up')
        self.assertEqual(rot.pages[0].later_label, 'Later in NHL')
        self.assertEqual(rot.pages[0].channel, 'NHL up next')

    def test_games_past_the_week_and_games_over_are_not_shown(self):
        self.game(self.nhl, 24 * un.UP_NEXT_DAYS + 2, 1)
        self.game(self.nhl, -6, 2)
        rot = self.rotation(self.nhl.id)
        self.assertEqual(rot.pages[0].state, ss.STATE_NONE)
        self.assertEqual(rot.pages[0].words.kicker, 'No games scheduled')

    def test_everything_is_a_page_per_league_soonest_first_and_names_a_quiet_league(self):
        self.game(self.nhl, 30, 1)
        self.game(self.f1, 5, 2, field=True)
        self.league(4391, 'NFL')
        rot = self.rotation(un.EVERYTHING)
        self.assertEqual([p.words.kicker for p in rot.pages], ['Formula 1 - next race', 'NHL - next game'])
        self.assertTrue(rot.pages[0].field)
        self.assertIn('Nothing in the next 7 days: NFL.', rot.pages[0].words.note)
        self.assertIn('Schedule from TheSportsDB', rot.pages[1].words.note)

    def test_everything_with_nothing_scheduled_says_so(self):
        rot = self.rotation(un.EVERYTHING)
        self.assertEqual(len(rot.pages), 1)
        self.assertEqual(rot.pages[0].state, ss.STATE_NONE)
        self.assertIn('None of the leagues you follow', rot.pages[0].words.note)

    def test_a_game_on_now_is_said_in_the_live_color(self):
        self.game(self.nhl, -1, 1)
        self.game(self.nhl, -0.5, 2)
        rot = self.rotation(self.nhl.id)
        page = rot.pages[0]
        self.assertEqual(page.words.kicker, 'On now')
        self.assertTrue(page.words.live)
        self.assertTrue(page.words.when.startswith('Started '))
        self.assertTrue(page.later[0].live)
        self.assertEqual(page.later[0].day, 'On now')

    def test_where_each_game_is_on(self):
        mine = self.game(self.nhl, 3, 1)
        listed = self.game(self.nhl, 4, 2)
        nowhere = self.game(self.nhl, 5, 3)
        self.nhl.feed_league_channels = True
        db.session.commit()
        sports_feed.place_league_events(self.nhl, datetime.utcnow())
        db.session.commit()
        listed.feed_number = None
        nowhere.feed_number = None
        db.session.commit()
        self.listed(listed, 'TSN 4')
        self.listed(listed, 'TSN 5')
        rot = self.rotation(self.nhl.id)
        hero, later = rot.pages[0].game, rot.pages[0].later
        self.assertEqual(mine.feed_number, 1)
        self.assertEqual((hero.where, hero.where_short), ('On NHL 1', 'NHL 1'))
        self.assertEqual((later[0].where, later[0].where_short), ('On TSN 4 and 1 more channel', 'TSN 4 +1'))
        self.assertEqual((later[1].where, later[1].where_short), (un.NOT_IN_GUIDE, un.NOT_IN_GUIDE_SHORT))

    def test_a_game_only_a_channels_name_gives_is_on_that_channel(self):
        """The channel Record would take for a game no guide lists (dev/changelog/1643)."""
        from app import sports_match
        ev = self.game(self.nhl, 3, 1)
        ch = make_channel(make_account('Events', max_connections=1), name='NHL 01: Gulls @ Bears 7:00 PM ET')
        db.session.add(SportsMatch(event_id=ev.id, channel_id=ch.id, kind='channel_name', strength='strong',
                                   airing='unknown', program_start_time=ev.start_at, title_key='n',
                                   reason=f'{sports_match._NAME_PREFIX}: matched.'))
        db.session.commit()
        hero = self.rotation(self.nhl.id).pages[0].game
        self.assertEqual((hero.where, hero.where_short),
                         ('On NHL 01: Gulls @ Bears 7:00 PM ET', 'NHL 01: Gulls @ Bears 7:00 PM ET'))

    def test_the_pages_are_a_fixed_number_of_queries_whatever_is_drawn(self):
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        def count(fn):
            # On the Engine class: outside a request a query runs on the background pool's engine.
            n = [0]

            def tick(*_a, **_k):
                n[0] += 1
            event.listen(Engine, 'before_cursor_execute', tick)
            try:
                fn()
            finally:
                event.remove(Engine, 'before_cursor_execute', tick)
            return n[0]

        cfg = {'sports_data': {'download_images': True}}
        un.set_up_next(un.EVERYTHING, True)
        un.set_up_next(self.nhl.id, True)
        for team in SportsTeam.query.all():
            team.badge_url = f'https://www.thesportsdb.com/images/{team.tsdb_id}.png'
        db.session.commit()
        self.listed(self.game(self.nhl, 3, 1), 'Listing')
        small = count(lambda: un.rotations(un.channels(), datetime.utcnow(), cfg))
        for n in range(12):
            ev = self.game(self.nhl if n % 2 else self.f1, 4 + n, 10 + n, field=not n % 2)
            if n % 2:
                ev.home_team_id, ev.away_team_id = 300 + n, 400 + n
                db.session.add(SportsTeam(tsdb_id=300 + n, full_name=f'Home {n}', names='[]',
                                          badge_url=f'https://www.thesportsdb.com/images/h{n}.png'))
                db.session.add(SportsTeam(tsdb_id=400 + n, full_name=f'Away {n}', names='[]',
                                          badge_url=f'https://www.thesportsdb.com/images/a{n}.png'))
                db.session.commit()
            self.listed(ev, f'Listing {n}')
        for k in range(4):
            self.league(5000 + k, f'Extra {k}')
        large = count(lambda: un.rotations(un.channels(), datetime.utcnow(), cfg))
        self.assertGreater(small, 5)
        self.assertEqual(small, large)


class CatalogTests(_Leagues, unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.t.sandbox_config(_cfg())
        self.seed()

    def test_feed_ids_decode_to_their_kind(self):
        self.assertEqual(feed.decode_feed_id(feed.feed_id_for_up_next(0)), (feed.KIND_UP_NEXT, 0))
        self.assertEqual(feed.decode_feed_id(feed.feed_id_for_up_next(7)), (feed.KIND_UP_NEXT, 7))
        self.assertEqual(feed.decode_feed_id(feed.feed_id_for_sports(7)), (feed.KIND_SPORTS, 7))
        self.assertLess(feed.feed_id_for_up_next(10_000_000), 2 ** 31)

    def test_up_next_goes_through_channelbin_in_every_mode(self):
        for mode in feed.MODES:
            self.assertTrue(feed.relays({'mode': mode}, feed.feed_id_for_up_next(0)), mode)

    def test_up_next_sits_with_the_sports_channels_ahead_of_them(self):
        un.set_up_next(un.EVERYTHING, True)
        nhl = un.set_up_next(self.nhl.id, True)
        sports_feed.set_league_channels(self.nhl.id, True)
        self.game(self.nhl, 3, 1)
        sports_feed.place_league_events(self.nhl, datetime.utcnow())
        db.session.commit()
        rows = feed.catalog()
        own = [(r.kind, r.name, r.category) for r in rows]
        self.assertEqual(own[0], (feed.KIND_UP_NEXT, 'Up next', feed.UP_NEXT_CATEGORY))
        self.assertEqual(own[1], (feed.KIND_UP_NEXT, 'NHL up next', 'NHL'))
        self.assertEqual(own[2][0], feed.KIND_SPORTS)
        self.assertEqual(rows[1].feed_id, feed.feed_id_for_up_next(nhl))
        self.assertEqual([r.num for r in rows], list(range(1, len(rows) + 1)))

    def test_channelbins_own_channels_carry_channelbins_logo(self):
        """dev/docs/BUGS.md 2026-11-17 02:38 PM, IPTV output listed no logos: the Up next and sports
        channels listed none. They list ChannelBin's own picture, a static file an IPTV player
        can fetch with no login."""
        un.set_up_next(un.EVERYTHING, True)
        sports_feed.set_league_channels(self.nhl.id, True)
        self.game(self.nhl, 3, 1)
        sports_feed.place_league_events(self.nhl, datetime.utcnow())
        db.session.commit()
        rows = feed.catalog()
        self.assertEqual({r.kind for r in rows}, {feed.KIND_UP_NEXT, feed.KIND_SPORTS})
        fcfg = {'username': 'u', 'password': 'p'}
        for row in rows:
            self.assertEqual(feed.logo_url(row, 'http://box:5000', fcfg),
                             f'http://box:5000{feed.OWN_LOGO_PATH}', row.name)
        resp = self.t.client.get(feed.OWN_LOGO_PATH)
        self.assertEqual((resp.status_code, resp.mimetype), (200, 'image/png'))
        resp.close()

    def test_an_off_one_is_not_served_and_the_page_view_keeps_it(self):
        rows = feed.catalog()
        self.assertEqual([r for r in rows if r.kind == feed.KIND_UP_NEXT], [])
        everything = [r for r in feed.catalog(everything=True) if r.kind == feed.KIND_UP_NEXT]
        self.assertEqual([(r.name, r.offered, r.num) for r in everything], [('Up next', False, 0)])
        self.assertIsNone(feed.resolve_play(feed.feed_id_for_up_next(0)))

    def test_its_guide_says_what_it_is(self):
        un.set_up_next(un.EVERYTHING, True)
        xml = ''.join(feed.xmltv_lines(feed.catalog(), {'integrations': {'output_feed': {'xmltv_days': 1}}}))
        fid = feed.feed_id_for_up_next(0)
        self.assertIn(f'<channel id="{fid}"><display-name>Up next</display-name>', xml)
        self.assertGreaterEqual(xml.count(f'channel="{fid}"><title>Up next</title>'), 12)

    def test_the_pages_show_the_switch_and_the_row(self):
        un.set_up_next(self.nhl.id, True)
        html = self.t.client.get(f'/sports/{self.nhl.id}').get_data(as_text=True)
        self.assertIn('data-act="up-next"', html)
        self.assertIn('NHL up next', html)
        html = self.t.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn(f'data-feed="{feed.feed_id_for_up_next(0)}"', html)
        self.assertIn('Up next channel - every league you follow', html)


class ClipCacheTests(_Leagues, unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.t.sandbox_config(_cfg())
        self.seed()
        self.renders = []
        patcher = mock.patch.object(ss, 'render_clip', self._fake_render)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_render(self, cfg, screen, path, *, still=False, background=False):
        self.renders.append((path, still, background))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(b'G' * 188)

    def _cfg(self):
        from app.config import load_config
        return load_config()

    def test_the_prune_keeps_up_next_clips_of_rows_that_exist(self):
        cfg = self._cfg()
        number = un.set_up_next(self.nhl.id, True)
        folder = ss.clip_dir(cfg)
        os.makedirs(folder, exist_ok=True)
        keep = [ss.clip_path(cfg, 'u0', 'a' * 20), ss.clip_path(cfg, ss.up_next_owner(number), 'b' * 20)]
        gone = ss.clip_path(cfg, 'u9999', 'c' * 20)
        for path in keep + [gone]:
            open(path, 'wb').close()
        ss.prune_clips(cfg)
        self.assertTrue(all(os.path.exists(p) for p in keep))
        self.assertFalse(os.path.exists(gone))

    def test_an_owner_is_a_row_id_or_an_up_next_number_and_nothing_else(self):
        cfg = self._cfg()
        self.assertTrue(os.path.basename(ss.clip_path(cfg, 'u3', 'a' * 20)).startswith('u3-'))
        self.assertTrue(os.path.basename(ss.clip_path(cfg, 3, 'a' * 20)).startswith('3-'))
        with self.assertRaises(ValueError):
            ss.clip_path(cfg, 'u0/../x', 'a' * 20)


class DrawTests(unittest.TestCase):

    def test_a_short_rotation_is_repeated_to_a_full_loop(self):
        page = object()
        self.assertEqual(len(draw.rotation_pages([page])) * draw.PAGE_FRAMES, 3 * draw.PAGE_FRAMES)
        self.assertGreaterEqual(len(draw.rotation_pages([page])) * draw.PAGE_FRAMES, draw.PLAIN_LOOP_FRAMES)
        self.assertEqual(len(draw.rotation_pages([page] * 5)), 5)

    def test_a_page_says_where_its_games_are_on(self):
        side = ss.Side('Harborview Gulls', 'HAR', ('#0038a8', '#ffffff'))
        home = ss.Side('Ironvale Bears', 'IRO', ('#c8102e', '#ffffff'))
        game = ss.Game('Harborview Gulls at Ironvale Bears', 'Today', '7:00 PM', 'Arena', side, home,
                       'On NHL 1', 'NHL 1')
        words = ss.Words('Next game', game.name, 'Today 7:00 PM', 'Schedule from TheSportsDB.')
        plain = ss.Screen('NHL up next', '#c8102e', ss.STATE_NEXT, ss.MODE_PROXY, words, game, (game,))
        bare = ss.Game(game.name, 'Today', '7:00 PM', 'Arena', side, home)
        without = ss.Screen('NHL up next', '#c8102e', ss.STATE_NEXT, ss.MODE_PROXY, words, bare, (bare,))
        self.assertNotEqual(draw.still(plain)[0].tobytes(), draw.still(without)[0].tobytes())
        self.assertNotEqual(ss.clip_key(ss.Rotation('x', (plain,))), ss.clip_key(ss.Rotation('x', (without,))))


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class RotationClipTests(unittest.TestCase):
    """dev/changelog/1614: concat's output was numbered in its own time base, so the encode's -r
    filled the whole clip with the last page. Each page of the made clip must be its own."""

    def test_each_page_of_the_clip_is_its_own_picture(self):
        words = ss.Words('Next race', 'x', 'Today', 'n')
        pages = tuple(ss.Screen('Up next', color, ss.STATE_NONE, ss.MODE_PROXY, words)
                      for color in ('#ff0000', '#0000ff'))
        out = os.path.join(make_tmp(self), 'r.ts')
        with mock.patch.object(draw, 'PAGE_FRAMES', 16), mock.patch.object(draw, 'PLAIN_LOOP_FRAMES', 32):
            ss.render_clip({'ffmpeg': {'path': 'ffmpeg'}}, ss.Rotation('Up next', pages), out)
        means = []
        for n in (8, 24):
            raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', out, '-vf', f'select=eq(n\\,{n}),scale=4:4',
                                  '-fps_mode', 'passthrough', '-frames:v', '1', '-f', 'rawvideo',
                                  '-pix_fmt', 'rgb24', '-'], capture_output=True, check=True).stdout
            means.append((sum(raw[0::3]), sum(raw[2::3])))
        (r1, b1), (r2, b2) = means
        self.assertGreater(r1, b1, 'the first page is the red one')
        self.assertGreater(b2, r2, 'the second page is the blue one')


def make_tmp(test):
    import tempfile
    d = tempfile.mkdtemp(prefix='upnext-')
    test.addCleanup(shutil.rmtree, d, True)
    return d


def _build(d):
    subprocess.run(
        ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=size=192x108:rate=30',
         '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '20',
         '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '30', '-bf', '0', '-pix_fmt', 'yuv420p',
         '-c:a', 'aac', '-f', 'mpegts', os.path.join(d, 'screen.ts')],
        check=True, timeout=180)


def _wait(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg not available')
class PlayTests(_Leagues, unittest.TestCase):
    """A play of an Up next channel through the relay."""
    FEED = _cfg(max_viewers=1)

    @classmethod
    def setUpClass(cls):
        cls.media = clips.shared('sports-up-next', _build)

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.client
        self.addCleanup(self.t.cleanup)
        self.addCleanup(feed.stop_all)
        self.t.sandbox_config(self.FEED)
        self.seed()
        self.game(self.nhl, 3, 1)
        self.renders = []
        patcher = mock.patch.object(ss, 'render_clip', self._fake_render)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.open = []
        self.addCleanup(self._close_all)
        un.set_up_next(un.EVERYTHING, True)
        self.feed_id = feed.feed_id_for_up_next(0)

    def _fake_render(self, cfg, screen, path, *, still=False, background=False):
        self.renders.append((path, still, screen))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(os.path.join(self.media, 'screen.ts'), path)

    def _close_all(self):
        for resp in self.open:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - a response the test already closed
                pass

    def play(self, expect=200):
        resp = self.client.get(f'/live/{USER}/{PASS}/{self.feed_id}.ts')
        self.assertEqual(resp.status_code, expect, resp.get_data(as_text=True)[:300] if expect != 200 else '')
        if expect == 200:
            self.open.append(resp)
        return resp

    def only_session(self):
        live = feed.live_sessions()
        self.assertEqual(len(live), 1, live)
        return live[0]

    def test_it_plays_its_picture_with_no_seat_and_is_not_counted(self):
        resp = self.play()
        self.assertEqual(next(iter(resp.response))[0], 0x47)
        s = self.only_session()
        self.assertEqual(s.state, feed.STATE_SHOWING)
        self.assertTrue(s.own_picture)
        self.assertFalse(s.waiting)
        self.assertFalse(s.counted)
        self.assertIsNone(s.account_id)
        self.assertTrue(all(not holders for holders in connlim._holders.values()))
        self.assertEqual(self.renders[0][1], True, 'a tune-in with no clip made starts on the quick still')
        self.assertTrue(_wait(lambda: not s.screen_still), 'the moving clip is swapped in when made')

    def test_redirect_mode_streams_it_too(self):
        self.t.sandbox_config(_cfg(mode=feed.MODE_REDIRECT))
        resp = self.play()
        self.assertEqual(resp.mimetype, 'video/mp2t')
        self.assertEqual(self.only_session().state, feed.STATE_SHOWING)
        html = self.client.get('/iptv-output').get_data(as_text=True)
        self.assertIn('The Up next picture', html)

    def test_turning_it_off_ends_the_play_by_name_and_says_so(self):
        self.play()
        s = self.only_session()
        r = self.client.put('/api/sports/up-next', json={'league_id': 0, 'enabled': False})
        self.assertEqual(r.get_json()['cut'], 1)
        self.assertEqual(s.reason, feed.REASON_TAKEN_OFF)
        self.assertIsNotNone(s.proc.poll(), 'its ffmpeg is gone')
        self.assertTrue(feed.flush_play_log())
        line = OutputFeedPlay.query.order_by(OutputFeedPlay.id.desc()).first()
        self.assertEqual(line.channel_name, feed.PICTURE_NAME)

    def test_a_changed_picture_is_swapped_in_at_the_re_check(self):
        self.play()
        s = self.only_session()
        self.assertTrue(_wait(lambda: not s.screen_still))
        key, old = s.screen_key, s.screen_path
        self.game(self.nhl, 2, 2)
        s.recheck_due = True
        self.assertTrue(_wait(lambda: s.screen_key != key and s.proc is not None), 'swapped in place')
        self.assertTrue(s.live)
        self.assertTrue(_wait(lambda: not s.screen_still))
        self.assertFalse(os.path.exists(old), 'the picture it left is deleted: no job clears the folder')
        self.assertEqual([r[1] for r in self.renders], [True, False, True, False],
                         'each picture is made on the play, its quick still first')

    def test_unfollowing_its_league_cuts_a_league_channel_play_at_once(self):
        self.feed_id = feed.feed_id_for_up_next(un.set_up_next(self.nhl.id, True))
        self.play()
        s = self.only_session()
        r = self.client.delete(f'/api/sports/leagues/{self.nhl.id}/following')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()['cut'], 1)
        self.assertFalse(s.live)
        self.assertEqual(s.reason, feed.REASON_TAKEN_OFF)

    def test_no_longer_served_ends_at_the_re_check(self):
        self.play()
        s = self.only_session()
        SportsUpNextChannel.query.filter_by(league_id=0).update({'enabled': False})
        db.session.commit()
        s.recheck_due = True
        self.assertTrue(_wait(lambda: not s.live))
        self.assertEqual(s.reason, feed.REASON_TAKEN_OFF)


if __name__ == '__main__':
    unittest.main()
