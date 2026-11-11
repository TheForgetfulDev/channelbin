"""A Sports Schedules league page with its games matched to the guide (app/sports_match.py,
app/sports_view.py, app/routes/sports.py, static/js/sports.js), per mockup 51 rounds 3 and 6 and
dev/changelog/1463 and 1472. Made-up leagues and teams; throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_league_matches
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_epg_entry  # noqa: E402

from app import db, sports_data, sports_match  # noqa: E402
from app.database import (  # noqa: E402
    SportsEvent, SportsFollow, SportsLeague, SportsLeagueCategory, SportsMatch, SportsState,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'sports_league_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')


def seed_league(tsdb_id=9001, terms=('Pretend Football League', 'PFL'), trust=(0, 0)):
    """A followed league with two upcoming games, one past what the guide holds, and channels with
    guide rows so "Not in your guide yet" has an end to compare with."""
    now = datetime.utcnow().replace(microsecond=0)
    league = SportsLeague(tsdb_id=tsdb_id, name='Pretend Football League', sport='Pretend Ball', current_season='2099',
                          match_terms=json.dumps(list(terms)), trust_sample=trust[0], trust_agreed=trust[1])
    db.session.add(league)
    db.session.flush()
    db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0, rule_id=0))
    soon = now + timedelta(hours=3)
    later = now + timedelta(days=9)
    events = [SportsEvent(tsdb_id=7001, league_id=league.id, name='a', home_team_id=1, away_team_id=2,
                          home_name='Ironvale Bears', away_name='Harborview Gulls', start_at=soon, local_date=soon.date()),
              SportsEvent(tsdb_id=7002, league_id=league.id, name='b', home_team_id=3, away_team_id=4,
                          home_name='Northgate Panthers', away_name='Saint-Remy Comets', start_at=later,
                          local_date=later.date())]
    db.session.add_all(events)
    acc = make_account()
    channels = [make_channel(acc, name=f'Local {i}', category_name='PFL Package' if i < 2 else 'Locals') for i in range(4)]
    make_epg_entry(channels[3], title='Anything', start_time=now + timedelta(days=2))
    db.session.flush()
    return league, events, channels, soon


def add_match(event, channel, start, title='PFL Football', sub='Harborview Gulls at Ironvale Bears', airing='first', **kw):
    m = SportsMatch(event_id=event.id, channel_id=channel.id, kind='guide', program_start_time=start,
                    program_stop_time=start + timedelta(hours=3), title_key=title.lower(), program_title=title,
                    program_sub_title=sub, strength='strong', airing=airing, reason='Matched on the league named in the listing.',
                    **kw)
    db.session.add(m)
    return m


def add_name(event, channel, airing='first', strength='strong', **kw):
    """A channel-name match as the matcher stores one (dev/changelog/1467): the channel's name as the text."""
    m = SportsMatch(event_id=event.id, channel_id=channel.id, kind='channel_name', program_start_time=event.start_at,
                    title_key=channel.name.lower(), episode_key='', program_title=channel.name, strength=strength,
                    airing=airing, reason=f"{sports_match._NAME_PREFIX}: matched on the league and both teams in it.",
                    **kw)
    db.session.add(m)
    return m


class LeaguePageCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.t.sandbox_config({'display': {'timezone': 'America/New_York', 'time_format': '12h'}})
        self.client = self.t.client

    def page(self, league_id):
        db.session.remove()
        r = self.client.get(f'/sports/{league_id}')
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)


