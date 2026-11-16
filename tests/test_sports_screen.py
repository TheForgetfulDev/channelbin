"""A sports channel's waiting screen: its words, its picture, its clip and the clip cache
(app/sports_screen.py, app/sports_screen_draw.py, proc_utils.build_screen_clip_cmd),
dev/changelog/1533. The words and geometry are dev/mockups/55's look 7.

Real ffmpeg over local pictures only, gated on ffmpeg being on PATH; throwaway temp SQLite DB and
images folder - never the live dvr.db.
  python3 -m unittest tests.test_sports_screen
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, sports_feed, sports_screen as ss, sports_screen_draw as draw  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import SportsEvent, SportsFollow, SportsLeague, SportsTeam  # noqa: E402
from app.sports_feed import NowPlaying  # noqa: E402
from app.sports_screen import Game, Screen, Side, Words  # noqa: E402
from app.storage_dirs import SPORTS_SCREENS, configured_write_dirs, image_dir  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.images import png_bytes  # noqa: E402

NY = ZoneInfo('America/New_York')
# 17:00 UTC is noon in New York on 2099-03-02.
NOW = datetime(2099, 3, 2, 17, 0)
HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def every_word(words: Words) -> str:
    return ' '.join([words.kicker, words.headline, words.when, words.note, words.ticker_label, *words.ticker])


class WordsTests(unittest.TestCase):
    """One builder says what every state says, in both modes (mockup 55's copyFor())."""

    def test_next_game_in_proxy_mode(self):
        w = ss.words_for(ss.STATE_NEXT, ss.MODE_PROXY, 'game', game='Flyers at Rangers', when='Today 7:00 PM')
        self.assertEqual(w.kicker, 'No game on right now - next game')
        self.assertEqual((w.headline, w.when), ('Flyers at Rangers', 'Today 7:00 PM'))
        self.assertEqual(w.note, 'It starts here by itself at game time. You can leave this on.')
        self.assertEqual((w.ticker_label, w.ticker), ('', ()))

    def test_next_game_in_redirect_mode_says_when_the_stream_stops(self):
        w = ss.words_for(ss.STATE_NEXT, ss.MODE_REDIRECT, 'game', game='Flyers at Rangers', when='Today 7:00 PM',
                         stops=('6:00 PM', '6:00 PM'), before_minutes=60)
        self.assertEqual(w.ticker_label, 'Stops by 6:00 PM')
        self.assertEqual(w.ticker, (
            "You're using redirect mode, so ChannelBin can't tune you in to the game when it starts",
            'This stream stops by 6:00 PM, an hour before the game',
            'Then tune in to this channel again to watch Flyers at Rangers'))

    def test_a_zero_window_stops_when_the_game_starts(self):
        w = ss.words_for(ss.STATE_NEXT, ss.MODE_REDIRECT, 'game', game='A at B', when='Today 7:00 PM',
                         stops=('7:00 PM', '7:00 PM'), before_minutes=0)
        self.assertEqual(w.ticker[1], 'This stream stops by 7:00 PM, when the game starts')

    def test_unlisted_game(self):
        proxy = ss.words_for(ss.STATE_UNLISTED, ss.MODE_PROXY, 'game', game='A at B', clock='7:00 PM')
        self.assertEqual((proxy.kicker, proxy.when, proxy.live),
                         ('On now - no stream found yet', 'Started 7:00 PM', True))
        self.assertTrue(proxy.note.endswith('It starts here by itself the moment one does.'))
        soon = ss.words_for(ss.STATE_UNLISTED, ss.MODE_PROXY, 'game', game='A at B', clock='7:00 PM', started=False)
        self.assertEqual((soon.kicker, soon.when), ('Starting soon - no stream found yet', 'Starts 7:00 PM'))
        redirect = ss.words_for(ss.STATE_UNLISTED, ss.MODE_REDIRECT, 'game', game='A at B', clock='7:00 PM')
        self.assertEqual(redirect.ticker_label, 'This stream will stop')
        self.assertIn("This stream stops as soon as one of your channels' guides lists A at B", redirect.ticker)

    def test_nothing_scheduled(self):
        w = ss.words_for(ss.STATE_NONE, ss.MODE_PROXY, 'game', carries='Every Philadelphia Flyers game')
        self.assertEqual((w.kicker, w.headline, w.when), ('No games scheduled', 'Every Philadelphia Flyers game', ''))
        self.assertTrue(w.note.startswith('TheSportsDB lists no upcoming games for this channel.'))
        r = ss.words_for(ss.STATE_NONE, ss.MODE_REDIRECT, 'game', carries='x')
        self.assertEqual(r.ticker_label, 'Redirect mode')

    def test_held_says_which_thing_holds_it(self):
        one = ss.words_for(ss.STATE_HELD, ss.MODE_PROXY, 'game', game='A at B', clock='7:00 PM',
                           held=ss.HELD_NO_SLOT, slots=1)
        self.assertEqual(one.kicker, "On now - can't start here yet")
        self.assertIn("using the IPTV output's one slot", one.note)
        three = ss.words_for(ss.STATE_HELD, ss.MODE_PROXY, 'game', game='A at B', clock='7:00 PM',
                             held=ss.HELD_NO_SLOT, slots=3)
        self.assertIn("All 3 of the IPTV output's slots are in use", three.note)
        seat = ss.words_for(ss.STATE_HELD, ss.MODE_PROXY, 'game', game='A at B', clock='7:00 PM',
                            held=ss.HELD_NO_CONNECTION)
        self.assertIn('using all of its connections', seat.note)
        with self.assertRaises(ValueError):
            ss.words_for(ss.STATE_HELD, ss.MODE_REDIRECT, 'game', game='A at B', held=ss.HELD_NO_SLOT)
        with self.assertRaises(ValueError):
            ss.words_for(ss.STATE_HELD, ss.MODE_PROXY, 'game', game='A at B', held='bored')

    def test_a_race_is_never_a_game(self):
        for state in ss.STATES:
            for mode in (ss.MODE_PROXY, ss.MODE_REDIRECT):
                if state == ss.STATE_HELD and mode == ss.MODE_REDIRECT:
                    continue
                w = ss.words_for(state, mode, 'race', game='Las Vegas Grand Prix', when='Sun Nov 22 1:00 AM',
                                 clock='1:00 AM', stops=('12:00 AM', '12:00 AM'), carries='Formula 1 races',
                                 held=ss.HELD_NO_SLOT)
                self.assertNotIn('game', every_word(w).replace('Las Vegas Grand Prix', '').lower(), (state, mode))

    def test_an_unknown_state_or_mode_is_refused(self):
        with self.assertRaises(ValueError):
            ss.words_for('waiting', ss.MODE_PROXY, 'game')
        with self.assertRaises(ValueError):
            ss.words_for(ss.STATE_NEXT, 'tunnel', 'game')

    def test_before_words(self):
        self.assertEqual([ss.before_words(m) for m in (60, 120, 90, 1, 15)],
                         ['an hour', '2 hours', '90 minutes', 'a minute', '15 minutes'])

    def test_what_a_channel_carries(self):
        self.assertEqual(ss.carries('Philadelphia Flyers', 'NHL', 0, 'game'), 'Every Philadelphia Flyers game')
        self.assertEqual(ss.carries(None, 'Formula 1', 1, 'race'), 'Formula 1 races')
        self.assertEqual(ss.carries(None, 'NFL', 2, 'game'), 'The NFL games that overlap another')
        self.assertEqual(ss.carries(None, 'English Premier League', 3, 'game'),
                         'The English Premier League games that overlap two others')


class StopByTests(unittest.TestCase):
    """Redirect mode's stop time: the game's start less the window's `before`, with its day
    words (mockup 55's windowOpens())."""

    def stop(self, start_local, before=60):
        start = start_local.replace(tzinfo=NY).astimezone(ZoneInfo('UTC')).replace(tzinfo=None)
        return ss.stop_by(start, before, NOW, NY, False)

    def test_today(self):
        self.assertEqual(self.stop(datetime(2099, 3, 2, 19, 0)), ('6:00 PM', '6:00 PM'))

    def test_tomorrow(self):
        self.assertEqual(self.stop(datetime(2099, 3, 3, 13, 0)), ('12:00 PM tomorrow', '12:00 PM tomorrow'))

    def test_a_later_day_names_it(self):
        self.assertEqual(self.stop(datetime(2099, 3, 8, 13, 0), 90),
                         ('11:30 AM on Sun Mar 8', 'Sun Mar 8 11:30 AM'))

    def test_the_window_can_open_the_day_before_the_game(self):
        self.assertEqual(self.stop(datetime(2099, 3, 3, 0, 30)), ('11:30 PM', '11:30 PM'))


def a_screen(state=ss.STATE_NEXT, mode=ss.MODE_PROXY, *, field=False, words=None, badge=None, later=2):
    if field:
        game = Game('Las Vegas Grand Prix', 'Sun Nov 22', '1:00 AM', 'Las Vegas Strip Circuit')
        rest = [Game(f'Race {i}', 'Sun Nov 29', '11:00 AM', 'Somewhere') for i in range(later)]
    else:
        sides = ss.side_colors(['#f74902', '#000000'], ['#0038a8', '#ce1126'])
        game = Game('Philadelphia Flyers at New York Rangers', 'Today', '7:00 PM', 'Madison Square Garden, New York',
                    Side('Philadelphia Flyers', 'PHI', sides.away), Side('New York Rangers', 'NYR', sides.home, badge))
        rest = [Game(f'Team {i} at Team {i + 1}', 'Sun Nov 15', '1:00 PM', '',
                     Side(f'Team {i}', 'TM', ss.NEUTRAL_AWAY), Side(f'Team {i + 1}', 'TN', ss.NEUTRAL_HOME))
                for i in range(later)]
    words = words or ss.words_for(state, mode, 'race' if field else 'game', game=game.name, when='Today 7:00 PM',
                                  clock='7:00 PM', stops=('6:00 PM', '6:00 PM'), held=ss.HELD_NO_SLOT)
    return Screen('Philadelphia Flyers', '#f74902', state, mode, words, game, tuple(rest), field)


class TextFitTests(unittest.TestCase):
    """Text is measured: a team name wraps to two lines then shrinks; a stripe's name shrinks
    then ellipsizes."""

    def test_a_short_name_stays_on_one_line_at_full_size(self):
        f, lines = draw.fit_lines('Philadelphia Flyers', 800, 54, 562, 2, 34)
        self.assertEqual((f.size, lines), (54, ['Philadelphia Flyers']))

    def test_a_long_name_wraps_before_it_shrinks(self):
        f, lines = draw.fit_lines('Wolverhampton Wanderers', 800, 54, 562, 2, 34)
        self.assertEqual((f.size, lines), (54, ['Wolverhampton', 'Wanderers']))

    def test_a_longer_one_shrinks_and_keeps_two_lines(self):
        f, lines = draw.fit_lines('Some Very Long Named Football Club of the North', 800, 54, 562, 2, 34)
        self.assertLess(f.size, 54)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(f.getlength(x) <= 562 for x in lines))

    def test_a_stripe_name_ellipsizes_at_its_smallest(self):
        name = 'Wolverhampton Wanderers at Brighton and Hove Albion and then some more words'
        f, text = draw.fit_line(name, 600, 36, 840, 28)
        self.assertEqual(f.size, 28)
        self.assertTrue(text.endswith('...'))
        self.assertLessEqual(f.getlength(text), 840)


