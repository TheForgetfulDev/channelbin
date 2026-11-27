"""A login's own hosts (app/account_links.py, app/connection_limits.py,
DESIGN-account-providers.md §14, dev/changelog/1778): a stream seated on a login that lists
hosts goes out on that login's active host, on every account holding it; a login with none
uses the account's hosts exactly as before.

The properties guarded here, each of which fails in its own way:

  * with no login listing a host, launches, seats and refusals are what they were (§14.10);
  * two logins from two resellers on one account launch on two hosts, and a URL on a host
    neither list names is launched as it is;
  * the hosts a login would use decide its seats: its own list when it has one, else the
    account's - so a guide-service account whose hosts are all off still streams through a
    login that brings its own, and a login whose own hosts are all off takes no seat;
  * the three "nothing can stream" reasons, each with its sentence;
  * the writers: the first host is active, Copy takes only the account's hosts that are on
    and leaves the account's list alone, switching and removing hand the active role on;
  * a resolution failure rolls the list the launch was rendered from and no other;
  * every path that deletes a login takes its hosts and its roll alert with it.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_login_hosts
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import account_links as links  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.alerts import ACCOUNT_HOST_ROLLED, LOGIN_HOST_ROLLED  # noqa: E402
from app.database import (  # noqa: E402
    Account, AccountHost, AccountLogin, Alert, Channel, Login, LoginHost, Provider,
)

OLD = 'old.curation.example'
A1, A2 = 'h1.alpha.example', 'h2.alpha.example:8080'
B1 = 'b1.bravo.example'


class _Case(unittest.TestCase):
    """harbor-curated: a guide-service account whose stream URLs are on its playlist's dead
    host with the playlist's dead credentials, holding two logins from two resellers."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.app.test_client()
        acc = seed.make_account(name='harbor-curated', max_connections=1)
        for i in (1, 2):
            ch = seed.make_channel(acc, stream_id=i, name=f'ch{i}')
            ch.stream_url = ch.raw_stream_url = f'http://{OLD}/live/dead/dpw/{i}.ts'
        radio = seed.make_channel(acc, stream_id=9, name='radio')
        radio.stream_url = radio.raw_stream_url = 'http://radio.example:8000/mount'
        db.session.commit()
        self.acc_id = acc.id
        self.radio_url = 'http://radio.example:8000/mount'
        self.url = f'http://{OLD}/live/dead/dpw/1.ts'
        connlim._holders.clear()
        connlim._seat_of.clear()

    def tearDown(self):
        connlim._holders.clear()
        connlim._seat_of.clear()
        self.t.cleanup()

    def _logins(self):
        """The playlist's host on the account's list, as a sync lists it (§13.2); the
        playlist's login (listed first by the first add, then turned off), alpha and bravo,
        one seat each. Returns (playlist, alpha, bravo) ids."""
        links.add_host(self.acc_id, OLD)
        alpha = links.add_login(self.acc_id, 'alpha', 'a1', 'pa', 1).id
        bravo = links.add_login(self.acc_id, 'bravo', 'b1', 'pb', 1).id
        playlist = next(lg.id for lg in links.logins_for_account(self.acc_id)
                        if lg.id not in (alpha, bravo))
        links.set_login_enabled(self.acc_id, playlist, False)
        db.session.expire_all()
        return playlist, alpha, bravo

    def _account_host_off(self):
        row = AccountHost.query.filter_by(account_id=self.acc_id, host=OLD).one()
        links.set_host_enabled(self.acc_id, row.id, False)
        db.session.expire_all()


class NothingListedTests(_Case):
    """§14.10: with the table empty, every answer is what it was before it existed. A URL
    on a host on no list at all (rule 4's condition) is covered by the radio mount below."""

    def test_launches_and_seats_are_unchanged_with_no_login_hosts(self):
        _pl, alpha, bravo = self._logins()
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 1))
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 2))
        self.assertFalse(connlim.try_acquire(self.acc_id, 'recording', 3))
        logins = links.logins_for_account(self.acc_id)
        for rid, lid in ((1, alpha), (2, bravo)):
            lg = next(x for x in logins if x.id == lid)
            launch = links.render_held_login(self.url, self.acc_id, 'recording', rid)
            self.assertEqual(launch.url, links.render_login(self.url, logins, lg))
            self.assertEqual((launch.login_id, launch.host_login_id), (lid, None))
        self.assertEqual(links.unusable_reasons([self.acc_id]), {})

    def test_every_account_host_off_still_stops_every_login_that_lists_none(self):
        self._logins()
        self._account_host_off()
        self.assertFalse(connlim.try_acquire(self.acc_id, 'recording', 1))
        self.assertEqual(links.unusable_reasons([self.acc_id]),
                         {self.acc_id: links.UNUSABLE_HOSTS})