class GuideColumnTests(LeaguePageCase):
    def test_a_game_says_how_many_channels_carry_it_without_wrong_or_gone_ones(self):
        league, (soon_ev, _), ch, soon = seed_league()
        add_match(soon_ev, ch[0], soon)
        add_match(soon_ev, ch[1], soon)
        add_match(soon_ev, ch[2], soon, rejected_at=datetime.utcnow())
        add_match(soon_ev, ch[3], soon, gone_since=datetime.utcnow())
        add_match(soon_ev, ch[0], soon + timedelta(hours=6), airing='replay')
        db.session.commit()
        text = self.page(league.id)
        self.assertIn('On 2 channels', text)
        self.assertIn('Live, and 1 replay later', text)

    def test_a_game_past_the_guide_and_one_the_guide_lacks_say_different_things(self):
        league, (soon_ev, later_ev), _, _ = seed_league()
        db.session.add(SportsState(id=1, last_match_at=datetime.utcnow()))
        db.session.commit()
        text = self.page(league.id)
        self.assertIn('Not found in your guide', text)
        self.assertIn('No listing names the league and both teams near this time.', text)
        self.assertIn('Not in your guide yet', text)
        self.assertIn('Your guide reaches', text)

    def test_a_game_the_matcher_has_not_looked_at_says_so(self):
        """Followed after the last run, or no run yet: not checked is never shown as not found."""
        league, _, _, _ = seed_league()
        db.session.add(SportsState(id=1, last_match_at=datetime.utcnow() - timedelta(hours=1)))
        db.session.commit()
        text = self.page(league.id)
        self.assertIn('Not checked yet', text)
        self.assertNotIn('Not found in your guide', text)

    def test_a_game_with_every_listing_marked_wrong_can_still_be_opened(self):
        league, (soon_ev, _), ch, soon = seed_league()
        add_match(soon_ev, ch[0], soon, rejected_at=datetime.utcnow())
        add_match(soon_ev, ch[1], soon, rejected_at=datetime.utcnow())
        db.session.commit()
        event_id = soon_ev.id
        text = self.page(league.id)
        self.assertIn('On 0 channels', text)
        self.assertIn('All 2 listings marked a wrong match. Open to undo.', text)
        self.assertRegex(text, r'<button class="sp-onguide" data-act="listings" data-event="%d"' % event_id)
        self.assertNotIn('Not found in your guide', text)

    def test_listings_whose_airing_is_not_known_do_not_read_as_replays(self):
        league, (soon_ev, _), ch, soon = seed_league()
        add_match(soon_ev, ch[0], soon, airing='unknown')
        db.session.commit()
        text = self.page(league.id)
        self.assertIn('Not known yet if live or replay', text)
        self.assertNotIn('<span class="s">Replays only', text)

    def test_a_race_at_the_placeholder_time_shows_no_second_time_and_says_series(self):
        league, (soon_ev, later_ev), ch, soon = seed_league()
        midnight = datetime.combine(soon.date() + timedelta(days=1), datetime.min.time())
        race = SportsEvent(tsdb_id=7003, league_id=league.id, name='Lakeside 200', start_at=midnight,
                           local_date=midnight.date(), guide_start=midnight + timedelta(hours=19, minutes=30))
        db.session.add(race)
        db.session.flush()
        add_match(race, ch[0], race.guide_start, title='Pretend Football League', sub='Lakeside 200')
        race2 = SportsEvent(tsdb_id=7004, league_id=league.id, name='Harbor 100', start_at=soon, local_date=soon.date())
        db.session.add(race2)
        db.session.add(SportsState(id=1, last_match_at=datetime.utcnow() + timedelta(minutes=1)))
        db.session.commit()
        text = self.page(league.id)
        self.assertNotIn("not TheSportsDB's", text)
        self.assertIn('No listing names the series on this day.', text)

    def test_the_checked_row_gives_this_leagues_figure_and_keeps_the_last_time_while_waiting(self):
        league, (soon_ev, _), ch, soon = seed_league()
        add_match(soon_ev, ch[0], soon)
        db.session.add(SportsState(id=1, last_match_at=datetime.utcnow() - timedelta(minutes=5),
                                   match_note='Waiting: an account sync is running. Tries again in 5 minutes.'))
        db.session.commit()
        text = self.page(league.id)
        self.assertIn('Found 1 of 2 upcoming games in your guide.', text)
        self.assertIn('Last checked today', text)

    def test_a_disagreement_shows_both_times(self):
        league, (soon_ev, _), ch, soon = seed_league()
        soon_ev.guide_start = soon + timedelta(hours=12)
        add_match(soon_ev, ch[0], soon + timedelta(hours=12))
        db.session.commit()
        text = self.page(league.id)
        self.assertIn('your guide</span>', text)
        self.assertIn('TheSportsDB</span>', text)
        self.assertIn("in your guide, not TheSportsDB's", text)

    def test_every_game_links_to_its_event_on_thesportsdb(self):
        league, _, _, _ = seed_league()
        db.session.commit()
        text = self.page(league.id)
        self.assertIn('href="https://www.thesportsdb.com/event/7001"', text)
        self.assertIn('href="https://www.thesportsdb.com/event/7002"', text)

    def test_the_start_times_row_says_how_far_they_can_be_believed(self):
        league, _, _, _ = seed_league()
        db.session.commit()
        league_id = league.id
        self.assertIn('Not measured yet', self.page(league_id))

        def trust(sample, agreed):
            row = db.session.get(SportsLeague, league_id)
            row.trust_sample, row.trust_agreed = sample, agreed
            db.session.commit()
        trust(18, 6)
        text = self.page(league_id)
        self.assertIn('Match your guide on 6 of the last 18 events', text)
        self.assertIn('only 6 of the last 18', text)
        self.assertIn('Not enough to rely on', text)
        trust(5, 5)
        text = self.page(league_id)
        self.assertIn('Too few checked yet to rely on', text)
        self.assertIn('5 of the 5 checked so far, too few to rely on yet', text)
        trust(12, 12)
        text = self.page(league_id)
        self.assertNotIn('Not enough', text)
        self.assertIn('both times are shown', text)

    def test_a_waiting_matcher_says_so_on_the_page(self):
        league, _, _, _ = seed_league()
        db.session.add(SportsState(id=1, match_note='Waiting: an account sync is running. Tries again in 5 minutes.'))
        db.session.commit()
        self.assertIn('Waiting: an account sync is running', self.page(league.id))