class StillTests(unittest.TestCase):
    """The still is look 7's 1920x1080 picture, with the ticker's bar only in redirect mode."""

    def test_every_state_draws(self):
        for state in (ss.STATE_NEXT, ss.STATE_UNLISTED, ss.STATE_HELD):
            img, crawl = draw.still(a_screen(state))
            self.assertEqual(img.size, (1920, 1080))
            self.assertIsNone(crawl)
        none = Screen('NFL 2', ss.ACCENT, ss.STATE_NONE, ss.MODE_PROXY,
                      ss.words_for(ss.STATE_NONE, ss.MODE_PROXY, 'game', carries='The NFL games that overlap another'))
        self.assertEqual(draw.still(none)[0].size, (1920, 1080))

    def test_redirect_mode_draws_the_amber_label_and_leaves_room_for_the_crawl(self):
        img, crawl = draw.still(a_screen(mode=ss.MODE_REDIRECT))
        self.assertGreater(crawl, 300)
        self.assertEqual(img.getpixel((60, 990)), draw.AMBER)
        self.assertEqual(img.getpixel((crawl + 20, 990)), draw.TICKER_BG)

    def test_the_hero_splits_into_the_two_sides_colors(self):
        img, _ = draw.still(a_screen())
        left, right = img.getpixel((20, 20)), img.getpixel((1900, 20))
        self.assertGreater(left[0], left[2])     # Flyers orange
        self.assertGreater(right[2], right[0])   # Rangers blue

    def test_a_downloaded_badge_is_drawn_in_place_of_the_code(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, 'b.png')
        with open(path, 'wb') as f:
            f.write(png_bytes(64, 64))
        with_badge, _ = draw.still(a_screen(badge=path))
        without, _ = draw.still(a_screen())
        # The home disc's middle: white behind a badge, the team's own color behind its code.
        self.assertNotEqual(with_badge.getpixel((1102, 290)), without.getpixel((1102, 290)))

    def test_a_badge_that_cannot_be_decoded_falls_back_to_the_code(self):
        """dev/docs/BUGS.md 2026-11-15 01:36 PM: a badge is checked by its header only when downloaded, and
        one whose body was cut short cost the channel its whole waiting screen."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, 'cut.png')
        with open(path, 'wb') as f:
            f.write(png_bytes(64, 64)[:60])
        with self.assertLogs('app.sports_screen_draw', level='WARNING') as logs:
            with_bad, _ = draw.still(a_screen(badge=path))
        without, _ = draw.still(a_screen())
        self.assertEqual(with_bad.getpixel((1102, 290)), without.getpixel((1102, 290)))
        self.assertIn('cannot be read as a picture', logs.output[0])

    def _bad_badge_matches_no_badge(self, data):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, 'big.png')
        with open(path, 'wb') as f:
            f.write(data)
        with self.assertLogs('app.sports_screen_draw', level='WARNING'):
            with_bad, _ = draw.still(a_screen(badge=path))
        without, _ = draw.still(a_screen())
        self.assertEqual(with_bad.getpixel((1102, 290)), without.getpixel((1102, 290)))

    def test_a_badge_larger_than_one_is_ever_drawn_from_is_never_decoded(self):
        """dev/docs/BUGS.md 2026-11-15 01:36 PM: a small PNG can declare a picture that decodes to
        hundreds of MB, so one past BADGE_MAX_PIXELS is refused before it is decoded."""
        with patch.object(draw, 'BADGE_MAX_PIXELS', 32 * 32):
            self._bad_badge_matches_no_badge(png_bytes(64, 64))

    def test_a_badge_past_pillows_own_limit_falls_back_to_the_code(self):
        """dev/docs/BUGS.md 2026-11-15 01:36 PM: past Pillow's limit the error raised is not an
        OSError, so it went through the screen's own handling and the stream route answered 500."""
        from PIL import Image
        with patch.object(draw, '_badge_image', side_effect=Image.DecompressionBombError('too large')):
            self._bad_badge_matches_no_badge(png_bytes(64, 64))


