"""The output feed's test rig: a guide of two channels and a group on two accounts, the feed
on, and a stand-in for a live relay session (tests/test_output_feed_switch.py,
tests/test_iptv_output_page.py)."""
import time
import unittest

import app.output_feed as feed
from app import db
from tests.support.app import make_test_app
from tests.support.seed import make_account, make_channel, make_group

USER, PASS = 'tv', 'sofa'


def feed_cfg(**over):
    block = {'enabled': True, 'username': USER, 'password': PASS}
    block.update(over)
    return {'integrations': {'output_feed': block}}


class FeedCase(unittest.TestCase):
    FEED = None

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.client
        self.addCleanup(self.t.cleanup)
        self.addCleanup(feed.stop_all)
        self.t.sandbox_config(self.FEED or feed_cfg())
        self.account = make_account('Acct', max_connections=1)
        self.other = make_account('Other', max_connections=1)
        self.alpha = make_channel(self.account, name='Alpha', in_guide=True)
        self.alpha.guide_sort_order = 1
        self.beta = make_channel(self.account, name='Beta', in_guide=True)
        self.beta.guide_sort_order = 3
        self.member = make_channel(self.other, name='Member')
        self.group = make_group('Game feeds', members=[self.member])
        self.group.guide_sort_order = 2
        self.loose = make_channel(self.account, name='Not in the guide')
        db.session.commit()

    def get(self, path, **params):
        params.setdefault('username', USER)
        params.setdefault('password', PASS)
        return self.client.get(path + '?' + '&'.join(f'{k}={v}' for k, v in params.items()))

    def streams(self):
        return self.get('/player_api.php', action='get_live_streams').get_json()

    def switch(self, kind, row_id, on):
        row, moved = feed.set_on_output(kind, row_id, on)
        db.session.commit()
        return row, moved

    def session(self, feed_id, app=True):
        s = feed.RelaySession(
            id=f's{feed_id}-{time.monotonic_ns()}', feed_id=feed_id, label='channel (Alpha)',
            client='192.168.1.23', group_id=None, channel_id=self.alpha.id,
            channel_name='Alpha', account_id=self.account.id, ring=feed.Ring(1024, 0),
            started_mono=time.monotonic() - 90, app=self.t.app if app else None)
        s.state = feed.STATE_STREAMING
        s.bytes_sent = 4096
        with feed._lock:
            feed._sessions[s.id] = s
        return s