class EventChannelTests(LeaguePageCase):
    """Event channels on the league page, per mockup 51 round 6 (dev/changelog/1472)."""

    def slots(self, n, hidden=0):
        acc = make_account(name='Slots')
        return [make_channel(acc, name=f'NEXT | PFL: GULLS @ BEARS | 8K | US: PPV {i}', category_name='US| PPV',
                             hidden=i < hidden) for i in range(n)]

    def test_the_columns_are_venue_and_where_its_on(self):
        league, _, _, _ = seed_league()
        db.session.commit()
        text = self.page(league.id)
        self.assertIn('<th>Venue</th><th>Where it&rsquo;s on</th>', text)
        self.assertNotIn('On your guide', text)

    def test_a_game_with_no_listing_says_how_many_event_channels_name_it(self):
        league, (soon_ev, _), _, _ = seed_league()
        db.session.add(SportsState(id=1, last_match_at=datetime.utcnow()))
        for ch in self.slots(3):
            add_name(soon_ev, ch)
        db.session.commit()
        event_id = soon_ev.id
        text = self.page(league.id)
        self.assertIn('On 3 event channels', text)
        self.assertIn('Read from their names. No listing in your guide names it.', text)
        self.assertRegex(text, r'data-event="%d" data-name="[^"]*" data-names-only="1">On 3' % event_id)
        self.assertIn('Event channels naming it', text)
        self.assertNotIn('Not found in your guide', text)

    def test_a_game_past_the_guide_says_where_the_guide_ends(self):
        league, (_, later_ev), _, _ = seed_league()
        add_name(later_ev, self.slots(1)[0])
        db.session.commit()
        text = self.page(league.id)
        self.assertIn('On 1 event channel', text)
        self.assertIn('Read from its name. Not in your guide yet. Your guide reaches', text)

    def test_a_game_with_listings_also_counts_its_event_channels(self):
        league, (soon_ev, _), ch, soon = seed_league()
        add_match(soon_ev, ch[0], soon)
        for slot in self.slots(2):
            add_name(soon_ev, slot)
        db.session.commit()
        text = self.page(league.id)
        self.assertIn('On 1 channel', text)
        self.assertIn('<span class="s">Also named by 2 event channels</span>', text)
        self.assertRegex(text, r'data-names-only="0">On 1 channel')

    def test_hidden_gone_and_wrong_event_channels_are_not_counted(self):
        """dev/docs/BUGS.md 2026-11-08 11:47: a channel the user hid is never offered."""
        league, (soon_ev, _), _, _ = seed_league()
        slots = self.slots(5, hidden=2)
        for ch in slots[:3]:
            add_name(soon_ev, ch)
        add_name(soon_ev, slots[3], gone_since=datetime.utcnow())
        add_name(soon_ev, slots[4], rejected_at=datetime.utcnow())
        db.session.commit()
        event_id = soon_ev.id
        text = self.page(league.id)
        self.assertIn('On 1 event channel', text)
        self.assertEqual(sports_match.name_counts([event_id]), {event_id: {'channels': 1, 'wrong': 1}})

    def test_a_game_whose_every_event_channel_is_marked_wrong_can_still_be_opened(self):
        league, (soon_ev, _), _, _ = seed_league()
        for ch in self.slots(2):
            add_name(soon_ev, ch, rejected_at=datetime.utcnow())
        db.session.commit()
        event_id = soon_ev.id
        text = self.page(league.id)
        self.assertIn('On 0 event channels', text)
        self.assertIn('All 2 event channels marked a wrong match. Open to undo.', text)
        self.assertRegex(text, r'<button class="sp-onguide" data-act="listings" data-event="%d"' % event_id)

    def test_the_dialog_lists_each_event_channel_with_why_and_no_repeated_clause(self):
        league, (soon_ev, _), _, _ = seed_league()
        slots = self.slots(4, hidden=1)
        add_name(soon_ev, slots[1], strength='weak', airing='unknown')
        add_name(soon_ev, slots[2], airing='replay')
        wrong = add_name(soon_ev, slots[3], rejected_at=datetime.utcnow())
        add_name(soon_ev, slots[0])
        db.session.commit()
        data = self.client.get(f'/api/sports/events/{soon_ev.id}/listings').get_json()
        self.assertEqual(data['groups'], [])
        self.assertEqual(data['one'], 'game')
        names = data['names']
        self.assertEqual([n['channel_id'] for n in names], [slots[2].id, slots[3].id, slots[1].id])
        self.assertEqual(names[0]['why'], 'Matched on the league and both teams in it.')
        self.assertEqual([n['badge'] for n in names], [['b-abort', 'Replay'], ['b-done', 'Live'], None])
        self.assertEqual([n['wrong'] for n in names], [False, True, False])
        r = self.client.post(f'/api/sports/events/{soon_ev.id}/wrong', json={'match_ids': [wrong.id], 'wrong': False})
        self.assertEqual(r.get_json(), {'success': True, 'changed': 1})


