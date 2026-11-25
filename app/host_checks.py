"""Host checks: is each listed host up, and how fast does it answer (dev/changelog/1674).

An account's Hosts card lists the host names a reseller handed out. The scheduled check
(`account_links.check_hosts()`) has only ever asked DNS whether each name still exists, and a
host that resolves but no longer answers looks the same as a good one. A **full check** asks
the host itself, in three timed steps:

1. **Lookup** - `getaddrinfo`, on a helper thread because it has no timeout of its own.
2. **Connect** - a TCP connection to the port the streams use: the host's own `:port`, else
   the scheme's default, the scheme read off the account's stream URLs. Not ICMP: an
   unprivileged ICMP socket is refused on the reference box (`net.ipv4.ping_group_range` is
   `1 0`), Docker host networking inherits that, and a CDN answers a ping from its own edge.
3. **Answer** - one plain request, `GET /` with no username, password, channel or account in
   it, on the same connection, timed to the first byte. Any answer counts as up whatever its
   code, except a server error (5xx): a host in front of a CDN keeps resolving and connecting
   after the server behind it is gone, and the 502 is the only thing that says so.

**Pacing is the point** (principle 2: never touch a provider hard enough to risk the account).
Every check, scheduled or asked for, goes through ONE line and one worker thread:

- one host at a time, with a random gap of 0.75-1.5x `hosts.check_spacing_seconds` (default
  60) between the end of one check and the start of the next - a fixed rhythm through a
  reseller's host list is what a scanner looks like, and pressing three buttons cannot raise
  the rate;
- a host NAME is checked once per run even when two accounts list it;
- no host gets a full check twice inside `REPEAT_FLOOR`, by hand or not;
- a host a stream is using right now is not touched, because a check must never risk a
  working capture. The skip is recorded (`HOST_CHECK_IN_USE`, `in_use` set) but it is no
  verdict: a stream stuck reconnecting to a dead host holds its seat too, so the host's
  previous verdict stands and the warning is left as it was (dev/changelog/1730).

The scheduled run stays a lookup only unless `hosts.scheduled_check` is `full`; Check now,
Check all and Check all hosts are always the full check. The line lives in memory: a restart
empties it, and the next scheduled run or click fills it again.

Results go to `HostCheck` (one row per check, keyed by host name, kept `KEEP_DAYS`). A check
never swaps hosts - only a host that stops resolving does (DESIGN-account-providers.md §4.3);
`rank()` lets that swap prefer a host that answered at its last check. An ACTIVE host that
stops answering raises `ACCOUNT_HOST_NOT_ANSWERING`, cleared when it answers again or another
host becomes active; a standby host's change of answer is an INFO line on the account's
Activity card.
"""
from __future__ import annotations

import logging
import random
import re
import socket
import ssl
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import func

from . import db
from .database import (HOST_CHECK_ANSWERS, HOST_CHECK_IN_USE, HOST_CHECK_NO_ANSWER,
                       HOST_CHECK_REFUSED, HOST_CHECK_RESOLVES, HOST_CHECK_SERVER_ERROR,
                       HOST_CHECK_UNRESOLVED, HOST_PROBE_FULL, HOST_PROBE_LOOKUP, HostCheck)
from .db_utils import chunked, retry_on_locked

log = logging.getLogger(__name__)

LOOKUP_TIMEOUT_SECONDS = 5.0
CONNECT_TIMEOUT_SECONDS = 5.0
ANSWER_TIMEOUT_SECONDS = 10.0
#: No host gets a full check twice inside this, whoever asks.
REPEAT_FLOOR = timedelta(minutes=10)
#: How long a check's row is kept; "answered N of M" counts the full checks inside it.
KEEP_DAYS = 14
DEFAULT_SPACING_SECONDS = 60
GAP_LOW, GAP_HIGH = 0.75, 1.5

TRIGGER_SCHEDULED = 'scheduled'
TRIGGER_MANUAL = 'manual'
TRIGGER_ROLL = 'roll'

#: The full-check statuses that mean the host is not up. ANSWERS is the only good one.
NOT_ANSWERING = (HOST_CHECK_SERVER_ERROR, HOST_CHECK_REFUSED, HOST_CHECK_NO_ANSWER,
                 HOST_CHECK_UNRESOLVED)

_HTTP_STATUS = re.compile(rb'^HTTP/\d(?:\.\d)?\s+(\d{3})')


# ── The probe: network only, no database ────────────────────────────────────────────────

