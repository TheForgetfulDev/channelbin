"""The playlist's own host and login as rows, and the on/off switch on every row
(app/account_links.py, app/connection_limits.py, app/accounts.py; dev/changelog/1670,
DESIGN-account-providers.md §13).

The properties guarded here, each of which fails in its own way:

  * listing the playlist's rows changes nothing while every switch is on - the same stream
    URLs byte for byte, the same seats (the playlist login's seats are the account's own Max
    connections or the global default), the same launch URL;
  * a login turned off takes no new seat and its credentials are swapped for a login that is
    on; every login off refuses every seat, and every refusal surface names the switch rather
    than the connection limit;
  * a host turned off is never the active one: turning the active host off hands the role on
    and rewrites the URLs, every host off refuses every seat, the roll skips hosts that are off;
  * a recording's relaunch moves its seat off a login turned off since, when another is free,
    and keeps it when none is;
  * the row follows the playlist and the switch follows the user: a changed host or login
    rewrites the row, keeps its switch, and raises one alert naming old and new, never a
    password;
  * a login the user already listed with the playlist's credentials is adopted, not doubled;
    a playlist login listed beside the user's own logins starts off;
  * an account's delete leaves its shared playlist login with the account sharing it, with a
    seat count of its own;
  * migration 138 makes logins.max_connections nullable on an old-shape table.

Runs against throwaway temp SQLite DBs - never the live dvr.db.
  python3 -m unittest tests.test_playlist_rows
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, preview  # noqa: E402
from app import account_links as links  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app import migrations as M  # noqa: E402
from app.account_blocks import account_limits  # noqa: E402
from app.alerts import ACCOUNT_PLAYLIST_CHANGED, ACCOUNT_PLAYLIST_CHANGED_UNUSED  # noqa: E402
from app.database import AccountHost, AccountLogin, Alert, Channel, Login  # noqa: E402

PLAYLIST_HOST = 'cdn.oldreseller.example'
PLAYLIST_PAIR = ('k8x2expired', 'oldpw')


class _Case(unittest.TestCase):
    """One guide-service account whose three stream URLs carry the playlist's host and login,
    plus a radio mount with no triplet. One connection of its own."""

    max_connections = 1

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.app.test_client()
        self.acc = seed.make_account(name='northwind-guide', max_connections=self.max_connections)
        for i in range(1, 4):
            ch = seed.make_channel(self.acc, stream_id=i, name=f'ch{i}')
            ch.stream_url = ch.raw_stream_url = (
                f'http://{PLAYLIST_HOST}/{PLAYLIST_PAIR[0]}/{PLAYLIST_PAIR[1]}/{i}')
        radio = seed.make_channel(self.acc, stream_id=9, name='radio')
        radio.stream_url = radio.raw_stream_url = 'http://radio.example:8000/mount'
        db.session.commit()
        self.acc_id = self.acc.id
        self.ch_id = Channel.query.filter_by(account_id=self.acc_id, name='ch1').one().id
        connlim._holders.clear()
        connlim._seat_of.clear()

    def tearDown(self):
        connlim._holders.clear()
        connlim._seat_of.clear()
        self.t.cleanup()

    def _urls(self):
        db.session.expire_all()
        return [u for (u,) in db.session.query(Channel.stream_url)
                .filter(Channel.account_id == self.acc_id).order_by(Channel.id).all()]

    def _list_playlist(self):
        facts = links.playlist_facts(self.acc_id)
        changes = links.list_playlist_rows(self.acc_id, facts['host'], facts['pair'])
        db.session.expire_all()
        return changes

    def _playlist_login(self):
        return Login.query.filter_by(playlist_account_id=self.acc_id).one()

    def _playlist_host(self):
        return AccountHost.query.filter_by(account_id=self.acc_id, from_playlist=True).one()


class EverySwitchOnChangesNothingTests(_Case):

    def test_listing_the_rows_keeps_every_url_seat_and_launch_url(self):
        before = self._urls()
        url = before[0]
        self.assertEqual(self._list_playlist(), [], 'a first listing is not a change')
        self.assertEqual(self._urls(), before, 'byte-identical stream URLs')
        self.assertEqual(self._playlist_host().host, PLAYLIST_HOST)
        self.assertTrue(self._playlist_host().is_active)
        login = self._playlist_login()
        self.assertEqual((login.username, login.password), PLAYLIST_PAIR)
        self.assertIsNone(login.max_connections, 'its seats follow the account')
        self.assertEqual(account_limits([self.acc_id])[self.acc_id], 1)
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 11))
        self.assertFalse(connlim.try_acquire(self.acc_id, 'recording', 12),
                         'still one seat: the account\'s own Max connections')
        self.assertEqual(links.render_held_login(url, self.acc_id, 'recording', 11),
                         (url, login.id, None))

    def test_a_second_listing_changes_nothing(self):
        self._list_playlist()
        before = self._urls()
        self.assertEqual(self._list_playlist(), [])
        self.assertEqual(AccountHost.query.filter_by(account_id=self.acc_id).count(), 1)
        self.assertEqual(Login.query.count(), 1)
        self.assertEqual(self._urls(), before)

    def test_the_playlist_logins_seats_follow_max_connections_and_the_global_default(self):
        self._list_playlist()
        login_id = self._playlist_login().id
        self.acc.max_connections = 3
        db.session.commit()
        self.assertEqual(links.login_seats_for_accounts([self.acc_id])[self.acc_id][0][1], 3)
        self.acc.max_connections = None
        db.session.commit()
        cfg = {'accounts': {'default_max_connections': 2}}
        self.assertEqual(links.login_seats_for_accounts([self.acc_id], cfg)[self.acc_id],
                         [(login_id, 2, None)])
        self.assertEqual(links.configured_seats([self._playlist_login()], cfg), {login_id: 2})


class LoginSwitchTests(_Case):
    max_connections = 1

    def setUp(self):
        super().setUp()
        self._list_playlist()
        self.playlist = self._playlist_login().id
        self.mine = links.add_login(self.acc_id, 'tom', 'tom_main', 'tompw', 1).id

    def test_an_off_login_is_never_seated_and_its_credentials_are_swapped(self):
        links.set_login_enabled(self.acc_id, self.playlist, False)
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 11))
        self.assertFalse(connlim.try_acquire(self.acc_id, 'recording', 12),
                         'the playlist\'s login is off, so only one seat is left')
        self.assertEqual(connlim.held_login_id('recording', 11), self.mine)
        url = self._urls()[0]
        launch, _, _ = links.render_held_login(url, self.acc_id, 'recording', 11)
        self.assertEqual(launch, f'http://{PLAYLIST_HOST}/tom_main/tompw/1')
        self.assertNotIn(PLAYLIST_PAIR[0], launch)

    def test_every_login_off_refuses_every_seat_and_says_why(self):
        links.set_login_enabled(self.acc_id, self.playlist, False)
        links.set_login_enabled(self.acc_id, self.mine, False)
        self.assertFalse(connlim.try_acquire(self.acc_id, 'recording', 11))
        self.assertTrue(connlim.at_limit(self.acc_id))
        self.assertEqual(connlim.seat_standing([self.acc_id], 'recording'),
                         {self.acc_id: connlim.SEAT_NONE})
        self.assertEqual(links.unusable_reason(self.acc_id, 'northwind-guide', capital=True),
                         'Every login on account "northwind-guide" is turned off')

    def test_a_running_holder_keeps_its_seat_and_moves_at_relaunch_when_it_can(self):
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 11))
        self.assertEqual(connlim.held_login_id('recording', 11), self.playlist)
        links.set_login_enabled(self.acc_id, self.playlist, False)
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 11),
                        'a seat already held is still held - the stream keeps going')
        self.assertEqual(connlim.reseat_before_relaunch(self.acc_id, 'recording', 11), self.mine)
        self.assertEqual(connlim.held_login_id('recording', 11), self.mine)

    def test_with_nowhere_to_move_the_relaunch_keeps_the_seat(self):
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 11))
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 12))
        links.set_login_enabled(self.acc_id, self.playlist, False)
        self.assertEqual(connlim.reseat_before_relaunch(self.acc_id, 'recording', 11),
                         self.playlist, 'the other login\'s seat is taken: stay, keep recording')

    def test_the_playlists_login_is_turned_off_never_removed_or_edited(self):
        """Its seats are the account's Max connections: an edit would pin a number the
        account form no longer moves (fresh-context review of dev/changelog/1670)."""
        with self.assertRaises(ValueError):
            links.remove_login(self.acc_id, self.playlist)
        with self.assertRaises(ValueError):
            links.update_login(self.acc_id, self.playlist, 'x', 'someone', 'else', 1)
        with self.assertRaises(ValueError):
            links.update_login(self.acc_id, self.playlist, PLAYLIST_PAIR[0], PLAYLIST_PAIR[0], '', 2)
        self.assertIsNone(db.session.get(Login, self.playlist).max_connections)

    def test_a_seat_held_before_the_first_listing_is_counted_on_the_login(self):
        """Fresh-context review of dev/changelog/1670: a recording seated in the account's own
        pool when a sync first lists the playlist's login was counted against nothing, so a
        second stream took the login's one seat."""
        connlim._holders.clear()
        connlim._seat_of.clear()
        other = seed.make_account(name='plain', max_connections=1)
        ch = seed.make_channel(other, stream_id=1, name='p1')
        ch.stream_url = ch.raw_stream_url = 'http://plain.example/u/p/1'
        db.session.commit()
        self.assertTrue(connlim.try_acquire(other.id, 'recording', 77))
        self.assertEqual(connlim._holders[other.id], [('recording', 77)])
        facts = links.playlist_facts(other.id)
        links.list_playlist_rows(other.id, facts['host'], facts['pair'])
        login = Login.query.filter_by(playlist_account_id=other.id).one()
        self.assertEqual(connlim.held_login_id('recording', 77), login.id)
        self.assertFalse(connlim.try_acquire(other.id, 'preview', 'p1'),
                         'the one seat is the running recording\'s')

    def test_the_route_needs_a_real_boolean(self):
        url = f'/api/accounts/{self.acc_id}/logins/{self.playlist}/enabled'
        resp = self.client.post(url, json={'enabled': 'false'})
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(db.session.get(AccountLogin, (self.acc_id, self.playlist)).enabled)
        resp = self.client.post(url, json={'enabled': False})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertIn('turned off', resp.get_json()['message'])
        db.session.expire_all()
        self.assertFalse(db.session.get(AccountLogin, (self.acc_id, self.playlist)).enabled)
        resp = self.client.post(f'/api/accounts/{self.acc_id}/logins/99999/enabled',
                                json={'enabled': True})
        self.assertEqual(resp.status_code, 404)


