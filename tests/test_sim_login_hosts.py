"""Two resellers' logins on one guide-service account, each launched on its own reseller's
host at every launch site, against a simulated provider whose hosts refuse the other
reseller's login (DESIGN-account-providers.md §14.12, dev/changelog/1778).

The simulator serves one backend on two stream hosts. A `wrong_reseller_login` fault on each
makes it a reseller's: the first serves `reeduser` only, the second `fernuser` only, and a
login on the wrong one is answered 401 (sim/faults.py; assumed behavior, its evidence says
why). The account is set up the way §14.5 describes a guide-service account: its stream URLs
carry the playlist's dead host and dead login, that host is its only row and is turned off, and
each login brings its reseller's host.

Each launch site's own command URL is caught where the command is built - the recorder's
segment, the channel test, the preview (which the channel page's screenshot goes through) and
the output feed's relay (which a picture-in-picture channel's pictures are) - and dialed on
the simulator, which says whether that host served that login. Nothing about which login is
seated or which host is rendered is mocked.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sim_login_hosts
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import serve_for_test  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.sim_streams import library, live_ids, read_for, stream_world, wait_until  # noqa: E402
from app import db  # noqa: E402
from app import account_links as links  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.database import AccountHost, REC_STATUS_IN_PROGRESS  # noqa: E402

OLD = 'old.curation.example'
REED, FERN = ('reeduser', 'reedpass1'), ('fernuser', 'fernpass1')


class _Built(RuntimeError):
    """Raised from a patched command builder once the launch URL is known."""


class _Rig(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        connlim._holders.clear()
        connlim._seat_of.clear()
        self.addCleanup(connlim._seat_of.clear)
        self.addCleanup(connlim._holders.clear)
        world = stream_world()
        self.sim = serve_for_test(self, world, content=library())
        self.h1, self.h2 = world.backends['cove'].stream_host_names()
        self.sim.inject('wrong_reseller_login', self.h1, accepts=[REED[0]])
        self.sim.inject('wrong_reseller_login', self.h2, accepts=[FERN[0]])
        self.addr = {h: links.host_of(self.sim.url(h)) for h in (self.h1, self.h2)}

        acc = seed.make_account(name='harbor-curated', max_connections=2)
        self.channel_ids = []
        for sid in live_ids(world)[:2]:
            ch = seed.make_channel(acc, stream_id=sid, name=f'ch{sid}')
            ch.stream_url = ch.raw_stream_url = f'http://{OLD}/deaduser/deadpw/{sid}'
            self.channel_ids.append(ch.id)
        db.session.commit()
        self.acc_id = acc.id
        links.add_host(self.acc_id, OLD)
        self.alpha = links.add_login(self.acc_id, 'alpha', *REED, 1).id
        self.bravo = links.add_login(self.acc_id, 'bravo', *FERN, 1).id
        playlist = next(lg.id for lg in links.logins_for_account(self.acc_id)
                        if lg.id not in (self.alpha, self.bravo))
        links.set_login_enabled(self.acc_id, playlist, False)
        db.session.expire_all()

    def resellers(self):
        """Each login's own host, and the playlist's dead host turned off (§14.5)."""
        links.add_login_host(self.alpha, self.addr[self.h1])
        links.add_login_host(self.bravo, self.addr[self.h2])
        row = AccountHost.query.filter_by(account_id=self.acc_id, host=OLD).one()
        links.set_host_enabled(self.acc_id, row.id, False)
        db.session.expire_all()

    def stored_url(self, n=0):
        from app.database import Channel
        return db.session.get(Channel, self.channel_ids[n]).stream_url

    def hold_alpha(self):
        """Alpha's one seat taken, so the next launch is seated on bravo."""
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 900))
        self.assertEqual(connlim.held_login_id('recording', 900), self.alpha)

    def dial(self, url):
        """(status, bytes read) of the launch URL on the simulator, its connection let go."""
        status, data = read_for(url, 0.3)
        for user in (REED[0], FERN[0]):
            self.assertTrue(wait_until(lambda: self.sim.connections(user) == 0), user)
        return status, len(data)

    def assertServed(self, url, host, cred):
        """`url` goes out on `host` with `cred`, and the simulator plays it."""
        self.assertEqual(links.host_of(url), self.addr[host], url)
        self.assertIn(f'/{cred[0]}/{cred[1]}/', url)
        status, got = self.dial(url)
        self.assertEqual(status, 200, f'{url} was refused')
        self.assertGreater(got, 0)
        asked = [r for r in self.sim.ledger.entries(endpoint='stream')
                 if r.host == host and r.login == cred[0]]
        self.assertTrue(asked, f'{host} never saw {cred[0]}')

    # The four launch sites, each returning the URL its command was built with.

    def recorder_url(self):
        from app import recorder
        rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, channel_id=self.channel_ids[0],
                                  url=self.stored_url())
        db.session.commit()
        rid = rec.id
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', rid))
        seen = {}

        def build(cfg, url, *a, **kw):
            seen['url'] = url
            raise _Built()

        with mock.patch.object(recorder, 'build_capture_cmd', build), self.assertRaises(_Built):
            recorder._launch_segment(self.t.app, rid, 1)
        connlim.release(self.acc_id, 'recording', rid)
        return seen['url']

    def channel_test_url(self):
        from app import channel_tester
        seen = {}

        def build(cfg, url, *a, **kw):
            seen['url'] = url
            raise _Built()

        with mock.patch('app.proc_utils.build_capture_cmd', build), self.assertRaises(_Built):
            channel_tester.run_channel_test(self.t.app, self.channel_ids[0])
        return seen['url']

    def preview_url(self):
        from app import preview
        seen = {}
        real = preview.build_preview_cmd

        def build(cfg, url, *a, **kw):
            seen['url'] = url
            return real(cfg, url, *a, **kw)

        with mock.patch.object(preview, 'build_preview_cmd', build), \
                mock.patch.object(preview.subprocess, 'Popen', side_effect=OSError('caught')), \
                self.t.app.test_request_context():
            with self.assertRaises(preview.PreviewRefused):
                preview.start_preview(self.channel_ids[0])
        preview._sessions.clear()
        return seen['url']

    def relay_url(self):
        import app.output_feed as feed
        from app.config import load_config
        from app.database import Channel
        member = db.session.get(Channel, self.channel_ids[0])
        session = feed.RelaySession(
            id=f'v-{time.monotonic_ns()}', feed_id=1, label='ch', client='192.168.1.50',
            group_id=None, channel_id=member.id, channel_name=member.name,
            account_id=self.acc_id, ring=feed.Ring(1024, 0), started_mono=time.monotonic(),
            app=self.t.app)
        seen = {}

        def build(cfg, url, *a, **kw):
            seen['url'] = url
            raise _Built()

        with mock.patch.object(feed, 'build_relay_cmd', build), self.assertRaises(_Built):
            feed._launch(session, member, load_config())
        connlim.release(self.acc_id, 'viewer', session.id)
        return seen['url']


