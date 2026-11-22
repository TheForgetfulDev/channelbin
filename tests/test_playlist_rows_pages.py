"""The pages for the playlist's own rows and the Use switches (templates/account_detail.html,
templates/accounts.html, templates/channels/detail.html, app/routes/accounts.py,
app/routes/channels.py, app/account_links.py, app/account_activity.py; dev/changelog/1671).

The properties guarded here, each of which fails in its own way:

  * every host and login row has a Use switch, the playlist's rows are marked and cannot be
    removed, a sync-listed playlist login cannot be edited, and a row that is off says so
    with the time it was turned off;
  * the account page says when streams skip the playlist's host or login, and says instead
    that nothing can stream when every host or every login is off;
  * the Accounts list and the account header carry the same "Playlist ... off" badge;
  * "the next stream will use" names the login try_acquire() would seat it on, and the host;
  * a switch moved by the user stamps `switched_at` and lands on the Activity card; a no-op
    flip stamps nothing;
  * the channel page's shown URL is the Copy URL masked - the active host, the next login's
    credentials - and the playlist's URL is labeled not used when its rows are off
    (dev/docs/BUGS.md 2026-11-21);
  * an M3U account with no username of its own shows no second "from the playlist" row once
    the playlist's login is listed;
  * migration 139 adds both switched_at columns and is re-runnable.

Runs against throwaway temp SQLite DBs - never the live dvr.db.
  python3 -m unittest tests.test_playlist_rows_pages
"""
import os
import re
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import account_links as links  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app import migrations as M  # noqa: E402
from app.database import AccountHost, AccountLogin, Channel, Login  # noqa: E402

PLAYLIST_HOST = 'cdn.oldreseller.example'
PLAYLIST_PAIR = ('k8x2expired', 'oldpw')


