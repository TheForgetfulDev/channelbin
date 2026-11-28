"""The IPTV output's arrangement: categories with fixed ids, and the one function that applies
what is stored to the channel list (app/output_arrangement.py, dev/docs/DESIGN-output-feed.md 16,
dev/changelog/1797).

  python3 -m unittest tests.test_output_arrangement
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.iocount import IOCounter, all_engines  # noqa: E402
from tests.support.output_feed import add_login  # noqa: E402
from tests.support.seed import make_account, make_channel, make_group  # noqa: E402

import app.output_feed as feed  # noqa: E402
from app import db, output_arrangement  # noqa: E402
from app.database import (OUTPUT_BUILTIN_UNCATEGORIZED, OUTPUT_ITEM_FEED,  # noqa: E402
                          OutputCategory, OutputFeedLogin, OutputItem)

USER, PASS = 'tv', 'tv-secret'
FEED_ON = {'integrations': {'output_feed': {'enabled': True, 'reserve_seconds': 0}}}


class _ArrangeCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.client
        self.addCleanup(self.t.cleanup)
        self.addCleanup(feed.stop_all)
        self.t.sandbox_config(FEED_ON)
        self.login = add_login(USER, PASS)
        self.account = make_account('Acct')
        db.session.commit()

    def channel(self, name, category, order):
        ch = make_channel(self.account, name=name, in_guide=True, category_name=category)
        ch.guide_sort_order = order
        db.session.commit()
        return ch

    def get(self, path, username=USER, password=PASS, **params):
        query = '&'.join(f'{k}={v}' for k, v in dict(params, username=username, password=password).items())
        return self.client.get(f'{path}?{query}')

    def streams(self, **kw):
        return self.get('/player_api.php', action='get_live_streams', **kw).get_json()

    def categories(self, **kw):
        return self.get('/player_api.php', action='get_live_categories', **kw).get_json()

    def category_ids(self):
        return {c.source_name: c.id for c in OutputCategory.query.all()}


class SameOutputAsBeforeTests(_ArrangeCase):
    """With nothing arranged, all four answers come out as they did before the arrangement
    existed, apart from the category ids, which are now stored row ids."""

    def setUp(self):
        super().setUp()
        self.a = self.channel('Alpha', 'News', 1)
        self.b = self.channel('Bravo', 'News', 2)
        self.c = self.channel('Charlie', 'Sports', 3)
        self.d = self.channel('Delta', '', 4)
        output_arrangement.reconcile()
        feed.forget_answers()

    def test_streams_numbers_names_and_category_ids(self):
        ids = self.category_ids()
        got = [(s['num'], s['name'], s['stream_id'], s['category_id'], s['category_ids'])
               for s in self.streams()]
        self.assertEqual(got, [
            (1, 'Alpha', feed.feed_id_for_channel(self.a.id), str(ids['News']), [ids['News']]),
            (2, 'Bravo', feed.feed_id_for_channel(self.b.id), str(ids['News']), [ids['News']]),
            (3, 'Charlie', feed.feed_id_for_channel(self.c.id), str(ids['Sports']), [ids['Sports']]),
            (4, 'Delta', feed.feed_id_for_channel(self.d.id), str(ids[feed.UNCATEGORIZED]),
             [ids[feed.UNCATEGORIZED]]),
        ])

    def test_categories_in_first_seen_order_with_stored_ids(self):
        ids = self.category_ids()
        self.assertEqual(self.categories(), [
            {'category_id': str(ids['News']), 'category_name': 'News', 'parent_id': 0},
            {'category_id': str(ids['Sports']), 'category_name': 'Sports', 'parent_id': 0},
            {'category_id': str(ids[feed.UNCATEGORIZED]), 'category_name': feed.UNCATEGORIZED,
             'parent_id': 0},
        ])

    def test_playlist_and_guide_channels(self):
        m3u = self.get('/get.php', type='m3u_plus').get_data(as_text=True)
        titles = [line.split('group-title="')[1] for line in m3u.splitlines() if line.startswith('#EXTINF')]
        self.assertEqual(titles, ['News",Alpha', 'News",Bravo', 'Sports",Charlie',
                                  f'{feed.UNCATEGORIZED}",Delta'])
        xml = self.get('/xmltv.php').get_data(as_text=True)
        names = [part.split('</display-name>')[0] for part in xml.split('<display-name>')[1:]]
        self.assertEqual(names, ['Alpha', 'Bravo', 'Charlie', 'Delta'])

    def test_a_category_the_guide_order_splits_is_brought_together_once(self):
        """DESIGN-output-feed.md 16.1: the arrangement is categories holding channels, so a
        channel of an earlier category later in the guide joins its category."""
        echo = self.channel('Echo', 'News', 5)
        output_arrangement.reconcile()
        feed.forget_answers()
        names = [(s['num'], s['name']) for s in self.streams()]
        self.assertEqual(names, [(1, 'Alpha'), (2, 'Bravo'), (3, 'Echo'), (4, 'Charlie'), (5, 'Delta')])
        self.assertTrue(db.session.get(OutputItem, OutputItem.query.filter_by(
            item_kind=OUTPUT_ITEM_FEED, item_id=feed.feed_id_for_channel(echo.id)).one().id).placed_auto)


class FixedIdTests(_ArrangeCase):
    def setUp(self):
        super().setUp()
        self.news = self.channel('Alpha', 'News', 1)
        self.sport = self.channel('Charlie', 'Sports', 2)
        output_arrangement.reconcile()
        feed.forget_answers()

    def test_a_login_with_fewer_channels_gets_the_same_category_id(self):
        """A category's id was its place in one answer, so a login without the News channel was
        told Sports was category 1. A player that hid it by id hid something else."""
        add_login('kid', 'kid-pass', name='Kid',
                  lineup=json.dumps([feed.feed_id_for_channel(self.sport.id)]))
        feed.forget_answers()
        everyone = {c['category_name']: c['category_id'] for c in self.categories()}
        kid = {c['category_name']: c['category_id'] for c in self.categories(username='kid', password='kid-pass')}
        self.assertEqual(list(kid), ['Sports'])
        self.assertEqual(kid['Sports'], everyone['Sports'])
        self.assertEqual(self.streams(username='kid', password='kid-pass')[0]['category_id'], everyone['Sports'])

    def test_reordering_moves_categories_and_never_their_ids(self):
        before = {c['category_name']: c['category_id'] for c in self.categories()}
        news = OutputCategory.query.filter_by(source_name='News').one()
        sports = OutputCategory.query.filter_by(source_name='Sports').one()
        news.position, sports.position = sports.position, news.position
        db.session.commit()
        feed.forget_answers()
        after = self.categories()
        self.assertEqual([c['category_name'] for c in after], ['Sports', 'News'])
        self.assertEqual({c['category_name']: c['category_id'] for c in after}, before)
        self.assertEqual([s['name'] for s in self.streams()], ['Charlie', 'Alpha'])
        self.assertEqual([s['num'] for s in self.streams()], [1, 2])

    def test_a_guide_reorder_moves_nothing_on_the_output(self):
        self.news.guide_sort_order, self.sport.guide_sort_order = 9, 1
        db.session.commit()
        feed.forget_answers()
        self.assertEqual([s['name'] for s in self.streams()], ['Alpha', 'Charlie'])


class AGetNeverWritesTests(_ArrangeCase):
    def test_an_unrecorded_row_is_listed_unkept_and_named_and_nothing_is_written(self):
        """DESIGN-output-feed.md 16.4: a channel no write path recorded is still listed, at the
        end of the category it arrives in, under the id the next reconcile() gives that
        category; the GET writes nothing and keeps nothing, and the log says a write path missed
        it."""
        self.channel('Alpha', 'News', 1)
        output_arrangement.reconcile()
        later = self.channel('Zulu', 'Movies', 2)
        feed.forget_answers()
        rows_before = (OutputCategory.query.count(), OutputItem.query.count())
        with self.assertLogs('app.output_arrangement', level='WARNING') as logs:
            got = self.streams()
        self.assertIn(f'Zulu ({feed.feed_id_for_channel(later.id)})', logs.output[0])
        self.assertIn('a write path missed', logs.output[0])
        db.session.remove()
        self.assertEqual((OutputCategory.query.count(), OutputItem.query.count()), rows_before)
        self.assertEqual([s['name'] for s in got], ['Alpha', 'Zulu'])
        self.assertEqual(feed._answers, {}, 'an answer built around an unrecorded row is not kept')
        provisional = got[1]['category_id']
        output_arrangement.reconcile()
        self.assertEqual(str(OutputCategory.query.filter_by(source_name='Movies').one().id), provisional)
        feed.forget_answers()
        self.streams()
        self.assertTrue(feed._answers, 'once recorded, the answer is kept again')

    def test_a_write_request_records_what_it_added(self):
        """The after-request hook runs reconcile() after every successful write, so a channel
        put in the guide by any route has its place before the next player asks."""
        self.channel('Alpha', 'News', 1)
        self.assertEqual(OutputItem.query.count(), 0)
        resp = self.client.post('/api/iptv-output/logins', json={'name': 'Other', 'username': 'other', 'password': 'other-pass'})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        db.session.remove()
        self.assertEqual(OutputItem.query.count(), 1)
        self.assertEqual([c.source_name for c in OutputCategory.query.all()], ['News'])

    def test_the_catalogs_query_count_does_not_grow_with_its_rows(self):
        self.channel('One', 'News', 1)
        output_arrangement.reconcile()
        with IOCounter(all_engines()) as small:
            feed.catalog()
        for n in range(2, 30):
            self.channel(f'Ch {n}', f'Cat {n % 7}', n)
        output_arrangement.reconcile()
        with IOCounter(all_engines()) as large:
            feed.catalog()
        self.assertEqual(small.queries, large.queries)

    def test_reconcile_and_an_arrangement_change_end_no_play(self):
        """DESIGN-output-feed.md 16.8: an arrangement change moves no stream. Nothing on the
        path ends a play or moves the counts a live play is checked against."""
        from unittest import mock
        self.channel('Alpha', 'News', 1)
        before = feed._login_generation()
        with mock.patch.object(feed, '_finish') as finish, mock.patch.object(feed, 'stop_login') as stop:
            output_arrangement.reconcile()
            cat = OutputCategory.query.one()
            cat.position = 5
            db.session.commit()
            self.client.post('/api/iptv-output/logins', json={'name': 'Other', 'username': 'other',
                                                              'password': 'other-pass'})
            self.streams()
        finish.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(feed._login_generation()[0], before[0])


class WhereSomethingNewLandsTests(_ArrangeCase):
    def test_a_provider_renaming_a_category_moves_nothing(self):
        """DESIGN-output-feed.md 16.2: an item's category is fixed when it is placed. A channel
        added after the rename arrives in a new category at the end, placed automatically."""
        ch = self.channel('Alpha', 'US Sports', 1)
        output_arrangement.reconcile()
        old_id = OutputCategory.query.one().id
        ch.category_name = 'USA Sports'
        db.session.commit()
        output_arrangement.reconcile()
        self.assertEqual(OutputCategory.query.count(), 1, 'a placed channel makes no category of the new name')
        feed.forget_answers()
        self.assertEqual(self.categories(), [{'category_id': str(old_id), 'category_name': 'US Sports',
                                              'parent_id': 0}])
        self.channel('Bravo', 'USA Sports', 2)
        output_arrangement.reconcile()
        feed.forget_answers()
        self.assertEqual([c['category_name'] for c in self.categories()], ['US Sports', 'USA Sports'])
        self.assertTrue(OutputCategory.query.filter_by(source_name='USA Sports').one().placed_auto)

    def test_the_identity_is_the_name_normalized(self):
        self.channel('Alpha', 'News', 1)
        self.channel('Bravo', '  news ', 2)
        self.channel('Charlie', 'NEWS', 3)
        output_arrangement.reconcile()
        self.assertEqual(OutputCategory.query.count(), 1)
        feed.forget_answers()
        self.assertEqual([c['category_name'] for c in self.categories()], ['News'])

    def test_the_uncategorized_row_is_marked_built_in(self):
        self.channel('Alpha', '', 1)
        output_arrangement.reconcile()
        self.assertEqual(OutputCategory.query.one().builtin, OUTPUT_BUILTIN_UNCATEGORIZED)

    def test_a_group_arrives_in_its_first_members_category_and_stays_there(self):
        """The group's folder no longer follows whichever member plays now (16.2)."""
        one = make_channel(self.account, name='One', category_name='Movies')
        two = make_channel(self.account, name='Two', category_name='Kids')
        group = make_group('Films', members=[one, two])
        db.session.commit()
        output_arrangement.reconcile()
        item = OutputItem.query.filter_by(item_id=feed.feed_id_for_group(group.id)).one()
        self.assertEqual(db.session.get(OutputCategory, item.category_id).source_name, 'Movies')
        # Pinning the other member's listings, which is the member a group's row used to take
        # its category from, moves nothing.
        group.guide_listings_channel_id = two.id
        db.session.commit()
        output_arrangement.reconcile()
        feed.forget_answers()
        row = next(s for s in self.streams() if s['stream_id'] == feed.feed_id_for_group(group.id))
        self.assertEqual(row['category_id'], str(item.category_id))
        self.assertEqual([c['category_name'] for c in self.categories()], ['Movies'])