class ListingsAndWrongMatchTests(LeaguePageCase):
    def test_listings_are_grouped_by_wording_with_every_channel(self):
        league, (soon_ev, _), ch, soon = seed_league()
        for c in ch[:3]:
            add_match(soon_ev, c, soon)
        add_match(soon_ev, ch[3], soon, title='Live PFL Gulls @ Bears', sub=None)
        db.session.commit()
        data = self.client.get(f'/api/sports/events/{soon_ev.id}/listings').get_json()
        self.assertEqual([len(g['channels']) for g in data['groups']], [3, 1])
        self.assertEqual((data['listings'], data['channels']), (4, 4))
        self.assertEqual(data['tsdb_url'], 'https://www.thesportsdb.com/event/7001')

    def test_wrong_match_marks_only_that_events_rows_and_undo_clears_it(self):
        league, (soon_ev, later_ev), ch, soon = seed_league()
        mine = add_match(soon_ev, ch[0], soon)
        other = add_match(later_ev, ch[1], soon + timedelta(days=9))
        db.session.commit()
        r = self.client.post(f'/api/sports/events/{soon_ev.id}/wrong', json={'match_ids': [mine.id, other.id], 'wrong': True})
        self.assertEqual(r.get_json(), {'success': True, 'changed': 1})
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(SportsMatch, mine.id).rejected_at)
        self.assertIsNone(db.session.get(SportsMatch, other.id).rejected_at)
        data = self.client.get(f'/api/sports/events/{soon_ev.id}/listings').get_json()
        self.assertTrue(data['groups'][0]['all_wrong'])
        self.assertEqual(data['listings'], 0)
        self.client.post(f'/api/sports/events/{soon_ev.id}/wrong', json={'match_ids': [mine.id], 'wrong': False})
        db.session.expire_all()
        self.assertIsNone(db.session.get(SportsMatch, mine.id).rejected_at)

    def test_wrong_match_for_an_event_that_is_gone_is_a_404(self):
        self.assertEqual(self.client.post('/api/sports/events/999/wrong', json={'match_ids': [1]}).status_code, 404)

    def test_a_wrong_match_takes_no_part_in_the_guides_start(self):
        """Marking the stray listing wrong lets the next run read the game's start from the rest."""
        from app.database import SportsTeam
        league, (soon_ev, _), ch, soon = seed_league()
        for tid, full, nick in ((1, 'Ironvale Bears', 'Bears'), (2, 'Harborview Gulls', 'Gulls')):
            db.session.add(SportsTeam(tsdb_id=tid, full_name=full, names=json.dumps([full, nick]),
                                      city=full[:-len(nick)].strip(), last_looked_up=datetime.utcnow()))
        db.session.add(SportsState(id=1))
        stray = soon + timedelta(hours=2)
        for c in ch[:2]:
            make_epg_entry(c, title='PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start_time=stray,
                           duration_minutes=180)
        make_epg_entry(ch[2], title='PFL Football', sub_title='Harborview Gulls at Ironvale Bears', start_time=soon,
                       duration_minutes=180)
        db.session.commit()
        sports_match.run(now=soon - timedelta(hours=1), pause=0)
        db.session.expire_all()
        self.assertEqual(db.session.get(SportsEvent, soon_ev.id).guide_start, stray)
        ids = [m.id for m in SportsMatch.query.filter_by(event_id=soon_ev.id, program_start_time=stray)]
        self.client.post(f'/api/sports/events/{soon_ev.id}/wrong', json={'match_ids': ids})
        sports_match.run(now=soon - timedelta(hours=1), pause=0)
        db.session.expire_all()
        self.assertEqual(db.session.get(SportsEvent, soon_ev.id).guide_start, soon)

    def test_wrong_match_needs_ids(self):
        league, (soon_ev, _), _, _ = seed_league()
        db.session.commit()
        self.assertEqual(self.client.post(f'/api/sports/events/{soon_ev.id}/wrong', json={'match_ids': []}).status_code, 400)
        self.assertEqual(self.client.post(f'/api/sports/events/{soon_ev.id}/wrong', json={'match_ids': 'x'}).status_code, 400)


