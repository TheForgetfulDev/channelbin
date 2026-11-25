"""EPG sources at the provider level (DESIGN-epg-sources.md §15, dev/changelog/1721).

A provider may own url sources and has an ordered list with one "Provider's EPG" slot that
each account's own guide fills. An account on a provider follows that list or keeps its own
order; epg_source_subscriptions stays the one thing every resolver reads. These tests pin:

  - MigrationTests: step 148 rebuilds epg_sources into the fresh shape, gives every provider
    [Provider's EPG], marks an M3U account's own guide, and sets follow or own so that no
    account's subscriptions move; a re-run changes nothing.
  - OrderTests: the pure order functions and own_guide_ids().
  - JoinLeaveTests: joining follows the provider's list with what the database holds copied
    at once; leaving stops using the provider's sources and re-resolves the channels.
  - FollowOwnTests: what still counts as following, what gives an account its own order
    (refused without a confirmation, in the function and in the route), and Follow again.
  - ProviderSourceTests: a provider's source survives deleting an account; adding one, moving
    an account's source to the provider and back, and reordering the list.
  - DeleteProviderTests: the keep-sources choice is required, kept sources go to the lowest
    reader and no guide changes, unkept ones are deleted.

No network: every feed is a local byte string, and the moves are asserted to fetch nothing.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app import account_links as links  # noqa: E402
from app import migrations as M  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.database import (Account, Channel, EPGEntry, EpgAlternateEntry, EpgSource,  # noqa: E402
                          EpgSourceSubscription, ProviderEpgOrder, EPG_ORDER_FOLLOW,
                          EPG_ORDER_OWN, PROVIDER_ORDER_OWN_GUIDE, PROVIDER_ORDER_SOURCE)
from app import epg_sources as es  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_epg_source  # noqa: E402
from tests.test_epg_sources import BANNER, CFG, _schedule, _xmltv  # noqa: E402

FULL = ['Full News', 'Full Film', 'Full Sport']


class _Fixture(unittest.TestCase):
    """Two M3U accounts, Alpha and Beta, each with its own guide. Alpha also owns `full`, a
    url source after its own guide, whose file lists `a.test` and `b.test` - Alpha's channel
    carries `a.test`, Beta's carries `b.test`. Alpha's own guide has only a one-program
    banner for `a.test`; Beta's own guide has nothing for `b.test`."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.app.test_client()
        self.alpha = seed.make_account(name='Alpha')
        self.aguide = make_epg_source(self.alpha)
        self.beta = seed.make_account(name='Beta')
        self.bguide = make_epg_source(self.beta)
        self.full = EpgSource(kind='url', owner_account_id=self.alpha.id, name='Full',
                              url='http://full.test/epg.xml')
        db.session.add(self.full)
        db.session.flush()
        db.session.add(EpgSourceSubscription(source_id=self.full.id, account_id=self.alpha.id,
                                             priority=2))
        self.ach = seed.make_channel(self.alpha, name='Alpha A', epg_channel_id='a.test')
        self.ach_b = seed.make_channel(self.alpha, name='Alpha B', epg_channel_id='b.test')
        self.bch = seed.make_channel(self.beta, name='Beta B', epg_channel_id='b.test')
        db.session.commit()
        import_source(self.aguide, _xmltv(_schedule('a.test', BANNER)), epg_days=3, cfg=CFG)
        import_source(self.full, _xmltv(_schedule('a.test', FULL) + _schedule('b.test', FULL)),
                      epg_days=3, cfg=CFG)
        self.ids = {'alpha': self.alpha.id, 'beta': self.beta.id, 'aguide': self.aguide.id,
                    'bguide': self.bguide.id, 'full': self.full.id}

    def tearDown(self):
        self.t.cleanup()

    def _order(self, account_id):
        db.session.expire_all()
        return es.subscriptions_for([account_id])[account_id]

    def _mode(self, account_id):
        db.session.expire_all()
        return db.session.get(Account, account_id).epg_order_mode

    def _winner(self, ch_id):
        db.session.expire_all()
        return db.session.get(Channel, ch_id).epg_source_id

    def _rows(self, ch_id, source_id):
        """(active, alternate) listing counts for a channel from one source."""
        return (EPGEntry.query.filter_by(channel_id=ch_id, source_id=source_id).count(),
                EpgAlternateEntry.query.filter_by(channel_id=ch_id, source_id=source_id).count())

    def _provider(self, *account_ids, name='Strong'):
        provider, _ = links.create_provider(name, list(account_ids), case_sensitive=False)
        return provider.id

    def _assert_one_timeline(self):
        """No channel holds a source's listings in both tables, or twice in one: a doubled
        program at a seam is the defect every move here could make."""
        for model in (EPGEntry, EpgAlternateEntry):
            dup = db.session.query(model.channel_id, model.start_time, model.source_id,
                                   db.func.count()).group_by(
                model.channel_id, model.start_time, model.source_id).having(
                db.func.count() > 1).all()
            self.assertEqual(dup, [], f'{model.__tablename__} holds a listing twice')
        both = db.session.query(EPGEntry.channel_id, EPGEntry.source_id).join(
            EpgAlternateEntry, db.and_(EpgAlternateEntry.channel_id == EPGEntry.channel_id,
                                       EpgAlternateEntry.source_id == EPGEntry.source_id)).all()
        self.assertEqual(both, [], 'a channel holds one source in both tables')


