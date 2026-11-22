"""A block on one login (app/account_blocks.py, dev/changelog/1672): ChannelBin keeps off that
login - a TV is watching on it - and streams through the account's other logins.

The properties guarded here, each of which fails in its own way:

  * a new seat skips a blocked login and takes the next one, on every account holding it -
    the block is one more input to pool_taken()'s per-pool sum, not a second counter;
  * an account left with no usable login reads as blocked where a member is chosen, and every
    refusal names the login and when it frees, never the connection limit;
  * a stream already running on the blocked login keeps going: the watchdog is not told to
    move it, and its next segment moves to another login only when one has a free seat;
  * a blocked login's pool is taken before a partial account block spreads its slots, so the
    account block takes the next login rather than one already kept free;
  * the account and channel pages' "next stream" line and the channel page's Copy skip it;
  * a sync that rewrites the playlist login to another username ends its block, and one that
    drops the sync-listed row takes its block with it;
  * the routes set and end the block, the Logins card and the Activity card say so on every
    account holding the login, and deleting the login takes its blocks with it.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_login_blocks
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import account_blocks as blocks  # noqa: E402
from app import account_links as links  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.database import Account, Login, LoginBlock  # noqa: E402


class _Case(unittest.TestCase):
    """harbor-direct, whose URLs carry main/pw1, listing `main` (its playlist login, 1 seat)
    and `spare` (1 seat)."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.app.test_client()
        self.acc = seed.make_account(name='harbor-direct', max_connections=1)
        ch = seed.make_channel(self.acc, stream_id=1, name='ch1')
        ch.stream_url = ch.raw_stream_url = 'http://h1.harbor.example/live/main/pw1/1.ts'
        db.session.commit()
        self.acc_id, self.ch_id = self.acc.id, ch.id
        connlim._holders.clear()
        connlim._seat_of.clear()
        self.main = links.add_login(self.acc_id, 'main', 'main', 'pw1', 1).id
        self.spare = links.add_login(self.acc_id, 'spare', 'spare', 'pw2', 1).id
        db.session.expire_all()

    def tearDown(self):
        connlim._holders.clear()
        connlim._seat_of.clear()
        self.t.cleanup()

    def _block(self, login_id, hours=1):
        stop = None if hours is None else datetime.utcnow() + timedelta(hours=hours)
        return blocks.add_login_block(login_id, stop)


class SeatTests(_Case):

    def test_a_new_seat_skips_the_blocked_login_for_the_next_one(self):
        self._block(self.main)
        self.assertTrue(connlim.try_acquire(self.acc_id, 'preview', 'p1'))
        self.assertEqual(connlim.held_login_id('preview', 'p1'), self.spare)
        self.assertFalse(connlim.try_acquire(self.acc_id, 'test', 2),
                         'main is blocked and spare is taken: no seat left')

    def test_with_every_login_blocked_nothing_takes_a_seat_and_the_account_reads_blocked(self):
        self._block(self.main)
        self._block(self.spare, hours=None)
        self.assertFalse(connlim.try_acquire(self.acc_id, 'recording', 1))
        self.assertTrue(connlim.at_limit(self.acc_id))
        self.assertIn(self.acc_id, blocks.blocked_account_ids([self.acc_id]))
        self.assertIn(self.acc_id, blocks.blocked_account_ids(None))
        self.assertIsNotNone(blocks.free_at(self.acc_id))
        reason = blocks.blocked_reason(self.acc_id, 'harbor-direct', capital=True)
        self.assertEqual(reason[:52], 'Account "harbor-direct" has logins "main", "spare" b')
        self.assertIn('blocked until', reason)
        self.assertNotIn('connection limit', reason)

    def test_one_blocked_login_of_two_is_not_a_blocked_account(self):
        self._block(self.main)
        self.assertNotIn(self.acc_id, blocks.blocked_account_ids([self.acc_id]))
        self.assertIsNone(blocks.free_at(self.acc_id))
        self.assertIsNone(blocks.blocked_reason(self.acc_id, 'harbor-direct'))

    def test_a_blocked_login_beside_one_turned_off_says_both(self):
        links.set_login_enabled(self.acc_id, self.spare, False)
        self._block(self.main, hours=None)
        self.assertIn(self.acc_id, blocks.blocked_account_ids([self.acc_id]))
        self.assertTrue(blocks.no_end(blocks.free_at(self.acc_id)))
        reason = blocks.blocked_reason(self.acc_id, 'harbor-direct')
        self.assertEqual(reason, 'account "harbor-direct" has login "main" blocked until you '
                                 'unblock it and its other logins turned off')

    def test_a_partial_account_block_takes_the_login_after_the_blocked_one(self):
        """Two TVs: one on login main (its block), one counted by an account block of one
        connection. Nothing is left for ChannelBin."""
        self._block(self.main)
        blocks.add_account_block(self.acc_id, datetime.utcnow() + timedelta(hours=1), slots=1)
        self.assertFalse(connlim.try_acquire(self.acc_id, 'preview', 'p1'))
        self.assertIn(self.acc_id, blocks.blocked_account_ids([self.acc_id]))

    def test_free_at_is_when_the_block_ends(self):
        links.set_login_enabled(self.acc_id, self.spare, False)
        stop = datetime.utcnow() + timedelta(hours=2)
        blocks.add_login_block(self.main, stop)
        self.assertEqual(blocks.free_at(self.acc_id), stop)
        self.assertIsNone(blocks.free_at(self.acc_id, at=stop + timedelta(seconds=1)))

    def test_a_block_that_has_ended_takes_nothing(self):
        self._block(self.main)
        blocks.end_login_block(self.main)
        self.assertTrue(connlim.try_acquire(self.acc_id, 'preview', 'p1'))
        self.assertEqual(connlim.held_login_id('preview', 'p1'), self.main)

    def test_a_new_block_replaces_the_one_in_force(self):
        self._block(self.main, hours=None)
        self._block(self.main, hours=1)
        live = blocks.login_blocks_at(None, [self.main])
        self.assertEqual(len(live), 1)
        self.assertFalse(blocks.no_end(live[0].stop))
        self.assertEqual(LoginBlock.query.filter_by(login_id=self.main).count(), 2,
                         'the replaced block is kept, ended, for the Activity card')

    def test_an_account_with_no_logins_asks_for_no_login_block(self):
        other = seed.make_account(name='plain', max_connections=1)
        db.session.commit()
        self._block(self.main)
        with mock.patch.object(blocks, 'login_blocks_at',
                               side_effect=AssertionError('asked')):
            self.assertTrue(connlim.try_acquire(other.id, 'preview', 'p9'))


