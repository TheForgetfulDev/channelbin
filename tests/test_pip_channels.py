"""Picture-in-picture channels on the output feed: the rows, their feed ids, the catalog, the
guide a player is sent, what each kind of picture resolves to, the routes and the IPTV Output
page's card (app/pip_channels.py, dev/docs/DESIGN-pip-channels.md 4, 5 and 12).

What a tune-in plays is tests/test_pip_play.py. No dev/docs/BUGS.md entry: a feature.

  python3 -m unittest tests.test_pip_channels
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.output_feed import FeedCase  # noqa: E402
from tests.support.seed import make_guide_match  # noqa: E402

import app.migrations as M  # noqa: E402
import app.output_feed as feed  # noqa: E402
import app.pip_channels as pip  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (PIP_SOURCE_CHANNEL, PIP_SOURCE_GAME, PIP_SOURCE_GROUP, PIP_SOURCE_SEARCH,  # noqa: E402
                          PipChannel, SportsEvent, SportsFollow, SportsLeague)


def src(kind, ref=None, query=''):
    return {'kind': kind, 'ref': ref, 'query': query}


class _Pip(FeedCase):
    def spec(self, **over):
        data = {'name': 'PiP 1', 'main': src(PIP_SOURCE_CHANNEL, self.alpha.id),
                'inset': src(PIP_SOURCE_GROUP, self.group.id), 'layout': 'corner', 'corner': 'br',
                'size': 'm', 'swapped_copy': False}
        data.update(over)
        return data

    def make(self, **over):
        spec = pip.clean(self.spec(**over))
        pip.check_sources(spec, load_config())
        return pip.create(spec)

    def game(self, start, tsdb_id=7001, name='g'):
        league = SportsLeague.query.first()
        if league is None:
            league = SportsLeague(tsdb_id=9001, name='Pretend League', match_terms='[]')
            db.session.add(league)
            db.session.flush()
            db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0, rule_id=0))
        event = SportsEvent(tsdb_id=tsdb_id, league_id=league.id, name=name, home_team_id=101,
                            away_team_id=102, home_name='Ironvale Bears', away_name='Harborview Gulls',
                            start_at=start, local_date=start.date())
        db.session.add(event)
        db.session.commit()
        return event


class FeedIdTests(unittest.TestCase):
    def test_two_ids_to_a_row_decoded_with_no_table(self):
        self.assertEqual(feed.feed_id_for_pip(7), feed.PIP_FEED_BASE + 14)
        self.assertEqual(feed.feed_id_for_pip(7, True), feed.PIP_FEED_BASE + 15)
        for swapped in (False, True):
            fid = feed.feed_id_for_pip(7, swapped)
            self.assertEqual(feed.decode_feed_id(fid), (feed.KIND_PIP, 7))
            self.assertEqual(feed.pip_swapped(fid), swapped)

    def test_the_other_kinds_ids_are_unmoved_and_never_swapped(self):
        self.assertEqual(feed.decode_feed_id(feed.UP_NEXT_FEED_BASE + 3), (feed.KIND_UP_NEXT, 3))
        self.assertEqual(feed.decode_feed_id(feed.SPORTS_FEED_BASE + 3), (feed.KIND_SPORTS, 3))
        self.assertEqual(feed.decode_feed_id(11), (feed.KIND_GROUP, 5))
        self.assertFalse(feed.pip_swapped(11))
        self.assertFalse(feed.pip_swapped(feed.UP_NEXT_FEED_BASE + 1))

    def test_an_xtream_client_reads_the_id_as_32_bits(self):
        self.assertLess(feed.feed_id_for_pip(70_000_000, True), 2 ** 31)

    def test_it_is_relayed_in_every_mode(self):
        for mode in feed.MODES:
            self.assertTrue(feed.relays({'mode': mode}, feed.feed_id_for_pip(1)), mode)


class WriteTests(_Pip):
    def test_create_stores_both_pictures_and_is_on_the_output(self):
        row = self.make(inset=src(PIP_SOURCE_SEARCH, query='  in   car '), swapped_copy=True)
        self.assertEqual((row.main_kind, row.main_ref), (PIP_SOURCE_CHANNEL, self.alpha.id))
        self.assertEqual((row.inset_kind, row.inset_ref, row.inset_query), (PIP_SOURCE_SEARCH, None, 'in car'))
        self.assertTrue(row.on_output)
        self.assertTrue(row.swapped_copy)
        self.assertTrue(pip.is_set(row))

    def test_what_clean_refuses(self):
        for over, said in (({'name': '   '}, 'Give it a name'),
                           ({'name': 'x' * 41}, 'at most 40'),
                           ({'main': None}, 'big picture'),
                           ({'inset': src('movie', 3)}, 'small picture'),
                           ({'inset': src(PIP_SOURCE_SEARCH, query=' ')}, 'part of a channel name'),
                           ({'main': src(PIP_SOURCE_CHANNEL, True)}, 'big picture'),
                           ({'layout': 'grid'}, 'not a layout'),
                           ({'corner': 'xx'}, 'not a layout'),
                           ({'swapped_copy': 'yes'}, 'swapped copy')):
            with self.assertRaises(pip.PipRefused, msg=over) as ctx:
                pip.clean(self.spec(**over))
            self.assertIn(said, ctx.exception.message)

    def test_a_body_of_the_wrong_shape_is_refused_in_words_never_raised(self):
        """dev/docs/BUGS.md 2026-11-23 @ 10:14:38 PM: a list for the body, a list or an object for a
        layout field, a list for a name or a search, and an id past what SQLite stores each
        raised instead of being refused."""
        for body in ([1], 'x', 7):
            with self.assertRaises(pip.PipRefused, msg=body):
                pip.clean(body)
        for over in ({'size': []}, {'size': {}}, {'layout': ['corner']}, {'corner': {'a': 1}},
                     {'name': ['PiP']}, {'main': src(PIP_SOURCE_SEARCH, query=['a'])},
                     {'main': src(PIP_SOURCE_CHANNEL, 2 ** 63)}):
            with self.assertRaises(pip.PipRefused, msg=over):
                pip.clean(self.spec(**over))

    def test_a_picture_that_is_not_in_the_guide_is_refused(self):
        for over in ({'main': src(PIP_SOURCE_CHANNEL, self.loose.id)}, {'main': src(PIP_SOURCE_CHANNEL, 99999)},
                     {'inset': src(PIP_SOURCE_GROUP, 99999)}, {'main': src(PIP_SOURCE_GAME, 424242)}):
            with self.assertRaises(pip.PipRefused, msg=over) as ctx:
                pip.check_sources(pip.clean(self.spec(**over)), load_config())
            self.assertEqual(ctx.exception.status, 409)

    def test_two_pictures_that_are_the_same_are_refused(self):
        same = ({'inset': src(PIP_SOURCE_CHANNEL, self.alpha.id)},
                {'main': src(PIP_SOURCE_SEARCH, query='Alp'), 'inset': src(PIP_SOURCE_SEARCH, query='aLP')})
        for over in same:
            with self.assertRaises(pip.PipRefused, msg=over) as ctx:
                pip.check_sources(pip.clean(self.spec(**over)), load_config())
            self.assertIn('same channel', ctx.exception.message)

    def test_a_game_on_now_and_the_channel_it_plays_from_are_the_same(self):
        now = datetime.utcnow()
        event = self.game(now - timedelta(minutes=5))
        make_guide_match(event, self.alpha, now - timedelta(minutes=5), title='g', reason='r')
        db.session.commit()
        cfg = load_config()
        game = pip.Source(PIP_SOURCE_GAME, event.tsdb_id)
        self.assertTrue(pip.same_now(game, pip.Source(PIP_SOURCE_CHANNEL, self.alpha.id), cfg))
        self.assertFalse(pip.same_now(game, pip.Source(PIP_SOURCE_CHANNEL, self.beta.id), cfg))
        self.assertFalse(pip.same_now(game, pip.Source(PIP_SOURCE_GROUP, self.group.id), cfg))

    def test_two_games_the_same_channel_lists_at_different_times_are_two_pictures(self):
        now = datetime.utcnow()
        one, two = self.game(now + timedelta(days=1)), self.game(now + timedelta(days=2), tsdb_id=7002)
        for event in (one, two):
            make_guide_match(event, self.alpha, event.start_at, title='g', reason='r')
        db.session.commit()
        self.assertFalse(pip.same_now(pip.Source(PIP_SOURCE_GAME, one.tsdb_id),
                                      pip.Source(PIP_SOURCE_GAME, two.tsdb_id), load_config()))

    def test_swap_exchanges_the_two_pictures(self):
        row = self.make()
        pip.swap(row.id)
        db.session.expire_all()
        self.assertEqual((row.main_kind, row.main_ref), (PIP_SOURCE_GROUP, self.group.id))
        self.assertEqual((row.inset_kind, row.inset_ref), (PIP_SOURCE_CHANNEL, self.alpha.id))

    def test_clear_keeps_the_row_and_drops_the_swapped_copy(self):
        row = self.make(swapped_copy=True)
        pip.clear(row.id)
        db.session.expire_all()
        self.assertFalse(pip.is_set(row))
        self.assertFalse(row.swapped_copy)
        self.assertEqual(row.name, 'PiP 1')
        with self.assertRaises(pip.PipRefused):
            pip.swap(row.id)
        with self.assertRaises(pip.PipRefused):
            pip.set_swapped_copy(row.id, True)

    def test_a_removed_rows_id_is_never_issued_again(self):
        first = self.make()
        old = first.id
        self.assertEqual(pip.remove(old), 'PiP 1')
        self.assertIsNone(pip.remove(old))
        self.assertGreater(self.make(name='PiP 2').id, old)

    def test_a_write_to_a_row_that_is_gone_says_so(self):
        self.assertIsNone(pip.change(99, pip.clean(self.spec())))
        self.assertIsNone(pip.swap(99))
        self.assertIsNone(pip.clear(99))
        self.assertIsNone(pip.set_swapped_copy(99, True))


class CatalogTests(_Pip):
    def test_listed_after_every_other_row_each_followed_by_its_swapped_copy(self):
        one = self.make(swapped_copy=True)
        two = self.make(name='PiP 2')
        rows = feed.catalog(load_config())
        self.assertEqual([(r.name, r.num) for r in rows[-3:]],
                         [('PiP 1', 4), ('PiP 1 (swapped)', 5), ('PiP 2', 6)])
        self.assertEqual([r.feed_id for r in rows[-3:]],
                         [feed.feed_id_for_pip(one.id), feed.feed_id_for_pip(one.id, True),
                          feed.feed_id_for_pip(two.id)])
        self.assertTrue(all(r.kind == feed.KIND_PIP and r.category == 'ChannelBin' for r in rows[-3:]))

    def test_one_with_nothing_set_up_is_still_listed_with_no_swapped_copy(self):
        row = self.make(swapped_copy=True)
        pip.clear(row.id)
        self.assertEqual([r.name for r in feed.catalog(load_config()) if r.kind == feed.KIND_PIP], ['PiP 1'])

    def test_taken_off_it_and_its_copy_leave_every_answer_and_keep_their_ids(self):
        row = self.make(swapped_copy=True)
        got, moved = self.switch(feed.KIND_PIP, row.id, False)
        self.assertTrue(moved)
        self.assertFalse(got.on_output)
        self.assertEqual([r for r in feed.catalog(load_config()) if r.kind == feed.KIND_PIP], [])
        everything = [r for r in feed.catalog(load_config(), everything=True) if r.kind == feed.KIND_PIP]
        self.assertEqual([(r.feed_id, r.num, r.offered) for r in everything],
                         [(feed.feed_id_for_pip(row.id), 0, False), (feed.feed_id_for_pip(row.id, True), 0, False)])
        self.assertEqual(self.switch(feed.KIND_PIP, row.id, False)[1], False, 'a switch that did not move')
        self.assertEqual(feed.set_on_output(feed.KIND_PIP, 999, False), (None, False))

    def test_a_player_is_sent_it_with_channelbins_logo_and_a_stream_address(self):
        row = self.make()
        mine = [s for s in self.streams() if s['stream_id'] == feed.feed_id_for_pip(row.id)]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]['name'], 'PiP 1')
        self.assertTrue(mine[0]['stream_icon'].endswith(feed.OWN_LOGO_PATH))
        self.assertIn(f'/{feed.feed_id_for_pip(row.id)}.ts', self.get('/get.php').get_data(as_text=True))


class GuideTests(_Pip):
    def blocks(self, row, swapped=False, days=1):
        now = datetime(2026, 11, 22, 15, 20)
        described = pip.describe([row], now)
        return now, list(pip.guide_blocks(pip.Served(row, swapped), described, now, days, feed.UP_NEXT_BLOCK))

    def test_two_hour_blocks_on_the_even_hours_naming_both_pictures(self):
        now, blocks = self.blocks(self.make())
        self.assertEqual(blocks[0][0], datetime(2026, 11, 22, 14, 0))
        self.assertTrue(all(stop - start == timedelta(hours=2) for start, stop, _t, _d in blocks))
        self.assertGreaterEqual(blocks[-1][1], now + timedelta(days=1))
        self.assertEqual({t for _a, _b, t, _d in blocks}, {'Alpha + Game feeds'})
        _now, swapped = self.blocks(self.make(name='Other'), swapped=True)
        self.assertEqual(swapped[0][2], 'Game feeds + Alpha')

    def test_nothing_set_up_says_so(self):
        row = self.make()
        pip.clear(row.id)
        self.assertEqual(self.blocks(row)[1][0][2], 'Nothing set up yet')

    def test_two_games_close_together_are_one_program(self):
        day = datetime(2026, 11, 22)
        one = self.game(day.replace(hour=18), name='One 400')
        two = self.game(day.replace(hour=20, minute=30), tsdb_id=7002, name='Two 300')
        row = self.make(main=src(PIP_SOURCE_GAME, one.tsdb_id), inset=src(PIP_SOURCE_GAME, two.tsdb_id))
        _now, blocks = self.blocks(row)
        spans = [(start, stop) for start, stop, _t, _d in blocks]
        long = [s for s in spans if s[1] - s[0] > timedelta(hours=2)]
        self.assertEqual(len(long), 1, spans)
        self.assertEqual(long[0][0], one.start_at)
        self.assertGreater(long[0][1], two.start_at)
        for (_a, stop), (start, _b) in zip(spans, spans[1:]):
            self.assertEqual(stop, start, 'no gap and no overlap between programs')
        self.assertEqual(blocks[0][2], 'Harborview Gulls at Ironvale Bears + Harborview Gulls at Ironvale Bears')

    def test_two_games_days_apart_stay_plain_blocks(self):
        day = datetime(2026, 11, 22)
        one, two = self.game(day.replace(hour=18)), self.game(day.replace(hour=18) + timedelta(days=3), tsdb_id=7002)
        row = self.make(main=src(PIP_SOURCE_GAME, one.tsdb_id), inset=src(PIP_SOURCE_GAME, two.tsdb_id))
        _now, blocks = self.blocks(row, days=5)
        self.assertTrue(all(stop - start == timedelta(hours=2) for start, stop, _t, _d in blocks))

    def test_the_guide_a_player_downloads_carries_its_programs(self):
        row = self.make(swapped_copy=True)
        xml = self.get('/xmltv.php').get_data(as_text=True)
        for fid, title in ((feed.feed_id_for_pip(row.id), 'Alpha + Game feeds'),
                           (feed.feed_id_for_pip(row.id, True), 'Game feeds + Alpha')):
            self.assertIn(f'<channel id="{fid}">', xml)
            self.assertIn(f'channel="{fid}"><title>{title}</title>', xml)


class ResolveTests(_Pip):
    def resolve(self, row, swapped=False, **kw):
        return feed.resolve_play(feed.feed_id_for_pip(row.id, swapped), load_config(), **kw)

    def test_a_channel_plays_as_its_own_row_would(self):
        play = self.resolve(self.make())
        self.assertEqual(play.member.id, self.alpha.id)
        self.assertEqual(play.pip.pane.state, pip.PANE_LIVE)
        self.assertFalse(play.pip.swapped)
        self.assertIn('Alpha', play.label)

    def test_the_swapped_copy_plays_the_other_picture(self):
        row = self.make(swapped_copy=True)
        play = self.resolve(row, swapped=True)
        self.assertEqual(play.member.id, self.member.id)
        self.assertEqual(play.group.id, self.group.id)
        self.assertEqual(play.pip.name, 'PiP 1 (swapped)')

    def test_a_swapped_copy_that_is_not_offered_names_nothing(self):
        self.assertIsNone(self.resolve(self.make(), swapped=True))
        self.assertIsNone(feed.resolve_play(feed.feed_id_for_pip(4242), load_config()))

    def test_a_picture_may_be_a_guide_row_kept_off_the_players_list(self):
        row = self.make()
        self.switch(feed.KIND_CHANNEL, self.alpha.id, False)
        self.switch(feed.KIND_GROUP, self.group.id, False)
        self.assertEqual(self.resolve(row).member.id, self.alpha.id)
        pip.set_swapped_copy(row.id, True)
        self.assertEqual(self.resolve(row, swapped=True).member.id, self.member.id)
        self.assertIsNone(feed.resolve_play(feed.feed_id_for_channel(self.alpha.id), load_config()).member,
                          'the row itself is still refused to a player')

    def test_a_channel_that_left_the_guide_is_gone_not_a_fault(self):
        row = self.make()
        self.alpha.in_guide = False
        db.session.commit()
        play = self.resolve(row)
        self.assertIsNone(play.member)
        self.assertFalse(play.fault)
        self.assertEqual(play.pip.pane.state, pip.PANE_GONE)

    def test_a_channel_that_already_failed_is_reconnecting(self):
        play = self.resolve(self.make(), exclude_ids=frozenset({self.alpha.id}))
        self.assertIsNone(play.member)
        self.assertEqual(play.pip.pane.state, pip.PANE_RECONNECTING)

    def test_a_channel_on_an_account_the_output_may_not_use_cannot_play(self):
        row = self.make()
        self.t.sandbox_config({'integrations': {'output_feed': {
            'enabled': True, 'username': 'tv', 'password': 'sofa', 'accounts': [self.other.id]}}})
        self.assertEqual(self.resolve(row).pip.pane.state, pip.PANE_CANNOT)

    def test_a_search_plays_the_first_guide_row_named_so_in_guide_order(self):
        # Guide order is Alpha (1), Game feeds (2), Beta (3): "a" is in all three.
        row = self.make(main=src(PIP_SOURCE_SEARCH, query='A'), inset=src(PIP_SOURCE_CHANNEL, self.beta.id))
        self.assertEqual(self.resolve(row).member.id, self.alpha.id)
        play = self.resolve(row, exclude_ids=frozenset({self.alpha.id}))
        self.assertEqual(play.member.id, self.member.id, 'it walks on down the matches on a failover')

    def test_a_search_that_matches_nothing_says_so(self):
        row = self.make(main=src(PIP_SOURCE_SEARCH, query='zebra'))
        play = self.resolve(row)
        self.assertIsNone(play.member)
        self.assertEqual(play.pip.pane.state, pip.PANE_NO_MATCH)

    def test_nothing_set_up_is_a_play_with_no_pane(self):
        row = self.make()
        pip.clear(row.id)
        play = self.resolve(row)
        self.assertIsNone(play.member)
        self.assertFalse(play.pip.set_up)
        self.assertIsNone(play.pip.pane)

    def test_taken_off_is_refused_by_name_and_a_preview_plays_it(self):
        row = self.make()
        self.switch(feed.KIND_PIP, row.id, False)
        play = self.resolve(row)
        self.assertEqual((play.why_none, play.fault, play.pip), (feed.WHY_TAKEN_OFF, False, None))
        self.assertEqual(self.resolve(row, preview=True).member.id, self.alpha.id)


class GameTests(_Pip):
    def pane(self, event, **kw):
        row = self.make(main=src(PIP_SOURCE_GAME, event.tsdb_id), name=f'G{event.tsdb_id}')
        return feed.resolve_play(feed.feed_id_for_pip(row.id), load_config(), **kw)

    def test_before_its_window_it_says_when_and_where(self):
        start = datetime.utcnow() + timedelta(days=1)
        event = self.game(start)
        make_guide_match(event, self.beta, start, title='g', reason='r')
        db.session.commit()
        play = self.pane(event)
        pane = play.pip.pane
        self.assertIsNone(play.member)
        self.assertEqual((pane.state, pane.listed_on, pane.noun), (pip.PANE_BEFORE, 'Beta', 'game'))
        self.assertEqual(pane.name, 'Pretend League: Harborview Gulls at Ironvale Bears')

    def test_long_past_it_is_over(self):
        self.assertEqual(self.pane(self.game(datetime.utcnow() - timedelta(days=2))).pip.pane.state, pip.PANE_OVER)

    def test_inside_its_window_with_no_listing_it_is_not_listed(self):
        self.assertEqual(self.pane(self.game(datetime.utcnow() - timedelta(minutes=5))).pip.pane.state,
                         pip.PANE_NOT_LISTED)

    def test_on_now_it_plays_the_channel_listing_it_held_to_the_game(self):
        now = datetime.utcnow()
        event = self.game(now - timedelta(minutes=5))
        make_guide_match(event, self.beta, now - timedelta(minutes=5), title='g', reason='r')
        db.session.commit()
        play = self.pane(event)
        self.assertEqual(play.member.id, self.beta.id)
        self.assertEqual(play.event_id, event.id)
        self.assertEqual(play.pip.pane.state, pip.PANE_LIVE)

    def test_a_game_that_left_the_schedule_is_gone(self):
        event = self.game(datetime.utcnow() + timedelta(days=1))
        row = self.make(main=src(PIP_SOURCE_GAME, event.tsdb_id))
        db.session.delete(event)
        db.session.commit()
        play = feed.resolve_play(feed.feed_id_for_pip(row.id), load_config())
        self.assertEqual(play.pip.pane.state, pip.PANE_GONE)


class CardWordsTests(_Pip):
    def words(self, **kw):
        tuned = pip.Tuned(1, 'PiP 1', False, kw.pop('set_up', True), kw.pop('pane', None))
        return pip.card_words(tuned, load_config(), **kw)

    def test_nothing_set_up(self):
        state, headline, note, text = self.words(set_up=False)
        self.assertEqual((state, headline, text), (pip.PANE_EMPTY, 'PiP 1 has nothing on it', 'nothing is set up'))
        self.assertIn('IPTV Output page', note)

    def test_every_state_a_big_picture_can_be_in_has_words(self):
        now = datetime.utcnow()
        source = pip.Source(PIP_SOURCE_SEARCH, query='in-car')
        for state in (pip.PANE_BEFORE, pip.PANE_NOT_LISTED, pip.PANE_OVER, pip.PANE_GONE, pip.PANE_NO_MATCH,
                      pip.PANE_CANNOT, pip.PANE_RECONNECTING):
            pane = pip.Pane(state, source, 'The Race', start=now + timedelta(hours=3), stop=now, noun='race',
                            why='its account is switched off')
            got, headline, _note, text = self.words(pane=pane)
            self.assertEqual(got, state)
            self.assertTrue(headline and text, state)
        self.assertEqual(self.words(pane=pip.Pane(pip.PANE_OVER, source, 'R', noun='race'))[1], 'The race is over')

    def test_a_picture_held_for_a_connection_or_a_slot_is_busy(self):
        import app.sports_screen as ss
        pane = pip.Pane(pip.PANE_LIVE, None, 'Alpha')
        self.assertEqual(self.words(pane=pane, held=ss.HELD_NO_CONNECTION)[:2],
                         (pip.PANE_BUSY, 'Waiting for a connection'))
        self.assertEqual(self.words(pane=pane, held=ss.HELD_NO_SLOT, slots=2)[0], pip.PANE_BUSY)

    def test_a_state_the_big_picture_cannot_be_in_is_an_error_never_a_default(self):
        for state in (pip.PANE_LIVE, pip.PANE_SAME, pip.PANE_YIELDED, pip.PANE_NO_GPU, 'new'):
            with self.assertRaises(ValueError, msg=state):
                self.words(pane=pip.Pane(state, None, 'Alpha'))
        self.assertEqual(len(set(pip.PANE_STATES)), 14)

    def test_a_big_picture_a_recording_took_says_so_and_that_it_comes_back(self):
        state, headline, note, text = self.words(pane=pip.Pane(pip.PANE_TAKEN, None, 'Alpha',
                                                               why='recording 7 took the connection'))
        self.assertEqual((state, headline), (pip.PANE_TAKEN, 'A recording needs this connection'))
        self.assertIn('comes back by itself', note)
        self.assertIn('recording 7 took the connection', text)

    def test_the_small_window_has_a_short_form_for_every_state_and_an_error_for_an_unknown_one(self):
        from datetime import datetime, timedelta
        start = datetime.utcnow() + timedelta(hours=3)
        said = {}
        for state in pip.PANE_STATES:
            said[state] = pip.pane_words(pip.Pane(state, None, 'Alpha', start=start, noun='race', why='because'))
        self.assertEqual(said[pip.PANE_LIVE], ('', ''))
        self.assertEqual(said[pip.PANE_OVER][0], 'The race is over')
        self.assertEqual(said[pip.PANE_TAKEN][0], 'Paused for a recording')
        self.assertEqual(said[pip.PANE_YIELDED][0], 'Paused: the big picture needed its connection')
        self.assertEqual(said[pip.PANE_SAME][0], 'Same channel as the big picture')
        self.assertEqual(said[pip.PANE_RECONNECTING][0], 'Reconnecting')
        self.assertEqual(said[pip.PANE_NO_MATCH][0], 'No channel matches')
        self.assertEqual(said[pip.PANE_NOT_LISTED][0], 'Not in the guide yet')
        self.assertTrue(said[pip.PANE_BEFORE][0].startswith('Starts '))
        self.assertEqual(said[pip.PANE_CANNOT], ('Cannot play right now', 'because'))
        # Every state but playing and the one that is never drawn has words for the window.
        self.assertEqual([s for s, (window, _text) in said.items() if not window],
                         [pip.PANE_LIVE, pip.PANE_NO_GPU])
        self.assertTrue(all(text for s, (_w, text) in said.items() if s != pip.PANE_LIVE))
        with self.assertRaises(ValueError):
            pip.pane_words(pip.Pane('new', None, 'Alpha'))

    def test_the_card_is_a_screen_of_its_own_state_keyed_by_what_it_says(self):
        import app.sports_screen as ss
        one = pip.card_for(pip.Tuned(1, 'PiP 1', False, False), load_config())
        two = pip.card_for(pip.Tuned(1, 'PiP 2', False, False), load_config())
        self.assertEqual(one.screen.state, ss.STATE_CARD)
        self.assertNotEqual(ss.clip_key(one.screen), ss.clip_key(two.screen))
        self.assertEqual(ss.clip_path(load_config(), ss.pip_owner(3), 'a' * 20).rsplit('/', 1)[1],
                         'p3-' + 'a' * 20 + '.ts')


class RouteTests(_Pip):
    def post(self, path, body=None):
        return self.client.post(path, json=body or {})

    def test_create_change_and_the_json_envelope(self):
        resp = self.post('/api/iptv-output/pip', self.spec(swapped_copy=True))
        made = resp.get_json()
        self.assertEqual(resp.status_code, 200, made)
        self.assertEqual((made['success'], made['name'], made['num'], made['swapped_num']), (True, 'PiP 1', 4, 5))
        changed = self.post(f'/api/iptv-output/pip/{made["id"]}', self.spec(name='Race day')).get_json()
        self.assertEqual((changed['name'], changed['kept'], changed['swapped_num']), ('Race day', 0, 0))
        self.assertEqual(db.session.get(PipChannel, made['id']).name, 'Race day')

    def test_a_malformed_body_is_a_400_on_every_route_that_reads_one(self):
        """dev/docs/BUGS.md 2026-11-23 @ 10:14:38 PM."""
        for body in ([1], self.spec(size=[]), self.spec(main=src(PIP_SOURCE_CHANNEL, 2 ** 63))):
            resp = self.client.post('/api/iptv-output/pip', json=body)
            self.assertEqual(resp.status_code, 400, body)
            self.assertIn('error', resp.get_json())
        self.assertEqual(PipChannel.query.count(), 0)
        resp = self.client.post('/api/iptv-output/pip/would-play', json=[1])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['seats'], '')

    def test_a_refused_create_names_why_and_makes_nothing(self):
        resp = self.post('/api/iptv-output/pip', self.spec(inset=src(PIP_SOURCE_CHANNEL, self.alpha.id)))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('same channel', resp.get_json()['error'])
        self.assertEqual(PipChannel.query.count(), 0)

    def test_swap_clear_swapped_copy_and_remove(self):
        row = self.make()
        self.assertTrue(self.post(f'/api/iptv-output/pip/{row.id}/swapped-copy', {'on': True}).get_json()['success'])
        self.assertEqual(self.post(f'/api/iptv-output/pip/{row.id}/swapped-copy', {}).status_code, 400)
        self.assertTrue(self.post(f'/api/iptv-output/pip/{row.id}/swap').get_json()['success'])
        cleared = self.post(f'/api/iptv-output/pip/{row.id}/clear').get_json()
        self.assertEqual((cleared['cut'], cleared['moved']), (0, 0))
        self.assertEqual(self.post(f'/api/iptv-output/pip/{row.id}/swap').status_code, 409)
        self.assertTrue(self.client.delete(f'/api/iptv-output/pip/{row.id}').get_json()['success'])
        for resp in (self.client.delete(f'/api/iptv-output/pip/{row.id}'),
                     self.post(f'/api/iptv-output/pip/{row.id}/swap'),
                     self.post(f'/api/iptv-output/pip/{row.id}/clear'),
                     self.post(f'/api/iptv-output/pip/{row.id}', self.spec())):
            self.assertEqual(resp.status_code, 404)
            self.assertIn('no longer exists', resp.get_json()['error'])

    def test_the_channels_switch_takes_it_off_and_a_swapped_copy_has_none(self):
        row = self.make(swapped_copy=True)
        s = self.session(feed.feed_id_for_pip(row.id, True))
        s.pip_row_id = row.id
        resp = self.post(f'/api/iptv-output/rows/{feed.feed_id_for_pip(row.id, True)}', {'on': False})
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(s.live)
        done = self.post(f'/api/iptv-output/rows/{feed.feed_id_for_pip(row.id)}', {'on': False}).get_json()
        self.assertEqual((done['on'], done['cut']), (False, 1), 'its swapped copy goes with it')
        self.assertEqual(s.reason, feed.REASON_TAKEN_OFF)
        self.assertFalse(db.session.get(PipChannel, row.id).on_output)

    def test_turning_the_swapped_copy_off_cuts_only_its_plays(self):
        row = self.make(swapped_copy=True)
        plain, twin = self.session(feed.feed_id_for_pip(row.id)), self.session(feed.feed_id_for_pip(row.id, True))
        plain.pip_row_id = twin.pip_row_id = row.id
        done = self.post(f'/api/iptv-output/pip/{row.id}/swapped-copy', {'on': False}).get_json()
        self.assertEqual(done['cut'], 1)
        self.assertTrue(plain.live)
        self.assertEqual(twin.reason, feed.REASON_TAKEN_OFF)

    def test_a_change_leaves_a_play_on_a_picture_alone_and_says_how_many(self):
        row = self.make()
        s = self.session(feed.feed_id_for_pip(row.id))
        s.pip_row_id = row.id
        changed = self.post(f'/api/iptv-output/pip/{row.id}', self.spec(name='Later')).get_json()
        self.assertEqual(changed['kept'], 1)
        self.assertTrue(s.live)

    def test_the_pickers_list_the_guide_in_its_order_and_the_coming_games(self):
        now = datetime.utcnow()
        soon = self.game(now + timedelta(hours=5))
        self.game(now - timedelta(days=3), tsdb_id=7002)
        make_guide_match(soon, self.beta, soon.start_at, title='g', reason='r')
        db.session.commit()
        data = self.client.get('/api/iptv-output/pip/pickers').get_json()
        self.assertEqual([(c['kind'], c['name']) for c in data['channels']],
                         [('channel', 'Alpha'), ('group', 'Game feeds'), ('channel', 'Beta')])
        self.assertEqual(data['channels'][1]['sub'], 'Channel group, 1 channel')
        self.assertEqual([g['ref'] for g in data['games']], [7001], 'a game long over is not offered')
        self.assertIn('on Beta', data['games'][0]['sub'])
        self.assertFalse(data['games'][0]['live'])


class PageTests(_Pip):
    def page(self):
        resp = self.client.get('/iptv-output')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_the_card_lists_each_channel_once_with_both_pictures(self):
        row = self.make(swapped_copy=True, inset=src(PIP_SOURCE_SEARCH, query='bet'))
        html = self.page()
        self.assertEqual(html.count(f'data-pip="{row.id}"'), 1)
        card = html[html.index('id="of-pip"'):html.index('id="of-channels"')]
        for said in ('PiP 1', 'Alpha', 'Channels named &#34;bet&#34;', 'A search: 1 channel matches now',
                     'Small picture bottom right, medium', 'data-act="pip-twin"', 'Swap the two pictures'):
            self.assertIn(said, card)
        self.assertIn('left:', card, 'the stand-in picture has its small window placed')

    def test_the_channels_card_says_what_each_picture_would_play_from(self):
        row = self.make(swapped_copy=True)
        html = self.page()
        chans = html[html.index('id="of-channels"'):html.index('id="of-recent"')]
        self.assertIn('Picture in picture, swapped', chans)
        tr = chans[chans.index(f'data-feed="{feed.feed_id_for_pip(row.id)}"'):]
        tr = tr[:tr.index('</tr>')]
        self.assertIn('>Big</span>', tr)
        self.assertIn('>Alpha</a>', tr)
        self.assertIn('>Member</a>', tr, 'a group names the channel it would start from')

    def test_none_yet_and_a_channel_with_nothing_set_up(self):
        self.assertIn('None yet. Create a PiP channel', self.page())
        row = self.make()
        pip.clear(row.id)
        html = self.page()
        self.assertIn('Nothing set up yet', html)
        self.assertIn('data-act="pip-setup">Set up</button>', html)

    def test_a_play_on_its_card_shows_on_watching_now_as_a_card(self):
        row = self.make()
        s = self.session(feed.feed_id_for_pip(row.id))
        s.pip_row_id, s.state = row.id, feed.STATE_CARD
        s.channel_id, s.account_id, s.card_text = None, None, 'no channel matches its search'
        html = self.page()
        watching = html[html.index('id="of-watching"'):html.index('id="of-pip"')]
        self.assertIn('Its card: No channel matches its search', watching)
        self.assertIn('>Card</span>', watching)
        self.assertIn('Not counted against the limit', watching)
        self.assertFalse(s.counted)


class M145StepTests(unittest.TestCase):
    """Migration 145 creates pip_channels with AUTOINCREMENT."""

    def test_creates_the_table_and_is_rerunnable(self):
        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
            cur = conn.cursor()
            M._m145_pip_channels(conn, cur)
            M._m145_pip_channels(conn, cur)
            sql = cur.execute("SELECT sql FROM sqlite_master WHERE name = 'pip_channels'").fetchone()[0]
            self.assertIn('AUTOINCREMENT', sql)
            cur.execute("INSERT INTO pip_channels (name) VALUES ('x')")
            self.assertEqual(cur.execute('SELECT layout, corner, size, swapped_copy, on_output FROM pip_channels')
                             .fetchone(), ('corner', 'br', 'm', 0, 1))
            conn.close()

    def test_registered_as_migration_145(self):
        self.assertIn((145, M._m145_pip_channels), [(v, fn) for v, _d, fn in M.SCHEMA_MIGRATIONS])


if __name__ == '__main__':
    unittest.main()