class LoopTests(unittest.TestCase):
    """The loop's length and motion are whole cycles of a whole number of frames, so a seam
    never jumps."""

    def test_no_ticker_is_the_plain_loop(self):
        self.assertEqual(draw.loop_frames(a_screen()), draw.PLAIN_LOOP_FRAMES)
        self.assertEqual(draw.PLAIN_LOOP_FRAMES % draw.LOOP_UNIT, 0)

    def test_a_ticker_loop_is_one_pass_of_its_text_at_eight_pixels_a_frame(self):
        screen = a_screen(mode=ss.MODE_REDIRECT)
        frames = draw.loop_frames(screen)
        width = draw.font(600, draw.CRAWL_SIZE).getlength(draw.crawl_pass(screen))
        self.assertEqual(frames % draw.LOOP_UNIT, 0)
        self.assertGreaterEqual(frames * draw.CRAWL_PX, width)
        self.assertLess(frames * draw.CRAWL_PX - width, draw.CRAWL_PX * draw.LOOP_UNIT)

    def test_the_drift_returns_to_where_it_started(self):
        def at(axis, n, period=912.0):
            expr = draw.drift_expr(axis, period)
            mem = {}

            def st(i, v):
                mem[i] = v
                return v
            env = {'n': n, 'mod': lambda a, b: a % b, 'floor': __import__('math').floor, 'eq': lambda a, b: a == b,
                   'ld': lambda i: mem[i], 'st': st, 'if': None}
            # ffmpeg's if(c,a,b) as a Python conditional; ';' sequences.
            py = expr.replace('if(', 'iff(')
            env['iff'] = lambda c, a, b: a if c else b
            out = None
            for part in py.split(';'):
                out = eval(part, {}, env)  # noqa: S307 - our own expression
            return out
        self.assertEqual((at(0, 0), at(1, 0)), (draw.MX, draw.MY))
        self.assertEqual((at(0, 912), at(1, 912)), (draw.MX, draw.MY))
        moved = {(at(0, n), at(1, n)) for n in range(0, 912, 19)}
        xs = {x for x, _ in moved}
        self.assertLessEqual(max(xs), draw.MX + 14)
        self.assertGreaterEqual(min(xs), draw.MX - 14)
        self.assertGreater(len(moved), 10)
        self.assertTrue(all(0 <= x <= 2 * draw.MX and 0 <= y <= 2 * draw.MY for x, y in moved))