class TwoResellersTests(_Case):

    def test_each_login_launches_on_its_own_resellers_host(self):
        _pl, alpha, bravo = self._logins()
        links.add_login_host(alpha, A1)
        links.add_login_host(bravo, B1)
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 1))
        self.assertTrue(connlim.try_acquire(self.acc_id, 'preview', 's1'))
        a = links.render_held_login(self.url, self.acc_id, 'recording', 1)
        b = links.render_held_login(self.url, self.acc_id, 'preview', 's1')
        self.assertEqual(a, (f'http://{A1}/live/a1/pa/1.ts', alpha, alpha))
        self.assertEqual(b, (f'http://{B1}/live/b1/pb/1.ts', bravo, bravo))

    def test_a_login_with_no_hosts_beside_one_with_hosts_keeps_the_stored_host(self):
        _pl, alpha, bravo = self._logins()
        links.add_login_host(alpha, A1)
        connlim.try_acquire(self.acc_id, 'recording', 1)   # alpha
        connlim.try_acquire(self.acc_id, 'recording', 2)   # bravo
        b = links.render_held_login(self.url, self.acc_id, 'recording', 2)
        self.assertEqual(b, (f'http://{OLD}/live/b1/pb/1.ts', bravo, None))

    def test_a_url_on_a_host_neither_list_names_is_launched_as_it_is(self):
        _pl, alpha, _b = self._logins()
        links.add_login_host(alpha, A1)
        connlim.try_acquire(self.acc_id, 'recording', 1)
        launch = links.render_held_login(self.radio_url, self.acc_id, 'recording', 1)
        self.assertEqual(launch.url, self.radio_url)

    def test_a_url_on_no_list_is_not_counted_as_on_the_logins_list(self):
        _pl, alpha, _b = self._logins()
        links.add_login_host(alpha, A1)
        connlim.try_acquire(self.acc_id, 'recording', 1)
        launch = links.render_held_login(self.radio_url, self.acc_id, 'recording', 1)
        self.assertIsNone(launch.host_login_id, 'a failure on it must not roll the login\'s list')

    def test_a_seat_kept_after_every_own_host_went_off_stays_on_the_logins_host(self):
        _pl, alpha, _b = self._logins()
        row = links.add_login_host(alpha, A1)
        connlim.try_acquire(self.acc_id, 'recording', 1)
        links.set_login_host_enabled(alpha, row.id, False)
        launch = links.render_held_login(self.url, self.acc_id, 'recording', 1)
        self.assertEqual(launch.url, f'http://{A1}/live/a1/pa/1.ts',
                         'never the account\'s dead host with this login\'s credentials')

    def test_the_port_travels_with_the_host_and_the_scheme_stays_the_urls(self):
        _pl, alpha, _b = self._logins()
        links.add_login_host(alpha, A2)
        connlim.try_acquire(self.acc_id, 'recording', 1)
        launch = links.render_held_login(self.url, self.acc_id, 'recording', 1)
        self.assertEqual(launch.url, f'http://{A2}/live/a1/pa/1.ts')

    def test_next_stream_and_copy_name_the_logins_own_host(self):
        _pl, alpha, _b = self._logins()
        links.add_login_host(alpha, A1)
        acc = db.session.get(Account, self.acc_id)
        nxt = links.next_stream(acc)
        self.assertEqual((nxt['state'], nxt['login'].id, nxt['host']), (links.NEXT_FREE, alpha, A1))
        self.assertEqual(links.next_stream_url(self.url, self.acc_id, nxt),
                         f'http://{A1}/live/a1/pa/1.ts')


