"""A real sync over loopback HTTP lists the playlist's own host and login as rows, changes
nothing about the account while every switch is on, and leaves a switch the user turned off
where it was (dev/changelog/1670, DESIGN-account-providers.md §13).

Sync tier: ChannelBin's real `_do_sync` against the simulator (tests/support/sim_sync.py).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.sim_sync import SimSyncCase  # noqa: E402
from app import account_links as links  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app import db  # noqa: E402
from app.database import AccountHost, AccountLogin, Alert, Login  # noqa: E402


class PlaylistRowsSyncTests(SimSyncCase):

    def setUp(self):
        super().setUp()
        connlim._holders.clear()
        connlim._seat_of.clear()
        self.addCleanup(connlim._seat_of.clear)
        self.addCleanup(connlim._holders.clear)

    def test_a_sync_lists_the_rows_and_changes_nothing_else(self):
        account_id = self.add_xtream('north-tv')
        self.sync(account_id)
        urls = [c.stream_url for c in self.channels(account_id)]
        facts = links.playlist_facts(account_id)
        host = AccountHost.query.filter_by(account_id=account_id, from_playlist=True).one()
        self.assertEqual((host.host, host.enabled), (facts['host'], True))
        login = Login.query.filter_by(playlist_account_id=account_id).one()
        self.assertEqual(login.username, self.login('north-tv'))
        self.assertIsNone(login.max_connections)
        self.assertEqual(self.account(account_id).stream_host, facts['stream_host'])

        self.sync(account_id)
        self.assertEqual([c.stream_url for c in self.channels(account_id)], urls,
                         'listing the rows rewrote no stream URL')
        self.assertEqual(AccountHost.query.filter_by(account_id=account_id).count(), 1)
        self.assertEqual(Login.query.filter_by(playlist_account_id=account_id).count(), 1)
        self.assertNoAlert('ACCOUNT_PLAYLIST_CHANGED')
        self.assertNoAlert('ACCOUNT_PLAYLIST_CHANGED_UNUSED')

    def test_a_switch_turned_off_stays_off_across_a_sync(self):
        account_id = self.add_xtream('north-tv')
        self.sync(account_id)
        login = Login.query.filter_by(playlist_account_id=account_id).one()
        links.set_login_enabled(account_id, login.id, False)
        self.sync(account_id)
        db.session.expire_all()
        self.assertFalse(db.session.get(AccountLogin, (account_id, login.id)).enabled)
        self.assertFalse(connlim.try_acquire(account_id, 'recording', 1),
                         'its only login is off, and the sync did not turn it back on')
        self.assertEqual(Alert.query.filter_by(alert_type='ACCOUNT_PLAYLIST_CHANGED').count(), 0)


if __name__ == '__main__':
    unittest.main()