class TeardownTests(_ArrangeCase):
    """An item never outlives the row it names: channel and group ids are reused (16.1)."""

    def _item(self, feed_id):
        return OutputItem.query.filter_by(item_kind=OUTPUT_ITEM_FEED, item_id=feed_id).first()

    def test_deleting_a_channel_takes_its_item_and_its_place_in_every_lineup(self):
        from app.database import Channel, detach_channel_references
        keep = self.channel('Alpha', 'News', 1)
        gone = self.channel('Bravo', 'News', 2)
        fid, keep_fid = feed.feed_id_for_channel(gone.id), feed.feed_id_for_channel(keep.id)
        only = add_login('only', 'only-pass', lineup=json.dumps([keep_fid, fid]))
        except_ = add_login('except', 'except-pass', lineup_except=json.dumps([fid]))
        output_arrangement.reconcile()
        self.assertIsNotNone(self._item(fid))
        detach_channel_references([gone.id])
        db.session.delete(db.session.get(Channel, gone.id))
        db.session.commit()
        self.assertIsNone(self._item(fid))
        self.assertIsNotNone(self._item(keep_fid))
        self.assertEqual(json.loads(db.session.get(OutputFeedLogin, only.id).lineup), [keep_fid])
        self.assertIsNone(db.session.get(OutputFeedLogin, except_.id).lineup_except)

    def test_deleting_a_group_takes_its_item_and_its_place_in_every_lineup(self):
        """Called directly rather than through the route, whose after-request reconcile()
        would delete the item anyway."""
        from app.routes.channel_groups import dissolve_group
        one = make_channel(self.account, name='One', category_name='Movies')
        group = make_group('Films', members=[one])
        db.session.commit()
        fid = feed.feed_id_for_group(group.id)
        login_id = add_login('only', 'only-pass', lineup=json.dumps([fid])).id
        output_arrangement.reconcile()
        self.assertIsNotNone(self._item(fid))
        with self.t.app.test_request_context():
            dissolve_group(group.id)
        db.session.remove()
        self.assertIsNone(self._item(fid))
        self.assertEqual(json.loads(db.session.get(OutputFeedLogin, login_id).lineup), [])

    def test_removing_a_picture_in_picture_channel_takes_its_item(self):
        from app import pip_channels
        from app.database import PipChannel
        row = PipChannel(name='Two games')
        db.session.add(row)
        db.session.commit()
        output_arrangement.reconcile()
        fid = feed.feed_id_for_pip(row.id)
        self.assertIsNotNone(self._item(fid))
        pip_channels.remove(row.id)
        self.assertIsNone(self._item(fid))

    def test_deleting_a_league_takes_its_items(self):
        from app import sports_data
        from app.database import (OUTPUT_ITEM_LEAGUE, SportsFeedChannel, SportsFollow,
                                  SportsLeague, SportsUpNextChannel)
        league = SportsLeague(tsdb_id=4380, name='NHL', sport='Ice Hockey', feed_league_channels=True)
        db.session.add(league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0))
        db.session.add(SportsFeedChannel(league_id=league.id, team_tsdb_id=0, number=1))
        up_next = SportsUpNextChannel(league_id=league.id, enabled=True)
        db.session.add(up_next)
        db.session.commit()
        output_arrangement.reconcile()
        self.assertEqual(OutputItem.query.filter_by(item_kind=OUTPUT_ITEM_LEAGUE, item_id=league.id).count(), 1)
        self.assertIsNotNone(self._item(feed.feed_id_for_up_next(up_next.id)))
        self.assertTrue(sports_data.delete_league(league.id))
        # The Up next channel for everything followed has no league and stays.
        self.assertEqual([(i.item_kind, i.item_id) for i in OutputItem.query.all()],
                         [(OUTPUT_ITEM_FEED, feed.feed_id_for_up_next(0))])

    def test_reconcile_deletes_an_item_whose_row_went_some_other_way(self):
        from app.database import Channel
        ch = self.channel('Alpha', 'News', 1)
        output_arrangement.reconcile()
        db.session.delete(db.session.get(Channel, ch.id))
        db.session.commit()
        output_arrangement.reconcile()
        self.assertEqual(OutputItem.query.count(), 0)
        self.assertEqual(OutputCategory.query.count(), 1, 'a category stays, so its id comes back')