class ClipKeyTests(unittest.TestCase):
    """A clip's key changes with what is drawn and not without, so a stale clip is never served."""

    def test_the_same_screen_is_the_same_key(self):
        self.assertEqual(ss.clip_key(a_screen()), ss.clip_key(a_screen()))

    def test_the_words_change_the_key(self):
        other = a_screen(words=ss.words_for(ss.STATE_NEXT, ss.MODE_PROXY, 'game', game='x', when='Tomorrow 7:00 PM'))
        self.assertNotEqual(ss.clip_key(a_screen()), ss.clip_key(other))

    def test_the_mode_and_the_colors_change_the_key(self):
        self.assertNotEqual(ss.clip_key(a_screen()), ss.clip_key(a_screen(mode=ss.MODE_REDIRECT)))
        s = a_screen()
        recolored = Screen(s.channel, '#123456', s.state, s.mode, s.words, s.game, s.later, s.field)
        self.assertNotEqual(ss.clip_key(s), ss.clip_key(recolored))

    def test_a_new_badge_file_changes_the_key(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, 'b.png')
        with open(path, 'wb') as f:
            f.write(png_bytes(64, 64))
        before = ss.clip_key(a_screen(badge=path))
        with open(path, 'wb') as f:
            f.write(png_bytes(80, 80))
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertNotEqual(before, ss.clip_key(a_screen(badge=path)))