# ── Migration 148 ────────────────────────────────────────────────────────────────────────

_OLD_EPG_SOURCES = (
    'CREATE TABLE epg_sources (\n\tid INTEGER NOT NULL, \n\tkind VARCHAR(16) NOT NULL, \n'
    '\tname VARCHAR(255) NOT NULL, \n\towner_account_id INTEGER NOT NULL, \n'
    '\turl VARCHAR(2048), \n\trefresh_interval_hours INTEGER, \n'
    '\tenabled BOOLEAN DEFAULT 1 NOT NULL, \n\tlast_refresh_at DATETIME, \n'
    '\tlast_success_at DATETIME, \n\trefresh_started_at DATETIME, \n'
    '\tlast_status VARCHAR(16), \n\tlast_error TEXT, \n'
    '\tentry_count INTEGER DEFAULT 0 NOT NULL, \n\tchannel_count INTEGER DEFAULT 0 NOT NULL, \n'
    '\tactive_channel_count INTEGER DEFAULT 0 NOT NULL, \n\tcreated_at DATETIME, \n'
    '\tupdated_at DATETIME, \n\tPRIMARY KEY (id), \n'
    '\tFOREIGN KEY(owner_account_id) REFERENCES accounts (id)\n)')


class MigrationTests(unittest.TestCase):
    """A database as prod's stood before step 148: Strong with three accounts - two whose
    order starts with their own guide, one (5) with an external file first - and an M3U
    account on no provider."""

    def _fresh_epg_sources_sql(self):
        t = make_test_app()
        try:
            return db.session.execute(db.text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='epg_sources'"
            )).scalar()
        finally:
            t.cleanup()

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(os.path.join(self.td.name, 'scratch.db'))
        cur = self.cur = self.conn.cursor()
        cur.execute('CREATE TABLE providers (id INTEGER PRIMARY KEY, name TEXT)')
        cur.execute('CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, '
                    'account_type TEXT, provider_id INTEGER)')
        cur.execute(_OLD_EPG_SOURCES)
        cur.execute('CREATE INDEX ix_epg_sources_owner_account_id ON epg_sources '
                    '(owner_account_id)')
        cur.execute('CREATE TABLE epg_source_subscriptions (id INTEGER PRIMARY KEY, '
                    'source_id INTEGER, account_id INTEGER, priority INTEGER)')
        cur.execute("INSERT INTO providers VALUES (1, 'Strong')")
        cur.executemany('INSERT INTO accounts VALUES (?, ?, ?, ?)', [
            (1, 'alpha', 'm3u', None), (2, 'bravo', 'xtream', 1), (3, 'charlie', 'xtream', 1),
            (5, 'echo', 'xtream', 1)])
        for sid, kind, owner in ((1, 'url', 1), (2, 'provider', 2), (3, 'provider', 3),
                                 (4, 'provider', 5), (5, 'url', 5), (6, 'url', 5)):
            cur.execute('INSERT INTO epg_sources (id, kind, name, owner_account_id) '
                        'VALUES (?, ?, ?, ?)', (sid, kind, f's{sid}', owner))
        for aid, prio, sid in ((1, 1, 1), (2, 1, 2), (3, 1, 3), (5, 1, 5), (5, 2, 4), (5, 3, 6)):
            cur.execute('INSERT INTO epg_source_subscriptions (account_id, priority, source_id) '
                        'VALUES (?, ?, ?)', (aid, prio, sid))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def _subs(self):
        return self.cur.execute('SELECT account_id, priority, source_id FROM '
                                'epg_source_subscriptions ORDER BY account_id, priority'
                                ).fetchall()

    def test_the_rebuilt_table_reads_as_a_fresh_one_and_keeps_its_rows(self):
        M._m148_provider_epg(self.conn, self.cur)
        stored = self.cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND "
                                  "name='epg_sources'").fetchone()[0]
        self.assertEqual(stored.replace('CREATE TABLE "epg_sources"', 'CREATE TABLE epg_sources'),
                         M._EPG_SOURCES_148_DDL.replace('epg_sources_owner_new', 'epg_sources'))
        # The later steps that add to the table bring it to the model's columns, in a fresh
        # build's order (each ALTER appends, and the model declares each one last).
        M._m150_epg_source_expiry(self.conn, self.cur)
        migrated = [r[1] for r in self.cur.execute('PRAGMA table_info(epg_sources)')]
        fresh = sqlite3.connect(':memory:')
        fresh.execute(self._fresh_epg_sources_sql())
        self.assertEqual(migrated, [r[1] for r in fresh.execute('PRAGMA table_info(epg_sources)')])
        fresh.close()
        self.assertEqual(self.cur.execute('SELECT COUNT(*) FROM epg_sources').fetchone()[0], 6)
        indexes = {r[0] for r in self.cur.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='epg_sources' "
            "AND sql IS NOT NULL")}
        self.assertEqual(indexes, {'ix_epg_sources_owner_account_id',
                                   'ix_epg_sources_owner_provider_id'})
        with self.assertRaises(sqlite3.IntegrityError):
            self.cur.execute("INSERT INTO epg_sources (kind, name) VALUES ('url', 'nobody')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.cur.execute("INSERT INTO epg_sources (kind, name, owner_account_id, "
                             "owner_provider_id) VALUES ('url', 'both', 1, 1)")

    def test_no_account_order_moves_and_each_account_gets_its_mode(self):
        before = self._subs()
        M._m148_provider_epg(self.conn, self.cur)
        self.assertEqual(self._subs(), before, 'the migration changes no guide')
        modes = dict(self.cur.execute('SELECT id, epg_order_mode FROM accounts'))
        self.assertEqual(modes, {1: None, 2: EPG_ORDER_FOLLOW, 3: EPG_ORDER_FOLLOW,
                                 5: EPG_ORDER_OWN})
        self.assertEqual(self.cur.execute(
            'SELECT provider_id, position, entry, source_id FROM provider_epg_order').fetchall(),
            [(1, 1, PROVIDER_ORDER_OWN_GUIDE, None)])
        marked = [r[0] for r in self.cur.execute(
            'SELECT id FROM epg_sources WHERE is_account_guide = 1')]
        self.assertEqual(marked, [1], "the M3U account's own url source, and nothing else")

    def test_a_rerun_changes_nothing(self):
        M._m148_provider_epg(self.conn, self.cur)
        snapshot = (self._subs(),
                    self.cur.execute('SELECT * FROM provider_epg_order').fetchall(),
                    self.cur.execute('SELECT id, epg_order_mode FROM accounts').fetchall())
        # An interrupted attempt: the obligation is still pending, so the backfill re-runs.
        self.cur.execute("UPDATE migration_backfills SET completed_at = NULL WHERE name = ?",
                         (M._BF_PROVIDER_EPG_ORDER,))
        self.conn.commit()
        with self.assertLogs('app.migrations', level='WARNING'):
            M._m148_provider_epg(self.conn, self.cur)
        self.assertEqual((self._subs(),
                          self.cur.execute('SELECT * FROM provider_epg_order').fetchall(),
                          self.cur.execute('SELECT id, epg_order_mode FROM accounts').fetchall()),
                         snapshot)


# ── The order, as pure functions ─────────────────────────────────────────────────────────

class _Entry:
    def __init__(self, entry, source_id=None):
        self.entry, self.source_id = entry, source_id


class OrderTests(unittest.TestCase):

    def test_the_slot_is_the_accounts_own_guide_and_drops_out_without_one(self):
        entries = [_Entry(PROVIDER_ORDER_SOURCE, 9), _Entry(PROVIDER_ORDER_OWN_GUIDE),
                   _Entry(PROVIDER_ORDER_SOURCE, 7)]
        self.assertEqual(es.placed_by_provider(entries, 3), [9, 3, 7])
        self.assertEqual(es.placed_by_provider(entries, None), [9, 7])

    def test_following_puts_the_accounts_own_sources_after_in_their_order(self):
        self.assertEqual(es.follow_order([9, 3], [4, 3, 8]), [9, 3, 4, 8])

    def test_what_still_counts_as_following(self):
        self.assertTrue(es.still_following([9, 3], [9, 3, 8, 4]), 'own sources reordered')
        self.assertFalse(es.still_following([9, 3], [3, 9, 8]), 'the provider order moved')
        self.assertFalse(es.still_following([9, 3], [9, 8, 3]), 'an own source above it')


class OwnGuideTests(_Fixture):

    def test_a_provider_kind_source_wins_over_a_marked_url(self):
        prov = EpgSource(kind='provider', owner_account_id=self.ids['beta'], name='b guide')
        db.session.add(prov)
        db.session.commit()
        got = es.own_guide_ids([self.ids['alpha'], self.ids['beta'], 999])
        self.assertEqual(got, {self.ids['alpha']: self.ids['aguide'], self.ids['beta']: prov.id})


# ── Joining and leaving ──────────────────────────────────────────────────────────────────

class JoinLeaveTests(_Fixture):

    def test_creating_a_provider_makes_its_accounts_follow(self):
        pid = self._provider(self.ids['alpha'], self.ids['beta'])
        self.assertEqual([(e.entry, e.source_id) for e in es.provider_list(pid)],
                         [(PROVIDER_ORDER_OWN_GUIDE, None)])
        self.assertEqual(self._mode(self.ids['alpha']), EPG_ORDER_FOLLOW)
        self.assertEqual(self._order(self.ids['alpha']), [self.ids['aguide'], self.ids['full']],
                         'own guide in the slot, its own source after')

    def test_leaving_stops_using_the_providers_sources_and_keeps_its_own(self):
        pid = self._provider(self.ids['alpha'], self.ids['beta'])
        es.move_source_to_provider(self.ids['full'], pid, 24, False)
        self.assertEqual(self._winner(self.bch.id), self.ids['full'])
        dropped, change = links.move_account_provider(self.ids['beta'], None, False)
        self.assertEqual(self._order(self.ids['beta']), [self.ids['bguide']])
        self.assertIsNone(self._mode(self.ids['beta']))
        self.assertIsNone(self._winner(self.bch.id), "Beta's own guide has nothing for it")
        self.assertEqual(self._rows(self.bch.id, self.ids['full']), (0, 0))
        self.assertEqual(change.removed.affected, 1)
        self.assertEqual(change.removed.lost, 1)
        self.assertEqual(self._winner(self.ach_b.id), self.ids['full'], 'Alpha keeps it')


# ── Following and its own order ──────────────────────────────────────────────────────────

class FollowOwnTests(_Fixture):

    def setUp(self):
        super().setUp()
        self.pid = self._provider(self.ids['alpha'], self.ids['beta'])
        self.extra = EpgSource(kind='url', owner_account_id=self.ids['alpha'], name='Extra',
                               url='http://extra.test/x.xml')
        db.session.add(self.extra)
        db.session.flush()
        db.session.add(EpgSourceSubscription(source_id=self.extra.id,
                                             account_id=self.ids['alpha'], priority=3))
        db.session.commit()

    def _move(self, source_id, direction, **body):
        return self.client.post(f'/api/accounts/{self.ids["alpha"]}/epg-sources/{source_id}/move',
                                json={'direction': direction, **body})

    def test_reordering_its_own_sources_still_follows(self):
        done = es.move_source(self.ids['alpha'], self.extra.id, -1, False)
        self.assertTrue(done.moved)
        self.assertFalse(done.went_own)
        self.assertEqual(self._order(self.ids['alpha']),
                         [self.ids['aguide'], self.extra.id, self.ids['full']])
        self.assertEqual(self._mode(self.ids['alpha']), EPG_ORDER_FOLLOW)

    def test_moving_above_the_providers_list_is_refused_until_confirmed(self):
        before = self._order(self.ids['alpha'])
        with self.assertRaises(es.NeedsOwnOrder):
            es.move_source(self.ids['alpha'], self.ids['full'], -1, False)
        self.assertEqual(self._order(self.ids['alpha']), before, 'nothing moved')
        r = self._move(self.ids['full'], 'up')
        self.assertEqual(r.status_code, 409)
        self.assertIn('its own EPG order', r.get_json()['confirm_required']['message'])
        self.assertEqual(self._mode(self.ids['alpha']), EPG_ORDER_FOLLOW)
        r = self._move(self.ids['full'], 'up', confirm=True)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIn('its own EPG order now', r.get_json()['message'])
        self.assertEqual(self._mode(self.ids['alpha']), EPG_ORDER_OWN)
        self.assertEqual(self._winner(self.ach.id), self.ids['full'])
        self._assert_one_timeline()

    def test_stopping_a_placed_source_is_refused_until_confirmed(self):
        url = f'/api/accounts/{self.ids["alpha"]}/epg-sources/{self.ids["aguide"]}/stop-using'
        self.assertEqual(self.client.post(url, json={}).status_code, 409)
        self.assertIn(self.ids['aguide'], self._order(self.ids['alpha']))
        self.assertEqual(self.client.post(url, json={'confirm': True}).status_code, 200)
        self.assertEqual(self._mode(self.ids['alpha']), EPG_ORDER_OWN)
        self.assertNotIn(self.ids['aguide'], self._order(self.ids['alpha']))

    def test_follow_again_resubscribes_and_restores_the_order(self):
        es.stop_using_source(self.ids['alpha'], self.ids['aguide'], False, own_order_ok=True)
        r = self.client.post(f'/api/accounts/{self.ids["alpha"]}/epg-order/follow')
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self._mode(self.ids['alpha']), EPG_ORDER_FOLLOW)
        self.assertEqual(self._order(self.ids['alpha'])[0], self.ids['aguide'])
        self.assertEqual(self.client.post(
            f'/api/accounts/{self.ids["alpha"]}/epg-order/follow').status_code, 400)

    def test_an_own_order_account_gets_a_new_provider_source_at_the_end(self):
        es.move_source(self.ids['alpha'], self.ids['full'], -1, False, own_order_ok=True)
        before = self._order(self.ids['alpha'])
        with mock.patch('app.accounts.start_source_refresh', return_value=(False, '')) as go:
            r = self.client.post(f'/api/providers/{self.pid}/epg-sources',
                                 json={'url': 'http://new.test/x.xml', 'name': 'New',
                                       'refresh_interval_hours': 24})
        go.assert_called_once()
        self.assertEqual(r.status_code, 200, r.get_json())
        new_id = r.get_json()['source_id']
        self.assertEqual(self._order(self.ids['alpha']), before + [new_id])
        # Beta follows: the provider's list placed it after the slot, before Beta's own.
        self.assertEqual(self._order(self.ids['beta']), [self.ids['bguide'], new_id])

    def test_a_provider_source_needs_an_interval(self):
        r = self.client.post(f'/api/providers/{self.pid}/epg-sources',
                             json={'url': 'http://new.test/x.xml', 'name': 'New'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('how often', r.get_json()['error'])


# ── A provider's own sources ─────────────────────────────────────────────────────────────

class ProviderSourceTests(_Fixture):

    def setUp(self):
        super().setUp()
        self.pid = self._provider(self.ids['alpha'], self.ids['beta'])

    def test_moving_a_source_to_the_provider_fetches_nothing_and_copies_what_is_held(self):
        with mock.patch('app.accounts.requests.get') as fetch:
            r = self.client.post(
                f'/api/accounts/{self.ids["alpha"]}/epg-sources/{self.ids["full"]}/move-to-provider',
                json={'refresh_interval_hours': 24})
        fetch.assert_not_called()
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        src = db.session.get(EpgSource, self.ids['full'])
        self.assertEqual((src.owner_account_id, src.owner_provider_id), (None, self.pid))
        self.assertEqual(src.refresh_interval_hours, 24)
        self.assertEqual([e.source_id for e in es.provider_list(self.pid)],
                         [None, self.ids['full']])
        self.assertEqual(self._order(self.ids['beta']), [self.ids['bguide'], self.ids['full']])
        self.assertEqual(self._winner(self.bch.id), self.ids['full'])
        self.assertEqual(self._rows(self.bch.id, self.ids['full']), (3, 0))
        self._assert_one_timeline()

    def test_the_own_guide_cannot_move_to_the_provider(self):
        r = self.client.post(
            f'/api/accounts/{self.ids["alpha"]}/epg-sources/{self.ids["aguide"]}/move-to-provider',
            json={'refresh_interval_hours': 24})
        self.assertEqual(r.status_code, 400)

    def test_it_survives_deleting_any_one_account(self):
        es.move_source_to_provider(self.ids['full'], self.pid, 24, False)
        r = self.client.delete(f'/api/accounts/{self.ids["alpha"]}')
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        self.assertIsNone(db.session.get(Account, self.ids['alpha']))
        self.assertIsNotNone(db.session.get(EpgSource, self.ids['full']))
        self.assertEqual(self._winner(self.bch.id), self.ids['full'])
        self.assertEqual(es.subscriber_ids(self.ids['full']), [self.ids['beta']])

    def test_moving_it_to_one_account_stops_the_others(self):
        es.move_source_to_provider(self.ids['full'], self.pid, 24, False)
        r = self.client.post(f'/api/providers/{self.pid}/epg-sources/{self.ids["full"]}/give',
                             json={'account_id': self.ids['alpha']})
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        src = db.session.get(EpgSource, self.ids['full'])
        self.assertEqual((src.owner_account_id, src.owner_provider_id), (self.ids['alpha'], None))
        self.assertEqual(es.subscriber_ids(self.ids['full']), [self.ids['alpha']])
        self.assertIsNone(self._winner(self.bch.id))
        self.assertEqual(self._rows(self.bch.id, self.ids['full']), (0, 0))
        self.assertEqual(self._mode(self.ids['beta']), EPG_ORDER_FOLLOW,
                         'losing a provider source is not the account changing its order')
        self.assertEqual(ProviderEpgOrder.query.filter_by(source_id=self.ids['full']).count(), 0)

    def test_reordering_the_list_moves_following_accounts_only(self):
        es.move_source_to_provider(self.ids['full'], self.pid, 24, False)
        es.move_source(self.ids['beta'], self.ids['full'], -1, False, own_order_ok=True)
        beta_before = self._order(self.ids['beta'])
        entry = ProviderEpgOrder.query.filter_by(source_id=self.ids['full']).one()
        r = self.client.post(f'/api/providers/{self.pid}/epg-order/{entry.id}/move',
                             json={'direction': 'up'})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self._order(self.ids['alpha']), [self.ids['full'], self.ids['aguide']])
        self.assertEqual(self._winner(self.ach.id), self.ids['full'],
                         'a real schedule now comes before the banner')
        self.assertEqual(self._order(self.ids['beta']), beta_before, 'own order left alone')
        self._assert_one_timeline()

    def test_moving_it_to_an_account_that_had_dropped_it_copies_what_is_held(self):
        es.move_source_to_provider(self.ids['full'], self.pid, 24, False)
        es.stop_using_source(self.ids['beta'], self.ids['full'], False, own_order_ok=True)
        self.assertEqual(self._rows(self.bch.id, self.ids['full']), (0, 0))
        with mock.patch('app.accounts.requests.get') as fetch:
            es.move_source_to_account(self.ids['full'], self.ids['beta'], False)
        fetch.assert_not_called()
        self.assertEqual(self._winner(self.bch.id), self.ids['full'],
                         'its listings came from what Alpha held, not a refresh')
        self.assertEqual(self._rows(self.bch.id, self.ids['full']), (3, 0))
        self._assert_one_timeline()

    def test_the_channel_page_renders_a_pending_key_on_a_providers_source(self):
        with mock.patch('app.accounts.start_source_refresh', return_value=(False, '')):
            r = self.client.post(f'/api/providers/{self.pid}/epg-sources',
                                 json={'url': 'http://new.test/x.xml', 'name': 'Fresh',
                                       'refresh_interval_hours': 24})
        src = db.session.get(EpgSource, r.get_json()['source_id'])
        es.set_channel_key(db.session.get(Channel, self.bch.id), src, 'fresh.test',
                           case_sensitive=False)
        db.session.commit()
        page = self.client.get(f'/channels/{self.bch.id}')
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'#provider-{self.pid}', page.get_data(as_text=True))

    def test_deleting_it_drops_its_job_and_alerts_and_tells_the_readers(self):
        es.move_source_to_provider(self.ids['full'], self.pid, 24, False)
        with mock.patch('app.scheduler.remove_epg_source_jobs') as jobs, \
                mock.patch('app.accounts.resolve_source_alerts') as alerts:
            r = self.client.delete(f'/api/providers/{self.pid}/epg-sources/{self.ids["full"]}')
        self.assertEqual(r.status_code, 200, r.get_json())
        jobs.assert_called_once_with(self.ids['full'])
        alerts.assert_called_once_with(self.ids['full'])
        from app.database import Alert
        self.assertEqual(Alert.query.filter_by(
            source=f'epg-source:{self.ids["full"]}:removed').count(), 1)
        self.assertIsNone(self._winner(self.bch.id))

    def test_joining_while_a_source_refreshes_is_refused(self):
        gamma = seed.make_account(name='Gamma')
        gsrc = make_epg_source(gamma)
        gsrc.refresh_started_at = datetime.utcnow()
        db.session.commit()
        r = self.client.post(f'/api/providers/{self.pid}/accounts', json={'account_id': gamma.id})
        self.assertEqual(r.status_code, 409)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Account, gamma.id).provider_id)

    def test_the_accounts_page_lists_the_providers_sources(self):
        es.move_source_to_provider(self.ids['full'], self.pid, 24, False)
        page = self.client.get('/accounts').get_data(as_text=True)
        start = page.index(f'id="provider-{self.pid}-epg"')
        epg = page[start:page.index('prov-epg-add', start)]
        self.assertIn("Provider&#39;s EPG", epg)
        self.assertIn(f'data-source-id="{self.ids["full"]}" data-source-name="Full"', epg)
        detail = self.client.get(f'/accounts/{self.ids["beta"]}').get_data(as_text=True)
        self.assertIn('id="acct-epg-order">Follows <a', detail)
        self.assertIn('placed by Strong', detail)


