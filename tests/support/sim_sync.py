"""The sync tier's shared footing: a generated provider over loopback HTTP, ChannelBin's real
`_do_sync` against it, and what a user would see afterwards (DESIGN-simulator.md §4).

`SimSyncCase` serves `FAULT_WORLD` for each test from a throwaway app (make_test_app), with
the simulator's clock held still so the guide window cannot move between a sync and the
expected answer. A test adds an account the way a user would - base URL and credentials, or
a playlist and guide URL - injects a fault, syncs, and asserts on the sync log, the open
alerts (by type and source key), the channels and listings, and the simulator's ledger of
what was asked.
"""
import copy
import datetime as dt
import threading
import unittest

from app import db
from app.accounts import _do_sync
from app.alerts import ALERT_TYPES
from app.database import (AccountSyncLog, Alert, Channel, EPGEntry, EpgSource, M3uAccount,
                          XtreamAccount)
from app.epg_sources import add_url_source
from sim import load_world, serve_for_test

from .app import make_test_app

# One provider on two Xtream panels - the second carries CUID, declares no stream origin and
# serves its streams from its own API host, a third of them spelled /live/ - and a plain M3U
# provider. Small enough that a sync costs little; big enough that a mass URL change crosses
# the 50-channel alert threshold.
FAULT_WORLD = {
    'seed': 11,
    'calibration': 'prod',
    'backends': [{'name': 'north', 'channels': 120, 'stream_hosts': 2},
                 {'name': 'vale', 'channels': 60}],
    'panels': [
        {'name': 'north-tv', 'backend': 'north', 'url_shape': {'bare': 0.9, 'live_ts': 0.1}},
        {'name': 'north-plus', 'backend': 'north', 'cuid': True, 'server_info': 'none',
         'streams_on_api_host': True, 'url_shape': {'bare': 0.61, 'live_ts': 0.39}},
        {'name': 'vale-m3u', 'backend': 'vale', 'kind': 'm3u'},
    ],
    'logins': [{'backend': 'north', 'panels': ['north-tv', 'north-plus']},
               {'backend': 'vale'}],
}


class SimSyncCase(unittest.TestCase):
    world_source = FAULT_WORLD

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        self.now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
        self.world = load_world(copy.deepcopy(self.world_source))
        self.sim = serve_for_test(self, self.world, clock=self.now)
        self.asked = []

    # --- accounts -----------------------------------------------------------------------

    def add_xtream(self, panel='north-tv', normalization=None) -> int:
        view = self.sim.account(panel)
        account = XtreamAccount(name=panel, base_url=view.base_url, username=view.username,
                                password=view.password, status='OK',
                                url_normalization=normalization)
        db.session.add(account)
        db.session.commit()
        return account.id

    def add_m3u(self, panel='vale-m3u') -> int:
        view = self.sim.account(panel)
        account = M3uAccount(name=panel, m3u_url=view.m3u_url, status='OK')
        db.session.add(account)
        db.session.flush()
        add_url_source(account, {'name': f'{panel} guide', 'url': view.epg_url})
        db.session.commit()
        return account.id

    def login(self, panel='north-tv') -> str:
        return self.sim.account(panel).username

    # --- syncing ------------------------------------------------------------------------

    def sync(self, account_id) -> AccountSyncLog:
        """One real sync. `self.asked` is what it asked the simulator, in order."""
        first = len(self.sim.ledger)
        _do_sync(account_id, threading.Event())
        db.session.expire_all()
        self.asked = self.sim.ledger.entries()[first:]
        return (AccountSyncLog.query.filter_by(account_id=account_id)
                .order_by(AccountSyncLog.id.desc()).first())

    # --- what a user would see ----------------------------------------------------------

    def account(self, account_id):
        found = db.session.get(XtreamAccount, account_id)
        return found if found is not None else db.session.get(M3uAccount, account_id)

    def channels(self, account_id) -> list:
        return Channel.query.filter_by(account_id=account_id).order_by(Channel.id).all()

    def entry_count(self, account_id) -> int:
        return (EPGEntry.query.join(Channel, Channel.id == EPGEntry.channel_id)
                .filter(Channel.account_id == account_id).count())

    def source_id(self, account_id) -> int:
        return EpgSource.query.filter_by(owner_account_id=account_id).one().id

    def open_alerts(self) -> dict:
        """{(alert_type, source): Alert} for every alert not dismissed."""
        rows = Alert.query.filter(Alert.dismissed_at.is_(None)).all()
        return {(a.alert_type, a.source): a for a in rows}

    def assertAlert(self, alert_type, source) -> Alert:
        self.assertIn(alert_type, ALERT_TYPES, 'not an alert type ChannelBin raises')
        found = self.open_alerts().get((alert_type, source))
        self.assertIsNotNone(found, f'no open {alert_type} under {source}; open: '
                                    f'{sorted(self.open_alerts())}')
        return found

    def assertNoAlert(self, alert_type, source=None):
        self.assertIn(alert_type, ALERT_TYPES, 'not an alert type ChannelBin raises')
        hits = [k for k in self.open_alerts() if k[0] == alert_type
                and (source is None or k[1] == source)]
        self.assertEqual(hits, [], f'unexpected open {alert_type}')

    def assertAsked(self, *endpoints):
        """The sync asked exactly these endpoints, in this order."""
        self.assertEqual([r.endpoint for r in self.asked], list(endpoints))
