"""Seats: connections counted per login at its backend, and the three over-limit policies
(DESIGN-simulator.md §3.4; dev/changelog/1219, 1220).

The default policy, kick_oldest with the cut-off connection still flowing for a few seconds, is
what a real provider did when a one-connection login opened a second (DESIGN-simulator.md
§3.10). Refuse and allow-then-ban are assumed. These characterize the simulator; what each
policy does is what the capture tier's connection-limit and account-block scenarios will be
judged against.
"""
import os
import sys
import threading
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import build_world, serve_for_test  # noqa: E402
from sim import streams  # noqa: E402

from tests.support.sim_streams import (library, live_ids, origin, read_for,  # noqa: E402
                                       stream_world, wait_until)


class _Seats(unittest.TestCase):
    over_limit = None

    def setUp(self):
        backend = {'over_limit': self.over_limit} if self.over_limit else {}
        self.world = stream_world(**backend)
        self.sim = serve_for_test(self, self.world, content=library())
        self.ids = live_ids(self.world)
        self.urls = self.sim.stream_urls('cove-tv', 'reeduser')
        self.url = self.urls[self.ids[0]]

    def hold(self, url):
        """An open connection with its first piece read, closed when the test ends."""
        r = requests.get(url, stream=True, timeout=10)
        self.addCleanup(r.close)
        if r.status_code == 200:
            next(r.iter_content(4096))
        return r

    def drains(self, r, seconds=3.0) -> bool:
        """True if the server ends this connection within `seconds`."""
        return self.draining(r).wait(seconds)

    def draining(self, r) -> threading.Event:
        """An event set when the server ends this connection."""
        done = threading.Event()

        def drain():
            try:
                for _ in r.iter_content(65536):
                    pass
            except requests.RequestException:
                pass
            done.set()

        threading.Thread(target=drain, daemon=True).start()
        return done


class RefuseTests(_Seats):
    over_limit = {'policy': 'refuse'}

    def test_one_connection_too_many_is_refused(self):
        self.hold(self.url)
        second = requests.get(self.urls[self.ids[1]], timeout=5)
        self.assertEqual(second.status_code, 403)
        self.assertIn('over the connection limit of 1', self.sim.ledger.entries()[-1].note)
        self.assertEqual(self.sim.connections('reeduser'), 1)

    def test_one_login_on_two_panels_has_one_set_of_seats(self):
        self.hold(self.url)
        plus = self.sim.stream_urls('cove-plus', 'reeduser')[self.ids[1]]
        self.assertEqual(requests.get(plus, timeout=5).status_code, 403)

    def test_another_account_on_the_backend_has_its_own_seats(self):
        self.hold(self.url)
        fern = self.sim.stream_urls('cove-tv', 'fernuser')
        a, b = self.hold(fern[self.ids[0]]), self.hold(fern[self.ids[1]])
        self.assertEqual((a.status_code, b.status_code), (200, 200))
        self.assertEqual(self.sim.connections('fernuser'), 2)

    def test_a_seat_comes_back_when_the_client_hangs_up(self):
        self.hold(self.url).close()
        self.assertTrue(wait_until(lambda: self.sim.connections('reeduser') == 0))
        self.assertEqual(read_for(self.url, 0.2)[0], 200)

    def test_an_hls_session_holds_a_seat(self):
        sid = self.ids[0]
        r = requests.get(f'{origin(self.url)}/live/reeduser/reedpass1/{sid}.m3u8', timeout=5)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(requests.get(self.url, timeout=5).status_code, 403)


