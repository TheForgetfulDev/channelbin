"""The words and lengths that keep a listing from counting as a league's game or race
(app/sports_skips.py, dev/changelog/1634), and the Beta mark (dev/changelog/1635): how a word is read, the built-in lists against the patterns
they replaced, a league's changes in config.yaml, the league page and its routes, and the Beta mark on
the pages still being built. The matcher honoring them and the listings dialog's Skipped section are in
tests/test_sports_match.py (SkipWordsTests, SkippedListingsTests), beside the rig they need."""
import os
import re
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import db, sports_skips as sk  # noqa: E402
from app.config import _load_config_file, load_config  # noqa: E402
from app.database import SportsEvent, SportsFollow, SportsLeague  # noqa: E402
from app.sports_match import normalize  # noqa: E402

# duplicated from app/sports_match.py as it stood before dev/changelog/1634 - the patterns the built-in
# lists replaced, kept here to prove the plain phrases skip exactly what they skipped.
OLD_NOT_A_GAME = re.compile(r'\b(highlights?|preview|pre ?game|post ?game|betting|odds|recap|condensed|'
                            r'countdown|in game|analysis)\b')
OLD_NOT_A_RACE = re.compile(r'\b(qualifier|pole|classics?|highlights?|hlts|best of|countdown|preview|recap|'
                            r'pre ?(?:race|qualifying|quali|practice)|post ?(?:race|qualifying|quali|practice)|'
                            r'on ?board|driver (?:tracker|data|cams?|cameras?)|data channel|pit lane)\b')
OLD_PLACEHOLDER = re.compile(r'\bnext event\b')

TITLES = (
    'Warriors Pregame Live', 'Warriors Pre Game Live', 'Warriors Postgame ᴸᶦᵛᵉ', 'NBA on NBC Highlights Factory',
    'Milwaukee Bucks vs. Miami Heat NBA Basketball In-Game Live Gameday ᴸᶦᵛᵉ', 'NBA Basketball: Bucks at Heat',
    'Oddsmakers Weekly', 'NFL Odds & Ends', 'Condensed Game: Jets at Giants', 'Golf Highlight',
    'Las Vegas F1 GP: Qualifying Hlts', 'Live | Onboard | Formel 1 Las Vegas', 'IT: F1 TV PIT LANE',
    'F1 Driver Tracker', 'F1 Driver Cams', 'F1 DATA CHANNEL', 'Formula 1 Pre Qualifying', 'Pre-Race Show',
    'Post Race Analysis', 'Next EVENT: Las Vegas Grand Prix Practice-1 on 2026-11-19 at 07:30PM EST',
    'NASCAR Classics', 'Pole Position', 'Polesitter interview', 'Live Las Vegas F1 Grand Prix', 'Best of 2025',
    'F1 Chequered Flag', 'Driver camera test',
)


