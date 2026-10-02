"""Providers and shared logins (app/account_links.py, app/account_blocks.py,
app/connection_limits.py, app/routes/providers.py; DESIGN-account-providers.md §3.2, §5.3,
§6; dev/changelog/1170).

The properties guarded here, each of which fails in its own way:

  * a login is shared only between accounts on one provider, never across two or with
    none, and never onto an account already holding that username or name;
  * an account leaving or moving off a provider stops sharing what it shared there, and
    keeps what only it holds; deleting a provider unlinks its accounts and leaves their
    hosts, logins and shares alone;
  * a shared login is one seat pool: a seat taken through one account is gone for the
    other, and a block on one account takes the shared seats from the other too - at the
    seat (preview refused), where a member is chosen (`blocked_account_ids`), and in the
    sentence that says why (`blocked_reason` names the account the block is on);
  * an account whose other login is not shared keeps that seat;
  * the Accounts page renders no Providers section with one account, the hint with two
    and none, and a card per provider; the account page names its provider, its shared
    logins and a sibling's block.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_account_providers
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, preview  # noqa: E402
from app import account_links as links  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.account_blocks import (add_account_block, blocked_account_ids,  # noqa: E402
                                blocked_reason, free_at)
from app.database import Account, AccountLogin, Login, Provider  # noqa: E402


class _Case(unittest.TestCase):
    """Two accounts reaching one backend - skyline-curated and skyline-direct - and an
    unrelated harbor-direct. Each starts with no provider and no logins."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.curated = seed.make_account(name='skyline-curated', max_connections=1)
        self.direct = seed.make_account(name='skyline-direct', max_connections=1)
        self.other = seed.make_account(name='harbor-direct', max_connections=1)
        chans = {}
        for acc, host in ((self.curated, 'curation.example'), (self.direct, 'a1.skyline.example'),
                          (self.other, 'h1.harbor.example')):
            ch = seed.make_channel(acc, stream_id=1, name=f'{acc.name} ch')
            ch.stream_url = ch.raw_stream_url = f'http://{host}/live/main/pw1/4471.ts'
            chans[acc.name] = ch
        db.session.commit()
        self.cur_id, self.dir_id, self.oth_id = self.curated.id, self.direct.id, self.other.id
        self.dir_ch = chans['skyline-direct'].id
        connlim._holders.clear()
        connlim._seat_of.clear()

    def tearDown(self):
        connlim._holders.clear()
        connlim._seat_of.clear()
        self.t.cleanup()

    def _provider(self, *account_ids, name='skyline'):
        provider, _ = links.create_provider(name, account_ids)
        db.session.expire_all()
        return provider.id

    def _shared_main(self):
        """skyline on both accounts, login `main` (1 seat) added on curated and shared."""
        pid = self._provider(self.cur_id, self.dir_id)
        main = links.add_login(self.cur_id, 'main', 'main', 'pw1', 1).id
        links.share_login(main, self.dir_id)
        db.session.expire_all()
        return pid, main

    def _holders(self, login_id):
        return sorted(a for (a,) in db.session.query(AccountLogin.account_id)
                      .filter(AccountLogin.login_id == login_id).all())