class NamesAndCategoriesTests(LeaguePageCase):
    def test_names_are_trimmed_deduplicated_and_never_empty(self):
        league, _, _, _ = seed_league()
        db.session.commit()
        r = self.client.put(f'/api/sports/leagues/{league.id}/terms', json={'terms': ['  PFL ', 'pfl', 'PFL on Two', '']})
        self.assertEqual(r.get_json()['terms'], ['PFL', 'PFL on Two'])
        r = self.client.put(f'/api/sports/leagues/{league.id}/terms', json={'terms': ['  ']})
        self.assertEqual(r.status_code, 400)
        self.assertIn('at least one name', r.get_json()['error'])
        db.session.expire_all()
        self.assertEqual(json.loads(db.session.get(SportsLeague, league.id).match_terms), ['PFL', 'PFL on Two'])

    def test_a_known_extra_name_seeds_a_new_follow_and_is_offered_when_missing(self):
        """TheSportsDB calls 5093 `NASCAR Truck Series`; guides say `NASCAR Craftsman Truck Series`."""
        league_id = sports_data.follow_league({'tsdb_id': 5093, 'name': 'NASCAR Truck Series', 'sport': 'Motorsport',
                                               'season': '2099', 'terms': ['NASCAR Truck Series']})
        league = db.session.get(SportsLeague, league_id)
        self.assertEqual(json.loads(league.match_terms), ['NASCAR Truck Series', 'NASCAR Craftsman Truck Series'])
        self.assertEqual(sports_match.suggested_terms(league), [])
        league = db.session.get(SportsLeague, league_id)
        league.match_terms = json.dumps(['NASCAR Truck Series'])
        db.session.commit()
        self.assertIn('Guides often call this league <strong>NASCAR Craftsman Truck Series</strong>', self.page(league_id))

    def test_categories_are_offered_by_name_tied_untied_and_flagged(self):
        league, _, _, _ = seed_league()
        db.session.commit()
        offer = self.client.get(f'/api/sports/leagues/{league.id}/categories/offer').get_json()['categories']
        self.assertEqual(offer, [{'name': 'PFL Package', 'channels': 2, 'tied': False}])
        self.client.put(f'/api/sports/leagues/{league.id}/categories', json={'add': ['PFL Package', 'US| PFL REPLAY']})
        cats = {c.category_name: c.is_replay for c in SportsLeagueCategory.query.filter_by(league_id=league.id)}
        self.assertEqual(cats, {'PFL Package': False, 'US| PFL REPLAY': True})
        text = self.page(league.id)
        self.assertIn('Not on any of your accounts right now', text)
        self.client.put(f'/api/sports/leagues/{league.id}/categories', json={'replay': {'PFL Package': True}})
        self.client.put(f'/api/sports/leagues/{league.id}/categories', json={'remove': ['US| PFL REPLAY']})
        db.session.expire_all()
        cats = {c.category_name: c.is_replay for c in SportsLeagueCategory.query.filter_by(league_id=league.id)}
        self.assertEqual(cats, {'PFL Package': True})

    def test_a_category_is_saved_exactly_as_the_channels_carry_it(self):
        league, _, ch, _ = seed_league()
        ch[3].category_name = 'PFL  Extra '
        db.session.commit()
        self.client.put(f'/api/sports/leagues/{league.id}/categories', json={'add': ['PFL  Extra ']})
        self.assertEqual([c.category_name for c in SportsLeagueCategory.query.all()], ['PFL  Extra '])
        self.assertNotIn('Not on any of your accounts right now', self.page(league.id))

    def test_a_category_change_with_the_wrong_types_is_a_400(self):
        league, _, _, _ = seed_league()
        db.session.commit()
        for body in ({'remove': [['x']]}, {'add': [1]}, {'replay': {'PFL Package': 'yes'}}):
            self.assertEqual(self.client.put(f'/api/sports/leagues/{league.id}/categories', json=body).status_code, 400)

    def test_a_tied_category_is_read_by_the_next_match(self):
        """The card is not decoration: a listing on a channel in a tied category counts as the league."""
        league, (soon_ev, _), ch, soon = seed_league()
        from app.database import SportsTeam
        for tid, full, nick in ((1, 'Ironvale Bears', 'Bears'), (2, 'Harborview Gulls', 'Gulls')):
            db.session.add(SportsTeam(tsdb_id=tid, full_name=full, names=json.dumps([full, nick]),
                                      city=full[:-len(nick)].strip(), last_looked_up=datetime.utcnow()))
        db.session.add(SportsState(id=1))
        make_epg_entry(ch[0], title='Football: Harborview Gulls at Ironvale Bears', start_time=soon, duration_minutes=180)
        db.session.commit()
        sports_match.run(now=soon - timedelta(hours=1), pause=0)
        self.assertEqual(SportsMatch.query.count(), 0)
        self.client.put(f'/api/sports/leagues/{league.id}/categories', json={'add': ['PFL Package']})
        sports_match.run(now=soon - timedelta(hours=1), pause=0)
        db.session.expire_all()
        self.assertEqual(SportsMatch.query.count(), 1)


