"""ChannelBin's real sync, over real HTTP, against a generated provider (dev/changelog/1217).

Until the simulator existed, sync tests replayed saved dumps through FileXtreamClient or
patched the fetch, so nothing exercised `requests` reading an actual response. These drive
the unmodified `_do_sync` against `sim/worlds/small.yaml` over loopback and check what was
imported against what the world says it served: every live channel and no VOD, each stream
URL exactly as the playlist spelled it, the guide entries for exactly the programs inside the
import window, and - from the simulator's ledger - each provider question asked once.

Characterization tests of today's sync rather than guards for a past defect; the faults
real providers have produced arrive as their own tests with the fault library.
"""
import datetime as dt
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import _do_sync  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (AccountSyncLog, Channel, EPGEntry, M3uAccount,  # noqa: E402
                          XtreamAccount)
from app.epg_sources import add_url_source  # noqa: E402
from sim import load_world, serve_for_test  # noqa: E402
from sim.guide import programs  # noqa: E402
from tests.support import make_test_app  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SMALL = os.path.join(ROOT, 'sim', 'worlds', 'small.yaml')
# ChannelBin imports listings from an hour before the sync (accounts._import_source); a
# program ending within this margin of that edge may land either side of it.
EDGE = dt.timedelta(seconds=90)


