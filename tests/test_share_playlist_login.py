"""Sharing a login onto an account whose sync already lists the same username and password as
its own playlist login (app/account_links.py::share_login, app/connection_limits.py::
merge_login_pool, app/routes/providers.py; dev/changelog/1692, dev/docs/BUGS.md 2026-11-22 08:35 PM).

One login sold through two services is the case sharing exists for, and since the playlist's
login became a row (dev/changelog/1670) each account's sync lists that one credential as its
own row. The share used to be refused there, and the row it collided with cannot be removed,
so the two accounts had no way to count the login's one seat once.

The properties guarded here, each of which fails in its own way:

  * the share takes the place of the target's own sync-listed copy - same position, same
    switch - and leaves one row for the credential, so the seat is counted once;
  * the target's next sync adopts the shared row and adds no copy back;
  * a seat already held on the replaced row moves with it, so no second stream opens;
  * a row the user typed, a copy that is itself shared, and a copy under a block that is
    still running are never replaced, and each refusal says what to do;
  * the share's message says the account's own row was replaced.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_share_playlist_login
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import account_links as links  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.database import AccountLogin, Login, LoginBlock  # noqa: E402

PAIR = ('harborshared', 'harborpass1')


class _Case(unittest.TestCase):
    """harbor-tv and harbor-plus: two panels on one backend, one login valid on both, one
    seat. Both synced (so each lists the login as its own playlist row), on one provider."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.app.test_client()
        self.tv = seed.make_account(name='harbor-tv', max_connections=1)
        self.plus = seed.make_account(name='harbor-plus', max_connections=1)
        for acc, host in ((self.tv, 'tv.harbor.example'), (self.plus, 'plus.harbor.example')):
            ch = seed.make_channel(acc, stream_id=1, name=f'{acc.name} ch')
            ch.stream_url = ch.raw_stream_url = f'http://{host}/{PAIR[0]}/{PAIR[1]}/1'
        db.session.commit()
        self.tv_id, self.plus_id = self.tv.id, self.plus.id
        connlim._holders.clear()
        connlim._seat_of.clear()
        self._sync(self.tv_id)
        self._sync(self.plus_id)
        links.create_provider('harbor', (self.tv_id, self.plus_id))
        db.session.expire_all()
        self.tv_login = self._own(self.tv_id).id
        self.plus_login = self._own(self.plus_id).id

    def tearDown(self):
        connlim._holders.clear()
        connlim._seat_of.clear()
        self.t.cleanup()

    def _sync(self, account_id):
        """What a sync does once its channels are written."""
        facts = links.playlist_facts(account_id)
        changes = links.list_playlist_rows(account_id, facts['host'], facts['pair'])
        db.session.expire_all()
        return changes

    def _own(self, account_id):
        return Login.query.filter_by(playlist_account_id=account_id).one()

    def _rows_for_pair(self):
        return Login.query.filter_by(username=PAIR[0], password=PAIR[1]).count()

    def _list(self, account_id):
        return [lg.id for lg in links.logins_for_account(account_id)]


