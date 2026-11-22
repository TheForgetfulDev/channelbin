"""Host checks: is each listed host up, and how fast does it answer (app/host_checks.py,
dev/changelog/1674).

The properties guarded here, each of which fails in its own way:

  * the probe names every way a host can fail - answers, a server error (5xx), a refused
    port, a port that takes the connection and says nothing, a name that does not resolve -
    and the request it sends carries no username, password or channel;
  * pacing: every check goes through one line, a host name is in it once however many
    accounts list it or buttons ask, a host given a full check in the last 10 minutes is left
    out, and two checks are always at least the spacing's low end apart;
  * the host a stream is using is not touched;
  * a check never swaps hosts, but a swap tries a host that answered before one that did not;
  * an active host that stops answering raises a self-clearing warning, a standby host's
    change of answer is one INFO line, and the first check of a host is not a change;
  * the scheduled run stays a lookup unless Settings says full;
  * the pages: the Answers column, Check now / Check all / Check all hosts, the progress line,
    the not-answering notice and badge, and the two Settings rows' validation.

Every socket here is on loopback; a name that must not resolve is a patched lookup.
  python3 -m unittest tests.test_host_checks
"""
import os
import socket
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import account_links as links  # noqa: E402
from app import host_checks as hc  # noqa: E402
from app.alerts import ACCOUNT_HOST_ANSWER_CHANGED, ACCOUNT_HOST_NOT_ANSWERING  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (  # noqa: E402
    AccountHost, Alert, HostCheck, HOST_CHECK_ANSWERS, HOST_CHECK_NO_ANSWER, HOST_CHECK_REFUSED,
    HOST_CHECK_RESOLVES, HOST_CHECK_SERVER_ERROR, HOST_CHECK_UNRESOLVED, HOST_PROBE_FULL,
    HOST_PROBE_LOOKUP,
)