class _SimSync(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        # Held still for the test, so the window the simulator serves cannot move between the
        # sync's fetch and the expected answer computed afterwards.
        self.now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
        self.world = load_world(SMALL)
        self.sim = serve_for_test(self, self.world, clock=self.now)

    def sync(self, account_id):
        self.before = dt.datetime.utcnow()
        _do_sync(account_id, threading.Event())
        self.after = dt.datetime.utcnow()
        db.session.expire_all()

    def latest_log(self, account_id):
        return (AccountSyncLog.query.filter_by(account_id=account_id)
                .order_by(AccountSyncLog.id.desc()).first())

    def expected_rows(self, panel_name, login):
        panel = self.world.panels[panel_name]
        p = self.sim.provider
        return {p.stream_url(panel, row, login): row
                for row in self.world.panel_lineups[panel_name].rows}

    def expected_entries(self, panel_name, rows, days_ahead):
        """{(stream url, start, stop, title)} the import must hold: every program the panel's
        guide served for a channel's id, inside the import window, minus those on its edge."""
        panel = self.world.panels[panel_name]
        served_start, served_end = self.sim.provider.window(panel.guide_days_behind,
                                                            panel.guide_days_ahead)
        lo = self.before - dt.timedelta(hours=1)
        hi_edge = (self.before - dt.timedelta(hours=1) - EDGE,
                   self.after - dt.timedelta(hours=1) + EDGE)
        guide = {g.guide_id.lower(): g for g in self.world.panel_lineups[panel_name].guide}
        shape = self.sim.provider.shape
        out, edge = set(), set()
        for url, row in rows.items():
            g = guide.get(row.tvg_id.lower()) if row.tvg_id else None
            if g is None or not g.has_programs:
                continue
            for prog in programs(self.world.seed, g.station, served_start, served_end, shape):
                key = (url, prog.start, prog.stop, prog.title)
                if hi_edge[0] <= prog.stop <= hi_edge[1]:
                    edge.add(key)
                elif prog.stop >= lo and prog.start <= self.before + dt.timedelta(days=days_ahead):
                    out.add(key)
        return out, edge

    def imported_entries(self, account_id):
        rows = (db.session.query(Channel.raw_stream_url, EPGEntry.start_time, EPGEntry.stop_time,
                                 EPGEntry.title)
                .join(EPGEntry, EPGEntry.channel_id == Channel.id)
                .filter(Channel.account_id == account_id))
        return set(rows)


class XtreamSyncOverHttpTests(_SimSync):
    def test_a_generated_xtream_account_syncs_through_the_real_client(self):
        view = self.sim.account('harbor-tv')
        login = self.world.logins[view.username]
        account = XtreamAccount(name='Harbor TV', base_url=view.base_url,
                                username=view.username, password=view.password, status='OK')
        db.session.add(account)
        db.session.commit()

        self.sync(account.id)

        log = self.latest_log(account.id)
        self.assertEqual(log.status, 'SUCCESS', log.error_message)
        expected = self.expected_rows('harbor-tv', login)
        channels = Channel.query.filter_by(account_id=account.id).all()
        self.assertEqual(log.channels_synced, len(expected))
        self.assertEqual({c.raw_stream_url for c in channels}, set(expected),
                         'every live channel imported, under the URL the playlist spelled, '
                         'and none of the playlist\'s VOD rows')
        for c in channels:
            row = expected[c.raw_stream_url]
            self.assertEqual((c.name, c.category_name, c.epg_channel_id or ''),
                             (row.name, row.category_name, row.tvg_id))
            self.assertEqual(c.logo_url or '', self.sim.provider.logo_url(row.channel.logo))

        account = db.session.get(XtreamAccount, account.id)
        self.assertEqual(account.provider_status, 'Active')
        self.assertEqual(account.provider_max_connections, login.max_connections)
        stream_host = self.world.backends['harbor'].stream_host_names()[0]
        self.assertEqual(account.provider_stream_origin, self.sim.url(stream_host))

        want, edge = self.expected_entries('harbor-tv', expected,
                                           load_config()['sync']['epg_days_ahead'])
        self.assertTrue(want)
        got = self.imported_entries(account.id) - edge
        self.assertEqual(got, want)

        asked = [(r.path, r.action) for r in self.sim.ledger.entries(login=view.username)]
        self.assertEqual(sorted(asked, key=str), sorted([
            ('/player_api.php', None), ('/player_api.php', 'get_live_categories'),
            ('/get.php', None), ('/player_api.php', 'get_live_streams'),
            ('/xmltv.php', None)], key=str),
            'one sync asks each provider question exactly once')
        self.assertEqual(len(self.sim.ledger), len(asked))

    def test_a_second_panel_on_the_same_backend_imports_its_own_spelling(self):
        """The same login through a second panel that renames and re-files channels and spells
        a third of its URLs with /live/ (DESIGN-account-providers.md §3's shape)."""
        view = self.sim.account('harbor-plus')
        login = self.world.logins[view.username]
        account = XtreamAccount(name='Harbor Plus', base_url=view.base_url,
                                username=view.username, password=view.password, status='OK')
        db.session.add(account)
        db.session.commit()

        self.sync(account.id)

        self.assertEqual(self.latest_log(account.id).status, 'SUCCESS')
        expected = self.expected_rows('harbor-plus', login)
        urls = {c.raw_stream_url for c in Channel.query.filter_by(account_id=account.id)}
        self.assertEqual(urls, set(expected))
        self.assertTrue(any('/live/' in u for u in urls))
        account = db.session.get(XtreamAccount, account.id)
        self.assertEqual(account.provider_stream_origin, self.sim.url('harbor-plus'),
                         'this panel declares its own API host in server_info')


class M3uSyncOverHttpTests(_SimSync):
    def test_a_generated_m3u_account_syncs_with_its_gzipped_guide(self):
        view = self.sim.account('meadow-m3u')
        login = self.world.logins[view.username]
        account = M3uAccount(name='Meadow', m3u_url=view.m3u_url, status='OK')
        db.session.add(account)
        db.session.flush()
        add_url_source(account, {'name': 'Meadow guide', 'url': view.epg_url})
        db.session.commit()

        self.sync(account.id)

        log = self.latest_log(account.id)
        self.assertEqual(log.status, 'SUCCESS', log.error_message)
        expected = self.expected_rows('meadow-m3u', login)
        urls = {c.raw_stream_url for c in Channel.query.filter_by(account_id=account.id)}
        self.assertEqual(urls, set(expected))
        want, edge = self.expected_entries('meadow-m3u', expected,
                                           load_config()['sync']['epg_days_ahead'])
        self.assertTrue(want)
        self.assertEqual(self.imported_entries(account.id) - edge, want)
        self.assertEqual(self.sim.ledger.count(login=view.username), 2)


class LogoOverHttpTests(_SimSync):
    def test_the_logo_cache_stores_a_served_logo(self):
        from app.logo_cache import _fetch_one_logo

        view = self.sim.account('harbor-tv')
        row = next(r for r in self.world.panel_lineups['harbor-tv'].rows if r.channel.logo)
        account = XtreamAccount(name='Harbor TV', base_url=view.base_url,
                                username=view.username, password=view.password, status='OK')
        db.session.add(account)
        db.session.flush()
        channel = Channel(account_id=account.id, stream_id=row.channel.stream_id, name=row.name,
                          stream_url='http://unused.invalid/', in_guide=True,
                          logo_url=self.sim.provider.logo_url(row.channel.logo))
        db.session.add(channel)
        db.session.commit()
        cache_dir = os.path.join(self.t._tmpdir, 'logos')

        _fetch_one_logo(channel, cache_dir, load_config())

        db.session.expire_all()
        channel = db.session.get(Channel, channel.id)
        self.assertEqual(channel.logo_cache_path, f'{channel.id}.png')
        with open(os.path.join(cache_dir, channel.logo_cache_path), 'rb') as f:
            self.assertEqual(f.read()[:8], b'\x89PNG\r\n\x1a\n')


if __name__ == '__main__':
    unittest.main()
