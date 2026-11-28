"""The output feed's test rig: a guide of two channels and a group on two accounts, the feed
on, and a stand-in for a live relay session (tests/test_output_feed_switch.py,
tests/test_iptv_output_page.py)."""
import time
import unittest

import app.output_feed as feed
from app import db
from app.database import OutputFeedLogin
from tests.support.app import make_test_app
from tests.support.seed import make_account, make_channel, make_group

USER, PASS = 'tv', 'sofa'


def feed_cfg(**over):
    block = {'enabled': True}
    block.update(over)
    return {'integrations': {'output_feed': block}}


def add_login(username=USER, password=PASS, *, name='Main login', max_streams=20,
              provider_direct=True, full_refusals=True, **fields):
    """One IPTV output login row, committed. The defaults are the owner's own login as startup
    moves it out of config.yaml (app/__init__.py::_move_main_output_login, dev/changelog/1781):
    every channel, the output's accounts, both home-only switches on and the most streams a login
    may have, so a test about the output itself gets the login the main one used to be."""
    row = OutputFeedLogin(name=name, username=username, password=password,
                          max_streams=max_streams, provider_direct=provider_direct,
                          full_refusals=full_refusals, **fields)
    db.session.add(row)
    db.session.commit()
    return row


def ensure_login(username=USER, password=PASS, **kwargs):
    """add_login() unless a login with `username` exists already, for a helper a test may call
    more than once."""
    row = OutputFeedLogin.query.filter_by(username=username).first()
    return row if row is not None else add_login(username, password, **kwargs)


class FeedCase(unittest.TestCase):
    FEED = None
    #: The rig's own login's password (add_login()'s USER, this).
    PASSWORD = PASS

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.client
        self.addCleanup(self.t.cleanup)
        self.addCleanup(feed.stop_all)
        self.t.sandbox_config(self.FEED or feed_cfg())
        self.login = add_login(USER, self.PASSWORD)
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
        params.setdefault('password', self.PASSWORD)
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