class SeatsTests(_Case):

    def test_a_guide_account_with_every_host_off_streams_through_a_login_with_its_own(self):
        _pl, alpha, bravo = self._logins()
        self._account_host_off()
        links.add_login_host(alpha, A1)
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 1))
        self.assertEqual(connlim.held_login_id('recording', 1), alpha)
        self.assertFalse(connlim.try_acquire(self.acc_id, 'recording', 2),
                         'bravo lists no hosts and the account\'s only host is off')
        self.assertEqual(links.unusable_reasons([self.acc_id]), {})

    def test_a_login_whose_own_hosts_are_all_off_takes_no_seat_anywhere(self):
        _pl, alpha, bravo = self._logins()
        row = links.add_login_host(alpha, A1)
        links.set_login_host_enabled(alpha, row.id, False)
        seats = dict((lid, n) for lid, n, _r in links.login_seats_for_accounts([self.acc_id])[self.acc_id])
        self.assertEqual((seats[alpha], seats[bravo]), (0, 1))
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 1))
        self.assertEqual(connlim.held_login_id('recording', 1), bravo)

    def test_a_running_recording_moves_off_a_login_whose_hosts_all_went_off(self):
        _pl, alpha, bravo = self._logins()
        row = links.add_login_host(alpha, A1)
        connlim.try_acquire(self.acc_id, 'recording', 1)
        self.assertEqual(connlim.held_login_id('recording', 1), alpha)
        links.set_login_host_enabled(alpha, row.id, False)
        self.assertEqual(connlim.reseat_before_relaunch(self.acc_id, 'recording', 1), bravo)

    def test_the_three_reasons_nothing_can_stream(self):
        playlist, alpha, bravo = self._logins()
        self._account_host_off()
        row = links.add_login_host(alpha, A1)
        links.set_login_host_enabled(alpha, row.id, False)
        self.assertEqual(links.unusable_reasons([self.acc_id]),
                         {self.acc_id: links.UNUSABLE_LOGIN_HOSTS})
        self.assertEqual(links.describe_unusable('harbor-curated', links.UNUSABLE_LOGIN_HOSTS),
                         'every host the logins on account "harbor-curated" would use is turned off')
        links.set_login_enabled(self.acc_id, alpha, False)
        self.assertEqual(links.unusable_reasons([self.acc_id]),
                         {self.acc_id: links.UNUSABLE_HOSTS})
        links.set_login_enabled(self.acc_id, bravo, False)
        self.assertEqual(links.unusable_reasons([self.acc_id]),
                         {self.acc_id: links.UNUSABLE_LOGINS})
        with self.assertRaises(ValueError):
            links.describe_unusable('x', 'something new')


class WriterTests(_Case):

    def test_the_first_host_is_active_and_a_duplicate_is_refused(self):
        _pl, alpha, _b = self._logins()
        first = links.add_login_host(alpha, 'H1.Alpha.example')
        second = links.add_login_host(alpha, A2)
        db.session.expire_all()
        self.assertEqual((first.host, db.session.get(LoginHost, first.id).is_active), (A1, True))
        self.assertFalse(db.session.get(LoginHost, second.id).is_active)
        with self.assertRaises(ValueError):
            links.add_login_host(alpha, A1)

    def test_copy_takes_the_account_hosts_that_are_on_and_leaves_the_account_alone(self):
        _pl, alpha, _b = self._logins()
        links.add_host(self.acc_id, A1)
        off = links.add_host(self.acc_id, 'dead.alpha.example')
        links.set_host_enabled(self.acc_id, off.id, False)
        links.add_host(self.acc_id, A2)
        before = [(h.host, h.enabled, h.is_active) for h in
                  AccountHost.query.filter_by(account_id=self.acc_id).order_by(AccountHost.position)]
        added = links.copy_account_hosts_to_login(self.acc_id, alpha)
        self.assertEqual(added, [OLD, A1, A2])
        self.assertEqual(links.copy_account_hosts_to_login(self.acc_id, alpha), [])
        after = [(h.host, h.enabled, h.is_active) for h in
                 AccountHost.query.filter_by(account_id=self.acc_id).order_by(AccountHost.position)]
        self.assertEqual(before, after)
        rows = links.login_hosts_for_logins([alpha])[alpha]
        self.assertEqual([r.is_active for r in rows], [True, False, False])

    def test_switching_and_removing_hand_the_active_role_on(self):
        _pl, alpha, _b = self._logins()
        a1 = links.add_login_host(alpha, A1)
        a2 = links.add_login_host(alpha, A2)
        out = links.set_login_host_enabled(alpha, a1.id, False)
        self.assertEqual(out['active'], A2)
        with self.assertRaises(ValueError):
            links.set_login_host_active(alpha, a1.id)
        links.set_login_host_enabled(alpha, a1.id, True)
        links.set_login_host_active(alpha, a1.id)
        links.remove_login_host(alpha, a1.id)
        db.session.expire_all()
        self.assertTrue(db.session.get(LoginHost, a2.id).is_active)
        links.remove_login_host(alpha, a2.id)
        self.assertEqual(links.login_host_state([alpha]), {})