class CarryTests(_ArrangeCase):
    def test_a_guide_row_moved_to_another_channel_keeps_its_place(self):
        """A replacement, a Re-point or a duplicate removed with transfer moves the guide row
        to another channel (channel_successors.replace()); the new one takes the old one's
        place, so the player's channel number and folder hold."""
        from app import channel_successors
        from app.database import Channel
        first = self.channel('Alpha', 'News', 1)
        old = self.channel('Bravo', 'News', 2)
        self.channel('Charlie', 'Sports', 3)
        new = make_channel(self.account, name='Bravo again', category_name='Other')
        db.session.commit()
        output_arrangement.reconcile()
        place = OutputItem.query.filter_by(item_id=feed.feed_id_for_channel(old.id)).one().id
        channel_successors.replace(db.session.get(Channel, old.id), db.session.get(Channel, new.id),
                                   channel_successors.DEDUP_PIECES, channel_successors.SURFACE_DEDUP, cfg={})
        db.session.commit()
        output_arrangement.reconcile()
        feed.forget_answers()
        got = [(s['num'], s['name'], s['category_id']) for s in self.streams()]
        news = str(OutputCategory.query.filter_by(source_name='News').one().id)
        self.assertEqual(got[:2], [(1, 'Alpha', news), (2, 'Bravo again', news)])
        self.assertIsNone(OutputItem.query.filter_by(item_id=feed.feed_id_for_channel(old.id)).first())
        self.assertEqual(OutputItem.query.filter_by(item_id=feed.feed_id_for_channel(new.id)).one().id, place)
        self.assertIsNotNone(first)