class RunningStreamTests(_Case):

    def test_a_recording_on_the_blocked_login_is_not_told_to_move(self):
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 7))
        self.assertEqual(connlim.held_login_id('recording', 7), self.main)
        self._block(self.main)
        self.assertFalse(connlim.must_yield_to_block(self.acc_id, 7))
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 7),
                        'a seat already held is still held')

    def test_its_next_segment_moves_to_a_login_with_a_free_seat(self):
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 7))
        self._block(self.main)
        self.assertEqual(connlim.reseat_before_relaunch(self.acc_id, 'recording', 7), self.spare)
        self.assertEqual(connlim._holders.get(('login', self.spare)), [('recording', 7)])

    def test_with_no_free_seat_elsewhere_it_keeps_going_on_the_blocked_login(self):
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 7))
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 8))
        self._block(self.main)
        self.assertEqual(connlim.reseat_before_relaunch(self.acc_id, 'recording', 7), self.main)


class NextStreamTests(_Case):

    def _next(self):
        return links.next_stream(db.session.get(Account, self.acc_id))

    def test_the_next_stream_names_the_login_after_the_blocked_one(self):
        self._block(self.main)
        nxt = self._next()
        self.assertEqual(nxt['state'], links.NEXT_FREE)
        self.assertEqual(nxt['login'].id, self.spare)

    def test_with_every_login_blocked_it_says_why_and_copy_hands_out_none_of_them(self):
        self._block(self.main)
        self._block(self.spare)
        nxt = self._next()
        self.assertEqual(nxt['state'], links.NEXT_BLOCKED)
        self.assertIn('blocked until', nxt['reason'])
        self.assertIsNone(nxt['copy_login'],
                          'Copy never hands out a login a TV is watching on')

    def test_while_full_copy_hands_out_a_login_that_is_not_blocked(self):
        self._block(self.main)
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 1))
        nxt = self._next()
        self.assertEqual(nxt['state'], links.NEXT_FULL)
        self.assertEqual(nxt['copy_login'].id, self.spare)