class HowAWordIsReadTests(unittest.TestCase):

    def test_whole_words_and_a_space_also_matches_none(self):
        s = sk.BUILTIN.of(1)
        self.assertTrue(s.says(sk.KIND_GAME, normalize('Warriors Pregame Live')))
        self.assertTrue(s.says(sk.KIND_GAME, normalize('Warriors Pre-Game Live')))
        self.assertFalse(s.says(sk.KIND_GAME, normalize('Oddsmakers Weekly')), 'odds is not in oddsmakers')
        self.assertEqual(s.said(sk.KIND_RACE, normalize('Las Vegas F1 GP Qualifying Hlts: Pole Position')),
                         ['pole', 'hlts'])

    def test_the_built_in_lists_skip_what_the_patterns_they_replaced_skipped(self):
        s = sk.BUILTIN.of(1)
        for title in TITLES:
            text = normalize(title)
            with self.subTest(title=title):
                self.assertEqual(s.says(sk.KIND_GAME, text), bool(OLD_NOT_A_GAME.search(text)))
                self.assertEqual(s.says(sk.KIND_RACE, text),
                                 bool(OLD_NOT_A_RACE.search(text) or OLD_PLACEHOLDER.search(text)))

    def test_a_space_also_matching_none_is_wider_than_the_old_patterns_in_two_places(self):
        """Deliberate: the old patterns let only some spaces go. `Bestof` and `Driverdata` now read as
        `best of` and `driver data` (mockup 59)."""
        s = sk.BUILTIN.of(1)
        for title in ('Bestof the season', 'Driverdata'):
            self.assertTrue(s.says(sk.KIND_RACE, normalize(title)), title)
            self.assertFalse(OLD_NOT_A_RACE.search(normalize(title)), title)

    def test_formula_1_alone_has_the_academy_words(self):
        f1, other = sk.BUILTIN.of(4370), sk.BUILTIN.of(4371)
        for title in ('Live F1 Academy: Qualifying', 'F1 Académie'):
            self.assertTrue(f1.says(sk.KIND_RACE, normalize(title)), title)
            self.assertFalse(other.says(sk.KIND_RACE, normalize(title)), title)
        self.assertNotIn('academy', f1.words(sk.KIND_GAME)[:len(sk.BUILTIN_WORDS[sk.KIND_GAME])])

    def test_supercars_alone_has_a_one_hour_race_length(self):
        """dev/docs/BUGS.md 2026-11-24: prod's guides list a Supercars race for 85 minutes and its
        replays for 60 or 90, so under the 2-hour floor none of Adelaide's race listings matched."""
        self.assertEqual(sk.BUILTIN.of(4489).minutes(sk.LENGTH_RACE), 60)
        self.assertEqual(sk.BUILTIN.of(4489).minutes(sk.LENGTH_SESSION), 30)
        self.assertEqual(sk.BUILTIN.of(4370).minutes(sk.LENGTH_RACE), 120)
        self.assertEqual(sk.Skips('4489', own_minutes={sk.LENGTH_RACE: 75}).minutes(sk.LENGTH_RACE), 75,
                         "a person's own length beats the league's built-in one")
        self.assertEqual(sk.reason(sk.BUILTIN.of(4489), [], 20, sk.LENGTH_RACE, 'race'),
                         'It is 20 minutes long, shorter than the 60 a race listing needs.')

    def test_the_reason_names_every_word_a_person_added_and_the_length(self):
        s = sk.Skips('4370', added=('chequered flag',))
        text = normalize('F1 Chequered Flag Highlights')
        said = s.said(sk.KIND_RACE, text)
        self.assertEqual(sk.reason(s, said, 60, sk.LENGTH_RACE, 'race'),
                         'It says "highlights" and "chequered flag", a word you added, and it is 60 minutes long, '
                         'shorter than the 120 a race listing needs.')
        self.assertEqual(sk.reason(s, [], 25, sk.LENGTH_SESSION, 'race'),
                         'It is 25 minutes long, shorter than the 30 a practice, qualifying or sprint listing needs.')
        self.assertEqual(sk.reason(s, [], 125, sk.LENGTH_RACE, 'race'), '')
        self.assertEqual(sk.reason(s, [], 100, sk.LENGTH_RACE, 'event'),
                         'It is 100 minutes long, shorter than the 120 an event listing needs.')