@unittest.skipUnless(HAVE_FFMPEG, 'needs ffmpeg and ffprobe on PATH')
class ClipTests(unittest.TestCase):
    """A made clip is H.264 and AAC at 1920x1080, as long as computed, and loops with -c copy:
    video DTS never goes backwards and the audio has no gap across a seam."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cfg = {'ffmpeg': {'path': 'ffmpeg'}}

    def probe(self, path, *args):
        return json.loads(subprocess.run(['ffprobe', '-v', 'error', *args, '-of', 'json', path],
                                         capture_output=True, text=True, check=True).stdout)

    def test_a_short_ticker_clip_with_every_motion(self):
        words = Words('k', 'h', 'w', 'n', ticker_label='Stops by 6:00 PM', ticker=('Short',))
        screen = a_screen(mode=ss.MODE_REDIRECT, field=True, words=words, later=1)
        frames = draw.loop_frames(screen)
        self.assertLess(frames, 200)  # a short pass, so the test stays quick
        path = os.path.join(self.tmp, 'c.ts')
        ss.render_clip(self.cfg, screen, path)
        streams = {s['codec_type']: s for s in self.probe(path, '-show_streams')['streams']}
        self.assertEqual((streams['video']['codec_name'], streams['video']['width'], streams['video']['height']),
                         ('h264', 1920, 1080))
        self.assertEqual(streams['audio']['codec_name'], 'aac')
        video = self.probe(path, '-select_streams', 'v', '-show_entries', 'packet=dts_time,flags')['packets']
        self.assertEqual(len(video), frames)
        self.assertIn('K', video[0]['flags'])
        looped = os.path.join(self.tmp, 'looped.ts')
        subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error', '-y', '-stream_loop', '2',
                        '-i', path, '-c', 'copy', '-f', 'mpegts', looped], check=True)
        dts = [float(p['dts_time']) for p in
               self.probe(looped, '-select_streams', 'v', '-show_entries', 'packet=dts_time')['packets']]
        self.assertEqual(len(dts), 3 * frames)
        steps = [b - a for a, b in zip(dts, dts[1:])]
        self.assertTrue(all(s > 0 for s in steps))
        self.assertLess(max(steps), 0.06)  # a frame plus the AAC priming at a seam, never more
        audio = [float(p['pts_time']) for p in
                 self.probe(looped, '-select_streams', 'a', '-show_entries', 'packet=pts_time')['packets']]
        gaps = [b - a for a, b in zip(audio, audio[1:])]
        self.assertLess(max(gaps), 0.0215)

    def test_the_quick_still(self):
        path = os.path.join(self.tmp, 's.ts')
        ss.render_clip(self.cfg, a_screen(mode=ss.MODE_REDIRECT), path, still=True)
        video = self.probe(path, '-select_streams', 'v', '-show_entries', 'packet=dts_time')['packets']
        self.assertEqual(len(video), ss.STILL_FRAMES)

    def test_an_ffmpeg_that_fails_is_named(self):
        cfg = {'ffmpeg': {'path': '/nonexistent/ffmpeg'}}
        with self.assertLogs('app.sports_screen', 'WARNING'):
            with self.assertRaises(ss.ClipFailed):
                ss.render_clip(cfg, a_screen(), os.path.join(self.tmp, 'x.ts'), still=True)
        self.assertEqual([n for n in os.listdir(self.tmp)], [])


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.t.sandbox_config({'integrations': {'output_feed': {'enabled': True, 'username': 'tv',
                                                                'password': 'tv-secret'}},
                               'timezone': 'America/New_York'})
        self.cfg = load_config()
        self.league = SportsLeague(tsdb_id=9001, name='Pretend League', sport='Pretend Ball',
                                   current_season='2099', match_terms=json.dumps(['PFL']))
        db.session.add(self.league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=self.league.id, team_tsdb_id=0, rule_id=0))
        db.session.add(SportsTeam(tsdb_id=101, full_name='Pretendville Bears', names='[]', color1='#c8102e',
                                  color2='#ffffff', short_name='PVB'))
        db.session.add(SportsTeam(tsdb_id=102, full_name='Pretendville Gulls', names='[]'))
        db.session.commit()
        self.assertTrue(sports_feed.set_team_channel(self.league.id, 101, True))
        self.served = sports_feed.served_channels()[0]
        self._tsdb = 7000

    def event(self, start, home=101, away=102):
        self._tsdb += 1
        e = SportsEvent(tsdb_id=self._tsdb, league_id=self.league.id, name='Gulls vs Bears', home_team_id=home,
                        away_team_id=away, home_name='Pretendville Bears', away_name='Pretendville Gulls',
                        venue='Bear Den', city='Pretendville', start_at=start, local_date=start.date())
        db.session.add(e)
        db.session.commit()
        return e


class ScreenForTests(_DbCase):
    """screen_for() turns a served channel's games and playing_now() answer into a Screen."""

    def test_the_next_game_and_the_later_ones(self):
        for days in (1, 2, 3, 4, 5):
            self.event(NOW + timedelta(days=days))
        s = ss.screen_for(self.served, NowPlaying(why_none='no game'), NOW, self.cfg, mode=ss.MODE_PROXY,
                          before_minutes=60)
        self.assertEqual(s.state, ss.STATE_NEXT)
        self.assertEqual(s.game.name, 'Pretendville Gulls at Pretendville Bears')
        self.assertEqual((s.game.day, s.game.clock, s.game.venue), ('Tomorrow', '12:00 PM', 'Bear Den, Pretendville'))
        self.assertEqual(len(s.later), 3)
        self.assertEqual(s.game.home.colors, ('#c8102e', '#ffffff'))
        self.assertEqual(s.game.away.colors, ss.NEUTRAL_AWAY)
        self.assertEqual((s.game.home.code, s.game.away.code), ('PVB', 'PRE'))
        self.assertEqual(s.color, '#c8102e')

    def test_redirect_mode_names_the_stop_time(self):
        self.event(NOW + timedelta(hours=7))  # 7:00 PM in New York
        s = ss.screen_for(self.served, NowPlaying(why_none='x'), NOW, self.cfg, mode=ss.MODE_REDIRECT,
                          before_minutes=60)
        self.assertEqual(s.words.ticker_label, 'Stops by 6:00 PM')

    def test_an_unlisted_game_on_now(self):
        e = self.event(NOW - timedelta(minutes=30))
        later = self.event(NOW + timedelta(days=1))
        s = ss.screen_for(self.served, NowPlaying(event=e, why_none='no stream'), NOW, self.cfg, mode=ss.MODE_PROXY,
                          before_minutes=60)
        self.assertEqual((s.state, s.words.when, s.words.live), (ss.STATE_UNLISTED, 'Started 11:30 AM', True))
        self.assertEqual(len(s.later), 1)
        self.assertNotEqual(s.later[0], s.game)
        self.assertIsNotNone(later)

    def test_a_held_game(self):
        e = self.event(NOW - timedelta(minutes=30))
        playing = NowPlaying(event=e, channels=['a channel'])
        s = ss.screen_for(self.served, playing, NOW, self.cfg, mode=ss.MODE_PROXY, before_minutes=60,
                          held=ss.HELD_NO_CONNECTION)
        self.assertEqual(s.state, ss.STATE_HELD)
        with self.assertRaises(ValueError):
            ss.screen_for(self.served, playing, NOW, self.cfg, mode=ss.MODE_PROXY, before_minutes=60)

    def test_nothing_scheduled(self):
        s = ss.screen_for(self.served, NowPlaying(why_none='x'), NOW, self.cfg, mode=ss.MODE_PROXY,
                          before_minutes=60)
        self.assertEqual((s.state, s.words.headline, s.game), (ss.STATE_NONE, 'Every Pretendville Bears game', None))

    def test_a_league_color_comes_before_the_accent(self):
        """A team with no colors of its own on a league with a color of its own (Formula 1's red,
        dev/changelog/1534), and the accent for a league with none."""
        team = SportsTeam.query.filter_by(tsdb_id=101).one()
        team.color1 = team.color2 = None
        db.session.commit()
        s = ss.screen_for(self.served, NowPlaying(why_none='x'), NOW, self.cfg, mode=ss.MODE_PROXY,
                          before_minutes=60)
        self.assertEqual(s.color, ss.ACCENT)
        with patch.dict(ss.LEAGUE_COLORS, {self.league.tsdb_id: '#123456'}):
            s = ss.screen_for(self.served, NowPlaying(why_none='x'), NOW, self.cfg, mode=ss.MODE_PROXY,
                              before_minutes=60)
        self.assertEqual(s.color, '#123456')
        self.assertEqual(ss.LEAGUE_COLORS[4370], '#E10600')

    def test_a_game_that_is_over_is_not_drawn(self):
        self.event(NOW - timedelta(hours=6))
        s = ss.screen_for(self.served, NowPlaying(why_none='x'), NOW, self.cfg, mode=ss.MODE_PROXY,
                          before_minutes=60)
        self.assertEqual(s.state, ss.STATE_NONE)