class HostSwitchTests(_Case):

    def setUp(self):
        super().setUp()
        self._list_playlist()
        links.add_host(self.acc_id, 'tv1.tomstreams.example')
        links.add_host(self.acc_id, 'tv2.tomstreams.example')
        db.session.expire_all()
        self.playlist = self._playlist_host().id

    def _active(self):
        db.session.expire_all()
        return AccountHost.query.filter_by(account_id=self.acc_id, is_active=True).one().host

    def test_turning_the_active_host_off_hands_the_role_on_and_rewrites_the_urls(self):
        self.assertEqual(self._active(), PLAYLIST_HOST)
        out = links.set_host_enabled(self.acc_id, self.playlist, False)
        self.assertEqual((out['active'], out['channels']), ('tv1.tomstreams.example', 3))
        self.assertEqual(self._active(), 'tv1.tomstreams.example')
        urls = self._urls()
        self.assertTrue(all(PLAYLIST_HOST not in u for u in urls), urls)
        self.assertIn('http://radio.example:8000/mount', urls, 'an unlisted host is left alone')
        with self.assertRaises(ValueError):
            links.set_active_host(self.acc_id, self.playlist)

    def test_a_sync_keeps_an_off_playlist_host_off_the_urls(self):
        links.set_host_enabled(self.acc_id, self.playlist, False)
        self.assertEqual(self._list_playlist(), [])
        self.assertTrue(all(PLAYLIST_HOST not in u for u in self._urls()))

    def test_every_host_off_refuses_every_seat_and_turning_one_on_makes_it_active(self):
        for row in AccountHost.query.filter_by(account_id=self.acc_id).all():
            links.set_host_enabled(self.acc_id, row.id, False)
        self.assertFalse(connlim.try_acquire(self.acc_id, 'recording', 11))
        self.assertEqual(links.unusable_reason(self.acc_id, 'northwind-guide'),
                         'every host on account "northwind-guide" is turned off')
        tv2 = AccountHost.query.filter_by(account_id=self.acc_id, host='tv2.tomstreams.example').one()
        links.set_host_enabled(self.acc_id, tv2.id, True)
        self.assertEqual(self._active(), 'tv2.tomstreams.example')
        self.assertTrue(all('tv2.tomstreams.example' in u for u in self._urls()[:3]))
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 11))

    def test_the_roll_never_lands_on_a_host_that_is_off(self):
        tv1 = AccountHost.query.filter_by(account_id=self.acc_id, host='tv1.tomstreams.example').one()
        links.set_host_enabled(self.acc_id, tv1.id, False)
        with mock.patch.object(links, 'resolve_host', return_value=(True, None)):
            summary = links.roll_host(self.acc_id, 'Recording 1 (segment 1)',
                                      cfg={'hosts': {'roll_cooldown_minutes': 0}})
        self.assertEqual(summary['host'], 'tv2.tomstreams.example')