# ── Deleting a provider ──────────────────────────────────────────────────────────────────

class DeleteProviderTests(_Fixture):

    def setUp(self):
        super().setUp()
        self.pid = self._provider(self.ids['alpha'], self.ids['beta'])
        es.move_source_to_provider(self.ids['full'], self.pid, 24, False)

    def test_the_choice_is_required_when_it_owns_sources(self):
        r = self.client.delete(f'/api/providers/{self.pid}', json={})
        self.assertEqual(r.status_code, 400)
        self.assertIsNotNone(db.session.get(EpgSource, self.ids['full']))

    def test_kept_sources_go_to_the_lowest_reader_and_no_guide_changes(self):
        before = (self._winner(self.ach.id), self._winner(self.bch.id))
        r = self.client.delete(f'/api/providers/{self.pid}', json={'keep_sources': True})
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        src = db.session.get(EpgSource, self.ids['full'])
        self.assertEqual((src.owner_account_id, src.owner_provider_id), (self.ids['alpha'], None))
        self.assertEqual(sorted(es.subscriber_ids(self.ids['full'])),
                         sorted([self.ids['alpha'], self.ids['beta']]))
        self.assertEqual((self._winner(self.ach.id), self._winner(self.bch.id)), before)
        self.assertIsNone(self._mode(self.ids['alpha']))
        self.assertEqual(ProviderEpgOrder.query.count(), 0)

    def test_unkept_sources_are_deleted(self):
        r = self.client.delete(f'/api/providers/{self.pid}', json={'keep_sources': False})
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        self.assertIsNone(db.session.get(EpgSource, self.ids['full']))
        self.assertIsNone(self._winner(self.bch.id))
        self.assertIn('deleted', r.get_json()['message'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