@unittest.skipIf(shutil.which('node') is None or not os.path.isdir(JSDOM), 'node or jsdom not installed')
class LeaguePageScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = make_test_app(csrf=False)
        try:
            league, (soon_ev, _), ch, soon = seed_league(tsdb_id=5093)
            for c in ch[:3]:
                add_match(soon_ev, c, soon)
            add_match(soon_ev, ch[3], soon, title='Live PFL Gulls @ Bears', sub=None)
            slot = make_channel(make_account(name='Slots'), name='NEXT | PFL: GULLS @ BEARS | US: PPV 2', category_name='US| PPV')
            db.session.flush()
            add_name(soon_ev, slot)
            db.session.add(SportsLeagueCategory(league_id=league.id, category_name='PFL Package'))
            db.session.commit()
            html = t.client.get(f'/sports/{league.id}').get_data(as_text=True)
            listings = t.client.get(f'/api/sports/events/{soon_ev.id}/listings').get_data(as_text=True)
            cls.match_ids = [m.id for m in SportsMatch.query.filter_by(kind='guide').order_by(SportsMatch.id)]
            cls.name_id = SportsMatch.query.filter_by(kind='channel_name').one().id
        finally:
            t.cleanup()
        with tempfile.TemporaryDirectory() as tmp:
            for name, body in (('league.html', html), ('listings.json', listings)):
                with open(os.path.join(tmp, name), 'w', encoding='utf-8') as fh:
                    fh.write(body)
            proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO], capture_output=True, text=True,
                                  cwd=REPO, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
        cls.obs = json.loads(proc.stdout)

    def test_no_script_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_the_dialog_opens_with_every_group_closed(self):
        self.assertIn('where it is in your guide', self.obs['dialog_title'])
        self.assertEqual(self.obs['groups'], 2)
        self.assertTrue(self.obs['closed'])
        self.assertEqual(self.obs['group_counts'], ['3 channels', '1 channel'])
        self.assertIn('Nothing is recorded, cancelled or deleted', self.obs['intro'])
        self.assertNotIn('Not this', self.obs['intro'])

    def test_a_group_carries_wrong_match_for_all_of_it(self):
        self.assertEqual(self.obs['group_buttons'], [{'text': 'Wrong match', 'danger': True}] * 2)

    def test_the_caret_opens_a_group_to_every_channel_each_with_a_plain_button(self):
        self.assertEqual(self.obs['opened_channels'], 3)
        self.assertEqual(self.obs['opened_expanded'], 'true')
        self.assertEqual(self.obs['channel_buttons'], [{'text': 'Wrong match', 'plain': True}] * 3)
        self.assertTrue(self.obs['still_open_after_reload'])

    def test_wrong_match_sends_one_channel_or_the_whole_group(self):
        posts = self.obs['wrong_posts']
        self.assertEqual(len(posts), 2)
        self.assertEqual(len(posts[0]['body']['match_ids']), 1)
        self.assertEqual(posts[1]['body'], {'match_ids': sorted(self.match_ids[:3]), 'wrong': True})
        self.assertGreaterEqual(self.obs['swaps_after_close'], 1)

    def test_event_channels_follow_the_groups_with_their_own_wrong_match(self):
        self.assertEqual(self.obs['names_heading'], 'Event channels 1')
        self.assertEqual(self.obs['names'], [{'text': 'NEXT | PFL: GULLS @ BEARS | US: PPV 2',
                                              'why': 'Matched on the league and both teams in it.',
                                              'badge': 'Live', 'button': 'Wrong match'}])
        self.assertEqual(self.obs['name_posts'], [{'match_ids': [self.name_id], 'wrong': True}])

    def test_names_are_removed_added_and_a_suggestion_accepted(self):
        puts = [p['body']['terms'] for p in self.obs['term_puts']]
        self.assertEqual(puts[0], ['PFL'])
        self.assertEqual(puts[1], ['Pretend Football League', 'PFL', 'PFL on Two'])
        self.assertEqual(puts[2][-1], 'NASCAR Craftsman Truck Series')
        self.assertEqual(self.obs['swaps_after_terms'], 3)

    def test_categories_flip_untie_and_add_from_the_offer(self):
        self.assertEqual(self.obs['offer'], ['PFL Sunday Ticket'])
        bodies = [p['body'] for p in self.obs['category_puts']]
        self.assertEqual(bodies[0], {'replay': {'PFL Package': True}})
        self.assertEqual(bodies[1], {'remove': ['PFL Package']})
        self.assertEqual(bodies[2], {'add': ['PFL Sunday Ticket']})


if __name__ == '__main__':
    unittest.main()