class EachLaunchSiteTests(_Rig):
    """Alpha's launch goes out on the first reseller's host with alpha's login, bravo's on the
    second's with bravo's, at every launch site."""

    def _both(self, launch):
        self.resellers()
        self.assertServed(launch(), self.h1, REED)
        self.hold_alpha()
        self.assertServed(launch(), self.h2, FERN)

    def test_the_recorder(self):
        self._both(self.recorder_url)

    def test_the_channel_test(self):
        self._both(self.channel_test_url)

    def test_the_preview(self):
        self._both(self.preview_url)

    def test_the_output_feed_relay(self):
        self._both(self.relay_url)

    def test_the_output_feed_redirect_hands_out_the_next_logins_own_host(self):
        """§14.4: the redirect takes no seat, so it answers with what Copy hands out."""
        from types import SimpleNamespace
        import app.output_feed as feed
        from app.database import Channel
        self.resellers()
        ch = db.session.get(Channel, self.channel_ids[0])
        play = SimpleNamespace(member=ch, fault=None, label=ch.name, note=None, why_none='')
        self.assertServed(feed.redirect_target(1, '127.0.0.1', play=play), self.h1, REED)


class TheMistakeItPreventsTests(_Rig):
    """Before a login could carry its own hosts, a login was launched on the account's active
    host whoever's reseller that was. The simulator refuses that launch; the login's own host
    is what makes it play."""

    def test_a_login_on_the_other_resellers_host_is_refused_until_it_brings_its_own(self):
        row = links.add_host(self.acc_id, self.addr[self.h2])
        links.set_active_host(self.acc_id, row.id)
        db.session.expire_all()
        url = self.recorder_url()     # seated on alpha, the first reseller's login
        self.assertEqual((links.host_of(url), f'/{REED[0]}/' in url), (self.addr[self.h2], True))
        self.assertEqual(self.dial(url)[0], 401, 'the second reseller\'s host refuses alpha')
        links.add_login_host(self.alpha, self.addr[self.h1])
        self.assertServed(self.recorder_url(), self.h1, REED)
        self.hold_alpha()
        self.assertServed(self.recorder_url(), self.h2, FERN)


if __name__ == '__main__':
    unittest.main()