class ShareTests(_Case):

    def test_a_login_is_shared_within_one_provider_and_nowhere_else(self):
        main = links.add_login(self.cur_id, 'main', 'main', 'pw1', 1).id
        with self.assertRaises(ValueError, msg='neither account is on a provider'):
            links.share_login(main, self.dir_id)
        self._provider(self.cur_id, name='skyline')
        self._provider(self.dir_id, name='harbor')
        with self.assertRaises(ValueError, msg='two different providers'):
            links.share_login(main, self.dir_id)
        links.set_account_provider(self.dir_id, db.session.get(Account, self.cur_id).provider_id)
        links.share_login(main, self.dir_id)
        self.assertEqual(self._holders(main), [self.cur_id, self.dir_id])
        self.assertEqual(Login.query.count(), 1, 'sharing never copies the row')
        with self.assertRaises(ValueError):
            links.share_login(main, self.dir_id)

    def test_sharing_refuses_a_username_or_name_the_target_already_lists(self):
        self._provider(self.cur_id, self.dir_id)
        main = links.add_login(self.cur_id, 'main', 'main', 'pw1', 1).id
        links.add_login(self.dir_id, 'other', 'main', 'pw9', 1)
        with self.assertRaises(ValueError):
            links.share_login(main, self.dir_id)
        spare = links.add_login(self.cur_id, 'spare', 'spare', 'pw2', 1).id
        links.add_login(self.dir_id, 'spare', 'someone', 'pw3', 1)
        with self.assertRaises(ValueError):
            links.share_login(spare, self.dir_id)

    def test_leaving_or_moving_drops_the_share_and_keeps_what_only_it_holds(self):
        pid, main = self._shared_main()
        own = links.add_login(self.dir_id, 'own', 'own', 'pw5', 1).id
        dropped = links.set_account_provider(self.dir_id, None)
        self.assertEqual(dropped, ['main'])
        self.assertEqual(self._holders(main), [self.cur_id], 'curated keeps it')
        self.assertEqual(self._holders(own), [self.dir_id])
        # And moving to another provider is leaving too.
        links.set_account_provider(self.dir_id, pid)
        links.share_login(main, self.dir_id)
        other_pid = self._provider(name='harbor')
        self.assertEqual(links.set_account_provider(self.dir_id, other_pid), ['main'])
        self.assertEqual(self._holders(main), [self.cur_id])

    def test_unshare_needs_another_holder_and_leaves_the_row_with_it(self):
        _, main = self._shared_main()
        links.unshare_login(main, self.dir_id)
        self.assertEqual(self._holders(main), [self.cur_id])
        with self.assertRaises(ValueError):
            links.unshare_login(main, self.cur_id)

    def test_deleting_a_provider_unlinks_and_leaves_lists_and_shares_alone(self):
        pid, main = self._shared_main()
        links.add_host(self.dir_id, 'a2.skyline.example')
        name, count = links.delete_provider(pid)
        db.session.expire_all()
        self.assertEqual((name, count), ('skyline', 2))
        self.assertIsNone(db.session.get(Provider, pid))
        self.assertIsNone(db.session.get(Account, self.cur_id).provider_id)
        self.assertIsNone(db.session.get(Account, self.dir_id).provider_id)
        self.assertEqual(self._holders(main), [self.cur_id, self.dir_id])
        self.assertEqual(len(links.hosts_for_accounts([self.dir_id])[self.dir_id]), 2)

    def test_provider_names_are_unique_ignoring_case(self):
        pid = self._provider(self.cur_id)
        with self.assertRaises(ValueError):
            links.create_provider('SKYLINE', [])
        other = self._provider(name='harbor')
        with self.assertRaises(ValueError):
            links.rename_provider(other, 'Skyline')
        links.rename_provider(pid, 'Skyline')
        self.assertEqual(db.session.get(Provider, pid).name, 'Skyline')


class PoolAndBlockTests(_Case):

    def test_a_shared_login_is_one_pool_across_both_accounts(self):
        _, main = self._shared_main()
        self.assertTrue(connlim.try_acquire(self.cur_id, 'recording', 1))
        self.assertFalse(connlim.try_acquire(self.dir_id, 'preview', 'p1'),
                         'the one seat is taken through the other account')
        self.assertTrue(connlim.at_limit(self.dir_id))

    def test_a_block_on_one_account_refuses_a_preview_on_the_account_sharing_its_login(self):
        """Design §11: the sibling refusal, at the seat and in the words."""
        self._shared_main()
        add_account_block(self.cur_id, datetime.utcnow() + timedelta(hours=1))
        self.assertFalse(connlim.try_acquire(self.dir_id, 'preview', 'p1'))
        self.assertIn(self.dir_id, blocked_account_ids([self.dir_id]))
        self.assertIsNotNone(free_at(self.dir_id))
        reason = blocked_reason(self.dir_id, 'skyline-direct', capital=True)
        self.assertIn('shares login "main" with "skyline-curated"', reason)
        with mock.patch.object(preview.subprocess, 'Popen', side_effect=AssertionError('no launch')), \
                self.t.app.test_request_context():
            with self.assertRaises(preview.PreviewRefused) as caught:
                preview.start_preview(self.dir_ch)
        self.assertIn('skyline-curated', str(caught.exception))
        self.assertNotIn('connection limit', str(caught.exception))

    def test_an_unshared_login_keeps_its_seat_under_a_siblings_block(self):
        self._shared_main()
        links.add_login(self.dir_id, 'own', 'own', 'pw5', 1)
        add_account_block(self.cur_id, datetime.utcnow() + timedelta(hours=1))
        self.assertNotIn(self.dir_id, blocked_account_ids([self.dir_id]))
        self.assertIsNone(blocked_reason(self.dir_id, 'skyline-direct'))
        self.assertTrue(connlim.try_acquire(self.dir_id, 'preview', 'p1'))
        self.assertEqual(connlim.held_login_id('preview', 'p1'),
                         db.session.query(Login.id).filter(Login.name == 'own').scalar())

    def test_a_block_does_not_reach_an_account_with_no_shared_login(self):
        self._shared_main()
        add_account_block(self.cur_id, datetime.utcnow() + timedelta(hours=1))
        self.assertNotIn(self.oth_id, blocked_account_ids(None))
        self.assertEqual(blocked_account_ids(None), {self.cur_id, self.dir_id})
        self.assertTrue(connlim.try_acquire(self.oth_id, 'preview', 'p2'))

    def test_a_recording_on_the_sibling_must_yield_when_the_shared_pool_is_blocked(self):
        self._shared_main()
        self.assertTrue(connlim.try_acquire(self.dir_id, 'recording', 7))
        self.assertFalse(connlim.must_yield_to_block(self.dir_id, 7))
        add_account_block(self.cur_id, datetime.utcnow() + timedelta(hours=1))
        self.assertTrue(connlim.must_yield_to_block(self.dir_id, 7))