class PlaylistChangeTests(_Case):

    def setUp(self):
        super().setUp()
        self._list_playlist()
        links.add_login(self.acc_id, 'tom', 'tom_main', 'tompw', 1)
        links.add_host(self.acc_id, 'tv1.tomstreams.example')
        db.session.expire_all()
        links.set_login_enabled(self.acc_id, self._playlist_login().id, False)
        links.set_host_enabled(self.acc_id, self._playlist_host().id, False)

    def _move_playlist(self, host, pair):
        for ch in Channel.query.filter_by(account_id=self.acc_id).all():
            if ch.stream_id == 9:
                continue
            ch.raw_stream_url = f'http://{host}/{pair[0]}/{pair[1]}/{ch.stream_id}'
        db.session.commit()

    def test_a_changed_host_and_login_rewrite_the_rows_and_keep_the_switches(self):
        self._move_playlist('cdn2.oldreseller.example', ('zz91old', 'pw-secret-9'))
        changes = self._list_playlist()
        self.assertEqual([(c['what'], c['old'], c['new'], c['on']) for c in changes],
                         [('host', PLAYLIST_HOST, 'cdn2.oldreseller.example', False),
                          ('login', 'k8x2expired', 'zz91old', False)])
        host = self._playlist_host()
        self.assertEqual((host.host, host.enabled), ('cdn2.oldreseller.example', False))
        login = self._playlist_login()
        self.assertEqual((login.username, login.password), ('zz91old', 'pw-secret-9'))
        self.assertFalse(db.session.get(AccountLogin, (self.acc_id, login.id)).enabled)
        self.assertEqual(Login.query.count(), 2, 'rewritten in place, never a second row')
        self.assertTrue(all('tv1.tomstreams.example' in u for u in self._urls()[:3]),
                        'the new playlist host is off too, so the URLs stay on the active one')

    def test_each_change_is_one_alert_naming_old_and_new_and_never_the_password(self):
        self._move_playlist('cdn2.oldreseller.example', ('zz91old', 'pw-secret-9'))
        changes = self._list_playlist()
        links.note_playlist_changes(self.acc_id, 'northwind-guide', changes)
        alerts = Alert.query.filter_by(source=f'account:{self.acc_id}:playlist-changed').all()
        self.assertEqual(len(alerts), 2)
        self.assertEqual({a.alert_type for a in alerts}, {ACCOUNT_PLAYLIST_CHANGED_UNUSED})
        login_alert = next(a for a in alerts if 'login' in a.title)
        self.assertIn("The playlist's login changed from k8x2expired to zz91old. It stays turned "
                      "off, so streams won't use it.", login_alert.title)
        for a in alerts:
            self.assertNotIn('pw-secret-9', a.title + (a.body or ''))
            self.assertNotIn('oldpw', a.title + (a.body or ''))

    def test_a_change_onto_a_login_the_user_holds_marks_theirs_never_a_second_row(self):
        """Fresh-context review of dev/changelog/1670: the playlist moving onto credentials the
        user already listed rewrote its own row to the same pair - two pools, one login."""
        old_row = self._playlist_login().id
        self._move_playlist(PLAYLIST_HOST, ('tom_main', 'tompw'))
        changes = self._list_playlist()
        self.assertEqual([(c['what'], c['old'], c['new'], c['on']) for c in changes],
                         [('login', 'k8x2expired', 'tom_main', True)])
        pairs = [(lg.username, lg.password) for lg in links.logins_for_account(self.acc_id)]
        self.assertEqual(pairs.count(('tom_main', 'tompw')), 1)
        self.assertIsNone(db.session.get(Login, old_row), 'the superseded sync row goes')
        self.assertEqual(self._playlist_login().name, 'tom', 'the user\'s row, adopted')

    def test_a_login_the_user_typed_is_never_rewritten_by_a_sync(self):
        """An adopted row stays the user's: a later playlist change unmarks it and lists the
        playlist's new login as a row of its own, off beside the user's logins."""
        self._move_playlist(PLAYLIST_HOST, ('tom_main', 'tompw'))
        self._list_playlist()
        mine = self._playlist_login().id
        self._move_playlist(PLAYLIST_HOST, ('zz91old', 'pw2'))
        changes = self._list_playlist()
        self.assertEqual([(c['new'], c['on']) for c in changes], [('zz91old', False)])
        row = db.session.get(Login, mine)
        self.assertEqual((row.username, row.password, row.playlist_account_id),
                         ('tom_main', 'tompw', None))
        new = self._playlist_login()
        self.assertEqual((new.username, new.max_connections), ('zz91old', None))

    def test_editing_an_adopted_logins_credentials_makes_it_the_users_again(self):
        self._move_playlist(PLAYLIST_HOST, ('tom_main', 'tompw'))
        self._list_playlist()
        mine = self._playlist_login().id
        links.update_login(self.acc_id, mine, 'tom', 'tom_main', 'renewed', 1)
        self.assertIsNone(db.session.get(Login, mine).playlist_account_id)

    def test_a_change_on_a_row_that_is_on_raises_the_warning(self):
        links.set_login_enabled(self.acc_id, self._playlist_login().id, True)
        self._move_playlist(PLAYLIST_HOST, ('zz91old', 'pw2'))
        changes = self._list_playlist()
        links.note_playlist_changes(self.acc_id, 'northwind-guide', changes)
        alert = Alert.query.filter_by(alert_type=ACCOUNT_PLAYLIST_CHANGED).one()
        self.assertIn("It's turned on, so new streams can use the new login.", alert.title)


