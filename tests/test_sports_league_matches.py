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
from unittest import mock
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
        self.assertIn('Matched your guide on 6 of the last 18 events', text)
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
    def test_an_undo_sent_as_text_is_refused_and_marks_nothing(self):
        """dev/docs/BUGS.md 2026-11-15 @ 12:58:58 PM: `"wrong": "false"` is truthy, and marked the listing a
        wrong match when the caller meant to undo one."""
        league, (soon_ev, _), ch, soon = seed_league()
        m = add_match(soon_ev, ch[0], soon)
        db.session.commit()
        r = self.client.post(f'/api/sports/events/{soon_ev.id}/wrong', json={'match_ids': [m.id], 'wrong': 'false'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('true or false', r.get_json()['error'])
        db.session.expire_all()
        self.assertIsNone(db.session.get(SportsMatch, m.id).rejected_at)

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

    def test_each_channel_links_to_its_page_and_names_its_account(self):
        """The dialog links every channel and the account it is on (dev/changelog/1638)."""
        league, (soon_ev, _), ch, soon = seed_league()
        other = make_account(name='Second login', color='#a1b2c3')
        on_other = make_channel(other, name='Elsewhere', category_name='PFL Package')
        named = make_channel(other, name='NEXT | PFL: GULLS @ BEARS', category_name='US| PPV')
        db.session.flush()
        add_match(soon_ev, ch[0], soon)
        add_match(soon_ev, on_other, soon)
        add_name(soon_ev, named)
        db.session.commit()
        data = self.client.get(f'/api/sports/events/{soon_ev.id}/listings').get_json()
        rows = {c['name']: c for g in data['groups'] for c in g['channels']}
        self.assertEqual(rows['Elsewhere']['url'], f'/channels/{on_other.id}')
        self.assertEqual(rows['Elsewhere']['account'], {'id': other.id, 'name': 'Second login', 'color': '#a1b2c3',
                                                        'url': f'/accounts/{other.id}'})
        self.assertEqual(rows['Local 0']['account']['id'], ch[0].account_id)
        self.assertEqual(rows['Local 0']['account']['url'], f'/accounts/{ch[0].account_id}')
        name = data['names'][0]
        self.assertEqual((name['url'], name['account']['name'], name['account']['url']),
                         (f'/channels/{named.id}', 'Second login', f'/accounts/{other.id}'))

    def test_the_accounts_cost_no_query_per_channel(self):
        """CLAUDE.md, no hidden I/O in per-row loops: the account comes in the same query as its channel,
        so the dialog runs as many statements for six channels on six accounts as for one."""
        from tests.support.iocount import IOCounter, all_engines
        league, (soon_ev, _), ch, soon = seed_league()
        add_match(soon_ev, ch[0], soon)
        add_name(soon_ev, ch[1])
        db.session.commit()
        event_id = soon_ev.id

        def count():
            db.session.remove()
            with IOCounter(all_engines()) as c:
                r = self.client.get(f'/api/sports/events/{event_id}/listings')
            self.assertEqual(r.status_code, 200)
            return c.queries, r.get_json()

        small, data = count()
        self.assertEqual(len(data['names']), 1)
        soon_ev = db.session.get(SportsEvent, event_id)
        for i in range(5):
            acc = make_account(name=f'Login {i}')
            add_match(soon_ev, make_channel(acc, name=f'Wide {i}', category_name='PFL Package'), soon)
            add_name(soon_ev, make_channel(acc, name=f'NEXT {i} | PFL: GULLS @ BEARS', category_name='US| PPV'))
        db.session.commit()
        large, data = count()
        self.assertEqual(sum(len(g['channels']) for g in data['groups']), 6)
        self.assertEqual(len(data['names']), 6)
        self.assertEqual(large, small)

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
        # Near TheSportsDB's start and on more channels, so only marking it wrong moves the start off it.
        stray = soon + timedelta(minutes=60)
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

    def test_adding_a_category_answers_what_was_added_and_what_arrived_replays_only(self):
        league, _, _, _ = seed_league()
        db.session.commit()
        r = self.client.put(f'/api/sports/leagues/{league.id}/categories', json={'add': ['PFL Package', 'US| PFL REPLAY']})
        self.assertEqual(r.get_json(), {'success': True, 'added': ['PFL Package', 'US| PFL REPLAY'],
                                        'replay_on': ['US| PFL REPLAY']})
        both = self.client.put(f'/api/sports/leagues/{league.id}/categories',
                               json={'add': ['US| PFL REPLAYS'], 'replay': {'US| PFL REPLAYS': False}})
        self.assertEqual(both.get_json()['replay_on'], [])
        again = self.client.put(f'/api/sports/leagues/{league.id}/categories', json={'add': ['PFL Package']})
        self.assertEqual(again.get_json(), {'success': True, 'added': [], 'replay_on': []})

    def test_a_replays_only_category_says_when_it_lists_a_coming_game_at_its_start(self):
        """dev/docs/BUGS.md 2026-11-16: a Replays-only category held the only listing of 8 games at
        their own start time, they read as replays, and nothing on the page showed it."""
        league, (soon_ev, later_ev), ch, soon = seed_league()
        add_match(soon_ev, ch[0], soon, airing='replay')
        add_match(soon_ev, ch[1], soon + timedelta(hours=5), airing='replay')     # a real replay, hours later
        add_match(later_ev, ch[2], later_ev.start_at, airing='first')             # not in the category
        db.session.add(SportsLeagueCategory(league_id=league.id, category_name='PFL Package', is_replay=True))
        db.session.commit()
        self.assertEqual(sports_match.replay_category_live_games(league.id, datetime.utcnow()), {'PFL Package': 1})
        text = self.page(league.id)
        self.assertIn('Lists 1 coming game at its own start time, which looks live', text)
        self.client.put(f'/api/sports/leagues/{league.id}/categories', json={'replay': {'PFL Package': False}})
        self.assertNotIn('which looks live', self.page(league.id))

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
        # In the league's own sport: a listing naming another one is never read by its category
        # (dev/changelog/1552).
        make_epg_entry(ch[0], title='Pretend Ball: Harborview Gulls at Ironvale Bears', start_time=soon, duration_minutes=180)
        db.session.commit()
        sports_match.run(now=soon - timedelta(hours=1), pause=0)
        self.assertEqual(SportsMatch.query.count(), 0)
        self.client.put(f'/api/sports/leagues/{league.id}/categories', json={'add': ['PFL Package']})
        sports_match.run(now=soon - timedelta(hours=1), pause=0)
        db.session.expire_all()
        self.assertEqual(SportsMatch.query.count(), 1)


class ListingsDialogWrapTests(unittest.TestCase):
    def test_every_channel_row_wraps_its_name_rather_than_widening_the_dialog(self):
        """dev/docs/BUGS.md 2026-11-19 @ 02:12:07 AM: a listing's channel named in one unbroken run pushed the
        dialog 130px past a 375px phone. jsdom computes no layout, so the rule itself is what is held."""
        import re
        with open(os.path.join(REPO, 'static', 'css', 'style.css'), encoding='utf-8') as fh:
            css = fh.read()
        rule = re.search(r'(?m)^\.sp-chan \.sp-cname \{([^}]*)\}', css)
        self.assertIsNotNone(rule, 'no rule for .sp-chan .sp-cname')
        self.assertIn('min-width: 0', rule.group(1))
        self.assertIn('overflow-wrap: anywhere', rule.group(1))


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
            from app.database import SportsTeam
            db.session.add(SportsTeam(tsdb_id=2, full_name='Harborview Gulls', names=json.dumps(['Harborview Gulls', 'Gulls'])))
            db.session.add(SportsTeam(tsdb_id=1, full_name='Ironvale Bears', names=json.dumps(['Ironvale Bears']),
                                      own_names=json.dumps(['Ironvale U'])))
            db.session.commit()
            html = t.client.get(f'/sports/{league.id}').get_data(as_text=True)
            listings = t.client.get(f'/api/sports/events/{soon_ev.id}/listings').get_json()
            # What app/sports_match.py::skipped_listings answers, two rows of it; the reader is
            # tested in tests/test_sports_match.py::SkippedListingsTests.
            listings['skipped'] = [
                {'title': 'PFL Pregame', 'sub': 'Gulls at Bears', 'when': '7:00 PM', 'channels': 3, 'start': '',
                 'why': 'It says "pre game", and it is 60 minutes long, shorter than the 90 a game listing needs.'},
                {'title': 'PFL Gulls at Bears', 'sub': '', 'when': '11:00 PM', 'channels': 1, 'start': '',
                 'why': 'It is 30 minutes long, shorter than the 90 a game listing needs.'}]
            listings = json.dumps(listings)
            # Restore defaults is offered only once something changed.
            from app import sports_skips
            with mock.patch('app.scheduler.queue_sports_match', return_value=True):
                sports_skips.add_word(league.tsdb_id, sports_skips.KIND_GAME, 'scoreboard')
            html = t.client.get(f'/sports/{league.id}').get_data(as_text=True)
            cls.match_ids = [m.id for m in SportsMatch.query.filter_by(kind='guide').order_by(SportsMatch.id)]
            cls.name_id = SportsMatch.query.filter_by(kind='channel_name').one().id
            cls.league_id = league.id
            cls.linked = ([(c.id, c.name, c.account_id) for c in ch[:3]], (slot.id, slot.name, slot.account_id))
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

    def test_every_listed_channel_has_a_preview_of_itself(self):
        """dev/changelog/1645: the channels whose guide lists the game can be watched from the dialog."""
        listed, _slot = self.linked
        self.assertEqual(self.obs['channel_previews'],
                         [{'channel': str(cid), 'name': name} for cid, name, _acct in listed])

    def test_wrong_match_sends_one_channel_or_the_whole_group(self):
        posts = self.obs['wrong_posts']
        self.assertEqual(len(posts), 2)
        self.assertEqual(len(posts[0]['body']['match_ids']), 1)
        self.assertEqual(posts[1]['body'], {'match_ids': sorted(self.match_ids[:3]), 'wrong': True})
        self.assertGreaterEqual(self.obs['swaps_after_close'], 1)

    def test_event_channels_follow_the_groups_with_their_own_wrong_match(self):
        self.assertEqual(self.obs['names_heading'], 'Event channels 1')
        slot_id = self.linked[1][0]
        self.assertEqual(self.obs['names'], [{'text': 'NEXT | PFL: GULLS @ BEARS | US: PPV 2',
                                              'why': 'Matched on the league and both teams in it.',
                                              'badge': 'Live', 'button': 'Wrong match',
                                              'preview': str(slot_id)}])
        self.assertEqual(self.obs['name_posts'], [{'match_ids': [self.name_id], 'wrong': True}])

    def test_every_channel_links_its_page_and_names_its_account_linked(self):
        """dev/changelog/1638: a listing's channel and an event channel each link to the channel page,
        with the account it is on beside it, linked to the account page."""
        listed, (slot_id, slot_name, slot_acct) = self.linked
        self.assertEqual(self.obs['channel_links'], [
            {'name': name, 'href': f'/channels/{cid}', 'account': 'Test M3U', 'account_href': f'/accounts/{acct}'}
            for cid, name, acct in listed])
        self.assertEqual(self.obs['name_links'], [
            {'name': slot_name, 'href': f'/channels/{slot_id}', 'account': 'Slots', 'account_href': f'/accounts/{slot_acct}'}])

    def test_names_are_removed_added_and_a_suggestion_accepted(self):
        puts = [p['body']['terms'] for p in self.obs['term_puts']]
        self.assertEqual(puts[0], ['PFL'])
        self.assertEqual(puts[1], ['Pretend Football League', 'PFL', 'PFL on Two'])
        self.assertEqual(puts[2][-1], 'NASCAR Craftsman Truck Series')
        self.assertEqual(self.obs['swaps_after_terms'], 3)

    def test_skipped_listings_hide_behind_show_which_becomes_hide(self):
        self.assertEqual(self.obs['skipped_closed'], {'button': 'Show 2 skipped listings', 'rows': 0})
        opened = self.obs['skipped_open']
        self.assertEqual((opened['button'], opened['expanded']), ('Hide skipped listings', 'true'))
        self.assertEqual(opened['rows'][0], {'title': 'PFL Pregame: Gulls at Bears',
                                             'why': '3 channels. It says "pre game", and it is 60 minutes long, '
                                                    'shorter than the 90 a game listing needs.'})
        self.assertEqual(len(opened['rows']), 2)
        self.assertEqual(self.obs['skipped_hidden'], {'button': 'Show 2 skipped listings', 'rows': 0})

    def test_skip_words_and_lengths_save_one_change_each_and_restore_asks_first(self):
        base = f'/api/sports/leagues/{self.league_id}'
        self.assertEqual(self.obs['skip_requests'], [
            {'method': 'PUT', 'path': f'{base}/skip-words', 'body': {'remove': 'pre game', 'kind': 'game'}},
            {'method': 'PUT', 'path': f'{base}/skip-words', 'body': {'add': 'Gameday', 'kind': 'game'}},
            {'method': 'PUT', 'path': f'{base}/skip-minutes', 'body': {'length': 'game', 'minutes': 75}},
            {'method': 'PUT', 'path': f'{base}/skip-minutes', 'body': {'length': 'game', 'minutes': None}},
            {'method': 'DELETE', 'path': f'{base}/skip-words', 'body': None}])
        self.assertIn("Restore ChannelBin's default words and lengths for this league?", self.obs['restore_confirm'])

    def test_categories_flip_untie_and_add_from_the_offer(self):
        self.assertEqual(self.obs['offer'], ['PFL Sunday Ticket'])
        bodies = [p['body'] for p in self.obs['category_puts']]
        self.assertEqual(bodies[0], {'replay': {'PFL Package': True}})
        self.assertEqual(bodies[1], {'remove': ['PFL Package']})
        self.assertEqual(bodies[2], {'add': ['PFL Sunday Ticket']})

    def test_an_added_category_says_what_it_switched_on_and_is_marked(self):
        """dev/docs/BUGS.md 2026-11-16: a category added with replay in its name arrived with Replays
        only on and nothing said so, in a list whose rows had just moved."""
        self.assertIn('Added 2 categories.', self.obs['add_toast'])
        self.assertIn('Replays only is on for "PFL Sunday Ticket", because the name says replay', self.obs['add_toast'])
        self.assertEqual(self.obs['new_row'], {'marked': True, 'badge': 'New'})

    def test_a_tap_while_the_list_settles_is_put_back_and_not_saved(self):
        """The tap that switched prod's NHL Center Ice to Replays only landed 1.4 seconds after a
        category was added, on the row that had moved under it."""
        self.assertTrue(self.obs['settling']['restored'])
        self.assertIn('that tap was not saved', self.obs['settling']['toast'])
        self.assertEqual(self.obs['settling']['puts'], 3)

    def test_a_teams_name_is_removed_and_added_from_the_dialog(self):
        self.assertEqual(self.obs['team_options'], ['Pick a team', 'Harborview Gulls', 'Ironvale Bears'])
        self.assertEqual(self.obs['team_theirs'], 'Read as "Harborview Gulls", "Gulls" now.')
        puts = self.obs['team_puts']
        self.assertEqual((puts[0]['path'], puts[0]['body']), ('/api/sports/teams/1/names', {'names': []}))
        self.assertEqual((puts[1]['path'], puts[1]['body']), ('/api/sports/teams/2/names', {'names': ['Harborview-on-Sea']}))


if __name__ == '__main__':
    unittest.main()