class PageTests(_Case):

    def _accounts_html(self):
        return self.client.get('/accounts').get_data(as_text=True)

    def test_no_section_with_one_account(self):
        self.client.delete(f'/api/accounts/{self.dir_id}')
        self.client.delete(f'/api/accounts/{self.oth_id}')
        self.assertEqual(Account.query.count(), 1)
        self.assertNotIn('id="acct-providers"', self._accounts_html())

    def test_the_hint_with_two_accounts_and_no_provider_then_a_card(self):
        html = self._accounts_html()
        self.assertIn('id="acct-providers"', html)
        self.assertIn('Have more than one account for the same provider?', html)
        _, main = self._shared_main()
        connlim.try_acquire(self.dir_id, 'recording', 3)
        html = self._accounts_html()
        self.assertNotIn('Have more than one account for the same provider?', html)
        self.assertIn('<h2>skyline</h2>', html)
        self.assertIn('login: main (shared)', html)
        self.assertIn('Login "main" &middot; 1 seat, 1 in use by a recording on skyline-direct', html)
        self.assertNotIn('Share with harbor-direct', html, 'not on this provider')

    def test_the_account_page_names_the_provider_the_share_and_a_siblings_block(self):
        pid, _ = self._shared_main()
        add_account_block(self.cur_id, datetime.utcnow() + timedelta(hours=1))
        html = self.client.get(f'/accounts/{self.dir_id}').get_data(as_text=True)
        self.assertIn(f'#provider-{pid}">skyline</a>', html)
        self.assertIn('Shared with skyline-curated', html)
        self.assertIn('A block on skyline-curated also covers this account', html)
        own = self.client.get(f'/accounts/{self.cur_id}').get_data(as_text=True)
        self.assertIn('This block also covers skyline-direct, which shares login "main".', own)

    def test_the_routes_create_share_unshare_remove_and_delete(self):
        resp = self.client.post('/api/providers', json={'name': 'skyline',
                                                        'account_ids': [self.cur_id, self.dir_id]})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        pid = resp.get_json()['provider_id']
        main = links.add_login(self.cur_id, 'main', 'main', 'pw1', 1).id
        resp = self.client.post(f'/api/logins/{main}/share', json={'account_id': self.oth_id})
        self.assertEqual(resp.status_code, 409, 'harbor-direct is not on skyline')
        resp = self.client.post(f'/api/logins/{main}/share', json={'account_id': self.dir_id})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        resp = self.client.delete(f'/api/providers/{pid}/accounts/{self.dir_id}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('stopped sharing login "main"', resp.get_json()['message'])
        resp = self.client.post(f'/api/providers/{pid}/accounts', json={'account_id': self.dir_id})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(f'/api/providers/{pid}/rename', json={'name': 'skyline tv'})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.delete(f'/api/providers/{pid}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Provider.query.count(), 0)
        self.assertEqual(self.client.delete(f'/api/providers/{pid}').status_code, 404)


if __name__ == '__main__':
    unittest.main()