class RollTests(_Case):

    def _alerts(self, kind):
        return Alert.query.filter_by(alert_type=kind, dismissed_at=None).all()

    def test_a_failure_on_a_logins_host_rolls_that_list_and_no_other(self):
        _pl, alpha, _b = self._logins()
        links.add_host(self.acc_id, 'spare.curation.example')
        links.add_login_host(alpha, A1)
        links.add_login_host(alpha, A2)
        ch_id = Channel.query.filter_by(account_id=self.acc_id).first().id
        with mock.patch.object(links, 'resolve_host', return_value=(True, None)):
            out = links.roll_host_for_channel(ch_id, 'Recording 7', recording_id=None,
                                              host_login_id=alpha)
        db.session.expire_all()
        self.assertEqual(out['host'], A2)
        self.assertEqual(links.host_list(self.acc_id)[1], OLD, 'the account\'s list did not move')
        self.assertEqual(self._alerts(ACCOUNT_HOST_ROLLED), [])
        alert = self._alerts(LOGIN_HOST_ROLLED)
        self.assertEqual(len(alert), 1)
        self.assertIn('Login "alpha"', alert[0].title)
        links.set_login_host_active(alpha, LoginHost.query.filter_by(host=A1).one().id)
        self.assertEqual(self._alerts(LOGIN_HOST_ROLLED), [], 'editing the list clears it')

    def test_a_failure_on_the_accounts_host_still_rolls_the_account(self):
        self._logins()
        links.add_host(self.acc_id, 'spare.curation.example')
        ch_id = Channel.query.filter_by(account_id=self.acc_id).first().id
        with mock.patch.object(links, 'resolve_host', return_value=(True, None)):
            out = links.roll_host_for_channel(ch_id, 'Recording 7', host_login_id=None)
        self.assertEqual(out['host'], 'spare.curation.example')
        self.assertEqual(self._alerts(LOGIN_HOST_ROLLED), [])


class TeardownTests(_Case):

    def _raise_alert(self, login_id):
        from app.accounts import _raise_or_resolve_standing_alert
        _raise_or_resolve_standing_alert(LOGIN_HOST_ROLLED, source=f'login:{login_id}:host-rolled',
                                         active=True, title='t', body='b')

    def test_removing_a_login_nobody_else_holds_takes_its_hosts_and_alert(self):
        _pl, alpha, _b = self._logins()
        links.add_login_host(alpha, A1)
        self._raise_alert(alpha)
        links.remove_login(self.acc_id, alpha)
        db.session.expire_all()
        self.assertEqual(LoginHost.query.filter_by(login_id=alpha).count(), 0)
        self.assertEqual(Alert.query.filter_by(alert_type=LOGIN_HOST_ROLLED, dismissed_at=None).count(), 0)

    def test_deleting_the_account_takes_the_hosts_of_the_logins_it_alone_held(self):
        _pl, alpha, _b = self._logins()
        links.add_login_host(alpha, A1)
        self._raise_alert(alpha)
        links.delete_logins_for_account(self.acc_id)
        db.session.commit()
        self.assertEqual(LoginHost.query.count(), 0)
        self.assertEqual(Alert.query.filter_by(alert_type=LOGIN_HOST_ROLLED, dismissed_at=None).count(), 0)

    def test_dropping_a_share_keeps_the_hosts_with_the_other_holder(self):
        _pl, alpha, _b = self._logins()
        links.add_login_host(alpha, A1)
        other = seed.make_account(name='harbor-direct')
        db.session.add(Provider(name='Harbor'))
        db.session.commit()
        prov = Provider.query.one()
        for acc_id in (self.acc_id, other.id):
            links.set_account_provider(acc_id, prov.id)
        links.share_login(alpha, other.id)
        links.remove_login(self.acc_id, alpha)
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Login, alpha))
        self.assertEqual(LoginHost.query.filter_by(login_id=alpha).count(), 1)
        self.assertEqual(AccountLogin.query.filter_by(login_id=alpha).count(), 1)