class CacheTests(_DbCase):
    """One maker per clip; a made clip is reused; the prune keeps what is asked for and in use."""

    def fake_render(self, calls, delay=0.0):
        def render(cfg, screen, path, *, still=False, background=False):
            calls.append((path, still, background))
            time.sleep(delay)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(b'ts')
        return render

    def test_one_maker_per_clip_and_the_second_caller_gets_its_answer(self):
        calls, out = [], []
        screen = a_screen()
        with patch.object(ss, 'render_clip', self.fake_render(calls, delay=0.3)):
            threads = [threading.Thread(target=lambda: out.append(ss.make_clip(self.cfg, 5, screen)))
                       for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(set(out)), 1)
            self.assertEqual(ss.make_clip(self.cfg, 5, screen), out[0])
            self.assertEqual(len(calls), 1)
        self.assertTrue(out[0].startswith(image_dir(self.cfg, SPORTS_SCREENS)))
        self.assertTrue(os.path.basename(out[0]).startswith('5-'))

    def test_the_still_is_its_own_clip(self):
        calls = []
        with patch.object(ss, 'render_clip', self.fake_render(calls)):
            full = ss.make_clip(self.cfg, 5, a_screen())
            still = ss.make_clip(self.cfg, 5, a_screen(), still=True)
        self.assertNotEqual(full, still)
        self.assertEqual([c[1] for c in calls], [False, True])

    def test_the_prune(self):
        folder = image_dir(self.cfg, SPORTS_SCREENS)
        os.makedirs(folder)
        row = self.served.row.id
        key = 'a' * 20

        def clip(name, age_hours):
            path = os.path.join(folder, name)
            with open(path, 'wb') as f:
                f.write(b'ts')
            stamp = time.time() - age_hours * 3600
            os.utime(path, (stamp, stamp))
            return path
        fresh = clip(f'{row}-{key}.ts', 1)
        stale = clip(f'{row}-{"b" * 20}.ts', 30)
        playing = clip(f'{row}-{"c" * 20}-still.ts', 30)
        orphan = clip(f'{row + 99}-{key}.ts', 1)
        stranger = clip('notes.txt', 30)
        work = os.path.join(folder, '.make-x')
        os.makedirs(work)
        os.utime(work, (time.time() - 7200, time.time() - 7200))
        self.assertEqual(ss.prune_clips(self.cfg, keep={playing}), 2)
        left = set(os.listdir(folder))
        self.assertEqual(left, {os.path.basename(p) for p in (fresh, playing, stranger)})
        self.assertFalse(os.path.exists(stale) or os.path.exists(orphan))

    def test_the_folder_is_a_configured_write_dir_only_with_the_feed_on(self):
        folder = image_dir(self.cfg, SPORTS_SCREENS)
        self.assertIn(folder, [p for p, _r in configured_write_dirs(self.cfg)])
        self.t.sandbox_config({'integrations': {'output_feed': {'enabled': False}}})
        self.assertNotIn(folder, [p for p, _r in configured_write_dirs(load_config())])


if __name__ == '__main__':
    unittest.main()