class _Server:
    """A loopback TCP server that does one thing per connection: answer `reply`, say
    nothing, or close at once. Records what it was sent."""

    def __init__(self, reply=None, mode='answer', delay=0.0):
        self.reply, self.mode, self.delay = reply, mode, delay
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.received = []
        self._stop = threading.Event()
        self._held = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.1)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self.mode == 'close':
                conn.close()
                continue
            if self.mode == 'silent':
                self._held.append(conn)
                continue
            conn.settimeout(2)
            try:
                self.received.append(conn.recv(4096))
                time.sleep(self.delay)
                conn.sendall(self.reply)
            except OSError:
                pass
            conn.close()

    def close(self):
        self._stop.set()
        for c in self._held:
            c.close()
        self.sock.close()
        self.thread.join(1)


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ProbeTests(unittest.TestCase):

    def _probe(self, server, **kw):
        return hc.probe(f'127.0.0.1:{server.port}', **kw)

    def test_a_host_that_answers_is_timed_at_every_step(self):
        srv = _Server(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n')
        try:
            r = self._probe(srv)
        finally:
            srv.close()
        self.assertEqual(r['status'], HOST_CHECK_ANSWERS)
        self.assertEqual(r['http_status'], 404, 'any code but a 5xx counts as answering')
        for key in ('lookup_ms', 'connect_ms', 'answer_ms'):
            self.assertIsNotNone(r[key], key)
        self.assertIsNone(r['error'])

    def test_the_request_names_no_login_and_no_channel(self):
        srv = _Server(b'HTTP/1.1 200 OK\r\n\r\n')
        try:
            self._probe(srv, user_agent='TestAgent/1')
        finally:
            srv.close()
        sent = srv.received[0].decode()
        self.assertTrue(sent.startswith('GET / HTTP/1.1\r\n'), sent)
        self.assertIn(f'Host: 127.0.0.1:{srv.port}', sent)
        self.assertIn('User-Agent: TestAgent/1', sent)
        self.assertNotIn('Authorization', sent)
        self.assertNotIn('/live/', sent)

    def test_a_server_error_is_its_own_verdict(self):
        srv = _Server(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
        try:
            r = self._probe(srv)
        finally:
            srv.close()
        self.assertEqual(r['status'], HOST_CHECK_SERVER_ERROR)
        self.assertEqual(r['http_status'], 502)
        self.assertIsNotNone(r['connect_ms'], 'the connection itself worked')

    def test_a_closed_port_is_refused(self):
        port = _free_port()
        r = hc.probe(f'127.0.0.1:{port}')
        self.assertEqual(r['status'], HOST_CHECK_REFUSED)
        self.assertIn(str(port), r['error'])
        self.assertIsNone(r['answer_ms'])

    def test_a_port_that_takes_the_connection_and_says_nothing_is_no_answer(self):
        srv = _Server(mode='silent')
        try:
            r = self._probe(srv, answer_timeout=0.3)
        finally:
            srv.close()
        self.assertEqual(r['status'], HOST_CHECK_NO_ANSWER)
        self.assertIsNotNone(r['connect_ms'])
        self.assertIn('nothing came back', r['error'])

    def test_a_connection_closed_without_a_word_is_no_answer(self):
        srv = _Server(mode='close')
        try:
            r = self._probe(srv, answer_timeout=1)
        finally:
            srv.close()
        self.assertEqual(r['status'], HOST_CHECK_NO_ANSWER)

    def test_a_name_that_does_not_resolve_tries_nothing_else(self):
        with mock.patch.object(hc, '_lookup', return_value=(None, 'Name or service not known')):
            r = hc.probe('gone.example')
        self.assertEqual(r['status'], HOST_CHECK_UNRESOLVED)
        self.assertEqual(r['error'], 'Name or service not known')
        self.assertIsNone(r['connect_ms'])

    def test_the_port_comes_from_the_host_or_the_scheme(self):
        with mock.patch.object(hc, '_lookup', return_value=(None, 'x')):
            self.assertEqual(hc.probe('a.example')['port'], 80)
            self.assertEqual(hc.probe('a.example', 'https')['port'], 443)
            self.assertEqual(hc.probe('a.example:8080')['port'], 8080)


class _Case(unittest.TestCase):
    """Two accounts listing a1 and a2 (a2 on both), a1 active on each."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.app.test_client()
        self.accs = []
        for name in ('skyline-direct', 'skyline-curated'):
            acc = seed.make_account(name=name)
            ch = seed.make_channel(acc, stream_id=1, name='ch')
            ch.stream_url = ch.raw_stream_url = 'http://a1.skyline.example/live/u/p/1.ts'
            db.session.commit()
            self.accs.append(acc.id)
        for acc_id in self.accs:
            links.add_host(acc_id, 'a2.skyline.example')
        links.add_host(self.accs[0], 'a3.skyline.example')
        db.session.expire_all()

    def tearDown(self):
        hc.reset_for_tests()
        self.t.cleanup()

    def _full(self, host, status, minutes_ago=60, **kw):
        row = HostCheck(host=host, kind=HOST_PROBE_FULL, status=status, trigger='manual',
                        checked_at=datetime.utcnow() - timedelta(minutes=minutes_ago), **kw)
        db.session.add(row)
        db.session.commit()
        return row

    def _alerts(self, kind, acc_id, open_only=True):
        q = Alert.query.filter(Alert.alert_type == kind, Alert.source.like(f'account:{acc_id}:%'))
        if open_only:
            q = q.filter(Alert.dismissed_at.is_(None))
        return q.all()


class LineTests(_Case):

    def test_a_host_on_two_accounts_goes_in_line_once(self):
        with mock.patch.object(hc, '_run'):
            hosts = [r.host for r in AccountHost.query.all()]
            self.assertEqual(hosts.count('a2.skyline.example'), 2)
            out = hc.request(self.t.app, hosts)
        self.assertEqual(sorted(out['added']),
                         ['a1.skyline.example', 'a2.skyline.example', 'a3.skyline.example'])
        self.assertEqual(list(hc._line).count('a2.skyline.example'), 1)

    def test_a_second_ask_does_not_add_it_twice(self):
        with mock.patch.object(hc, '_run'):
            hc.request(self.t.app, ['a1.skyline.example'])
            out = hc.request(self.t.app, ['a1.skyline.example'])
        self.assertEqual(out['added'], [])
        self.assertEqual(out['already'], ['a1.skyline.example'])
        self.assertIn('Already in line', hc.request_message(out, 'x'))

    def test_a_host_checked_in_the_last_ten_minutes_is_left_out_and_named(self):
        self._full('a1.skyline.example', HOST_CHECK_ANSWERS, minutes_ago=3)
        self._full('a2.skyline.example', HOST_CHECK_ANSWERS, minutes_ago=11)
        with mock.patch.object(hc, '_run'):
            out = hc.request(self.t.app, ['a1.skyline.example', 'a2.skyline.example'])
        self.assertEqual(out['added'], ['a2.skyline.example'])
        self.assertEqual([h for h, _ in out['recent']], ['a1.skyline.example'])
        with mock.patch.object(hc, '_run'):
            hc.reset_for_tests()
            alone = hc.request(self.t.app, ['a1.skyline.example'])
        self.assertIn('at most once every 10 minutes', hc.request_message(alone, 'x'))

    def test_checks_are_spaced_whoever_asks(self):
        starts = []

        def fake_check(host, trigger):
            starts.append(time.monotonic())

        cfg = load_config()
        cfg['hosts']['check_spacing_seconds'] = 0.4
        with mock.patch.object(hc, 'check_one', side_effect=fake_check), \
                mock.patch('app.config.load_config', return_value=cfg):
            hc.request(self.t.app, ['a1.skyline.example', 'a2.skyline.example'])
            hc.request(self.t.app, ['a3.skyline.example'])
            deadline = time.monotonic() + 5
            while (hc._line or hc._current or len(starts) < 3) and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertEqual(len(starts), 3)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertTrue(all(g >= 0.4 * hc.GAP_LOW - 0.02 for g in gaps), gaps)

    def test_stop_empties_the_line(self):
        with mock.patch.object(hc, '_run'):
            hc.request(self.t.app, ['a1.skyline.example', 'a2.skyline.example'])
        self.assertEqual(hc.stop(), 2)
        self.assertFalse(hc.status()['running'])

    def test_the_signature_moves_with_the_line(self):
        before = hc.signature()
        with mock.patch.object(hc, '_run'):
            hc.request(self.t.app, ['a1.skyline.example'])
        self.assertNotEqual(hc.signature(), before)


class CheckOneTests(_Case):

    def test_a_host_in_use_is_not_touched(self):
        with mock.patch.object(hc, 'streaming_hosts', return_value={'a1.skyline.example'}), \
                mock.patch.object(hc, 'probe') as probe:
            row = hc.check_one('a1.skyline.example', hc.TRIGGER_MANUAL)
        probe.assert_not_called()
        self.assertTrue(row.in_use)
        self.assertEqual(row.status, HOST_CHECK_ANSWERS)
        self.assertIn('wasn\'t touched', hc.describe(row)['line'])

    def test_streaming_hosts_is_the_active_host_of_an_account_holding_a_seat(self):
        with mock.patch('app.connection_limits.holder_counts', return_value={self.accs[0]: 1}):
            self.assertEqual(hc.streaming_hosts(), {'a1.skyline.example'})
        with mock.patch('app.connection_limits.holder_counts', return_value={}):
            self.assertEqual(hc.streaming_hosts(), set())

    def _check(self, host, status, **extra):
        result = {'status': status, 'lookup_ms': 10, 'connect_ms': 20, 'answer_ms': 30,
                  'http_status': 502 if status == HOST_CHECK_SERVER_ERROR else 200, 'port': 80,
                  'error': None if status == HOST_CHECK_ANSWERS else 'it answered HTTP 502, a server error'}
        result.update(extra)
        with mock.patch.object(hc, 'streaming_hosts', return_value=set()), \
                mock.patch.object(hc, 'probe', return_value=result):
            return hc.check_one(host, hc.TRIGGER_MANUAL)

    def test_an_active_host_that_stops_answering_warns_and_clears_when_it_answers(self):
        self._check('a1.skyline.example', HOST_CHECK_SERVER_ERROR)
        for acc_id in self.accs:
            open_ = self._alerts(ACCOUNT_HOST_NOT_ANSWERING, acc_id)
            self.assertEqual(len(open_), 1, 'a1 is active on both accounts')
            self.assertIn('server error', open_[0].title)
        self.assertEqual(hc.not_answering_accounts(self.accs),
                         {a: 'a1.skyline.example' for a in self.accs})
        db.session.query(HostCheck).update({HostCheck.checked_at: datetime.utcnow() - timedelta(hours=1)})
        db.session.commit()
        self._check('a1.skyline.example', HOST_CHECK_ANSWERS)
        for acc_id in self.accs:
            self.assertEqual(self._alerts(ACCOUNT_HOST_NOT_ANSWERING, acc_id), [])
        self.assertEqual(hc.not_answering_accounts(self.accs), {})

    def test_a_standby_host_changing_answer_is_one_info_line_and_a_first_check_is_none(self):
        self._check('a3.skyline.example', HOST_CHECK_ANSWERS)
        self.assertEqual(self._alerts(ACCOUNT_HOST_ANSWER_CHANGED, self.accs[0]), [],
                         'a first check is not a change')
        self._check('a3.skyline.example', HOST_CHECK_REFUSED, connect_ms=None, answer_ms=None,
                    http_status=None, error='nothing is taking connections on port 80')
        changed = self._alerts(ACCOUNT_HOST_ANSWER_CHANGED, self.accs[0])
        self.assertEqual(len(changed), 1)
        self.assertIn('stopped answering', changed[0].title)
        self.assertEqual(self._alerts(ACCOUNT_HOST_NOT_ANSWERING, self.accs[0]), [],
                         'a standby host raises no warning')

    def test_making_another_host_active_clears_the_warning(self):
        self._check('a1.skyline.example', HOST_CHECK_SERVER_ERROR)
        self._full('a2.skyline.example', HOST_CHECK_ANSWERS)
        a2 = AccountHost.query.filter_by(account_id=self.accs[0], host='a2.skyline.example').one()
        links.set_active_host(self.accs[0], a2.id)
        self.assertEqual(self._alerts(ACCOUNT_HOST_NOT_ANSWERING, self.accs[0]), [])
        self.assertEqual(len(self._alerts(ACCOUNT_HOST_NOT_ANSWERING, self.accs[1])), 1,
                         'the other account still uses a1')


class VerdictTests(_Case):

    def test_a_newer_failed_lookup_overrides_an_older_answer_but_a_resolving_one_does_not(self):
        self._full('a1.skyline.example', HOST_CHECK_ANSWERS, minutes_ago=60)
        hc.record_lookups({'a1.skyline.example': (True, None, 12)}, hc.TRIGGER_SCHEDULED)
        latest = hc.latest_for_hosts(['a1.skyline.example'])['a1.skyline.example']
        self.assertEqual(hc.effective(latest).status, HOST_CHECK_ANSWERS)
        hc.record_lookups({'a1.skyline.example': (False, 'Name or service not known', None)},
                          hc.TRIGGER_SCHEDULED)
        latest = hc.latest_for_hosts(['a1.skyline.example'])['a1.skyline.example']
        self.assertEqual(hc.effective(latest).status, HOST_CHECK_UNRESOLVED)

    def test_counts_are_full_checks_inside_the_window(self):
        self._full('a1.skyline.example', HOST_CHECK_ANSWERS, minutes_ago=30)
        self._full('a1.skyline.example', HOST_CHECK_SERVER_ERROR, minutes_ago=90, http_status=502)
        self._full('a1.skyline.example', HOST_CHECK_ANSWERS, minutes_ago=60 * 24 * 20)
        hc.record_lookups({'a1.skyline.example': (True, None, 5)}, hc.TRIGGER_SCHEDULED)
        latest = hc.latest_for_hosts(['a1.skyline.example'])['a1.skyline.example']
        self.assertEqual((latest['answered'], latest['checks']), (1, 2))

    def test_every_status_has_words_and_an_unknown_one_raises(self):
        for status in (HOST_CHECK_ANSWERS, HOST_CHECK_SERVER_ERROR, HOST_CHECK_REFUSED,
                       HOST_CHECK_NO_ANSWER, HOST_CHECK_UNRESOLVED, HOST_CHECK_RESOLVES):
            words = hc.describe(HostCheck(host='x', status=status, port=80, http_status=502,
                                          answer_ms=5, error='e'))
            self.assertTrue(words['label'] and words['badge'], status)
        with self.assertRaises(ValueError):
            hc.describe(HostCheck(host='x', status='MAYBE'))

    def test_old_rows_are_dropped_after_the_window(self):
        self._full('a1.skyline.example', HOST_CHECK_ANSWERS, minutes_ago=60 * 24 * (hc.KEEP_DAYS + 1))
        self._full('a1.skyline.example', HOST_CHECK_ANSWERS, minutes_ago=10)
        self.assertEqual(hc.cleanup_old_checks(), 1)
        self.assertEqual(HostCheck.query.count(), 1)


class RollTests(_Case):

    def test_a_roll_tries_a_host_that_answered_before_one_that_did_not(self):
        self._full('a2.skyline.example', HOST_CHECK_SERVER_ERROR, http_status=502, answer_ms=5)
        self._full('a3.skyline.example', HOST_CHECK_ANSWERS)
        lookups = []

        def resolve(host, timeout=None):
            lookups.append(host)
            return (False, 'Name or service not known') if host == 'a1.skyline.example' else (True, None)

        with mock.patch.object(links, 'resolve_host', side_effect=resolve):
            summary = links.roll_host(self.accs[0], 'Recording 1')
        self.assertEqual(summary['host'], 'a3.skyline.example')
        self.assertEqual(lookups, ['a3.skyline.example'], 'the answering host was tried first')
        alert = Alert.query.filter(Alert.source == f'account:{self.accs[0]}:host-rolled').one()
        self.assertIn('Passed over because they did not answer', alert.body)
        self.assertIn('a2.skyline.example', alert.body)

    def test_a_roll_still_takes_a_host_that_did_not_answer_when_nothing_else_resolves(self):
        self._full('a2.skyline.example', HOST_CHECK_SERVER_ERROR, http_status=502, answer_ms=5)
        with mock.patch.object(links, 'resolve_host',
                               side_effect=lambda h, timeout=None: (h == 'a2.skyline.example', None if h == 'a2.skyline.example' else 'gone')):
            summary = links.roll_host(self.accs[1], 'Recording 2')
        self.assertEqual(summary['host'], 'a2.skyline.example')
        alert = Alert.query.filter(Alert.source == f'account:{self.accs[1]}:host-rolled').one()
        self.assertIn('streams may fail', alert.body)

    def test_a_roll_writes_its_lookups_as_lookup_rows(self):
        with mock.patch.object(links, 'resolve_host',
                               side_effect=lambda h, timeout=None: (h != 'a1.skyline.example', None if h != 'a1.skyline.example' else 'gone')):
            links.roll_host(self.accs[0], 'Recording 1')
        kinds = {(r.host, r.kind, r.status) for r in HostCheck.query.all()}
        self.assertIn(('a2.skyline.example', HOST_PROBE_LOOKUP, HOST_CHECK_RESOLVES), kinds)


class ScheduledTests(_Case):

    def _cfg(self, mode):
        cfg = load_config()
        cfg['hosts']['scheduled_check'] = mode
        return cfg

    def test_by_default_the_scheduled_check_is_a_lookup_and_queues_nothing(self):
        with mock.patch('app.config.load_config', return_value=self._cfg('lookup')), \
                mock.patch.object(links, 'resolve_host', return_value=(True, None)), \
                mock.patch.object(hc, '_run'):
            out = links.check_hosts(app=self.t.app)
        self.assertEqual(out['failed'], 0)
        self.assertEqual(list(hc._line), [])
        rows = HostCheck.query.all()
        self.assertTrue(rows)
        self.assertTrue(all(r.kind == HOST_PROBE_LOOKUP and r.trigger == hc.TRIGGER_SCHEDULED for r in rows))

    def test_full_puts_every_host_name_in_the_line_once(self):
        with mock.patch('app.config.load_config', return_value=self._cfg('full')), \
                mock.patch.object(links, 'resolve_host') as resolve, \
                mock.patch.object(hc, '_run'):
            out = links.check_hosts(app=self.t.app)
        resolve.assert_not_called()
        self.assertEqual(out['queued'], 3)
        self.assertEqual(sorted(hc._line), ['a1.skyline.example', 'a2.skyline.example', 'a3.skyline.example'])
        self.assertEqual({hc._triggers[h] for h in hc._line}, {hc.TRIGGER_SCHEDULED})


class PageTests(_Case):

    def _page(self, acc_id=None):
        return self.client.get(f'/accounts/{acc_id or self.accs[0]}').get_data(as_text=True)

    def _card(self, html):
        return html.split('data-section="hosts"')[1].split('data-section="logins"')[0]

    def test_the_card_shows_each_verdict_with_its_numbers(self):
        self._full('a1.skyline.example', HOST_CHECK_ANSWERS, lookup_ms=14, connect_ms=42,
                   answer_ms=118, http_status=404, port=80)
        self._full('a2.skyline.example', HOST_CHECK_SERVER_ERROR, lookup_ms=12, connect_ms=38,
                   answer_ms=205, http_status=502, port=80)
        self._full('a3.skyline.example', HOST_CHECK_ANSWERS, lookup_ms=9, connect_ms=29,
                   answer_ms=88, http_status=200, port=80)
        card = self._card(self._page())
        self.assertIn('<th>Answers</th>', card)
        self.assertIn('lookup 14 ms · connect 42 ms · answer 118 ms', card)
        self.assertIn('HTTP 502 after 205 ms', card)
        self.assertIn('answered 1 of 1 check', card)
        a3 = card.split('<code>a3.skyline.example</code>')[1].split('</tr>')[0]
        self.assertIn('Fastest', a3)
        self.assertEqual(card.count('>Fastest<'), 1)
        self.assertIn('data-act="host-check"', card)
        self.assertIn('data-act="hosts-check"', card)

    def test_a_host_only_looked_up_says_not_tested_yet(self):
        hc.record_lookups({'a1.skyline.example': (True, None, 12)}, hc.TRIGGER_SCHEDULED)
        row = self._card(self._page()).split('<code>a1.skyline.example</code>')[1].split('</tr>')[0]
        self.assertIn('Not tested yet', row)
        self.assertIn('resolves', row)

    def test_while_checks_run_the_card_shows_progress_and_waiting_rows(self):
        with mock.patch.object(hc, '_run'):
            hc.request(self.t.app, ['a2.skyline.example', 'a3.skyline.example'])
        card = self._card(self._page())
        self.assertIn('data-host-progress', card)
        self.assertIn('one at a time', card)
        self.assertIn('Stop checking', card)
        self.assertIn('>Waiting<', card)
        self.assertIn('already in line', card)

    def test_an_active_host_not_answering_shows_a_notice_and_a_list_badge(self):
        self._full('a1.skyline.example', HOST_CHECK_NO_ANSWER, error='nothing came back', port=80)
        self.assertIn('acct-host-not-answering', self._page())
        listing = self.client.get('/accounts').get_data(as_text=True)
        self.assertIn('Active host not answering', listing)
        self.assertIn('data-hosts-check-all', listing)
        self.assertIn('data-act="hosts-check"', listing)

    def test_the_list_row_says_how_many_hosts_answer(self):
        self._full('a1.skyline.example', HOST_CHECK_ANSWERS, port=80, answer_ms=5)
        self._full('a2.skyline.example', HOST_CHECK_REFUSED, port=80)
        listing = self.client.get('/accounts').get_data(as_text=True)
        self.assertIn('hosts: 1 of 2 answers', listing)

    def test_the_check_routes_queue_and_answer_in_the_envelope(self):
        a3 = AccountHost.query.filter_by(account_id=self.accs[0], host='a3.skyline.example').one()
        with mock.patch.object(hc, '_run'):
            one = self.client.post(f'/api/accounts/{self.accs[0]}/hosts/{a3.id}/check').get_json()
            self.assertTrue(one['success'])
            self.assertEqual(one['queued'], 1)
            acct = self.client.post(f'/api/accounts/{self.accs[0]}/hosts/check').get_json()
            self.assertEqual(acct['queued'], 2, 'a3 was already in line')
            every = self.client.post('/api/accounts/hosts/check').get_json()
            self.assertEqual(every['queued'], 0)
            stop = self.client.post('/api/accounts/hosts/check/stop').get_json()
        self.assertEqual(stop['stopped'], 3)
        other = AccountHost.query.filter_by(account_id=self.accs[1]).first()
        r = self.client.post(f'/api/accounts/{self.accs[0]}/hosts/{other.id}/check')
        self.assertEqual(r.status_code, 404)

    def test_the_settings_rows_refuse_bad_values(self):
        r = self.client.post('/api/settings/field', json={'path': 'hosts.scheduled_check', 'value': 'often'})
        self.assertEqual(r.status_code, 400)
        r = self.client.post('/api/settings/field', json={'path': 'hosts.check_spacing_seconds', 'value': 3})
        self.assertEqual(r.status_code, 400)
        self.assertIn('at least 10', r.get_json()['error'])


if __name__ == '__main__':
    unittest.main()