class _Case(unittest.TestCase):
    """A guide-service account whose stream URLs carry the playlist's host and login, with the
    playlist's rows listed, one host of the user's and one login of the user's."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.app.test_client()
        self.acc = seed.make_account(name='northwind-guide', max_connections=1)
        for i in range(1, 3):
            ch = seed.make_channel(self.acc, stream_id=i, name=f'ch{i}')
            ch.stream_url = ch.raw_stream_url = (
                f'http://{PLAYLIST_HOST}/live/{PLAYLIST_PAIR[0]}/{PLAYLIST_PAIR[1]}/{i}.ts')
        db.session.commit()
        self.acc_id = self.acc.id
        self.ch_id = Channel.query.filter_by(account_id=self.acc_id, name='ch1').one().id
        connlim._holders.clear()
        connlim._seat_of.clear()
        facts = links.playlist_facts(self.acc_id)
        links.list_playlist_rows(self.acc_id, facts['host'], facts['pair'])
        self.mine = links.add_host(self.acc_id, 'tv1.tomstreams.example')
        self.mine_id = self.mine.id
        self.spare = links.add_login(self.acc_id, 'tom-2', 'tom_second', 'secondpw', 1)
        self.spare_id = self.spare.id
        db.session.expire_all()
        self.pl_host_id = AccountHost.query.filter_by(account_id=self.acc_id,
                                                      from_playlist=True).one().id
        self.pl_login_id = Login.query.filter_by(playlist_account_id=self.acc_id).one().id

    def tearDown(self):
        connlim._holders.clear()
        connlim._seat_of.clear()
        self.t.cleanup()

    def _page(self, url=None):
        db.session.remove()
        resp = self.client.get(url or f'/accounts/{self.acc_id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _both_off(self):
        links.set_host_enabled(self.acc_id, self.pl_host_id, False)
        links.set_login_enabled(self.acc_id, self.pl_login_id, False)

    @staticmethod
    def _row(html, attr, value):
        m = re.search(rf'<tr {attr}="{value}">(.*?)</tr>', html, re.S)
        assert m, f'no row {attr}={value}'
        return m.group(1)


class CardRowsTests(_Case):

    def test_every_row_has_a_use_switch(self):
        html = self._page()
        for hid in (self.pl_host_id, self.mine_id):
            self.assertIn(f'data-switch="host" data-host-id="{hid}" checked', html)
        for lid in (self.pl_login_id, self.spare_id):
            self.assertIn(f'data-switch="login" data-login-id="{lid}" checked', html)
        self.assertIn('<th>Use</th>', html)

    def test_the_playlist_host_is_marked_and_cannot_be_removed(self):
        html = self._page()
        row = self._row(html, 'data-host-row', self.pl_host_id)
        self.assertIn('From the playlist', row)
        self.assertNotIn('data-act="host-remove"', row)
        self.assertIn("can't be removed. Turn it off instead.", row)
        self.assertIn('data-act="host-remove"', self._row(html, 'data-host-row', self.mine_id))

    def test_the_sync_listed_playlist_login_has_no_edit_or_remove(self):
        html = self._page()
        row = self._row(html, 'data-login-row', self.pl_login_id)
        self.assertIn('<strong>From the playlist</strong>', row)
        self.assertNotIn('data-act="login-edit"', row)
        self.assertNotIn('data-act="login-remove"', row)
        mine = self._row(html, 'data-login-row', self.spare_id)
        self.assertIn('data-act="login-edit"', mine)
        self.assertIn('data-act="login-remove"', mine)

    def test_a_row_that_is_off_says_off_since(self):
        self._both_off()
        html = self._page()
        host = self._row(html, 'data-host-row', self.pl_host_id)
        self.assertIn('data-switch="host" data-host-id="%d">' % self.pl_host_id, host)
        self.assertIn('Off</span><div class="text-faint small">since', host)
        login = self._row(html, 'data-login-row', self.pl_login_id)
        self.assertIn('Off</span><div class="text-faint small">since', login)
        self.assertNotIn('Active', host)


class NoticeAndBadgeTests(_Case):

    def test_no_notice_or_badge_with_every_switch_on(self):
        html = self._page()
        self.assertNotIn('id="acct-switches"', html)
        self.assertNotIn('Playlist host', html)

    def test_both_off_says_streams_skip_them_and_badges_both_pages(self):
        self._both_off()
        html = self._page()
        self.assertIn("Streams skip the playlist's host and login", html)
        self.assertIn(f'host <code>{PLAYLIST_HOST}</code> and login <code>{PLAYLIST_PAIR[0]}</code>', html)
        self.assertIn('Playlist host and login off</span>', html)
        self.assertIn('Playlist host and login off</span>', self._page('/accounts'))

    def test_only_the_login_off_names_only_the_login(self):
        links.set_login_enabled(self.acc_id, self.pl_login_id, False)
        html = self._page()
        self.assertIn("Streams skip the playlist's login", html)
        self.assertIn('Playlist login off</span>', html)
        self.assertIn('Playlist login off</span>', self._page('/accounts'))

    def test_every_host_off_says_nothing_can_stream_instead(self):
        links.set_host_enabled(self.acc_id, self.pl_host_id, False)
        links.set_host_enabled(self.acc_id, self.mine_id, False)
        html = self._page()
        self.assertIn('Nothing on this account can stream', html)
        self.assertIn('Every host on it is turned off', html)
        self.assertNotIn("Streams skip the playlist", html)
        self.assertIn("The next stream won't start: every host on account", html)

    def test_every_login_off_says_nothing_can_stream(self):
        links.set_login_enabled(self.acc_id, self.pl_login_id, False)
        links.set_login_enabled(self.acc_id, self.spare_id, False)
        html = self._page()
        self.assertIn('Every login on it is turned off', html)


class NextStreamTests(_Case):

    def test_it_names_the_login_and_host_the_next_seat_takes(self):
        self._both_off()
        html = self._page()
        self.assertIn('The next stream will use login <strong>tom-2</strong> on host '
                      '<code>tv1.tomstreams.example</code>.', html)
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 7))
        self.assertEqual(connlim.held_login_id('recording', 7), self.spare_id,
                         'the line named the login the seat went on')

    def test_it_follows_try_acquire_past_a_full_login(self):
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 7))
        first = connlim.held_login_id('recording', 7)
        nxt = links.next_stream(db.session.get(type(self.acc), self.acc_id))
        self.assertEqual(nxt['state'], links.NEXT_FREE)
        self.assertNotEqual(nxt['login'].id, first)
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 8))
        self.assertEqual(connlim.held_login_id('recording', 8), nxt['login'].id)
        nxt = links.next_stream(db.session.get(type(self.acc), self.acc_id))
        self.assertEqual(nxt['state'], links.NEXT_FULL)

    def test_streams_on_an_unlisted_host_are_named_on_it(self):
        acc = db.session.get(type(self.acc), self.acc_id)
        acc.stream_host = 'elsewhere.example'
        db.session.commit()
        self.assertEqual(links.next_stream(acc)['host'], 'elsewhere.example')

    def test_an_account_with_no_rows_has_no_line(self):
        other = seed.make_account(name='plain')
        db.session.commit()
        self.assertIsNone(links.next_stream(other))
        self.assertNotIn('id="acct-next-stream"', self._page(f'/accounts/{other.id}'))


class SwitchedAtTests(_Case):

    def test_a_flip_stamps_switched_at_and_a_no_op_does_not(self):
        links.set_host_enabled(self.acc_id, self.mine_id, True)
        links.set_login_enabled(self.acc_id, self.spare_id, True)
        db.session.expire_all()
        self.assertIsNone(db.session.get(AccountHost, self.mine_id).switched_at)
        held = AccountLogin.query.filter_by(account_id=self.acc_id, login_id=self.spare_id).one()
        self.assertIsNone(held.switched_at)
        self._both_off()
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(AccountHost, self.pl_host_id).switched_at)
        held = AccountLogin.query.filter_by(account_id=self.acc_id,
                                            login_id=self.pl_login_id).one()
        self.assertIsNotNone(held.switched_at)

    def test_the_flip_lands_on_the_activity_card(self):
        self._both_off()
        html = self._page().replace('&#39;', "'")
        self.assertIn(f"Host <code>{PLAYLIST_HOST}</code> turned off.", html)
        self.assertIn(f"Login \"{PLAYLIST_PAIR[0]}\" turned off for this account.", html)
        self.assertIn('Hosts and logins', html)

    def test_a_sync_that_renames_the_row_clears_the_flip(self):
        """The flip was of the old host and login; the card must never say the new ones were
        turned off."""
        self._both_off()
        links.list_playlist_rows(self.acc_id, 'cdn2.oldreseller.example', ('zz91old', 'pw2'))
        db.session.expire_all()
        host = db.session.get(AccountHost, self.pl_host_id)
        self.assertEqual(host.host, 'cdn2.oldreseller.example')
        self.assertFalse(host.enabled, 'the switch stays where the user left it')
        self.assertIsNone(host.switched_at)
        held = AccountLogin.query.filter_by(account_id=self.acc_id,
                                            login_id=self.pl_login_id).one()
        self.assertIsNone(held.switched_at)
        html = self._page()
        self.assertNotIn('cdn2.oldreseller.example</code> turned off', html)
        self.assertNotIn('"zz91old" turned off', html)


class ChannelPageTests(_Case):

    def test_shown_url_is_the_copy_url_masked_on_the_active_host(self):
        """dev/docs/BUGS.md 2026-11-21: the shown URL was re-built from the raw URL and kept
        the playlist's host after a host swap while Copy handed out the stored one."""
        links.set_active_host(self.acc_id, self.mine_id)
        html = self._page(f'/channels/{self.ch_id}')
        m = re.search(r'data-full-url="([^"]*)">([^<]*)</code>', html)
        copy_url, shown = m.group(1), m.group(2)
        self.assertTrue(copy_url.startswith('http://tv1.tomstreams.example/'), copy_url)
        self.assertEqual(shown, 'http://tv1.tomstreams.example/live/***/***/1.ts')
        self.assertNotIn(PLAYLIST_HOST, shown)

    def test_copy_carries_the_next_logins_credentials_and_the_playlist_url_is_not_used(self):
        self._both_off()
        html = self._page(f'/channels/{self.ch_id}')
        copy_url = re.search(r'data-full-url="([^"]*)"', html).group(1)
        self.assertEqual(copy_url, 'http://tv1.tomstreams.example/live/tom_second/secondpw/1.ts')
        self.assertIn('The next stream will use login <strong>tom-2</strong>', html)
        self.assertIn(f'In the playlist (not used): <code>http://{PLAYLIST_HOST}/live/***/***/1.ts',
                      html)

    def test_copy_never_hands_out_a_login_that_is_off_while_no_seat_is_free(self):
        self._both_off()
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 7))
        html = self._page(f'/channels/{self.ch_id}')
        copy_url = re.search(r'data-full-url="([^"]*)"', html).group(1)
        self.assertEqual(copy_url, 'http://tv1.tomstreams.example/live/tom_second/secondpw/1.ts')
        self.assertIn('is free right now, so the next stream waits for one', html)

    def test_with_every_switch_on_the_url_is_unchanged(self):
        html = self._page(f'/channels/{self.ch_id}')
        copy_url = re.search(r'data-full-url="([^"]*)"', html).group(1)
        self.assertEqual(copy_url, db.session.get(Channel, self.ch_id).stream_url)
        self.assertNotIn('(not used)', html)