class StoredChangesTests(unittest.TestCase):
    """A league's changes in config.yaml, and only its changes."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        queued = mock.patch('app.scheduler.queue_sports_match', return_value=True)
        self.queue = queued.start()
        self.addCleanup(queued.stop)

    def own(self, tsdb_id=4370):
        return sk.SkipRules().of(tsdb_id)

    def test_add_remove_and_a_removed_built_in_word_comes_back_rather_than_twice(self):
        _s, word = sk.add_word(4370, sk.KIND_RACE, '  Chequered   FLAG ')
        self.assertEqual(word, 'chequered flag')
        sk.remove_word(4370, sk.KIND_RACE, 'pole')
        s = self.own()
        self.assertEqual((s.added, s.removed), (('chequered flag',), ('pole',)))
        self.assertNotIn('pole', s.words(sk.KIND_RACE))
        sk.add_word(4370, sk.KIND_RACE, 'Pole')
        s = self.own()
        self.assertEqual((s.added, s.removed), (('chequered flag',), ()))
        sk.remove_word(4370, sk.KIND_RACE, 'chequered flag')
        self.assertFalse(self.own().changed)
        self.assertNotIn('skip_words', _load_config_file().get('sports_data') or {},
                         'config.yaml holds only what was chosen')
        self.assertEqual(self.queue.call_count, 4, 'every change asks for a matcher run')

    def test_a_word_already_on_the_list_or_not_on_it_is_refused(self):
        for call, says in ((lambda: sk.add_word(4370, sk.KIND_RACE, 'pit lane'), 'already on the list'),
                           (lambda: sk.add_word(4370, sk.KIND_RACE, 'academy'), 'already on the list'),
                           (lambda: sk.add_word(4370, sk.KIND_RACE, ' !! '), 'Type a word'),
                           (lambda: sk.remove_word(4370, sk.KIND_RACE, 'nothing'), 'not on the list'),
                           (lambda: sk.set_minutes(4370, sk.LENGTH_RACE, 241), 'from 0 to 240'),
                           (lambda: sk.set_minutes(4370, sk.LENGTH_RACE, True), 'from 0 to 240'),
                           (lambda: sk.set_minutes(4370, 'lap', 10), 'no length')):
            with self.assertRaises(ValueError) as cm:
                call()
            self.assertIn(says, str(cm.exception))
        self.assertEqual(self.queue.call_count, 0, 'a refusal writes nothing')

    def test_a_game_league_may_add_a_word_only_races_have_built_in(self):
        sk.add_word(4387, sk.KIND_GAME, 'pole')
        self.assertIn('pole', self.own(4387).words(sk.KIND_GAME))

    def test_lengths_restore_and_forget(self):
        sk.set_minutes(4370, sk.LENGTH_RACE, 60)
        sk.add_word(4370, sk.KIND_RACE, 'gameday')
        self.assertEqual(self.own().minutes(sk.LENGTH_RACE), 60)
        sk.set_minutes(4370, sk.LENGTH_RACE, None)
        self.assertEqual(self.own().minutes(sk.LENGTH_RACE), 120)
        sk.restore(4370)
        self.assertFalse(self.own().changed)
        sk.add_word(4370, sk.KIND_RACE, 'gameday')
        self.assertTrue(sk.forget_league(4370))
        self.assertFalse(sk.forget_league(4370))
        self.assertEqual(load_config()['sports_data']['skip_words']['leagues'], {})

    def test_a_word_a_release_adds_reaches_a_league_someone_edited(self):
        sk.remove_word(4370, sk.KIND_RACE, 'pole')
        with mock.patch.dict(sk.BUILTIN_WORDS, {sk.KIND_RACE: sk.BUILTIN_WORDS[sk.KIND_RACE] + ('formation lap',)}):
            self.assertIn('formation lap', self.own().words(sk.KIND_RACE))

    def test_restore_puts_back_a_leagues_own_built_in_length(self):
        sk.set_minutes(4489, sk.LENGTH_RACE, 120)
        self.assertEqual(self.own(4489).minutes(sk.LENGTH_RACE), 120)
        sk.restore(4489)
        self.assertEqual(self.own(4489).minutes(sk.LENGTH_RACE), 60)

    def test_a_hand_edited_typo_falls_back_and_an_unquoted_id_reads_back(self):
        rules = sk.SkipRules({'sports_data': {'skip_words': {'leagues': {4370: {
            'added': ['Gameday', 7, ''], 'removed': 'pole', 'minutes': {'race': 'x', 'session': 45, 'lap': 3}}}}}})
        s = rules.of('4370')
        self.assertEqual(s.added, ('gameday',))
        self.assertEqual(s.minutes(sk.LENGTH_RACE), 120)
        self.assertEqual(s.minutes(sk.LENGTH_SESSION), 45)


class PageAndRoutesTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        queued = mock.patch('app.scheduler.queue_sports_match', return_value=True)
        self.queue = queued.start()
        self.addCleanup(queued.stop)
        now = datetime.utcnow()
        self.f1 = SportsLeague(tsdb_id=4370, name='Formula 1', sport='Motorsport', match_terms='["F1"]', created_at=now)
        self.nba = SportsLeague(tsdb_id=4387, name='NBA', sport='Basketball', match_terms='["NBA"]', created_at=now)
        db.session.add_all([self.f1, self.nba])
        db.session.flush()
        db.session.add_all([SportsFollow(league_id=lg.id, team_tsdb_id=0, rule_id=0, created_at=now)
                            for lg in (self.f1, self.nba)])
        db.session.add_all([
            SportsEvent(tsdb_id=1, league_id=self.f1.id, name='Harbor Grand Prix', start_at=now + timedelta(days=3),
                        local_date=(now + timedelta(days=3)).date(), home_team_id=0, away_team_id=0),
            SportsEvent(tsdb_id=2, league_id=self.nba.id, name='Bucks vs Heat', home_team_id=1, away_team_id=2,
                        home_name='Miami Heat', away_name='Milwaukee Bucks', start_at=now + timedelta(days=1),
                        local_date=(now + timedelta(days=1)).date())])
        db.session.commit()
        self.client = self.t.client

    def page(self, league):
        r = self.client.get(f'/sports/{league.id}')
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True).replace('&#34;', '"')

    def test_a_race_league_shows_its_words_its_own_and_its_lengths(self):
        html = self.page(self.f1)
        self.assertIn('Listings that are never a race', html)
        self.assertEqual(html.count('data-act="remove-skip"'), len(sk.BUILTIN_WORDS[sk.KIND_RACE]) + 2)
        self.assertIn('data-word="academie"', html)
        self.assertIn('"academy", "academie" are built in for Formula 1 only', html)
        self.assertIn('data-skip-length="race"', html)
        self.assertNotIn('data-skip-length="session"', html, 'no session length without sessions')
        self.assertNotIn('data-act="restore-skips"', html)
        self.assertIn("These are ChannelBin's defaults for Formula 1.", html)

    def test_supercars_page_shows_its_own_race_length_and_why(self):
        cars = SportsLeague(tsdb_id=4489, name='V8 Supercars', sport='Motorsport', match_terms='["Supercars"]',
                            created_at=datetime.utcnow())
        db.session.add(cars)
        db.session.commit()
        html = self.page(cars)
        self.assertIn('Default: <code>60 minutes</code>, built in for V8 Supercars: its races air in 60 to 90 '
                      'minutes, and its highlights shows in 20. Leave it empty to use it.', html)
        self.assertIn('placeholder="60"', html)
        self.assertIn('Default: <code>120 minutes</code>. Leave it empty to use it.', self.page(self.f1))

    def test_a_league_with_sessions_gets_the_session_length(self):
        db.session.add(SportsEvent(tsdb_id=3, league_id=self.f1.id, name='Harbor Grand Prix Practice 1',
                                   start_at=datetime.utcnow() + timedelta(days=2), local_date=date.today(),
                                   home_team_id=0, away_team_id=0))
        db.session.commit()
        self.assertIn('data-skip-length="session"', self.page(self.f1))

    def test_a_game_league_shows_the_game_list(self):
        html = self.page(self.nba)
        self.assertIn('Listings that are never a game', html)
        self.assertEqual(html.count('data-act="remove-skip"'), len(sk.BUILTIN_WORDS[sk.KIND_GAME]))
        self.assertNotIn('data-word="pole"', html)
        self.assertIn('data-skip-length="game"', html)

    def test_a_league_of_other_field_events_is_called_by_its_own_noun(self):
        """A golf tour has field events and is not a motorsport: an event, never a race (sports_view._noun)."""
        golf = SportsLeague(tsdb_id=4425, name='Pretend Golf Tour', sport='Golf', match_terms='["PGT"]',
                            created_at=datetime.utcnow())
        db.session.add(golf)
        db.session.flush()
        db.session.add(SportsEvent(tsdb_id=9, league_id=golf.id, name='Harbor Open', home_team_id=0, away_team_id=0,
                                   start_at=datetime.utcnow() + timedelta(days=2), local_date=date.today()))
        db.session.commit()
        html = self.page(golf)
        self.assertIn('Listings that are never an event</h3>', html)
        self.assertIn('Shortest event listing (minutes)', html)
        self.assertIn('data-skip-kind="race"', html)
        self.assertIn('a highlights show or a preview names the event too', html)
        self.assertNotIn('a race', html[html.index('id="sp-skips"'):html.index('Shortest listing')])

    def test_the_page_names_the_list_its_changes_go_to(self):
        self.assertIn('data-skip-kind="race"', self.page(self.f1))
        self.assertIn('data-skip-kind="game"', self.page(self.nba))

    def test_the_routes_change_the_list_and_the_page_says_what_changed(self):
        url = f'/api/sports/leagues/{self.f1.id}/skip-words'
        r = self.client.put(url, json={'add': 'Chequered Flag', 'kind': 'race'})
        self.assertEqual((r.status_code, r.get_json()), (200, {'success': True, 'word': 'chequered flag'}))
        self.assertEqual(self.client.put(url, json={'remove': 'pole', 'kind': 'race'}).status_code, 200)
        r = self.client.put(f'/api/sports/leagues/{self.f1.id}/skip-minutes', json={'length': 'race', 'minutes': '100'})
        self.assertEqual((r.status_code, r.get_json()['minutes']), (200, 100))
        html = self.page(self.f1)
        self.assertIn('''Changed from ChannelBin's defaults: added "chequered flag"; removed "pole"; the shortest race '''
                      '''listing set to 100 minutes.''', html)
        self.assertIn('data-act="restore-skips"', html)
        self.assertEqual(self.client.delete(url).status_code, 200)
        self.assertFalse(sk.SkipRules().of(4370).changed)
        self.assertNotIn('data-act="restore-skips"', self.page(self.f1))

    def test_the_routes_refuse_with_the_reason(self):
        url = f'/api/sports/leagues/{self.nba.id}/skip-words'
        for body, says in (({'add': 'odds', 'kind': 'game'}, 'already on the list'),
                           ({'remove': 'pole', 'kind': 'game'}, 'not on the list'),
                           ({'kind': 'game'}, 'Send one word'), ({'add': 'a', 'remove': 'b', 'kind': 'game'}, 'Send one word'),
                           ({'add': 'gameday'}, 'Say which list'), ({'add': 'gameday', 'kind': 'lap'}, 'Say which list')):
            r = self.client.put(url, json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(says, r.get_json()['error'])
        r = self.client.put(f'/api/sports/leagues/{self.nba.id}/skip-minutes', json={'length': 'game', 'minutes': 500})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.put('/api/sports/leagues/999/skip-words', json={'add': 'x', 'kind': 'game'}).status_code, 404)
        self.assertEqual(self.client.delete('/api/sports/leagues/999/skip-words').status_code, 404)

    def test_deleting_a_league_drops_its_words(self):
        sk.add_word(4370, sk.KIND_RACE, 'gameday')
        SportsFollow.query.filter_by(league_id=self.f1.id).delete()
        db.session.commit()
        self.assertEqual(self.client.delete(f'/api/sports/leagues/{self.f1.id}').status_code, 200)
        self.assertFalse(sk.SkipRules().of(4370).changed)


class BetaMarkTests(unittest.TestCase):
    """EPG Manager, Sports Schedules and IPTV Output say they are still being built, in their titles
    and on their menu items (dev/changelog/1635)."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        league = SportsLeague(tsdb_id=4370, name='Formula 1', sport='Motorsport', match_terms='["F1"]',
                              created_at=datetime.utcnow())
        db.session.add(league)
        db.session.commit()
        self.league_id = league.id

    def test_each_page_has_the_mark_in_its_title_and_the_menu_has_three(self):
        mark = re.compile(r'<span class="badge b-sched tip-plain beta-mark" data-tip="([^"]+) is in beta\. Still being '
                          r'built\. Some parts do not work yet, and how it works may change often\.">Beta</span>')
        for url, name in (('/epg-manager', 'EPG Manager'), ('/sports', 'Sports Schedules'),
                          (f'/sports/{self.league_id}', 'Sports Schedules'), ('/iptv-output', 'IPTV Output')):
            r = self.t.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            html = r.get_data(as_text=True)
            head = html[html.index('<div class="page-head">'):]
            head = head[:head.index('</h1>')]
            self.assertEqual(mark.findall(head), [name], url)
            menu = mark.findall(html[:html.index('<div class="page-head">')])
            self.assertEqual(set(menu), {'EPG Manager', 'IPTV Output', 'Sports Schedules'}, url)
            self.assertEqual(len(menu), 3 * menu.count('EPG Manager'), 'each menu item has its pill wherever the menu is drawn')
        html = self.t.client.get('/').get_data(as_text=True)
        self.assertEqual(set(mark.findall(html)), {'EPG Manager', 'IPTV Output', 'Sports Schedules'},
                         'a page not being built has only the menu items')
        head = html[html.index('<div class="page-head">'):]
        self.assertNotIn('beta-mark', head[:head.index('</h1>')])


if __name__ == '__main__':
    unittest.main()