class FirstListingTests(_Case):

    def test_a_login_the_user_already_listed_with_the_playlists_pair_is_adopted(self):
        mine = links.add_login(self.acc_id, 'main', *PLAYLIST_PAIR, 2)
        self.assertEqual(mine.playlist_account_id, self.acc_id, 'adopted at the add')
        self._list_playlist()
        self.assertEqual(Login.query.count(), 1)
        self.assertEqual(db.session.get(Login, mine.id).max_connections, 2, 'keeps its seats')

    def test_a_sync_adopts_a_login_listed_before_the_playlists_rows_existed(self):
        """A database from before dev/changelog/1670: the user listed the playlist's own
        credentials as a login. The first sync marks that row, never adds a second for the
        same credentials (two pools for one login would put two streams on it)."""
        row = Login(name='main', username=PLAYLIST_PAIR[0], password=PLAYLIST_PAIR[1],
                    max_connections=3)
        db.session.add(row)
        db.session.flush()
        db.session.add(AccountLogin(account_id=self.acc_id, login_id=row.id, position=0))
        db.session.commit()
        self._list_playlist()
        self.assertEqual(Login.query.count(), 1)
        self.assertEqual(self._playlist_login().id, row.id)
        self.assertEqual(account_limits([self.acc_id])[self.acc_id], 3)

    def test_a_playlist_login_listed_beside_the_users_own_starts_off(self):
        """Decision 8 of dev/changelog/1670: turning it on would add seats nobody set."""
        row = Login(name='mine', username='mine', password='minepw', max_connections=1)
        db.session.add(row)
        db.session.flush()
        db.session.add(AccountLogin(account_id=self.acc_id, login_id=row.id, position=0))
        db.session.commit()
        self._list_playlist()
        playlist = self._playlist_login()
        self.assertFalse(db.session.get(AccountLogin, (self.acc_id, playlist.id)).enabled)
        self.assertEqual(account_limits([self.acc_id])[self.acc_id], 1)

    def test_adding_the_first_login_lists_the_playlists_first(self):
        links.add_login(self.acc_id, 'tom', 'tom_main', 'tompw', 1)
        names = [lg.username for lg in links.logins_for_account(self.acc_id)]
        self.assertEqual(names, [PLAYLIST_PAIR[0], 'tom_main'])
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 11))
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 12),
                        'adding a second login does not take the first away')


