"""An account's host list (app/account_links.py, DESIGN-account-providers.md §4,
dev/changelog/1168): the other names a reseller hands out for one account, one active,
rolled when the active one stops resolving.

The properties guarded here, each of which fails in its own way:

  * an account with no list renders byte-identical URLs - the whole promise to the
    one-account user, cheap to assert and the first thing a second writer would break;
  * a URL on a host outside the list is never touched, by a render or by a roll (a radio
    mount or a third-party feed is not on the reseller's server), and the account's own URL
    follows a roll only when its host is listed (a curated account's service host is not);
  * the list is the user's: the first add seeds what is true today as the active host, the
    active host cannot be removed while another is listed, and nothing else writes a row;
  * a roll happens on a resolution failure and on nothing else - a stall, a refused
    connection or a 404 rolls nothing - at most once per cooldown, never removes a host,
    writes the recording event and the alert, and says so when no host resolves at all;
  * a sync after a roll keeps the rolled host (the provider keeps sending the dead one);
  * the probe stamps every host's verdict and never rolls.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_account_hosts
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import account_links as links  # noqa: E402
from app.accounts import _upsert_channels, normalize_url_with_mode  # noqa: E402
from app.alerts import ACCOUNT_HOST_ROLLED  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (  # noqa: E402
    Account, AccountHost, Alert, Channel, RecordingEvent, RECORDING_HOST_ROLLED,
)

RESOLVE_LINE = ('[tcp @ 0x5972c611fe00] Failed to resolve hostname a1.skyline.example: '
                'Name or service not known\n[in#0 @ 0x5972c611c5c0] Error opening input: '
                'Input/output error')
NXDOMAIN_LINE = ('[tcp @ 0x5893fff14e00] Failed to resolve hostname a1.skyline.example: '
                 'No address associated with hostname')
REFUSED_LINE = ('[tcp @ 0x5e2d65aeae00] Connection to tcp://a1.skyline.example:80 failed: '
                'Connection refused\n[in#0 @ 0x5e2d65ae75c0] Error opening input: Connection refused')
NOT_FOUND_LINE = ('[http @ 0x5ddcbdfebf40] HTTP error 404 File not found\n'
                  '[in#0 @ 0x5ddcbdfeb5c0] Error opening input: Server returned 404 Not Found')
STALL_TAIL = 'frame= 1500 fps= 25 q=-1.0 size=   12288kB time=00:01:00.00 bitrate=1677.7kbits/s'

HOSTS = frozenset({'a1.skyline.example', 'a2.skyline.example', 'a3.skyline.example:8080'})


def _open_alerts(account_id):
    return Alert.query.filter_by(alert_type=ACCOUNT_HOST_ROLLED,
                                 source=f'account:{account_id}:host-rolled',
                                 dismissed_at=None).all()


def _age_standing_alert(account_id, minutes):
    """Move the standing roll alert's timestamp back, as if the roll happened that long ago -
    the persisted half of the cooldown."""
    for a in _open_alerts(account_id):
        a.created_at = datetime.utcnow() - timedelta(minutes=minutes)
    db.session.commit()


class RenderTests(unittest.TestCase):
    """render_host() is pure and byte-exact."""

    def test_an_empty_list_renders_every_url_unchanged(self):
        for url in ('http://a1.skyline.example/live/u/p/1.ts',
                    'http://radio.example:8000/mount', 'rtsp://x/y', 'nope', ''):
            self.assertEqual(links.render_host(url, frozenset(), None), url)
            self.assertEqual(links.render_host(url, HOSTS, None), url)

    def test_a_listed_host_moves_to_the_active_one_and_nothing_else_moves(self):
        self.assertEqual(
            links.render_host('http://a1.skyline.example/live/u/p/1.ts?x=1#f', HOSTS,
                              'a2.skyline.example'),
            'http://a2.skyline.example/live/u/p/1.ts?x=1#f')
        self.assertEqual(
            links.render_host('https://A1.Skyline.Example/live/u/p/1.ts', HOSTS,
                              'a3.skyline.example:8080'),
            'https://a3.skyline.example:8080/live/u/p/1.ts')
        self.assertEqual(
            links.render_host('http://someone@a1.skyline.example/x', HOSTS, 'a2.skyline.example'),
            'http://someone@a2.skyline.example/x')

    def test_a_url_on_a_host_outside_the_list_is_untouched(self):
        for url in ('http://radio.example/mount', 'http://cdn.other.example/live/u/p/1.ts',
                    'http://a1.skyline.example.evil/live/1'):
            self.assertEqual(links.render_host(url, HOSTS, 'a2.skyline.example'), url)

    def test_the_active_host_renders_itself_unchanged(self):
        url = 'http://a2.skyline.example/live/u/p/1.ts'
        self.assertIs(links.render_host(url, HOSTS, 'a2.skyline.example'), url)

    def test_normalize_host_reduces_a_pasted_url_and_refuses_junk(self):
        self.assertEqual(links.normalize_host(' HTTP://A2.Skyline.Example:8080/live/x '),
                         'a2.skyline.example:8080')
        self.assertEqual(links.normalize_host('a2.skyline.example'), 'a2.skyline.example')
        # Userinfo in a pasted URL is dropped, like its scheme - the host is what is kept.
        self.assertEqual(links.normalize_host('http://someone@a2.skyline.example/x'), 'a2.skyline.example')
        for bad in ('', '   ', 'host:port', 'two words', 'bad!host'):
            with self.assertRaises(ValueError, msg=bad):
                links.normalize_host(bad)


class ClassifierTests(unittest.TestCase):
    """The stderr lines measured on this box's ffmpeg 7.1 (dev/changelog/1168)."""

    def test_the_two_resolver_spellings_are_resolution_failures(self):
        self.assertTrue(links.is_resolution_failure(RESOLVE_LINE))
        self.assertTrue(links.is_resolution_failure(NXDOMAIN_LINE))
        self.assertTrue(links.is_resolution_failure('Could not resolve host: x'))
        self.assertTrue(links.is_resolution_failure('Temporary failure in name resolution'))

    def test_a_refused_connection_a_404_a_stall_and_nothing_are_not(self):
        for tail in (REFUSED_LINE, NOT_FOUND_LINE, STALL_TAIL, '', None):
            self.assertFalse(links.is_resolution_failure(tail), tail)