class SharedLoginTests(unittest.TestCase):
    """A login shared by two accounts on one provider is one pool, so a block on it reaches
    both."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.app.test_client()
        self.a = seed.make_account(name='northwind-tom', max_connections=1)
        self.b = seed.make_account(name='northwind-guide', max_connections=1)
        for acc, host in ((self.a, 'a1.northwind.example'), (self.b, 'guide.example')):
            ch = seed.make_channel(acc, stream_id=1, name=f'{acc.name} ch')
            ch.stream_url = ch.raw_stream_url = f'http://{host}/live/old/pw0/1.ts'
        db.session.commit()
        self.a_id, self.b_id = self.a.id, self.b.id
        connlim._holders.clear()
        connlim._seat_of.clear()
        links.create_provider('northwind', [self.a_id, self.b_id])
        self.tom = links.add_login(self.a_id, 'tom', 'tom', 'pwT', 1).id
        for aid in (self.a_id, self.b_id):
            playlist = Login.query.filter_by(playlist_account_id=aid).first()
            if playlist is not None:
                links.set_login_enabled(aid, playlist.id, False)
        links.share_login(self.tom, self.b_id)
        db.session.expire_all()

    def tearDown(self):
        connlim._holders.clear()
        connlim._seat_of.clear()
        self.t.cleanup()

    def test_the_block_refuses_a_seat_on_both_accounts(self):
        blocks.add_login_block(self.tom, None)
        self.assertFalse(connlim.try_acquire(self.a_id, 'preview', 'p1'))
        self.assertFalse(connlim.try_acquire(self.b_id, 'preview', 'p2'))
        self.assertEqual(blocks.blocked_account_ids([self.a_id, self.b_id]), {self.a_id, self.b_id})
        self.assertIn('has login "tom" blocked', blocks.blocked_reason(self.b_id, 'northwind-guide'))

    def test_the_route_on_one_account_names_the_other_and_both_pages_show_it(self):
        res = self.client.post(f'/api/accounts/{self.a_id}/logins/{self.tom}/block',
                               json={'until_unblocked': True})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertIn('northwind-guide', res.get_json()['message'])
        for aid in (self.a_id, self.b_id):
            html = self.client.get(f'/accounts/{aid}').get_data(as_text=True)
            self.assertIn('Blocked until you unblock it', html)
            self.assertIn('data-act="login-unblock"', html)
            self.assertIn('Login "tom" blocked from', html)


class RouteTests(_Case):

    def test_block_and_unblock(self):
        res = self.client.post(f'/api/accounts/{self.acc_id}/logins/{self.main}/block',
                               json={'minutes': 60})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertTrue(res.get_json()['message'].startswith('Login "main" blocked until '))
        self.assertEqual(len(blocks.login_blocks_at(None, [self.main])), 1)
        with mock.patch('app.routes.accounts.rearm_waiting_starts') as rearm:
            res = self.client.delete(f'/api/accounts/{self.acc_id}/logins/{self.main}/block')
        self.assertEqual(res.status_code, 200)
        rearm.assert_called_once_with()
        self.assertEqual(blocks.login_blocks_at(None, [self.main]), [])
        row = LoginBlock.query.filter_by(login_id=self.main).one()
        self.assertIsNotNone(row.stop_time, 'the row is kept with its stop moved to now')

    def test_a_bad_body_or_a_login_the_account_does_not_hold_is_refused(self):
        for body in ({}, {'minutes': 'soon'}, {'until_unblocked': 'yes'}, {'minutes': -5}):
            res = self.client.post(f'/api/accounts/{self.acc_id}/logins/{self.main}/block',
                                   json=body)
            self.assertEqual(res.status_code, 400, body)
        other = seed.make_account(name='elsewhere', max_connections=1)
        db.session.commit()
        res = self.client.post(f'/api/accounts/{other.id}/logins/{self.main}/block',
                               json={'minutes': 60})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(LoginBlock.query.count(), 0)

    def test_the_logins_card_offers_block_use_until_blocked(self):
        html = self.client.get(f'/accounts/{self.acc_id}').get_data(as_text=True)
        self.assertIn('data-act="login-block"', html)
        self.assertNotIn('data-act="login-unblock"', html)
        self._block(self.spare)
        html = self.client.get(f'/accounts/{self.acc_id}').get_data(as_text=True)
        self.assertIn('data-act="login-unblock"', html)
        self.assertIn('Blocked until ', html)
        self.assertIn('Login "spare" blocked from', html)
        self.assertIn('The next stream will use login <strong>main</strong>', html)


class PlaylistRewriteTests(unittest.TestCase):
    """An account whose playlist login the sync listed itself (old/pw0)."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.acc = seed.make_account(name='northwind-guide', max_connections=1)
        db.session.commit()
        self.acc_id = self.acc.id
        connlim._holders.clear()
        connlim._seat_of.clear()
        links.list_playlist_rows(self.acc_id, None, ('old', 'pw0'))
        self.own = Login.query.filter_by(playlist_account_id=self.acc_id).one().id
        blocks.add_login_block(self.own, None)

    def tearDown(self):
        connlim._holders.clear()
        connlim._seat_of.clear()
        self.t.cleanup()

    def test_a_new_username_ends_the_block_and_the_line_says_so(self):
        changes = links.list_playlist_rows(self.acc_id, None, ('zz91', 'pwZ'))
        self.assertEqual(LoginBlock.query.filter_by(login_id=self.own).count(), 0)
        self.assertTrue(links.playlist_change_line(changes[0]).endswith(
            'The block on old ended with it.'))

    def test_a_new_password_on_the_same_username_keeps_the_block(self):
        changes = links.list_playlist_rows(self.acc_id, None, ('old', 'pw9'))
        self.assertEqual(len(blocks.login_blocks_at(None, [self.own])), 1)
        self.assertNotIn('block', links.playlist_change_line(changes[0]))

    def test_a_sync_listed_row_the_playlist_leaves_takes_its_block(self):
        links.add_login(self.acc_id, 'new', 'new', 'pwN', 1)
        links.list_playlist_rows(self.acc_id, None, ('new', 'pwN'))
        self.assertIsNone(db.session.get(Login, self.own))
        self.assertEqual(LoginBlock.query.count(), 0)


class TeardownTests(_Case):

    def test_removing_a_login_takes_its_blocks(self):
        self._block(self.spare)
        links.remove_login(self.acc_id, self.spare)
        self.assertEqual(LoginBlock.query.count(), 0)

    def test_deleting_the_account_takes_the_blocks_of_the_logins_it_alone_held(self):
        self._block(self.spare)
        self._block(self.main)
        res = self.client.delete(f'/api/accounts/{self.acc_id}')
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(LoginBlock.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