def split_host(host: str) -> tuple:
    """('name', port or None) from `name[:port]` - the one split of a stored host for the
    hosts code (account_links.normalize_host() and resolve_host() call it too)."""
    name, sep, port = host.rpartition(':')
    if sep and name and port.isdecimal():
        return name, int(port)
    return host, None


def _ms(start: float) -> int:
    return max(0, round((time.perf_counter() - start) * 1000))


def lookup(name: str, port: Optional[int] = None, timeout: float = LOOKUP_TIMEOUT_SECONDS):
    """(addrinfo list, None) or (None, error text) - the one DNS lookup of the hosts code,
    for the full check and for account_links.resolve_host(). getaddrinfo has no timeout, so
    it runs on a helper thread that is abandoned past `timeout`."""
    result: dict = {}

    def run():
        try:
            result['infos'] = socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            result['error'] = exc.strerror or str(exc)
        except OSError as exc:
            result['error'] = str(exc)

    worker = threading.Thread(target=run, name=f'host-lookup-{name}', daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return None, f'no answer from the resolver within {timeout:.0f} seconds'
    if result.get('infos'):
        return result['infos'], None
    return None, result.get('error') or 'did not resolve'


def probe(host: str, scheme: str = 'http', *, user_agent: str = 'VLC/3.0.18 LibVLC/3.0.18',
          lookup_timeout: float = LOOKUP_TIMEOUT_SECONDS,
          connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
          answer_timeout: float = ANSWER_TIMEOUT_SECONDS) -> dict:
    """One full check of `host`: lookup, connect, one plain request. Returns the HostCheck
    fields (status, lookup_ms, connect_ms, answer_ms, http_status, port, error). Never raises
    for anything the network does; every way it can go wrong is one of the statuses."""
    name, port = split_host(host)
    tls = scheme == 'https' or port == 443
    port = port or (443 if tls else 80)
    out = {'status': None, 'lookup_ms': None, 'connect_ms': None, 'answer_ms': None,
           'http_status': None, 'port': port, 'error': None}

    start = time.perf_counter()
    infos, error = lookup(name, port, lookup_timeout)
    if infos is None:
        out.update(status=HOST_CHECK_UNRESOLVED, error=error[:255])
        return out
    out['lookup_ms'] = _ms(start)

    sock = None
    refused = timed_out = 0
    last_error = None
    seen = set()
    for family, kind, proto, _, addr in infos:
        if addr in seen:
            continue
        seen.add(addr)
        s = socket.socket(family, kind, proto)
        s.settimeout(connect_timeout)
        start = time.perf_counter()
        try:
            s.connect(addr)
        except ConnectionRefusedError:
            refused += 1
            s.close()
            continue
        except (socket.timeout, TimeoutError):
            timed_out += 1
            s.close()
            continue
        except OSError as exc:
            last_error = exc.strerror or str(exc)
            s.close()
            continue
        out['connect_ms'] = _ms(start)
        sock = s
        break
    if sock is None:
        if refused and not timed_out and last_error is None:
            out.update(status=HOST_CHECK_REFUSED,
                       error=f'nothing is taking connections on port {port} (connection refused)')
        elif timed_out:
            out.update(status=HOST_CHECK_NO_ANSWER,
                       error=f'port {port} did not take a connection within {connect_timeout:.0f} seconds')
        else:
            out.update(status=HOST_CHECK_NO_ANSWER,
                       error=(last_error or f'could not connect to port {port}')[:255])
        return out

    try:
        start = time.perf_counter()
        sock.settimeout(answer_timeout)
        if tls:
            ctx = ssl.create_default_context()
            # Only timing is asked of the server, never trust: a reseller's spare host
            # commonly serves a certificate for another of its names.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=name)
        host_header = name if port in (80, 443) else f'{name}:{port}'
        sock.sendall((f'GET / HTTP/1.1\r\nHost: {host_header}\r\nUser-Agent: {user_agent}\r\n'
                      'Accept: */*\r\nConnection: close\r\n\r\n').encode('latin-1', 'replace'))
        first = sock.recv(64)
        if not first:
            out.update(status=HOST_CHECK_NO_ANSWER,
                       error='it took the connection and closed it without answering')
            return out
        out['answer_ms'] = _ms(start)
        m = _HTTP_STATUS.match(first)
        if m:
            out['http_status'] = int(m.group(1))
        if out['http_status'] is not None and 500 <= out['http_status'] <= 599:
            out.update(status=HOST_CHECK_SERVER_ERROR,
                       error=f'it answered HTTP {out["http_status"]}, a server error')
        else:
            out['status'] = HOST_CHECK_ANSWERS
        return out
    except (socket.timeout, TimeoutError):
        out.update(status=HOST_CHECK_NO_ANSWER,
                   error=f'it took the connection, but nothing came back within {answer_timeout:.0f} seconds')
        return out
    except ssl.SSLError as exc:
        out.update(status=HOST_CHECK_NO_ANSWER, error=f'the secure connection failed: {exc}'[:255])
        return out
    except OSError as exc:
        out.update(status=HOST_CHECK_NO_ANSWER,
                   error=f'the connection broke before it answered: {exc.strerror or exc}'[:255])
        return out
    finally:
        try:
            sock.close()
        except OSError:
            pass  # closing a socket the peer already reset; nothing left to release


# ── What the database says ──────────────────────────────────────────────────────────────

def latest_for_hosts(hosts: Iterable[str], now: Optional[datetime] = None) -> dict:
    """{host: {'full': HostCheck|None, 'lookup': HostCheck|None, 'skipped': HostCheck|None,
    'answered': n, 'checks': n}} for every host given, in four queries however many there
    are. `full` is the newest full check that asked the host; `skipped` the newest one left
    out because a stream was using it (`in_use`, whatever its status - rows before
    dev/changelog/1730 said ANSWERS). `checks` and `answered` count the full checks inside
    KEEP_DAYS that asked the host; a skipped one is neither."""
    names = sorted({h for h in hosts if h})
    out = {h: {'full': None, 'lookup': None, 'skipped': None, 'answered': 0, 'checks': 0}
           for h in names}
    if not names:
        return out
    now = now or datetime.utcnow()
    since = now - timedelta(days=KEEP_DAYS)
    for kind, in_use, key in ((HOST_PROBE_FULL, False, 'full'), (HOST_PROBE_FULL, True, 'skipped'),
                              (HOST_PROBE_LOOKUP, False, 'lookup')):
        for part in chunked(names, 400):
            newest = (db.session.query(func.max(HostCheck.id))
                      .filter(HostCheck.host.in_(part), HostCheck.kind == kind,
                              HostCheck.in_use.is_(in_use))
                      .group_by(HostCheck.host))
            for row in HostCheck.query.filter(HostCheck.id.in_(newest)).all():
                out[row.host][key] = row
    for part in chunked(names, 400):
        rows = (db.session.query(HostCheck.host, HostCheck.status, func.count(HostCheck.id))
                .filter(HostCheck.host.in_(part), HostCheck.kind == HOST_PROBE_FULL,
                        HostCheck.in_use.is_(False), HostCheck.checked_at >= since)
                .group_by(HostCheck.host, HostCheck.status).all())
        for host, status, n in rows:
            out[host]['checks'] += n
            if status == HOST_CHECK_ANSWERS:
                out[host]['answered'] += n
    return out


def effective(latest: Optional[dict]):
    """The verdict a host's row shows: its newest full check, unless a lookup since then
    found the name gone. A lookup that still resolves does not override a full check's
    verdict - resolving was never the question - except a full check that could not look the
    name up: that verdict was only about the lookup, so a newer lookup that resolves sets it
    aside and the host reads as untested (None) until a full check asks it again
    (dev/changelog/1731)."""
    if not latest:
        return None
    full, lookup = latest.get('full'), latest.get('lookup')
    if lookup is not None and lookup.status == HOST_CHECK_UNRESOLVED and (
            full is None or lookup.checked_at > full.checked_at):
        return lookup
    if resolved_since(latest):
        return None
    return full


def resolved_since(latest: Optional[dict]) -> bool:
    """True when the newest full check found the name gone and a newer lookup resolves it:
    effective() then gives no verdict, and the card says "Not checked since"."""
    if not latest:
        return False
    full, lookup = latest.get('full'), latest.get('lookup')
    return (full is not None and full.status == HOST_CHECK_UNRESOLVED and lookup is not None
            and lookup.status == HOST_CHECK_RESOLVES and lookup.checked_at > full.checked_at)


def is_answering(check) -> Optional[bool]:
    """True/False from a full check (or an overriding failed lookup); None when untested."""
    if check is None:
        return None
    if check.status == HOST_CHECK_ANSWERS:
        return True
    if check.status in NOT_ANSWERING:
        return False
    if check.status in (HOST_CHECK_RESOLVES, HOST_CHECK_IN_USE):
        return None
    raise ValueError(f'unknown host check status {check.status!r}')


def rank(hosts: Iterable[str]) -> dict:
    """{host: 0 answered at its last check, 1 never tested, 2 did not answer} - the order a
    swap tries hosts in (account_links.roll_host). The swap's own trigger and its lookups are
    unchanged; this only decides which resolving host it reaches first."""
    latest = latest_for_hosts(hosts)
    out = {}
    for host in hosts:
        answering = is_answering(effective(latest.get(host)))
        out[host] = 0 if answering else (1 if answering is None else 2)
    return out


def describe(check) -> dict:
    """The words for one verdict: {'label', 'badge', 'line', 'tip'}. Every status is named;
    an unknown one raises rather than rendering as a good state."""
    s = check.status
    port = check.port or 80
    timings = ' · '.join(p for p in (
        f'lookup {check.lookup_ms} ms' if check.lookup_ms is not None else '',
        f'connect {check.connect_ms} ms' if check.connect_ms is not None else '',
        f'answer {check.answer_ms} ms' if check.answer_ms is not None else '') if p)
    steps = []
    if check.lookup_ms is not None:
        steps.append(f'Lookup: {check.lookup_ms} ms to look the name up.')
    if check.connect_ms is not None:
        steps.append(f'Connect: {check.connect_ms} ms for port {port} to take a connection.')
    if check.answer_ms is not None:
        code = f' (HTTP {check.http_status})' if check.http_status else ''
        steps.append(f'Answer: {check.answer_ms} ms for the server to answer a plain request '
                     f'with no login in it{code}.')
    if s == HOST_CHECK_IN_USE or check.in_use:
        return {'label': 'Not checked', 'badge': 'b-abort',
                'line': 'A stream was using it, so it wasn\'t touched.',
                'tip': 'Not checked\nSomething was streaming from this host when its turn came, '
                       'so it was left alone. A stream that keeps reconnecting holds its seat '
                       'too, so this says nothing about whether the host answers.'}
    if s == HOST_CHECK_ANSWERS:
        return {'label': 'Answers', 'badge': 'b-done', 'line': timings,
                'tip': 'Answers\n' + '\n'.join(steps + [
                    'Any answer counts, whatever its code, except a server error (5xx).'])}
    if s == HOST_CHECK_SERVER_ERROR:
        return {'label': 'Server error', 'badge': 'b-warn',
                'line': (f'HTTP {check.http_status} after {check.answer_ms} ms. The name and the '
                         'connection work; the server behind them doesn\'t.'),
                'tip': 'Server error\n' + '\n'.join(steps + [
                    'A server error usually means the host is in front of a server that is gone.'])}
    if s == HOST_CHECK_REFUSED:
        return {'label': 'Refused', 'badge': 'b-fail',
                'line': f'Nothing is taking connections on port {port} (connection refused).',
                'tip': 'Refused\n' + '\n'.join(steps + [f'Connect: port {port} refused the connection.'])}
    if s == HOST_CHECK_NO_ANSWER:
        line = (check.error or 'no answer').rstrip('.')
        return {'label': 'No answer', 'badge': 'b-fail', 'line': line[:1].upper() + line[1:] + '.',
                'tip': 'No answer\n' + '\n'.join(steps + [line[:1].upper() + line[1:] + '.'])}
    if s == HOST_CHECK_UNRESOLVED:
        return {'label': 'Doesn\'t resolve', 'badge': 'b-fail', 'line': check.error or 'did not resolve',
                'tip': 'Doesn\'t resolve\nThe name could not be looked up, so nothing else was tried.'}
    if s == HOST_CHECK_RESOLVES:
        return {'label': 'Resolves', 'badge': 'b-done', 'line': timings,
                'tip': 'Resolves\nA lookup only: the name exists. Nothing was asked of the host.'}
    raise ValueError(f'unknown host check status {s!r}')


# ── Writing results ─────────────────────────────────────────────────────────────────────

def record_lookups(verdicts: dict, trigger: str) -> None:
    """Write a lookup-only row per host: {host: (ok, error, ms or None)}. The scheduled
    lookup and a swap's lookups land here, so the card's verdict follows the newest thing
    anybody learned about a name."""
    if not verdicts:
        return

    @retry_on_locked()
    def _write_and_commit():
        now = datetime.utcnow()
        for host, (ok, error, ms) in verdicts.items():
            db.session.add(HostCheck(
                host=host, checked_at=now, kind=HOST_PROBE_LOOKUP, trigger=trigger,
                status=HOST_CHECK_RESOLVES if ok else HOST_CHECK_UNRESOLVED,
                lookup_ms=ms if ok else None, error=None if ok else (error or 'did not resolve')[:255]))
        db.session.commit()

    _write_and_commit()
    _reassess_set_aside([h for h, (ok, _, _) in verdicts.items() if ok])


def _reassess_set_aside(hosts: list) -> None:
    """A resolving lookup that set aside a "Doesn't resolve" full check on an active host
    takes that host's warning down (dev/changelog/1731). Only those: re-asking every active
    host would bring a standing warning back unread at every scheduled lookup."""
    from .database import AccountHost
    if not hosts:
        return
    try:
        latest = latest_for_hosts(hosts)
        set_aside = [h for h in hosts if resolved_since(latest.get(h))]
        if not set_aside:
            return
        accounts = {a for (a,) in db.session.query(AccountHost.account_id)
                    .filter(AccountHost.host.in_(set_aside), AccountHost.is_active.is_(True),
                            AccountHost.enabled.is_(True)).all()}
    except Exception:
        # Never raises: a swap reaches here through its lookups, before it moves anything.
        log.exception('Host check: could not reassess hosts a lookup found again')
        return
    for account_id in sorted(accounts):
        reassess_active(account_id)


def _write_full(host: str, result: dict, trigger: str, in_use: bool) -> HostCheck:
    @retry_on_locked()
    def _write_and_commit():
        row = HostCheck(host=host, checked_at=datetime.utcnow(), kind=HOST_PROBE_FULL,
                        trigger=trigger, in_use=in_use, **result)
        db.session.add(row)
        db.session.commit()
        return row

    return _write_and_commit()


def cleanup_old_checks(app=None) -> int:
    """Drop rows older than KEEP_DAYS (the daily DB maintenance). Returns the count."""
    def run():
        @retry_on_locked()
        def _delete_and_commit():
            cutoff = datetime.utcnow() - timedelta(days=KEEP_DAYS)
            n = HostCheck.query.filter(HostCheck.checked_at < cutoff).delete(synchronize_session=False)
            db.session.commit()
            return n
        n = _delete_and_commit()
        if n:
            log.info('Host checks: dropped %d rows older than %d days', n, KEEP_DAYS)
        return n
    if app is None:
        return run()
    with app.app_context():
        return run()


def streaming_hosts() -> set:
    """The host names something is streaming from right now: the active host of every
    account holding a connection. Advisory, like every connection_limits count."""
    from .connection_limits import holder_counts
    from .database import AccountHost
    busy = [a for a, n in holder_counts().items() if n]
    if not busy:
        return set()
    rows = (db.session.query(AccountHost.host)
            .filter(AccountHost.account_id.in_(busy), AccountHost.is_active.is_(True),
                    AccountHost.enabled.is_(True)).all())
    return {h for (h,) in rows}


def scheme_for(host: str) -> str:
    """'https' or 'http': the scheme of the stream URLs on this host, else - a standby host,
    whose URLs were all rewritten onto the active one - of those on the active host of an
    account listing it, the scheme render_host() would keep if it became active
    (dev/changelog/1733). A few indexed queries over the channels of those accounts."""
    from .database import AccountHost, Channel
    account_ids = [a for (a,) in db.session.query(AccountHost.account_id)
                   .filter(AccountHost.host == host).all()]
    if not account_ids:
        return 'http'

    def first_scheme(name):
        escaped = name.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        for scheme in ('https', 'http'):
            hit = (db.session.query(Channel.id)
                   .filter(Channel.account_id.in_(account_ids),
                           Channel.stream_url.like(f'{scheme}://{escaped}/%', escape='\\'))
                   .limit(1).first())
            if hit:
                return scheme
        return None

    own = first_scheme(host)
    if own:
        return own
    actives = sorted({h for (h,) in db.session.query(AccountHost.host)
                      .filter(AccountHost.account_id.in_(account_ids),
                              AccountHost.is_active.is_(True), AccountHost.host != host).all()})
    for active in actives:
        found = first_scheme(active)
        if found:
            return found
    return 'http'


def spacing_seconds(cfg: dict) -> float:
    try:
        return max(0.0, float((cfg.get('hosts') or {}).get('check_spacing_seconds',
                                                            DEFAULT_SPACING_SECONDS)))
    except (TypeError, ValueError):
        return float(DEFAULT_SPACING_SECONDS)


def check_one(host: str, trigger: str) -> HostCheck:
    """Run one full check of `host` now (the worker's step; tests call it directly), write
    its row and say what changed. Needs an app context."""
    from .accounts import _request_headers
    from .config import load_config
    from . import account_links
    cfg = load_config()
    prev = latest_for_hosts([host]).get(host) or {}
    # The last verdict that asked the host: a full check a later lookup set aside still says
    # what the host did, so a standby host coming back after it says "answers again".
    was = is_answering(effective(prev) or prev.get('full'))
    if host in streaming_hosts():
        # No verdict: the previous one stands and nothing is reported (dev/changelog/1730).
        port = split_host(host)[1]
        row = _write_full(host, {'status': HOST_CHECK_IN_USE, 'port': port}, trigger, True)
        log.info('Host check: %s is in use by a stream, so it was not touched and its last '
                 'verdict stands', host)
        return row
    result = probe(host, scheme_for(host),
                   user_agent=_request_headers(cfg).get('User-Agent', 'VLC/3.0.18 LibVLC/3.0.18'))
    row = _write_full(host, result, trigger, False)
    account_links.stamp_lookup(host, result['status'] != HOST_CHECK_UNRESOLVED,
                               result['error'] if result['status'] == HOST_CHECK_UNRESOLVED else None)
    log.log(logging.INFO if result['status'] == HOST_CHECK_ANSWERS else logging.WARNING,
            'Host check: %s %s (lookup %s ms, connect %s ms, answer %s ms%s)%s', host,
            result['status'], result['lookup_ms'], result['connect_ms'], result['answer_ms'],
            f', HTTP {result["http_status"]}' if result['http_status'] else '',
            f': {result["error"]}' if result['error'] else '')
    _report(host, was, row)
    return row


# ── Saying what changed ─────────────────────────────────────────────────────────────────

def _not_answering_source(account_id: int) -> str:
    return f'account:{account_id}:host-not-answering'


def _report(host: str, was: Optional[bool], row: HostCheck) -> None:
    """The active host not answering raises (or clears) the account's standing warning; a
    standby host whose answer changed gets one INFO line on the account's Activity card.
    Never raises: the check's row is already written."""
    from .alerts import ACCOUNT_HOST_ANSWER_CHANGED, create_alert
    from .database import Account, AccountHost
    try:
        now = is_answering(row)
        words = describe(row)
        rows = (db.session.query(AccountHost, Account.name)
                .join(Account, Account.id == AccountHost.account_id)
                .filter(AccountHost.host == host).all())
        for ah, name in rows:
            if ah.is_active and ah.enabled:
                _set_not_answering(ah.account_id, name, host, words if now is False else None)
            elif was is not None and now is not None and was != now:
                line = (f'Host {host} answers again.' if now
                        else f'Host {host} stopped answering: {words["line"]}')
                create_alert(ACCOUNT_HOST_ANSWER_CHANGED, title=f'{name}: {line}',
                             body=(f'A check of {host}, a standby host on {name}, found it '
                                   f'{"answering again" if now else "not answering"}. Nothing '
                                   f'streams from it, so nothing changed for recordings.'),
                             source=f'account:{ah.account_id}:host-answer')
    except Exception:
        log.exception('Host check: could not report the result for %s', host)


def _set_not_answering(account_id: int, name: str, host: str, words: Optional[dict]) -> None:
    from .accounts import _raise_or_resolve_standing_alert
    from .alerts import ACCOUNT_HOST_NOT_ANSWERING
    if words is None:
        _raise_or_resolve_standing_alert(ACCOUNT_HOST_NOT_ANSWERING,
                                         source=_not_answering_source(account_id), active=False)
        return
    _raise_or_resolve_standing_alert(
        ACCOUNT_HOST_NOT_ANSWERING, source=_not_answering_source(account_id), active=True,
        title=f'{name}: the active host {host} isn\'t answering ({words["label"].lower()})',
        body=(f'A check of {host}, the host every stream on {name} uses, found: {words["line"]} '
              f'Streams still use it, because a host is swapped only when it stops resolving. '
              f'Make another host active on the account page to move now.'))


def reassess_active(account_id: int) -> None:
    """The account's active host changed (a swap, Make active, a switch): raise or clear its
    warning from what the new active host's last check found. Never raises."""
    from .database import Account, AccountHost
    try:
        account = db.session.get(Account, account_id)
        active = (AccountHost.query.filter_by(account_id=account_id, is_active=True, enabled=True)
                  .first())
        if account is None:
            return
        check = effective(latest_for_hosts([active.host]).get(active.host)) if active else None
        if active is not None and is_answering(check) is False:
            _set_not_answering(account_id, account.name, active.host, describe(check))
        else:
            _set_not_answering(account_id, account.name, '', None)
    except Exception:
        log.exception('Host check: could not reassess account %d\'s active host', account_id)


def not_answering_accounts(account_ids: Iterable[int]) -> dict:
    """{account_id: host} for the accounts whose active host did not answer at its last check
    - the Accounts list's badge and the account page's notice. Two queries."""
    from .database import AccountHost
    ids = [a for a in account_ids if a is not None]
    if not ids:
        return {}
    rows = (db.session.query(AccountHost.account_id, AccountHost.host)
            .filter(AccountHost.account_id.in_(ids), AccountHost.is_active.is_(True),
                    AccountHost.enabled.is_(True)).all())
    latest = latest_for_hosts(h for _, h in rows)
    return {a: h for a, h in rows if is_answering(effective(latest.get(h))) is False}


def account_summaries(account_ids: Iterable[int]) -> dict:
    """{account_id: {'hosts', 'tested', 'answering', 'active_not_answering'}} for the Accounts
    list, in a fixed number of queries: how many of each account's turned-on hosts answered at
    their last check, and the active host's name when it did not."""
    from .database import AccountHost
    ids = [a for a in account_ids if a is not None]
    out = {a: {'hosts': 0, 'tested': 0, 'answering': 0, 'active_not_answering': None} for a in ids}
    if not ids:
        return out
    rows = (db.session.query(AccountHost.account_id, AccountHost.host, AccountHost.is_active,
                             AccountHost.enabled)
            .filter(AccountHost.account_id.in_(ids)).all())
    latest = latest_for_hosts(h for _, h, _, _ in rows)
    for account_id, host, active, enabled in rows:
        cur = out[account_id]
        cur['hosts'] += 1
        if not enabled:
            continue
        answering = is_answering(effective(latest.get(host)))
        if answering is not None:
            cur['tested'] += 1
            cur['answering'] += 1 if answering else 0
        if active and answering is False:
            cur['active_not_answering'] = host
    return out


# ── The line: one host at a time, spaced ────────────────────────────────────────────────

_lock = threading.Lock()
_wake = threading.Event()
_line: deque = deque()
_triggers: dict = {}
_current: Optional[str] = None
_next_at = 0.0           # time.monotonic() before which the next check may not start
_next_at_utc: Optional[datetime] = None
_worker: Optional[threading.Thread] = None
_batch = {'total': 0, 'done': 0}
_finished = 0            # checks finished since start - the signature's moving part
_spacing_seen = float(DEFAULT_SPACING_SECONDS)


def request(app, hosts: Iterable[str], trigger: str = TRIGGER_MANUAL) -> dict:
    """Put hosts in line for a full check. Returns {'added': [...], 'recent': [(host,
    checked_at)], 'already': [...]}: a host already in line or being checked is not added
    twice, and one given a full check inside REPEAT_FLOOR is left out and named."""
    from .config import load_config
    names = list(dict.fromkeys(h for h in hosts if h))
    latest = latest_for_hosts(names)
    floor = datetime.utcnow() - REPEAT_FLOOR
    recent = []
    wanted = []
    for h in names:
        full = latest[h]['full']
        if full is not None and full.checked_at > floor:
            recent.append((h, full.checked_at))
        else:
            wanted.append(h)
    spacing = spacing_seconds(load_config())
    global _worker, _spacing_seen
    added, already = [], []
    with _lock:
        _spacing_seen = spacing
        for h in wanted:
            if h == _current or h in _line:
                already.append(h)
                continue
            _line.append(h)
            _triggers[h] = trigger
            added.append(h)
        _batch['total'] += len(added)
        if added and (_worker is None or not _worker.is_alive()):
            _worker = threading.Thread(target=_run, args=(app,), name='host-checks', daemon=True)
            _worker.start()
        _wake.set()
    if added:
        log.info('Host checks: %d host(s) put in line (%s)%s', len(added), trigger,
                 f'; {len(recent)} checked in the last 10 minutes left out' if recent else '')
    return {'added': added, 'recent': recent, 'already': already}


def stop() -> int:
    """Empty the line; a check already running finishes. Returns how many were taken out."""
    with _lock:
        n = len(_line)
        _line.clear()
        _triggers.clear()
        _wake.set()
    if n:
        log.info('Host checks: stopped with %d host(s) left unchecked', n)
    return n


def _gap(spacing: float) -> float:
    return spacing * random.uniform(GAP_LOW, GAP_HIGH)


def _run(app) -> None:
    """The one worker. Exits when the line is empty; request() starts another."""
    global _current, _next_at, _next_at_utc, _worker, _finished, _spacing_seen
    while True:
        with _lock:
            if not _line:
                _current = None
                _worker = None
                _batch.update(total=0, done=0)
                return
            wait = _next_at - time.monotonic()
            if wait <= 0:
                host = _line.popleft()
                trigger = _triggers.pop(host, TRIGGER_MANUAL)
                _current = host
            _wake.clear()
        if wait > 0:
            _wake.wait(wait)
            continue
        try:
            with app.app_context():
                check_one(host, trigger)
                from .config import load_config
                spacing = spacing_seconds(load_config())
        except Exception:
            log.exception('Host check of %s failed', host)
            spacing = _spacing_seen
        gap = _gap(spacing)
        with _lock:
            _current = None
            _finished += 1
            _batch['done'] += 1
            _spacing_seen = spacing
            _next_at = time.monotonic() + gap
            _next_at_utc = datetime.utcnow() + timedelta(seconds=gap)


def status() -> dict:
    """What the line is doing, for the pages: {'running', 'current', 'line', 'done', 'total',
    'next_at' (naive UTC or None), 'spacing', 'left_seconds'}."""
    with _lock:
        line = list(_line)
        current = _current
        done, total = _batch['done'], _batch['total']
        next_at = _next_at_utc if (line and not current and _next_at > time.monotonic()) else None
        spacing = _spacing_seen
        wait = max(0.0, _next_at - time.monotonic()) if line and not current else 0.0
    left = wait + max(0, len(line) - (0 if current else 1)) * spacing * (GAP_LOW + GAP_HIGH) / 2
    return {'running': bool(line or current), 'current': current, 'line': line, 'done': done,
            'total': total, 'next_at': next_at, 'spacing': spacing, 'left_seconds': left}


def signature() -> str:
    """Moves whenever a check starts, finishes or the line changes length - folded into
    accounts.sync_signature() so the account pages re-render as checks go by."""
    with _lock:
        return f'{_finished}.{len(_line)}.{_current or ""}'


def progress_view(scope_hosts: Optional[Iterable[str]] = None) -> Optional[dict]:
    """The progress line's words, or None when nothing in scope is in line. `scope_hosts`
    limits it to one account's hosts; the line itself is always the one shared line."""
    from .fmt_utils import counted
    from .tz_utils import format_local
    st = status()
    if not st['running']:
        return None
    scope = set(scope_hosts) if scope_hosts is not None else None
    mine = [h for h in st['line'] if scope is None or h in scope]
    if scope is not None and not mine and st['current'] not in scope:
        return None
    others = len(st['line']) - len(mine) if scope is not None else 0
    if st['current']:
        now = f'Now checking {st["current"]}.'
    elif st['next_at'] is not None:
        now = f'Next: {st["line"][0]} at {format_local(st["next_at"], "clock_sec")}.'
    else:
        now = f'Next: {st["line"][0]}.'
    minutes = round(st['left_seconds'] / 60)
    left = 'under a minute' if minutes < 1 else f'about {counted(minutes, "minute")}'
    return {
        'title': f'Checking hosts one at a time, about {st["spacing"]:.0f} seconds apart',
        'body': (f'{st["done"]} of {st["total"]} done. {now} {left[:1].upper() + left[1:]} left'
                 + (f', including {counted(others, "host")} on other accounts' if others else '')
                 + '.'),
    }


def request_message(result: dict, what: str) -> str:
    """The toast for a request(): what went in line and what was left out, and why."""
    from .fmt_utils import counted
    from .tz_utils import format_local
    bits = []
    added, recent, already = result['added'], result['recent'], result['already']
    if added:
        st = status()
        bits.append(f'Checking {counted(len(added), "host")}, one about every '
                    f'{st["spacing"]:.0f} seconds.')
    if recent:
        if len(recent) == 1 and not added and not already:
            host, at = recent[0]
            bits.append(f'{host} was checked at {format_local(at, "clock")}. A host is checked at '
                        f'most once every 10 minutes, so it can be checked again at '
                        f'{format_local(at + REPEAT_FLOOR, "clock")}.')
        else:
            bits.append(f'{counted(len(recent), "host")} checked in the last 10 minutes '
                        f'{"was" if len(recent) == 1 else "were"} left out.')
    if already and not added:
        bits.append('Already in line.' if len(already) == 1 else f'{counted(len(already), "host")} already in line.')
    return ' '.join(bits) or f'There are no hosts to check on {what}.'


def reset_for_tests() -> None:
    """Empty the line and forget the pacing - tests/support/app.py::reset_module_globals. A
    worker still running finishes its one check and exits on the empty line."""
    global _current, _next_at, _next_at_utc, _worker, _finished, _spacing_seen
    with _lock:
        _line.clear()
        _triggers.clear()
        _batch.update(total=0, done=0)
        _current = None
        _next_at = 0.0
        _next_at_utc = None
        _worker = None
        _finished = 0
        _spacing_seen = float(DEFAULT_SPACING_SECONDS)
        _wake.set()