class _Case(unittest.TestCase):
    """One account whose streams are on a1, with one radio mount on another host and the
    account's own URL on a1 too (a directly bought account)."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.acc = seed.make_account(name='skyline-direct')
        self.acc.m3u_url = 'http://a1.skyline.example/get.php?u=x'
        self.acc.epg_url = 'http://guide.curation.example/xmltv.php'
        self.urls = {}
        for i in range(1, 4):
            ch = seed.make_channel(self.acc, stream_id=i, name=f'ch{i}')
            ch.stream_url = ch.raw_stream_url = f'http://a1.skyline.example/live/u/p/{i}.ts'
            self.urls[ch.id] = ch.stream_url
        self.radio = seed.make_channel(self.acc, stream_id=9, name='radio')
        self.radio.stream_url = self.radio.raw_stream_url = 'http://radio.example:8000/mount'
        db.session.commit()
        self.acc_id = self.acc.id
        self.radio_id = self.radio.id

    def tearDown(self):
        self.t.cleanup()

    def _hosts(self):
        return [(r.host, r.is_active) for r in
                AccountHost.query.filter_by(account_id=self.acc_id)
                .order_by(AccountHost.position, AccountHost.id).all()]

    def _stream_urls(self):
        return {cid: url for cid, url in db.session.query(Channel.id, Channel.stream_url)
                .filter(Channel.account_id == self.acc_id).all()}

    def _list(self, *hosts):
        for h in hosts:
            links.add_host(self.acc_id, h)
        db.session.expire_all()


class ListWriterTests(_Case):

    def test_the_first_add_seeds_todays_host_as_the_active_one(self):
        row = links.add_host(self.acc_id, 'a2.skyline.example')
        self.assertFalse(row.is_active)
        self.assertEqual(self._hosts(), [('a1.skyline.example', True), ('a2.skyline.example', False)])
        # Nothing was rewritten: a1 is active and the URLs already carry it.
        self.assertEqual(self._stream_urls()[self.radio_id], 'http://radio.example:8000/mount')

    def test_adding_todays_host_first_makes_it_active_without_a_second_seed(self):
        links.add_host(self.acc_id, 'A1.skyline.example')
        self.assertEqual(self._hosts(), [('a1.skyline.example', True)])

    def test_a_duplicate_or_junk_host_is_refused(self):
        self._list('a2.skyline.example')
        with self.assertRaises(ValueError):
            links.add_host(self.acc_id, 'a2.skyline.example')
        with self.assertRaises(ValueError):
            links.add_host(self.acc_id, 'not a host')
        self.assertEqual(len(self._hosts()), 2)

    def test_the_active_host_is_refused_removal_while_another_is_listed(self):
        self._list('a2.skyline.example')
        active = AccountHost.query.filter_by(account_id=self.acc_id, is_active=True).one()
        with self.assertRaises(ValueError):
            links.remove_host(self.acc_id, active.id)
        other = AccountHost.query.filter_by(account_id=self.acc_id, is_active=False).one()
        self.assertEqual(links.remove_host(self.acc_id, other.id), 'a2.skyline.example')
        # Now the last one: removable, the list empties and the URLs stay.
        before = self._stream_urls()
        links.remove_host(self.acc_id, active.id)
        self.assertEqual(self._hosts(), [])
        self.assertEqual(self._stream_urls(), before)

    def test_make_active_rewrites_listed_urls_and_the_account_url_only_when_listed(self):
        self._list('a2.skyline.example')
        target = AccountHost.query.filter_by(account_id=self.acc_id, host='a2.skyline.example').one()
        summary = links.set_active_host(self.acc_id, target.id)
        self.assertEqual((summary['host'], summary['channels'], summary['account_urls']),
                         ('a2.skyline.example', 3, 1))
        urls = self._stream_urls()
        for cid, old in self.urls.items():
            self.assertEqual(urls[cid], old.replace('a1.skyline.example', 'a2.skyline.example'))
        self.assertEqual(urls[self.radio_id], 'http://radio.example:8000/mount')
        acc = db.session.get(Account, self.acc_id)
        self.assertEqual(acc.m3u_url, 'http://a2.skyline.example/get.php?u=x')
        # The guide URL is on the curation service's host, which is not listed: untouched.
        self.assertEqual(acc.epg_url, 'http://guide.curation.example/xmltv.php')
        self.assertEqual(self._hosts(), [('a1.skyline.example', False), ('a2.skyline.example', True)])
        # raw_stream_url is what the provider sent and never moves.
        raws = {u for (u,) in db.session.query(Channel.raw_stream_url)
                .filter(Channel.account_id == self.acc_id).all()}
        self.assertTrue(all('a1.skyline.example' in u or 'radio' in u for u in raws))

    def test_deleting_the_account_takes_its_host_rows(self):
        self._list('a2.skyline.example')
        resp = self.client.delete(f'/api/accounts/{self.acc_id}')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(AccountHost.query.filter_by(account_id=self.acc_id).count(), 0)


class RollTests(_Case):

    def setUp(self):
        super().setUp()
        self._list('a2.skyline.example', 'a3.skyline.example:8080')
        self.rec = seed.make_recording(status='IN_PROGRESS', channel_id=list(self.urls)[0],
                                       url=list(self.urls.values())[0])
        db.session.commit()
        self.rec_id = self.rec.id
        self.ch_id = list(self.urls)[0]

    def _resolver(self, dead):
        def fake(host, timeout=None):
            name = host.rpartition(':')[0] or host
            return (False, 'Name or service not known') if name in dead else (True, None)
        return fake

    def test_a_resolution_failure_rolls_once_and_the_recording_and_alert_say_so(self):
        with mock.patch.object(links, 'resolve_host', self._resolver({'a1.skyline.example'})):
            summary = links.roll_host_for_channel(self.ch_id, trigger='Recording 1 (segment 2)',
                                                  recording_id=self.rec_id)
        self.assertEqual((summary['old_host'], summary['host'], summary['channels']),
                         ('a1.skyline.example', 'a2.skyline.example', 3))
        db.session.expire_all()
        self.assertEqual(self._hosts(), [('a1.skyline.example', False),
                                         ('a2.skyline.example', True),
                                         ('a3.skyline.example:8080', False)])
        urls = self._stream_urls()
        self.assertTrue(all('a2.skyline.example' in urls[cid] for cid in self.urls))
        self.assertEqual(urls[self.radio_id], 'http://radio.example:8000/mount')
        self.assertEqual(db.session.get(Account, self.acc_id).m3u_url,
                         'http://a2.skyline.example/get.php?u=x')
        dead = AccountHost.query.filter_by(account_id=self.acc_id, host='a1.skyline.example').one()
        self.assertEqual(dead.last_resolve_error, 'did not resolve (reported by Recording 1 (segment 2))')
        events = RecordingEvent.query.filter_by(recording_id=self.rec_id,
                                                event_type=RECORDING_HOST_ROLLED).all()
        self.assertEqual(len(events), 1)
        self.assertIn('a1.skyline.example', events[0].detail)
        self.assertIn('a2.skyline.example', events[0].detail)
        alerts = _open_alerts(self.acc_id)
        self.assertEqual(len(alerts), 1)
        self.assertIn('now using a2.skyline.example', alerts[0].title)

    def test_a_second_failure_inside_the_cooldown_does_not_roll(self):
        with mock.patch.object(links, 'resolve_host', self._resolver({'a1.skyline.example'})):
            self.assertIsNotNone(links.roll_host(self.acc_id, 'first'))
        with mock.patch.object(links, 'resolve_host', self._resolver({'a2.skyline.example'})):
            self.assertIsNone(links.roll_host(self.acc_id, 'second'))
        db.session.expire_all()
        self.assertEqual([h for h, a in self._hosts() if a], ['a2.skyline.example'])
        # Past the cooldown it rolls again - both halves of the cooldown have to let it.
        _age_standing_alert(self.acc_id, minutes=11)
        links._last_roll_attempt.clear()
        with mock.patch.object(links, 'resolve_host', self._resolver({'a2.skyline.example'})):
            self.assertIsNotNone(links.roll_host(self.acc_id, 'third'))
        db.session.expire_all()
        self.assertEqual([h for h, a in self._hosts() if a], ['a3.skyline.example:8080'])

    def test_a_stall_a_refusal_and_a_404_roll_nothing(self):
        # The hooks ask is_resolution_failure() first; this pins the gate they share.
        for tail in (STALL_TAIL, REFUSED_LINE, NOT_FOUND_LINE):
            self.assertFalse(links.is_resolution_failure(tail))
        self.assertEqual(self._hosts()[0], ('a1.skyline.example', True))

    def test_no_host_resolving_changes_nothing_and_says_so(self):
        with mock.patch.object(links, 'resolve_host', self._resolver(
                {'a1.skyline.example', 'a2.skyline.example', 'a3.skyline.example'})):
            self.assertIsNone(links.roll_host(self.acc_id, 'Recording 1 (segment 2)',
                                              recording_id=self.rec_id))
        db.session.expire_all()
        self.assertEqual([h for h, a in self._hosts() if a], ['a1.skyline.example'])
        self.assertEqual(len(self._hosts()), 3)
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=self.rec_id, event_type=RECORDING_HOST_ROLLED).count(), 0)
        alerts = _open_alerts(self.acc_id)
        self.assertEqual(len(alerts), 1)
        self.assertIn('every host on the list failed to resolve', alerts[0].title)

    def test_an_account_with_no_list_never_rolls(self):
        other = seed.make_account(name='plain')
        ch = seed.make_channel(other)
        ch.stream_url = ch.raw_stream_url = 'http://x.example/live/u/p/1.ts'
        db.session.commit()
        with mock.patch.object(links, 'resolve_host', self._resolver({'x.example'})):
            self.assertIsNone(links.roll_host_for_channel(ch.id, trigger='t'))
        self.assertEqual(db.session.get(Channel, ch.id).stream_url, 'http://x.example/live/u/p/1.ts')
        self.assertEqual(_open_alerts(other.id), [])

    def test_the_hook_entry_never_raises(self):
        with mock.patch.object(links, 'roll_host', side_effect=RuntimeError('boom')):
            self.assertIsNone(links.roll_host_for_channel(self.ch_id, trigger='t'))
        self.assertIsNone(links.roll_host_for_channel(999999, trigger='t'))

    def test_the_alert_clears_on_the_next_sync_and_on_a_list_edit(self):
        with mock.patch.object(links, 'resolve_host', self._resolver({'a1.skyline.example'})):
            links.roll_host(self.acc_id, 'first')
        self.assertEqual(len(_open_alerts(self.acc_id)), 1)
        links.clear_rolled_alert_after_sync(self.acc_id)
        self.assertEqual(_open_alerts(self.acc_id), [])
        links._last_roll_attempt.clear()
        with mock.patch.object(links, 'resolve_host', self._resolver({'a2.skyline.example'})):
            links.roll_host(self.acc_id, 'second')
        self.assertEqual(len(_open_alerts(self.acc_id)), 1)
        links.add_host(self.acc_id, 'a4.skyline.example')
        self.assertEqual(_open_alerts(self.acc_id), [])


class SyncTests(_Case):

    def _streams(self):
        # The provider keeps sending URLs on a1, the host that just died.
        return [{'stream_id': i, 'name': f'ch{i}',
                 '_stream_url': f'http://a1.skyline.example/live/u/p/{i}.ts'} for i in (1, 2, 3)] + [
                {'stream_id': 9, 'name': 'radio', '_stream_url': 'http://radio.example:8000/mount'}]

    def test_an_empty_list_syncs_byte_identical_urls(self):
        cfg = load_config()
        with self.t.app.app_context():
            acc = db.session.get(Account, self.acc_id)
            _upsert_channels(acc, self._streams(), cfg)
            db.session.commit()
        mode = cfg['sync']['url_normalization']
        urls = self._stream_urls()
        for s in self._streams():
            ch = Channel.query.filter_by(account_id=self.acc_id, stream_id=s['stream_id']).one()
            self.assertEqual(urls[ch.id], normalize_url_with_mode(s['_stream_url'], mode))

    def test_a_sync_after_a_roll_keeps_the_rolled_host(self):
        self._list('a2.skyline.example')
        target = AccountHost.query.filter_by(account_id=self.acc_id, host='a2.skyline.example').one()
        links.set_active_host(self.acc_id, target.id)
        cfg = load_config()
        with self.t.app.app_context():
            acc = db.session.get(Account, self.acc_id)
            _upsert_channels(acc, self._streams(), cfg)
            db.session.commit()
        for i in (1, 2, 3):
            ch = Channel.query.filter_by(account_id=self.acc_id, stream_id=i).one()
            self.assertEqual(ch.stream_url, f'http://a2.skyline.example/live/u/p/{i}.ts')
            self.assertEqual(ch.raw_stream_url, f'http://a1.skyline.example/live/u/p/{i}.ts')
        radio = Channel.query.filter_by(account_id=self.acc_id, stream_id=9).one()
        self.assertEqual(radio.stream_url, 'http://radio.example:8000/mount')


class ProbeTests(_Case):

    def test_the_probe_stamps_every_host_and_never_rolls(self):
        self._list('a2.skyline.example')
        with mock.patch.object(links, 'resolve_host', lambda host, timeout=None: (
                (False, 'Name or service not known') if host.startswith('a1') else (True, None))):
            out = links.check_hosts()
        self.assertEqual(out, {'checked': 2, 'failed': 1, 'accounts': 1})
        db.session.expire_all()
        rows = {r.host: r for r in AccountHost.query.filter_by(account_id=self.acc_id).all()}
        self.assertEqual(rows['a1.skyline.example'].last_resolve_error, 'Name or service not known')
        self.assertTrue(rows['a1.skyline.example'].is_active)
        self.assertIsNone(rows['a2.skyline.example'].last_resolve_error)
        self.assertIsNotNone(rows['a2.skyline.example'].last_resolved_at)
        self.assertEqual(self._stream_urls()[list(self.urls)[0]], list(self.urls.values())[0])
        self.assertEqual(_open_alerts(self.acc_id), [])

    def test_a_lookup_that_hangs_is_reported_not_waited_for(self):
        import threading
        gate = threading.Event()
        with mock.patch('app.account_links.socket.getaddrinfo', lambda *a, **k: gate.wait(5)):
            ok, error = links.resolve_host('slow.example', timeout=0.05)
        gate.set()
        self.assertFalse(ok)
        self.assertIn('resolver', error)


class RouteAndPageTests(_Case):

    def test_the_page_shows_the_hint_with_no_list_and_the_rows_with_one(self):
        html = self.client.get(f'/accounts/{self.acc_id}').get_data(as_text=True)
        self.assertIn('data-section="hosts"', html)
        self.assertIn('Add the others your reseller gave you', html)
        self.assertEqual(html.count('data-act="host-add"'), 1)
        self._list('a2.skyline.example')
        html = self.client.get(f'/accounts/{self.acc_id}').get_data(as_text=True)
        self.assertIn('<code>a1.skyline.example</code>', html)
        self.assertIn('<code>a2.skyline.example</code>', html)
        self.assertIn('Not checked yet', html)
        self.assertEqual(html.count('data-act="host-activate"'), 2)

    def test_the_routes_add_activate_and_remove_through_the_one_writer(self):
        resp = self.client.post(f'/api/accounts/{self.acc_id}/hosts', json={'host': 'a2.skyline.example'})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200, body)
        self.assertTrue(body['success'])
        self.assertIn('a1.skyline.example is the host', body['message'])
        host_id = body['host_id']
        self.assertEqual(self._hosts(), [('a1.skyline.example', True), ('a2.skyline.example', False)])

        resp = self.client.post(f'/api/accounts/{self.acc_id}/hosts', json={'host': 'a2.skyline.example'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('already', resp.get_json()['error'])

        resp = self.client.post(f'/api/accounts/{self.acc_id}/hosts/{host_id}/activate')
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200, body)
        self.assertEqual(body['channels'], 3)
        db.session.expire_all()
        self.assertEqual([h for h, a in self._hosts() if a], ['a2.skyline.example'])

        active_old = AccountHost.query.filter_by(account_id=self.acc_id, host='a1.skyline.example').one()
        resp = self.client.delete(f'/api/accounts/{self.acc_id}/hosts/{active_old.id}')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        resp = self.client.delete(f'/api/accounts/{self.acc_id}/hosts/{host_id}')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(self._hosts(), [])
        resp = self.client.delete(f'/api/accounts/{self.acc_id}/hosts/{host_id}')
        self.assertEqual(resp.status_code, 404)

    def test_removing_the_active_host_with_another_listed_is_a_409(self):
        self._list('a2.skyline.example')
        active = AccountHost.query.filter_by(account_id=self.acc_id, is_active=True).one()
        resp = self.client.delete(f'/api/accounts/{self.acc_id}/hosts/{active.id}')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('Make another host active first', resp.get_json()['error'])


class HookPresenceTests(unittest.TestCase):
    """The three capture paths ask the classifier and call the roll. A static check, since
    driving a real stall through the watchdog is a whole-process test; the roll itself is
    covered above."""

    def test_the_watchdog_the_tester_and_the_preview_each_hook_the_roll(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel in ('app/watchdog.py', 'app/channel_tester.py', 'app/preview.py'):
            with open(os.path.join(root, rel), encoding='utf-8') as fh:
                src = fh.read()
            self.assertIn('is_resolution_failure(', src, rel)
            self.assertIn('roll_host_for_channel(', src, rel)


if __name__ == '__main__':
    unittest.main()