class PageAndRouteTests(_Case):
    """The Logins card says where each login's streams go, its Hosts dialog is in the page,
    and the routes write through the module's writers."""

    def _page(self):
        db.session.remove()
        r = self.client.get(f'/accounts/{self.acc_id}')
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def test_add_login_with_hosts_connects_the_two_in_one_form(self):
        self._logins()
        r = self.client.post(f'/api/accounts/{self.acc_id}/logins', json={
            'name': 'charlie', 'username': 'c1', 'password': 'pc', 'max_connections': 1,
            'hosts': [A1, 'http://H2.alpha.example:8080/', '']})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn(f'go out on {A1}', r.get_json()['message'])
        lid = r.get_json()['login_id']
        self.assertEqual([h.host for h in links.login_hosts_for_logins([lid])[lid]], [A1, A2])

    def test_a_bad_host_adds_no_login(self):
        self._logins()
        before = Login.query.count()
        r = self.client.post(f'/api/accounts/{self.acc_id}/logins', json={
            'name': 'charlie', 'username': 'c1', 'password': 'pc', 'max_connections': 1,
            'hosts': ['not a host!']})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Login.query.count(), before)

    def test_the_routes_add_copy_switch_activate_and_remove(self):
        _pl, alpha, _b = self._logins()
        base = f'/api/accounts/{self.acc_id}/logins/{alpha}/hosts'
        self.assertEqual(self.client.post(base, json={'host': A1}).status_code, 200)
        r = self.client.post(f'{base}/copy')
        self.assertEqual(r.get_json()['added'], [OLD])
        self.assertIn('from harbor-curated', r.get_json()['message'], 'names the account, not the login')
        rows = links.login_hosts_for_logins([alpha])[alpha]
        a1, old = rows[0].id, rows[1].id
        r = self.client.post(f'{base}/{a1}/enabled', json={'enabled': False})
        self.assertIn(f'now go out on {OLD}', r.get_json()['message'])
        self.assertEqual(self.client.post(f'{base}/{a1}/activate').status_code, 409)
        self.assertEqual(self.client.post(f'{base}/{a1}/enabled', json={'enabled': 'false'}).status_code, 400)
        self.assertEqual(self.client.delete(f'{base}/{old}').status_code, 200)
        self.assertEqual(self.client.post(f'/api/accounts/9999/logins/{alpha}/hosts',
                                          json={'host': B1}).status_code, 404)

    def test_the_logins_card_names_each_logins_hosts(self):
        _pl, alpha, bravo = self._logins()
        self._account_host_off()
        links.add_login_host(alpha, A1)
        links.add_login_host(alpha, A2)
        html = self._page()
        self.assertIn(f'Hosts: <code>{A1}</code> (active) and 1 more', html)
        self.assertIn("Uses this account's hosts", html)
        self.assertIn('No hosts on', html)
        self.assertIn(f'<template data-login-hosts="{alpha}">', html)
        self.assertIn('data-act="login-hosts"', html)
        self.assertIn('1 login uses its own hosts instead: alpha.', html)
        self.assertIn("Every host here is off. Streams go out on each login's own hosts.", html)
        self.assertNotIn('Nothing on this account can stream', html)


class RedirectTests(_Case):
    """§14.4: the output feed's redirect hands out what Copy hands out - the next stream's host
    and credentials - and the stored URL, byte for byte, for an account that lists nothing."""

    def _redirect(self, ch):
        from types import SimpleNamespace
        from app import output_feed
        play = SimpleNamespace(member=ch, fault=None, label=ch.name, note=None, why_none='')
        return output_feed.redirect_target(1, '127.0.0.1', play=play)

    def test_the_redirect_goes_to_the_next_logins_own_host(self):
        _pl, alpha, _b = self._logins()
        links.add_login_host(alpha, A1)
        ch = Channel.query.filter_by(account_id=self.acc_id, stream_id=1).one()
        self.assertEqual(self._redirect(ch), f'http://{A1}/live/a1/pa/1.ts')

    def test_an_account_listing_nothing_redirects_to_the_stored_url(self):
        ch = Channel.query.filter_by(account_id=self.acc_id, stream_id=1).one()
        self.assertEqual(self._redirect(ch), self.url)


class RelayRollTests(_Case):
    """The output feed's relay rolls on a resolution failure, handing the roll the list its
    launch was rendered from (§14.6) - it rolled nothing before."""

    def test_an_unresolved_relay_rolls_the_launchs_list(self):
        import threading
        from types import SimpleNamespace
        from app import output_feed
        done = threading.Event()
        calls = []

        def fake_roll(channel_id, trigger, host_login_id=None, recording_id=None):
            calls.append((channel_id, host_login_id))
            done.set()

        session = SimpleNamespace(app=self.t.app, channel_id=7, host_login_id=42,
                                  channel_name='ch', id='v1')
        with mock.patch('app.account_links.roll_host_for_channel', side_effect=fake_roll):
            output_feed._roll_if_unresolved(session, 'Failed to resolve hostname x: Name or service not known')
            self.assertTrue(done.wait(5))
            output_feed._roll_if_unresolved(session, 'HTTP error 404 Not Found')
        self.assertEqual(calls, [(7, 42)])


if __name__ == '__main__':
    unittest.main()