class ReplaceTests(_Case):

    def test_the_share_takes_the_place_of_the_targets_own_copy(self):
        typed = links.add_login(self.plus_id, 'spare', 'plusspare', 'pw2', 1).id
        self.assertEqual(self._list(self.plus_id), [self.plus_login, typed])
        links.share_login(self.tv_login, self.plus_id)
        db.session.expire_all()
        self.assertEqual(self._list(self.plus_id), [self.tv_login, typed],
                         'in the same place on the list')
        self.assertIsNone(db.session.get(Login, self.plus_login))
        self.assertEqual(self._rows_for_pair(), 1, 'one row for the credential')

    def test_the_targets_switch_comes_with_it(self):
        links.set_login_enabled(self.plus_id, self.plus_login, False)
        links.share_login(self.tv_login, self.plus_id)
        db.session.expire_all()
        self.assertFalse(db.session.get(AccountLogin, (self.plus_id, self.tv_login)).enabled)
        self.assertTrue(db.session.get(AccountLogin, (self.tv_id, self.tv_login)).enabled)

    def test_the_seat_is_counted_once(self):
        links.share_login(self.tv_login, self.plus_id)
        db.session.expire_all()
        self.assertTrue(connlim.try_acquire(self.tv_id, 'recording', 1))
        self.assertFalse(connlim.try_acquire(self.plus_id, 'recording', 2),
                         'one seat across both accounts')

    def test_the_targets_next_sync_adds_no_copy_back(self):
        links.share_login(self.tv_login, self.plus_id)
        self.assertEqual(self._sync(self.plus_id), [])
        self.assertEqual(self._sync(self.tv_id), [])
        self.assertEqual(self._rows_for_pair(), 1)
        self.assertEqual(self._list(self.plus_id), [self.tv_login])

    def test_a_seat_held_on_the_replaced_row_moves_with_it(self):
        self.assertTrue(connlim.try_acquire(self.plus_id, 'recording', 7))
        self.assertEqual(connlim.held_login_id('recording', 7), self.plus_login)
        links.share_login(self.tv_login, self.plus_id)
        db.session.expire_all()
        self.assertEqual(connlim.held_login_id('recording', 7), self.tv_login)
        self.assertFalse(connlim.try_acquire(self.tv_id, 'recording', 8),
                         'the seat is still taken')
        connlim.release(self.plus_id, 'recording', 7)
        self.assertTrue(connlim.try_acquire(self.tv_id, 'recording', 8))

    def test_the_message_says_the_row_was_replaced(self):
        r = self.client.post(f'/api/logins/{self.tv_login}/share',
                             json={'account_id': self.plus_id})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn('took that row\'s place', r.get_json()['message'])
        self.assertIn('harbor-plus', r.get_json()['message'])


class RefuseTests(_Case):

    def test_a_row_the_user_typed_is_theirs_to_remove(self):
        other = seed.make_account(name='harbor-guide', max_connections=1)
        db.session.commit()
        other_id = other.id
        links.set_account_provider(other_id, db.session.get(type(other), self.tv_id).provider_id)
        typed = links.add_login(other_id, 'mine', PAIR[0], PAIR[1], 1).id
        db.session.expire_all()
        self.assertIsNone(db.session.get(Login, typed).playlist_account_id)
        with self.assertRaises(ValueError) as caught:
            links.share_login(self.tv_login, other_id)
        self.assertIn('Remove it from harbor-guide first', str(caught.exception))
        self.assertIsNotNone(db.session.get(Login, typed))

    def test_a_copy_under_a_running_block_is_kept(self):
        now = datetime.utcnow()
        db.session.add(LoginBlock(login_id=self.plus_login, start_time=now - timedelta(hours=1),
                                  stop_time=None))
        db.session.commit()
        with self.assertRaises(ValueError) as caught:
            links.share_login(self.tv_login, self.plus_id)
        self.assertIn('blocked', str(caught.exception))
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Login, self.plus_login))

    def test_an_ended_block_does_not_keep_it(self):
        now = datetime.utcnow()
        db.session.add(LoginBlock(login_id=self.plus_login, start_time=now - timedelta(hours=2),
                                  stop_time=now - timedelta(hours=1)))
        db.session.commit()
        links.share_login(self.tv_login, self.plus_id)
        db.session.expire_all()
        self.assertEqual(LoginBlock.query.filter_by(login_id=self.plus_login).count(), 0,
                         'a block goes with its login')

    def test_a_copy_that_is_itself_shared_is_kept(self):
        third = seed.make_account(name='harbor-extra', max_connections=1)
        db.session.commit()
        third_id = third.id
        links.set_account_provider(third_id, db.session.get(type(third), self.tv_id).provider_id)
        links.share_login(self.plus_login, third_id)
        with self.assertRaises(ValueError) as caught:
            links.share_login(self.tv_login, self.plus_id)
        self.assertIn('Stop sharing it first', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