class TeardownTests(_Case):

    def test_a_shared_playlist_login_outlives_its_account_with_seats_of_its_own(self):
        self.acc.max_connections = 2
        other = seed.make_account(name='northwind-tom', max_connections=1)
        db.session.commit()
        links.create_provider('northwind', (self.acc_id, other.id))
        self._list_playlist()
        playlist = self._playlist_login().id
        links.share_login(playlist, other.id)
        self.client.delete(f'/api/accounts/{self.acc_id}')
        db.session.expire_all()
        row = db.session.get(Login, playlist)
        self.assertIsNotNone(row, 'the sharer still holds it')
        self.assertIsNone(row.playlist_account_id)
        self.assertEqual(row.max_connections, 2)


class RefusalWordingTests(_Case):

    def setUp(self):
        super().setUp()
        self._list_playlist()
        links.set_login_enabled(self.acc_id, self._playlist_login().id, False)

    def test_a_preview_names_the_switch(self):
        with mock.patch.object(preview.subprocess, 'Popen', side_effect=AssertionError('no launch')), \
                self.t.app.test_request_context():
            with self.assertRaises(preview.PreviewRefused) as caught:
                preview.start_preview(self.ch_id)
        self.assertIn('Every login on account "northwind-guide" is turned off', str(caught.exception))
        self.assertNotIn('connection limit', str(caught.exception))

    def _scheduled(self, stop_in_minutes):
        from datetime import datetime, timedelta
        from app.database import REC_STATUS_SCHEDULED
        now = datetime.utcnow()
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, channel_id=self.ch_id,
                                  start_time=now - timedelta(minutes=1),
                                  stop_time=now + timedelta(minutes=stop_in_minutes))
        db.session.commit()
        return rec.id

    def test_a_recordings_wait_names_the_switch_and_raises_no_alert(self):
        """Fresh-context review of dev/changelog/1670: a switched-off wait borrowed the
        connection limit's event kind and alert; it is the user's state, like a block's."""
        import json
        from app import recorder
        from app.database import RECORDING_START_DEFERRED, Recording, RecordingEvent
        rid = self._scheduled(30)
        with mock.patch('app.scheduler.reschedule_recording_start'):
            recorder._defer_start_for_slot(self.t.app, rid, self.acc_id)
        events = RecordingEvent.query.filter_by(recording_id=rid,
                                                event_type=RECORDING_START_DEFERRED).all()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0].extra_data)['kind'], 'switched_off')
        self.assertIn('every login on account "northwind-guide" is turned off', events[0].detail)
        self.assertNotIn('connection limit', events[0].detail)
        self.assertEqual(Alert.query.filter_by(
            alert_type='RECORDING_WAITING_FOR_CONNECTION_SLOT').count(), 0)
        db.session.expire_all()
        self.assertIn('to be turned on', db.session.get(Recording, rid).start_deferred_for)

    def test_a_wait_that_outlasts_its_window_fails_with_its_own_reason(self):
        from app import recorder
        from app.database import FAILURE_SWITCHED_OFF, REC_STATUS_FAILED, Recording
        rid = self._scheduled(-0.5)
        recorder._defer_start_for_slot(self.t.app, rid, self.acc_id)
        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual((rec.status, rec.failure_reason), (REC_STATUS_FAILED, FAILURE_SWITCHED_OFF))
        self.assertEqual(Alert.query.filter_by(
            alert_type='RECORDING_FAILED_CONNECTION_LIMIT').count(), 0)

    def test_a_switched_off_account_preempts_nothing(self):
        """A stream already running keeps going: with its usable limit at 0, cutting a channel
        test off would free nothing for the recording."""
        from app import recorder
        playlist = self._playlist_login().id
        links.set_login_enabled(self.acc_id, playlist, True)
        self.assertTrue(connlim.try_acquire(self.acc_id, 'test', 501))
        links.set_login_enabled(self.acc_id, playlist, False)
        rid = self._scheduled(30)
        with mock.patch('app.channel_tester.kill_active_test_for_account') as kill_test, \
                mock.patch.object(preview, 'preempt_for_account') as stop_preview:
            self.assertFalse(recorder._try_acquire_slot_with_preemption(self.t.app, rid, self.acc_id))
        kill_test.assert_not_called()
        stop_preview.assert_not_called()
        self.assertEqual(connlim.held_login_id('test', 501), playlist)

    def test_a_channel_test_is_skipped_naming_the_switch(self):
        from app import channel_tester
        with mock.patch.object(channel_tester, '_append_log') as append:
            self.assertIsNone(channel_tester.run_channel_test(self.t.app, self.ch_id))
        lines = ' '.join(c.args[1] for c in append.call_args_list)
        self.assertIn('every login on account "northwind-guide" is turned off', lines)
        self.assertNotIn('connection limit', lines)