class OwnLoginRowTests(unittest.TestCase):

    def test_an_m3u_account_without_a_username_shows_no_second_playlist_row(self):
        from app.routes.accounts import own_login_view
        t = make_test_app(csrf=False)
        try:
            acc = seed.make_account(name='m3u', username=None, password=None)
            db.session.commit()
            playlist = {'is_account_login': False, 'from_playlist': True}
            typed = {'is_account_login': False, 'from_playlist': False}
            self.assertIsNone(own_login_view(acc, [playlist, typed]))
            self.assertIsNotNone(own_login_view(acc, []), 'before listing it stands in for it')
            self.assertIsNotNone(own_login_view(acc, [typed]),
                                 'with no playlist login listed it still says what syncs use')
        finally:
            t.cleanup()


class Migration139Tests(unittest.TestCase):

    def test_adds_both_columns_and_reruns(self):
        with tempfile.TemporaryDirectory() as d:
            conn = sqlite3.connect(os.path.join(d, 'm.db'))
            cur = conn.cursor()
            cur.execute('CREATE TABLE account_hosts (id INTEGER PRIMARY KEY, host TEXT)')
            cur.execute('CREATE TABLE account_logins (account_id INTEGER, login_id INTEGER)')
            M._m139_switched_at(conn, cur)
            M._m139_switched_at(conn, cur)
            for table in ('account_hosts', 'account_logins'):
                cols = {r[1] for r in cur.execute(f'PRAGMA table_info({table})')}
                self.assertIn('switched_at', cols)
            conn.close()


if __name__ == '__main__':
    unittest.main()