class SportsPlacementTests(_ArrangeCase):
    def test_the_placement_pass_records_a_leagues_numbered_channels(self):
        """A league's numbered channels are made by the sports worker with no request behind it,
        so its item is recorded at the end of that pass (DESIGN-output-feed.md 16.4)."""
        from datetime import datetime, timedelta

        from app import sports_feed
        from app.database import OUTPUT_ITEM_LEAGUE, SportsFeedChannel, SportsFollow, SportsLeague
        league = SportsLeague(tsdb_id=4380, name='NHL', sport='Ice Hockey', feed_league_channels=True)
        db.session.add(league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0))
        db.session.add(SportsFeedChannel(league_id=league.id, team_tsdb_id=0, number=1))
        db.session.commit()
        sports_feed.place_all_guarded(datetime.utcnow(), timedelta(days=3))
        self.assertEqual(OutputItem.query.filter_by(item_kind=OUTPUT_ITEM_LEAGUE, item_id=league.id).count(), 1)


class BackfillTests(_ArrangeCase):
    def _register(self):
        from sqlalchemy import text

        from app.migrations import _BACKFILL_LEDGER_DDL, _BF_OUTPUT_ARRANGEMENT
        db.session.execute(text(_BACKFILL_LEDGER_DDL))
        db.session.execute(text('INSERT OR IGNORE INTO migration_backfills (name, registered_at) '
                                "VALUES (:n, '2026-11-28 00:00:00')"), {'n': _BF_OUTPUT_ARRANGEMENT})
        db.session.commit()

    def test_the_first_arrangement_is_todays_order_and_nothing_is_marked(self):
        from app.migrations import _BF_OUTPUT_ARRANGEMENT, obligation_pending
        self.channel('Alpha', 'News', 1)
        self.channel('Charlie', 'Sports', 2)
        self.channel('Bravo', 'News', 3)
        self._register()
        output_arrangement.backfill(None)
        self.assertFalse(obligation_pending(_BF_OUTPUT_ARRANGEMENT))
        cats = OutputCategory.query.order_by(OutputCategory.position).all()
        self.assertEqual([c.source_name for c in cats], ['News', 'Sports'])
        self.assertFalse(any(c.placed_auto for c in cats))
        self.assertFalse(any(i.placed_auto for i in OutputItem.query.all()))
        feed.forget_answers()
        self.assertEqual([s['name'] for s in self.streams()], ['Alpha', 'Bravo', 'Charlie'])
        later = self.channel('Delta', 'Movies', 4)
        output_arrangement.backfill(None)
        self.assertEqual(OutputCategory.query.count(), 2, 'a finished backfill does not run again')
        output_arrangement.reconcile()
        self.assertTrue(OutputItem.query.filter_by(item_id=feed.feed_id_for_channel(later.id)).one().placed_auto,
                        'what came after the backfill is placed automatically, and says so')


if __name__ == '__main__':
    unittest.main()