class MigrationTests(unittest.TestCase):

    def test_step_138_makes_max_connections_nullable_and_keeps_the_rows(self):
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            cur = conn.cursor()
            cur.executescript('''
                CREATE TABLE accounts (id INTEGER PRIMARY KEY);
                CREATE TABLE account_hosts (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL,
                    host VARCHAR(255) NOT NULL, position INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE logins (
                    id                  INTEGER PRIMARY KEY,
                    name                VARCHAR(100) NOT NULL,
                    username            VARCHAR(255) NOT NULL,
                    password            VARCHAR(255) NOT NULL,
                    max_connections     INTEGER NOT NULL,
                    last_refused_at     DATETIME,
                    last_refused_detail VARCHAR(255),
                    created_at          DATETIME NOT NULL,
                    updated_at          DATETIME NOT NULL
                );
                CREATE TABLE account_logins (account_id INTEGER NOT NULL, login_id INTEGER NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (account_id, login_id));
                INSERT INTO account_hosts (account_id, host) VALUES (1, 'a.example');
                INSERT INTO logins (name, username, password, max_connections, created_at, updated_at)
                    VALUES ('main', 'u', 'p', 2, '2026-01-01', '2026-01-01');
                INSERT INTO account_logins (account_id, login_id) VALUES (1, 1);
            ''')
            conn.commit()
            M._m138_playlist_rows(conn, cur)
            M._m138_playlist_rows(conn, cur)   # re-runnable from the top
            cols = {r[1]: r for r in cur.execute('PRAGMA table_info(logins)')}
            self.assertEqual(cols['max_connections'][3], 0, 'nullable')
            self.assertIn('playlist_account_id', cols)
            self.assertEqual(cur.execute('SELECT name, max_connections FROM logins').fetchall(),
                             [('main', 2)])
            self.assertEqual(cur.execute('SELECT enabled, from_playlist FROM account_hosts').fetchall(),
                             [(1, 0)])
            self.assertEqual(cur.execute('SELECT enabled FROM account_logins').fetchall(), [(1,)])
            self.assertIsNotNone(cur.execute(
                "SELECT 1 FROM sqlite_master WHERE name='uq_logins_playlist_account'").fetchone())
            conn.close()
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