class KickOldestTests(_Seats):
    over_limit = {'kick_delay_seconds': 1.0}

    def test_the_oldest_connection_is_cut_off_a_little_later_and_the_new_one_plays(self):
        """A real provider served the second connection and ended the first, cleanly, about
        5.5 s later (dev/changelog/1220); here the delay is 1 s."""
        first = self.hold(self.url)
        second = self.hold(self.urls[self.ids[1]])
        self.assertEqual(second.status_code, 200)
        ended = self.draining(first)
        self.assertFalse(ended.wait(0.5), 'the oldest connection ended at once')
        self.assertTrue(ended.wait(3.0), 'the oldest connection was not cut off')
        self.assertTrue(wait_until(lambda: any('cut off' in row.note for row in
                                               self.sim.ledger.entries(endpoint='stream'))))
        self.assertEqual(self.sim.connections('reeduser'), 1)


class AllowThenBanTests(_Seats):
    over_limit = {'policy': 'allow_then_ban', 'ban_after': 2}

    def test_one_over_is_allowed_and_the_next_bans_the_login(self):
        first = self.hold(self.url)
        second = self.hold(self.urls[self.ids[1]])
        self.assertEqual(second.status_code, 200)
        self.assertIn('allowed', self.sim.ledger.entries()[-1].note)
        third = requests.get(self.urls[self.ids[2]], timeout=5)
        self.assertEqual(third.status_code, 403)
        self.assertTrue(self.drains(first) and self.drains(second))
        acct = self.sim.account('cove-tv', 'reeduser')
        info = requests.get(f'{acct.base_url}/player_api.php', timeout=5,
                            params={'username': 'reeduser', 'password': 'reedpass1'}).json()
        self.assertEqual(info['user_info']['status'], 'Banned')


class MeasuredDefaultsTests(unittest.TestCase):
    """A backend's defaults are what a real provider was measured doing (DESIGN-simulator.md
    §3.10, dev/changelog/1220), and the assumed alternatives stay available."""

    def test_the_defaults_are_the_measured_answers(self):
        spec = stream_world().backends['cove']
        self.assertEqual(spec.over_limit.policy, 'kick_oldest')
        self.assertEqual(spec.over_limit.kick_delay_seconds, 5.5)
        self.assertTrue(spec.stream_redirect)
        self.assertEqual(stream_world(hls_segment_seconds=10.0).backends['cove']
                         .hls_segment_seconds, 10.0)
        bare = build_world({'seed': 1, 'backends': [{'name': 'b'}],
                            'panels': [{'name': 'p', 'backend': 'b'}],
                            'logins': [{'backend': 'b', 'username': 'abcuser',
                                        'password': 'abcpass1'}]}).backends['b']
        self.assertEqual((bare.hls_segment_seconds, bare.hls_window, bare.hls_idle_seconds),
                         (10.0, 6, 60.0))

    def test_a_backend_without_the_redirect_serves_where_it_was_asked(self):
        world = stream_world(stream_redirect=False)
        sim = serve_for_test(self, world, content=library())
        url = sim.stream_urls('cove-tv', 'reeduser')[live_ids(world)[0]]
        with requests.get(url, stream=True, allow_redirects=False, timeout=5) as r:
            self.assertEqual((r.status_code, r.headers['Content-Type']), (200, 'video/mp2t'))


class HlsSeatLapseTests(unittest.TestCase):
    """An HLS client holds no socket, so its seat lapses after it stops fetching."""

    def test_an_idle_hls_seat_lapses_and_a_fetching_one_does_not(self):
        world = stream_world()
        lg, spec = world.logins['reeduser'], world.backends['cove']
        seats = streams.Seats({'reeduser': 'Active'})
        now = [1000.0]
        with mock.patch.object(streams.time, 'monotonic', lambda: now[0]):
            grant = seats.acquire(lg, spec, streams.KIND_HLS, hls_idle=14.0)
            now[0] += 10
            grant.seat.touch()
            now[0] += 10
            self.assertEqual(seats.count('reeduser', 14.0), 1)
            now[0] += 5
            self.assertEqual(seats.count('reeduser', 14.0), 0)
            self.assertIsNotNone(seats.acquire(lg, spec, streams.KIND_TS, 14.0).seat)


if __name__ == '__main__':
    unittest.main()
