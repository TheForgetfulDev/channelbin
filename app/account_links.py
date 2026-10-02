"""
Account links: the lists an account holds beyond its one URL and one login.

**Hosts** (dev/docs/DESIGN-account-providers.md §4). A reseller hands out several host names
for one account and shuts them down over time; the user lists them on the account page, one
of them is active, and every stream URL on the account whose host is in the list carries the
active one. When a capture fails because the active host **stopped resolving**, the app rolls
to the next listed host that does resolve, rewrites the account's stream URLs once, and says
so. A dead channel, a stall, a 404 or a refused connection rolls nothing: a host that answers
and misbehaves is not a dead host.

**Logins** (design §5, dev/changelog/1169). A reseller sells a second username/password on
the same account, or a login gets refused by the panel while the account is otherwise fine.
The user lists the logins with their seat counts; `app/connection_limits.py` takes a seat on
one of them and the capture is launched with that login's credentials rendered into the
stream URL (`render_login()`, pure, applied on the way to the command and never stored).
When a capture fails because the server **refused the credentials** (401/403), the login is
stamped and skipped for a while when another login has a free seat. The sync keeps using the
account's own credentials: a login list changes what a capture is launched with, not what
the sync talks to.

**Providers** (design §6, dev/changelog/1170). The user's label for accounts that reach one
backend - a curated copy beside the direct one, or the same backend bought from two
resellers. Two accounts on one provider may share a login row, which is how one real
account seen through two services counts its seats once: the pool is the login's, so
`connection_limits` needs nothing new to count it, and a block on either account takes the
shared seats from both (`account_blocks.pool_taken()`). Sharing is only within a provider;
leaving one drops the share. Nothing links by itself.

An account with no host rows, no login rows and no provider behaves byte for byte as it did
before the tables existed.

This module is the ONE writer of the lists and of `Account.provider_id` (CLAUDE.md, participation-switch rule): the rows
are the user's judgment about which names reach the same server and which credentials it
accepts, and nothing adds to, edits or removes from a list but the functions here, called
from the explicit user action. `AccountHost.is_active` / `activated_at` and
`Login.last_refused_at` / `last_refused_detail` are state the app writes - which of the
user's own declared equivalents is dialed right now, and which one the server last refused -
and `last_resolved_at` / `last_resolve_error` are the DNS probe's stamps. All are selection
among what the user listed, never a change to a list itself;
`tests/test_static_invariants.py::AccountHostWriteBypassTests`,
`AccountLoginWriteBypassTests` and `ProviderWriteBypassTests` prove nothing else writes one.

The probe job (`check_hosts()`) is DNS only and never rolls: a probe that rolled would move
an account off a host still serving a running recording over a resolver hiccup.
"""
import logging
import socket
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import update

from . import db
from .db_utils import retry_on_locked

log = logging.getLogger(__name__)

#: The stderr lines that mean "the host name did not resolve", lower-cased. The first is
#: ffmpeg 7.1's own prefix on this toolchain, verified against a host that does not exist
#: (`Failed to resolve hostname X: Name or service not known` from a private TLD, `... : No
#: address associated with hostname` from a real resolver's NXDOMAIN), dev/changelog/1168.
#: The rest are the C resolver's spellings and curl's, in case a build words it differently.
#: A refused connection (`Connection to tcp://... failed: Connection refused`) and an HTTP
#: error (`HTTP error 404 File not found`) match none of them, by design.
RESOLUTION_FAILURE_MARKERS = (
    'failed to resolve hostname',
    'name or service not known',
    'no address associated with hostname',
    'could not resolve host',
    'temporary failure in name resolution',
)

#: The stderr lines that mean "the server refused these credentials", lower-cased. Verified
#: against this toolchain's ffmpeg 7.1 and a loopback HTTP server (dev/changelog/1169): a 401
#: prints `Server returned 401 Unauthorized (authorization failed)` and no `HTTP error` line
#: at all; a 403 prints `HTTP error 403 Forbidden` and `Server returned 403 Forbidden (access
#: denied)`. A 404 (`Server returned 404 Not Found`) and a 5xx (`Server returned 5XX Server
#: Error reply`) match none of them, by design: those are the channel's or the provider's
#: problem, not the login's.
CREDENTIAL_REFUSAL_MARKERS = (
    'server returned 401',
    'server returned 403',
    'http error 401',
    'http error 403',
    'authorization failed',
    'access denied',
)

#: How long one DNS lookup may take in the probe and the roll. getaddrinfo has no timeout of
#: its own, so the lookup runs on a helper thread and is abandoned past this.
RESOLVE_TIMEOUT_SECONDS = 5.0

_ROLL_CHUNK = 500

# account_id -> monotonic time of the last roll ATTEMPT, so a stall storm on an account whose
# hosts all fail to resolve cannot re-run the lookups on every retry. The standing
# ACCOUNT_HOST_ROLLED alert's timestamp is the other half of the cooldown (see roll_host):
# it survives a restart, this does not, and a restart's worth of forgetting is harmless.
_last_roll_attempt: dict = {}
_attempt_lock = threading.Lock()


# ── Classification and rendering (pure) ─────────────────────────────────────────────────

def is_resolution_failure(stderr_tail: Optional[str]) -> bool:
    """True when a capture's stderr tail says the host name could not be resolved."""
    if not stderr_tail:
        return False
    text = stderr_tail.lower()
    return any(marker in text for marker in RESOLUTION_FAILURE_MARKERS)


def is_credential_refusal(stderr_tail: Optional[str]) -> bool:
    """True when a capture's stderr tail says the server refused the credentials."""
    if not stderr_tail:
        return False
    text = stderr_tail.lower()
    return any(marker in text for marker in CREDENTIAL_REFUSAL_MARKERS)


def render_login(url: str, logins, login) -> str:
    """`url` with its user/password replaced by `login`'s when its own pair is one of the
    account's listed `logins`.

    Pure and byte-exact. `logins` is an iterable of objects with `username`/`password` (the
    rows, or any pairs); `login` is one of them. A URL with no user/pass/id triplet (a
    radio mount, a third-party feed), one whose pair the list does not know (a curated
    account whose streams carry the backend's credentials while its list is empty), an
    empty list or no login hands the URL back unchanged - the seat still counts, the
    launch is as-is. Only the two path segments move; scheme, host, the rest of the path
    and the query are kept as they are, so a one-login account whose login equals the
    credentials in its URLs renders exactly today's URL.
    """
    if not url or not logins or login is None:
        return url
    from urllib.parse import urlsplit
    from .accounts import _STREAM_URL_RE
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    m = _STREAM_URL_RE.match(parts.path)
    if m is None:
        return url
    pair = (m.group('user'), m.group('password'))
    if pair == (login.username, login.password):
        return url
    if not any((lg.username, lg.password) == pair for lg in logins):
        return url
    head = len(parts.scheme) + 3 + len(parts.netloc)
    path = parts.path
    if url[head:head + len(path)] != path:
        return url
    spliced = (path[:m.start('user')] + login.username + '/' + login.password
               + path[m.end('password'):])
    return url[:head] + spliced + url[head + len(path):]


def host_of(url: Optional[str]) -> Optional[str]:
    """The `name[:port]` a URL is on, lower-cased, without any userinfo; None for a string
    that is not a URL with a host."""
    if not url or '://' not in url:
        return None
    rest = url.split('://', 1)[1]
    netloc = rest.split('/', 1)[0].split('?', 1)[0].split('#', 1)[0]
    netloc = netloc.rsplit('@', 1)[-1]
    return netloc.lower() or None


def normalize_host(raw: str) -> str:
    """The stored form of a host the user typed: lower-cased `name[:port]`, with a scheme
    or a path stripped if they pasted a whole URL. Raises ValueError for anything that is
    not a host name."""
    text = (raw or '').strip()
    if '://' in text:
        text = text.split('://', 1)[1]
    text = text.split('/', 1)[0].split('?', 1)[0].rsplit('@', 1)[-1].strip().lower()
    if not text:
        raise ValueError('Enter a host name, like a2.skyline.example or a2.skyline.example:8080.')
    name, _, port = text.rpartition(':')
    if not name:
        name, port = text, ''
    if port and not port.isdigit():
        raise ValueError(f'"{raw.strip()}" is not a host name. The part after the colon must be a port number.')
    if any(ch.isspace() for ch in name) or '@' in name or not all(
            c.isalnum() or c in '.-_[]' for c in name):
        raise ValueError(f'"{raw.strip()}" is not a host name.')
    return text


def render_host(url: str, hosts, active: Optional[str]) -> str:
    """`url` with its host replaced by `active` when its own host is one of `hosts`.

    Pure and byte-exact: a URL on a host outside the list (a radio mount, a third-party
    feed, a CDN the provider put one category on), an empty list or no active host hands
    the URL back unchanged, so an account with nothing listed renders exactly today's URL.
    Only the netloc moves - scheme, userinfo, path and query are kept as they are.
    """
    if not url or not hosts or not active:
        return url
    current = host_of(url)
    if current is None or current == active or current not in hosts:
        return url
    scheme, rest = url.split('://', 1)
    netloc_end = len(rest)
    for stop in '/?#':
        idx = rest.find(stop)
        if idx != -1:
            netloc_end = min(netloc_end, idx)
    netloc = rest[:netloc_end]
    userinfo = netloc[:-len(current)]
    return f'{scheme}://{userinfo}{active}{rest[netloc_end:]}'


# ── Reading ─────────────────────────────────────────────────────────────────────────────

def host_list(account_id: int) -> tuple[frozenset, Optional[str]]:
    """(the account's listed hosts, the active one) in one query - what a sync fetches
    once before its row loop. (frozenset(), None) for an account with no list."""
    from .database import AccountHost
    rows = (db.session.query(AccountHost.host, AccountHost.is_active)
            .filter(AccountHost.account_id == account_id).all())
    if not rows:
        return frozenset(), None
    active = next((h for h, is_active in rows if is_active), None)
    return frozenset(h for h, _ in rows), active


def hosts_for_accounts(account_ids: Iterable[int]) -> dict:
    """{account_id: [AccountHost, ...] in position order} for the account pages, batched."""
    from .database import AccountHost
    ids = {a for a in account_ids if a is not None}
    out: dict = {a: [] for a in ids}
    if not ids:
        return out
    rows = (AccountHost.query.filter(AccountHost.account_id.in_(ids))
            .order_by(AccountHost.account_id, AccountHost.position, AccountHost.id).all())
    for row in rows:
        out[row.account_id].append(row)
    return out


def current_host(account_id: int) -> Optional[str]:
    """The host most of the account's stream URLs carry right now - what the first entry
    in a new list is seeded from, so the list always starts from what is already true.
    None for an account with no channels. One columns-only query; the count is in Python
    because SQLite cannot pull a netloc out of a URL."""
    from .database import Channel
    counts: Counter = Counter()
    for (url,) in db.session.query(Channel.stream_url).filter(
            Channel.account_id == account_id).yield_per(2000):
        host = host_of(url)
        if host:
            counts[host] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


# ── The writer: the list is the user's ──────────────────────────────────────────────────

def add_host(account_id: int, raw_host: str):
    """Append a host to the account's list. The first add seeds the list with the host the
    account's stream URLs carry today as the active one (design §3.2), so the list always
    contains what is already true; the host just added becomes active only when it IS that
    host, or when the account has no channels yet to read one from. Returns the new row.
    Raises ValueError for a bad name or one already listed."""
    from .database import AccountHost
    host = normalize_host(raw_host)

    @retry_on_locked()
    def _add_and_commit():
        rows = (AccountHost.query.filter_by(account_id=account_id)
                .order_by(AccountHost.position, AccountHost.id).all())
        if any(r.host == host for r in rows):
            raise ValueError(f'{host} is already on the list.')
        now = datetime.utcnow()
        position = (max((r.position for r in rows), default=-1)) + 1
        if not rows:
            # The seeded host carries no activated_at: it has been the host all along,
            # and "active since <when the list was made>" would say otherwise.
            seed = current_host(account_id)
            if seed and seed != host:
                db.session.add(AccountHost(account_id=account_id, host=seed, position=position,
                                           is_active=True, created_at=now))
                position += 1
                row = AccountHost(account_id=account_id, host=host, position=position,
                                  created_at=now)
            else:
                row = AccountHost(account_id=account_id, host=host, position=position,
                                  is_active=True, created_at=now)
        else:
            row = AccountHost(account_id=account_id, host=host, position=position, created_at=now)
        db.session.add(row)
        db.session.commit()
        return row

    row = _add_and_commit()
    _clear_rolled_alert(account_id)
    log.info('Account %d: host %s added to its list', account_id, host)
    return row


def remove_host(account_id: int, host_id: int) -> str:
    """Take a host off the list. The active host can go only when it is the last one (the
    list then empties, which is today's behavior); otherwise the user makes another host
    active first, so the list can never be non-empty with nothing active. Returns the host
    name. Raises LookupError for an unknown row and ValueError for a refused removal."""
    from .database import AccountHost

    @retry_on_locked()
    def _remove_and_commit():
        row = db.session.get(AccountHost, host_id)
        if row is None or row.account_id != account_id:
            raise LookupError('Host not found.')
        others = AccountHost.query.filter(AccountHost.account_id == account_id,
                                          AccountHost.id != host_id).count()
        if row.is_active and others:
            raise ValueError(f'{row.host} is the active host. Make another host active first.')
        name = row.host
        db.session.delete(row)
        db.session.commit()
        return name

    name = _remove_and_commit()
    _clear_rolled_alert(account_id)
    log.info('Account %d: host %s removed from its list', account_id, name)
    return name


def set_active_host(account_id: int, host_id: int) -> dict:
    """The user's own switch: make `host_id` the active host and rewrite the account's URLs
    onto it. No cooldown and no alert - the person asked for it - and the standing roll
    alert, if any, is cleared because the list was just edited. Returns the roll summary.
    Raises LookupError for an unknown row."""
    from .database import AccountHost
    row = db.session.get(AccountHost, host_id)
    if row is None or row.account_id != account_id:
        raise LookupError('Host not found.')
    summary = _activate(account_id, row.host)
    _clear_rolled_alert(account_id)
    log.info('Account %d: host %s made active by hand (%d stream URLs rewritten)',
             account_id, row.host, summary['channels'])
    return summary


def delete_hosts_for_account(account_id: int) -> int:
    """Drop the account's host rows inside the caller's delete unit (no commit here): the
    account is going, and a row left behind would list hosts for a future account that
    reused the id. Returns the count removed."""
    from .database import AccountHost
    return AccountHost.query.filter_by(account_id=account_id).delete(synchronize_session=False)


# ── The roll ────────────────────────────────────────────────────────────────────────────

def roll_host_for_channel(channel_id: int, trigger: str,
                          recording_id: Optional[int] = None) -> Optional[dict]:
    """`roll_host()` for the account `channel_id` belongs to. Never raises: it is called
    from the capture path's failure handling, and a diagnostic must not harm the capture
    it is diagnosing (CLAUDE.md)."""
    try:
        from .database import Channel
        account_id = db.session.query(Channel.account_id).filter(
            Channel.id == channel_id).scalar()
        if account_id is None:
            return None
        return roll_host(account_id, trigger, recording_id=recording_id)
    except Exception:
        log.exception('Host roll for channel %d failed', channel_id)
        return None


def roll_host(account_id: int, trigger: str, recording_id: Optional[int] = None,
              cfg: Optional[dict] = None) -> Optional[dict]:
    """Move the account onto the next listed host that resolves, because the active one
    stopped resolving (design §4.4). Returns the summary dict when it rolled, None when it
    did not - nothing listed to roll to, inside the cooldown, or no listed host resolves.

    Order: the hosts after the active one in list order, wrapping around, the active one
    last - a resolver hiccup on the active host still gets it re-checked, after the others.
    Never removes a host: the dead one stays, marked with its resolve error, for the user.
    """
    from .database import Account, AccountHost
    from .config import load_config
    cfg = cfg or load_config()
    cooldown = timedelta(minutes=float((cfg.get('hosts') or {}).get('roll_cooldown_minutes', 10)))

    rows = (AccountHost.query.filter_by(account_id=account_id)
            .order_by(AccountHost.position, AccountHost.id).all())
    if len(rows) < 2:
        return None
    active = next((r for r in rows if r.is_active), None)
    if active is None:
        return None
    # Two halves of the cooldown. The standing ACCOUNT_HOST_ROLLED row is the persisted
    # record of the last roll (or failed attempt): its timestamp survives a restart, and it
    # is refreshed on every attempt and dismissed by the two things that end a roll's story
    # - a sync succeeding on the new host, or the user editing the list - so either of those
    # lets the next failure roll at once. The in-memory stamp below covers the attempts that
    # raise no alert at all (the active host turned out to resolve).
    from .alerts import ACCOUNT_HOST_ROLLED
    from .database import Alert
    now = datetime.utcnow()
    standing = (Alert.query.filter(Alert.alert_type == ACCOUNT_HOST_ROLLED,
                                   Alert.source == _alert_source(account_id),
                                   Alert.dismissed_at.is_(None),
                                   Alert.created_at > now - cooldown)
                .order_by(Alert.created_at.desc()).first())
    if standing is not None:
        log.info('Account %d: %s - a roll was recorded %s ago, inside the %s cooldown; not '
                 'rolling', account_id, trigger, now - standing.created_at, cooldown)
        return None
    with _attempt_lock:
        last = _last_roll_attempt.get(account_id)
        mono = time.monotonic()
        if last is not None and mono - last < cooldown.total_seconds():
            log.info('Account %d: %s - a roll was attempted %.0fs ago, inside the cooldown; '
                     'not rolling', account_id, trigger, mono - last)
            return None
        _last_roll_attempt[account_id] = mono

    idx = rows.index(active)
    order = rows[idx + 1:] + rows[:idx] + [active]
    account = db.session.get(Account, account_id)
    name = account.name if account else f'account {account_id}'
    chosen = None
    verdicts = {}
    for row in order:
        ok, error = resolve_host(row.host)
        verdicts[row.id] = (ok, error)
        if ok:
            chosen = row
            break
    if chosen is not None and chosen is not active and active.id not in verdicts:
        # Moving off the active host without having looked it up ourselves: ffmpeg's
        # verdict is the one on record, so the row says where it came from.
        verdicts[active.id] = (False, f'did not resolve (reported by {trigger})')
    _stamp_resolve_verdicts(verdicts)
    if chosen is None:
        log.error('Account %d (%s): %s - no listed host resolves (%s)', account_id, name,
                  trigger, ', '.join(f'{r.host}: {verdicts[r.id][1]}' for r in order))
        _raise_rolled_alert(
            account_id, name,
            title=f'{name}: every host on the list failed to resolve',
            body=(f'{trigger} failed because {active.host} did not resolve, and none of the '
                  f'other hosts on {name}\'s list resolves either: '
                  + '; '.join(f'{r.host} ({verdicts[r.id][1]})' for r in order if r is not active)
                  + f'. {active.host} is still the active host. Check the list on the '
                  f'account page.'))
        return None
    if chosen is active:
        # The active host resolves after all - a resolver blip, or the failure was about a
        # URL on another host. Nothing to roll onto; the stamp above records it resolved.
        log.info('Account %d (%s): %s - active host %s resolves; not rolling',
                 account_id, name, trigger, active.host)
        return None

    summary = _activate(account_id, chosen.host)
    old = active.host
    summary.update({'old_host': old, 'trigger': trigger})
    log.warning('Account %d (%s): %s - %s stopped resolving; now using %s (%d stream URLs '
                'rewritten)', account_id, name, trigger, old, chosen.host, summary['channels'])
    _raise_rolled_alert(
        account_id, name,
        title=f'{name}: {old} stopped resolving; now using {chosen.host}',
        body=(f'{trigger} failed because {old} did not resolve. {chosen.host} resolves, so '
              f'every stream URL on {name} that was on {old} now uses {chosen.host} '
              f'({summary["channels"]:,} channels'
              + (', and the account\'s own URL' if summary['account_urls'] else '')
              + f'). {old} is still on the list, marked as failing, for you to remove or '
              f'keep. A running recording picks the new host up at its next segment.'))
    if recording_id is not None:
        _log_recording_roll(recording_id, old, chosen.host, summary['channels'])
    return summary


def _activate(account_id: int, host: str) -> dict:
    """Flip `is_active` to `host` and rewrite every stream URL on the account whose host is
    listed, plus the account's own URLs when their host is listed, in one retry unit. Shared
    by the roll and the user's Make active. Returns {'host', 'channels', 'account_urls'}."""
    from .database import Account, AccountHost, Channel

    @retry_on_locked()
    def _activate_and_commit():
        rows = AccountHost.query.filter_by(account_id=account_id).all()
        hosts = frozenset(r.host for r in rows)
        now = datetime.utcnow()
        for r in rows:
            if r.host == host:
                if not r.is_active:
                    r.is_active = True
                    r.activated_at = now
            elif r.is_active:
                r.is_active = False
        changed = []
        for cid, url in db.session.query(Channel.id, Channel.stream_url).filter(
                Channel.account_id == account_id).yield_per(2000):
            rendered = render_host(url, hosts, host)
            if rendered != url:
                changed.append({'b_id': cid, 'b_url': rendered})
        # A Core executemany on the table, on the session's own connection so it rides in
        # this unit's transaction - the ORM's bulk-update path wants primary keys spelled
        # its way and would synchronize rows nothing here reads back. stream_url is a
        # search text column, so the index watermark moves with it; updated_at is
        # self-assigned to keep it still, because the provider changed nothing
        # (dev/changelog/673).
        table = Channel.__table__
        stmt = (update(table)
                .where(table.c.id == db.bindparam('b_id'))
                .values(stream_url=db.bindparam('b_url'),
                        search_text_updated_at=now, updated_at=table.c.updated_at))
        conn = db.session.connection()
        for i in range(0, len(changed), _ROLL_CHUNK):
            conn.execute(stmt, changed[i:i + _ROLL_CHUNK])
        account_urls = 0
        account = db.session.get(Account, account_id)
        if account is not None:
            for col in ('base_url', 'm3u_url', 'epg_url'):
                value = getattr(account, col)
                rendered = render_host(value, hosts, host) if value else value
                if rendered != value:
                    setattr(account, col, rendered)
                    account_urls += 1
        db.session.commit()
        return {'host': host, 'channels': len(changed), 'account_urls': account_urls}

    return _activate_and_commit()


def _log_recording_roll(recording_id: int, old: str, new: str, channels: int) -> None:
    from .database import RECORDING_HOST_ROLLED, add_recording_event

    @retry_on_locked()
    def _log_and_commit():
        add_recording_event(
            recording_id, RECORDING_HOST_ROLLED,
            detail=(f'The stream host {old} stopped resolving; the account now uses {new} '
                    f'({channels:,} channels rewritten). The next segment records from the '
                    f'new URL.'),
            extra={'old_host': old, 'new_host': new, 'channels': channels})
        db.session.commit()

    _log_and_commit()


# ── The probe: DNS only, never rolls ────────────────────────────────────────────────────

def resolve_host(host: str, timeout: float = RESOLVE_TIMEOUT_SECONDS) -> tuple[bool, Optional[str]]:
    """(resolved, error text). A DNS lookup and nothing else - no connection is opened, so
    this costs the provider nothing and cannot take a seat. Run on a helper thread because
    getaddrinfo has no timeout; past `timeout` the lookup is abandoned and reported as such."""
    name, _, port = host.rpartition(':')
    if not name or not port.isdigit():
        name = host
    result: dict = {}

    def _lookup():
        try:
            socket.getaddrinfo(name, None)
            result['ok'] = True
        except socket.gaierror as exc:
            result['error'] = exc.strerror or str(exc)
        except OSError as exc:
            result['error'] = str(exc)

    worker = threading.Thread(target=_lookup, name=f'resolve-{name}', daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return False, f'no answer from the resolver within {timeout:.0f}s'
    if result.get('ok'):
        return True, None
    return False, result.get('error') or 'did not resolve'


def _stamp_resolve_verdicts(verdicts: dict) -> None:
    """Write {host_id: (ok, error)} onto the rows. Resolved: last_resolved_at moves and the
    error clears; failed: the error is recorded and the last good time is kept, so the page
    can say "failed since"."""
    from .database import AccountHost
    if not verdicts:
        return

    @retry_on_locked()
    def _stamp_and_commit():
        now = datetime.utcnow()
        for host_id, (ok, error) in verdicts.items():
            row = db.session.get(AccountHost, host_id)
            if row is None:
                continue
            if ok:
                row.last_resolved_at = now
                row.last_resolve_error = None
            else:
                row.last_resolve_error = (error or 'did not resolve')[:255]
        db.session.commit()

    _stamp_and_commit()


def check_hosts(account_ids: Optional[Iterable[int]] = None) -> dict:
    """The `host_resolve_check` job (design §4.5): resolve every listed host and stamp the
    verdict. DNS only, and it never rolls - its one job is the account page's list, so a
    list going bad is visible before a recording finds out. Returns
    {'checked', 'failed', 'accounts'}."""
    from .database import AccountHost
    q = AccountHost.query
    if account_ids is not None:
        ids = {a for a in account_ids if a is not None}
        if not ids:
            return {'checked': 0, 'failed': 0, 'accounts': 0}
        q = q.filter(AccountHost.account_id.in_(ids))
    rows = q.order_by(AccountHost.account_id, AccountHost.position).all()
    verdicts = {row.id: resolve_host(row.host) for row in rows}
    _stamp_resolve_verdicts(verdicts)
    failed = sum(1 for ok, _ in verdicts.values() if not ok)
    accounts = len({row.account_id for row in rows})
    if failed:
        log.warning('Host check: %d of %d listed hosts on %d accounts did not resolve',
                    failed, len(rows), accounts)
    else:
        log.info('Host check: %d listed hosts on %d accounts resolve', len(rows), accounts)
    return {'checked': len(rows), 'failed': failed, 'accounts': accounts}


# ── The alert ───────────────────────────────────────────────────────────────────────────

def _alert_source(account_id: int) -> str:
    return f'account:{account_id}:host-rolled'


def _raise_rolled_alert(account_id: int, name: str, title: str, body: str) -> None:
    from .accounts import _raise_or_resolve_standing_alert
    from .alerts import ACCOUNT_HOST_ROLLED
    _raise_or_resolve_standing_alert(ACCOUNT_HOST_ROLLED, source=_alert_source(account_id),
                                     active=True, title=title, body=body)


def _clear_rolled_alert(account_id: int) -> None:
    """The user touched the list, or the account synced on its new host: the roll it was
    telling them about has been seen to. The self-clearing half of ACCOUNT_HOST_ROLLED."""
    from .alerts import ACCOUNT_HOST_ROLLED, dismiss_open_alerts
    dismiss_open_alerts(ACCOUNT_HOST_ROLLED, _alert_source(account_id))


def clear_rolled_alert_after_sync(account_id: int) -> None:
    """Called from the sync's success path: a sync that reached the end on the rolled host
    is the proof the roll worked."""
    _clear_rolled_alert(account_id)


# ── Logins: reading ─────────────────────────────────────────────────────────────────────

def logins_for_account(account_id: int) -> list:
    """The account's Login rows in position order, one query."""
    from .database import AccountLogin, Login
    return (db.session.query(Login).join(AccountLogin, AccountLogin.login_id == Login.id)
            .filter(AccountLogin.account_id == account_id)
            .order_by(AccountLogin.position, Login.id).all())


def logins_for_accounts(account_ids: Iterable[int]) -> dict:
    """{account_id: [Login, ...] in position order} for the account pages, batched."""
    from .database import AccountLogin, Login
    ids = {a for a in account_ids if a is not None}
    out: dict = {a: [] for a in ids}
    if not ids:
        return out
    rows = (db.session.query(AccountLogin.account_id, Login)
            .join(Login, Login.id == AccountLogin.login_id)
            .filter(AccountLogin.account_id.in_(ids))
            .order_by(AccountLogin.account_id, AccountLogin.position, Login.id).all())
    for account_id, login in rows:
        out[account_id].append(login)
    return out


def login_seats_for_accounts(account_ids: Iterable[int]) -> dict:
    """{account_id: [(login_id, max_connections, last_refused_at), ...] in position order}
    - what the connection counter needs to size and walk an account's pools, columns only.
    An account with no logins maps to [] (one pool, sized by the account, as before)."""
    from .database import AccountLogin, Login
    ids = {a for a in account_ids if a is not None}
    out: dict = {a: [] for a in ids}
    if not ids:
        return out
    rows = (db.session.query(AccountLogin.account_id, Login.id, Login.max_connections,
                             Login.last_refused_at)
            .join(Login, Login.id == AccountLogin.login_id)
            .filter(AccountLogin.account_id.in_(ids))
            .order_by(AccountLogin.account_id, AccountLogin.position, Login.id).all())
    for account_id, login_id, seats, refused_at in rows:
        out[account_id].append((login_id, seats, refused_at))
    return out


def refusal_cooldown(cfg: Optional[dict] = None) -> timedelta:
    """How long a refused login is skipped while another has a free seat
    (`logins.refusal_cooldown_minutes`)."""
    if cfg is None:
        from .config import load_config
        cfg = load_config()
    return timedelta(minutes=float((cfg.get('logins') or {}).get('refusal_cooldown_minutes', 30)))


def is_refused_now(last_refused_at: Optional[datetime], now: datetime, cooldown: timedelta) -> bool:
    return last_refused_at is not None and now - last_refused_at < cooldown


def render_held_login(url: str, account_id: int, holder_kind: str, holder_id) -> tuple[str, Optional[int]]:
    """(the capture URL for `holder`, the login id it is seated on). The one call a launch
    site makes: asks the connection counter which login the holder's seat is on, and renders
    that login's credentials into `url` when it is one. (url, None) - no query at all - for
    a holder on an account with no logins, so the no-list account launches exactly today's
    URL."""
    from . import connection_limits as connlim
    login_id = connlim.held_login_id(holder_kind, holder_id)
    if login_id is None:
        return url, None
    logins = logins_for_account(account_id)
    login = next((lg for lg in logins if lg.id == login_id), None)
    if login is None:
        return url, login_id
    return render_login(url, logins, login), login_id


# ── Logins: the writer ──────────────────────────────────────────────────────────────────

def _validate_login_fields(name: str, username: str, password: Optional[str],
                           max_connections, *, password_required: bool) -> tuple[str, str, Optional[str], int]:
    name = (name or '').strip()
    username = (username or '').strip()
    password = password if password is None else password.strip()
    if not name:
        raise ValueError('Give the login a name, like "main" or "spare".')
    if not username:
        raise ValueError('Enter the username.')
    if password_required and not password:
        raise ValueError('Enter the password.')
    if '/' in username or (password and '/' in password):
        raise ValueError('A username or password cannot contain "/": it is rendered into '
                         'the stream URL as a path segment.')
    try:
        seats = int(max_connections)
    except (TypeError, ValueError):
        raise ValueError('Seats must be a whole number of 1 or more.')
    if seats < 1:
        raise ValueError('Seats must be 1 or more.')
    return name, username, password, seats


def add_login(account_id: int, name: str, username: str, password: str, max_connections):
    """Append a login to the account's list. Returns the new row. Raises ValueError for a
    missing field, a bad seat count, or a username already listed on this account."""
    from .database import AccountLogin, Login
    name, username, password, seats = _validate_login_fields(
        name, username, password, max_connections, password_required=True)

    @retry_on_locked()
    def _add_and_commit():
        existing = logins_for_account(account_id)
        if any(lg.username == username for lg in existing):
            raise ValueError(f'A login with the username {username} is already on the list.')
        if any(lg.name.lower() == name.lower() for lg in existing):
            raise ValueError(f'A login named {name} is already on the list.')
        positions = [p for (p,) in db.session.query(AccountLogin.position)
                     .filter(AccountLogin.account_id == account_id).all()]
        now = datetime.utcnow()
        row = Login(name=name, username=username, password=password, max_connections=seats,
                    created_at=now, updated_at=now)
        db.session.add(row)
        db.session.flush()
        db.session.add(AccountLogin(account_id=account_id, login_id=row.id,
                                    position=max(positions, default=-1) + 1))
        db.session.commit()
        return row

    row = _add_and_commit()
    log.info('Account %d: login "%s" (%s, %d seats) added to its list', account_id, row.name,
             row.username, row.max_connections)
    return row


def update_login(account_id: int, login_id: int, name: str, username: str,
                 password: Optional[str], max_connections):
    """Edit a login the account holds. A blank password keeps the stored one (the form
    never shows it back, so blank means "unchanged", as on the account form). A changed
    username or password clears the refusal stamp: the server has not seen the new
    credentials yet. A holder already seated on the login keeps its seat and launches its
    next segment with the new credentials. Raises LookupError / ValueError."""
    from .database import AccountLogin, Login
    name, username, password, seats = _validate_login_fields(
        name, username, password, max_connections, password_required=False)

    @retry_on_locked()
    def _update_and_commit():
        held = db.session.query(AccountLogin).filter_by(account_id=account_id,
                                                        login_id=login_id).first()
        row = db.session.get(Login, login_id) if held is not None else None
        if row is None:
            raise LookupError('Login not found.')
        others = [lg for lg in logins_for_account(account_id) if lg.id != login_id]
        if any(lg.username == username for lg in others):
            raise ValueError(f'A login with the username {username} is already on the list.')
        if any(lg.name.lower() == name.lower() for lg in others):
            raise ValueError(f'A login named {name} is already on the list.')
        credentials_changed = (row.username != username
                               or (password is not None and password != '' and row.password != password))
        row.name = name
        row.username = username
        if password:
            row.password = password
        row.max_connections = seats
        if credentials_changed:
            row.last_refused_at = None
            row.last_refused_detail = None
        row.updated_at = datetime.utcnow()
        db.session.commit()
        return row

    row = _update_and_commit()
    log.info('Account %d: login "%s" (%s, %d seats) edited', account_id, row.name, row.username,
             row.max_connections)
    return row


def remove_login(account_id: int, login_id: int) -> str:
    """Take a login off the account's list, and delete the row when no other account holds
    it. A holder seated on it keeps its seat until it releases (the counter remembers the
    pool it was counted in) and launches its next segment with the URL's own credentials.
    Returns the login's name. Raises LookupError for a row the account does not hold."""
    from .database import AccountLogin, Login

    @retry_on_locked()
    def _remove_and_commit():
        held = db.session.query(AccountLogin).filter_by(account_id=account_id,
                                                        login_id=login_id).first()
        row = db.session.get(Login, login_id) if held is not None else None
        if row is None:
            raise LookupError('Login not found.')
        name = row.name
        db.session.delete(held)
        still_held = db.session.query(AccountLogin).filter(
            AccountLogin.login_id == login_id, AccountLogin.account_id != account_id).count()
        if not still_held:
            db.session.delete(row)
        db.session.commit()
        return name

    name = _remove_and_commit()
    log.info('Account %d: login "%s" removed from its list', account_id, name)
    return name


def move_login(account_id: int, login_id: int, direction: str) -> list:
    """Move a login one step `up` or `down` the list - the order seats are taken in.
    Returns the new order of login ids. Raises LookupError / ValueError."""
    from .database import AccountLogin
    if direction not in ('up', 'down'):
        raise ValueError('Direction must be "up" or "down".')

    @retry_on_locked()
    def _move_and_commit():
        rows = (AccountLogin.query.filter_by(account_id=account_id)
                .order_by(AccountLogin.position, AccountLogin.login_id).all())
        idx = next((i for i, r in enumerate(rows) if r.login_id == login_id), None)
        if idx is None:
            raise LookupError('Login not found.')
        other = idx - 1 if direction == 'up' else idx + 1
        if 0 <= other < len(rows):
            rows[idx], rows[other] = rows[other], rows[idx]
        for position, r in enumerate(rows):
            if r.position != position:
                r.position = position
        db.session.commit()
        return [r.login_id for r in rows]

    return _move_and_commit()


def delete_logins_for_account(account_id: int) -> int:
    """Drop the account's login rows inside the caller's delete unit (no commit here): the
    join rows, and every login no other account holds. Returns the count of logins
    deleted."""
    from .database import AccountLogin, Login
    held = [lid for (lid,) in db.session.query(AccountLogin.login_id)
            .filter(AccountLogin.account_id == account_id).all()]
    AccountLogin.query.filter_by(account_id=account_id).delete(synchronize_session=False)
    if not held:
        return 0
    still_held = {lid for (lid,) in db.session.query(AccountLogin.login_id)
                  .filter(AccountLogin.login_id.in_(held)).all()}
    orphans = [lid for lid in held if lid not in still_held]
    if not orphans:
        return 0
    return Login.query.filter(Login.id.in_(orphans)).delete(synchronize_session=False)


# ── Logins: a refusal ───────────────────────────────────────────────────────────────────

def note_refusal_for_holder(holder_kind: str, holder_id, trigger: str, stderr_tail: str,
                            recording_id: Optional[int] = None,
                            login_id: Optional[int] = None) -> Optional[int]:
    """The capture-path entry: `holder` failed and its stderr says the credentials were
    refused. Stamps the login the holder's seat is on (`login_id` when the caller already
    knows it - a preview whose seat is gone by the time its stderr is read) and raises the
    alert. Never raises, and does nothing for a holder on an account with no logins: a
    diagnostic must not harm the capture it is diagnosing (CLAUDE.md). Returns the login id
    stamped, or None."""
    try:
        if login_id is None:
            from . import connection_limits as connlim
            login_id = connlim.held_login_id(holder_kind, holder_id)
        if login_id is None:
            return None
        note_refusal(login_id, trigger, stderr_tail, recording_id=recording_id)
        return login_id
    except Exception:
        log.exception('Recording a credential refusal for %s %s failed', holder_kind, holder_id)
        return None


def note_refusal(login_id: int, trigger: str, stderr_tail: str,
                 recording_id: Optional[int] = None, cfg: Optional[dict] = None) -> bool:
    """Stamp `last_refused_at` / `last_refused_detail` on the login (design §5.4), so the
    connection counter skips it for the cooldown while another login has a seat. Raises
    ACCOUNT_LOGIN_REFUSED at most once per cooldown window (a login refused on every
    segment is one fact, not one alert per segment) and logs the event on the recording.
    Returns True when a fresh alert was raised."""
    from .database import Login
    from .url_utils import mask_creds_in_text
    cooldown = refusal_cooldown(cfg)
    detail = _refusal_line(stderr_tail)

    @retry_on_locked()
    def _stamp_and_commit():
        row = db.session.get(Login, login_id)
        if row is None:
            return None, False
        now = datetime.utcnow()
        fresh = not is_refused_now(row.last_refused_at, now, cooldown)
        row.last_refused_at = now
        row.last_refused_detail = mask_creds_in_text(detail)[:255]
        db.session.commit()
        return row, fresh

    row, fresh = _stamp_and_commit()
    if row is None:
        return False
    log.warning('Login "%s" (%s): %s - the server refused the credentials (%s)', row.name,
                row.username, trigger, row.last_refused_detail)
    if recording_id is not None:
        _log_recording_refusal(recording_id, row, trigger)
    if not fresh:
        return False
    from .alerts import ACCOUNT_LOGIN_REFUSED, create_alert
    create_alert(
        ACCOUNT_LOGIN_REFUSED,
        title=f'Login "{row.name}" was refused by the server',
        body=(f'{trigger} failed because the server refused the credentials of login '
              f'"{row.name}" ({row.username}): {row.last_refused_detail}. For the next '
              f'{int(cooldown.total_seconds() // 60)} minutes a capture on the account takes '
              f'a seat on another login when one is free; with no other login it tries this '
              f'one again, since a refusal is sometimes a panel hiccup. The login stays on '
              f'the list for you to check or edit.'),
        source=_refused_alert_source(login_id), recording_id=recording_id)
    return True


def _refusal_line(stderr_tail: str) -> str:
    """The one stderr line that carried the refusal, for the stamp and the alert."""
    for line in (stderr_tail or '').splitlines():
        if is_credential_refusal(line):
            return line.strip()
    return (stderr_tail or '').strip().splitlines()[-1] if (stderr_tail or '').strip() else 'refused'


def _refused_alert_source(login_id: int) -> str:
    return f'login:{login_id}:refused'


def _log_recording_refusal(recording_id: int, login, trigger: str) -> None:
    from .database import RECORDING_LOGIN_REFUSED, add_recording_event

    @retry_on_locked()
    def _log_and_commit():
        add_recording_event(
            recording_id, RECORDING_LOGIN_REFUSED,
            detail=(f'The server refused the credentials of login "{login.name}" '
                    f'({login.last_refused_detail}). The next segment takes a seat on another '
                    f'login on the account if one is free.'),
            extra={'login_id': login.id, 'login_name': login.name, 'trigger': trigger})
        db.session.commit()

    _log_and_commit()


# ── Providers (design §6, dev/changelog/1170) ───────────────────────────────────────────

def login_holders(login_ids: Iterable[int]) -> dict:
    """{login_id: [account_id, ...] in account id order} for `login_ids`, one query. A login
    with two or more holders is shared (design §3.1 case 3)."""
    from .database import AccountLogin
    ids = {i for i in login_ids if i is not None}
    out: dict = {i: [] for i in ids}
    if not ids:
        return out
    for login_id, account_id in (db.session.query(AccountLogin.login_id, AccountLogin.account_id)
                                 .filter(AccountLogin.login_id.in_(ids))
                                 .order_by(AccountLogin.account_id).all()):
        out[login_id].append(account_id)
    return out


def login_siblings(account_ids: Iterable[int]) -> dict:
    """{account_id: {sibling_account_id: [login_id, ...]}} - the other accounts that hold a
    login each asked account holds. Two queries, and one when none of the asked accounts
    holds a login (no list, so no sibling can exist)."""
    from .database import AccountLogin
    ids = {a for a in account_ids if a is not None}
    out: dict = {a: {} for a in ids}
    if not ids:
        return out
    held = db.session.query(AccountLogin.account_id, AccountLogin.login_id).filter(
        AccountLogin.account_id.in_(ids)).all()
    if not held:
        return out
    holders = login_holders({lid for _, lid in held})
    for account_id, login_id in held:
        for other in holders.get(login_id, []):
            if other != account_id:
                out[account_id].setdefault(other, []).append(login_id)
    return out


def normalize_provider_name(raw: str) -> str:
    name = ' '.join((raw or '').split())
    if not name:
        raise ValueError('Give the provider a name, like the service you bought the accounts from.')
    if len(name) > 100:
        raise ValueError('A provider name can be at most 100 characters.')
    return name


def _check_provider_name(name: str, exclude_id: Optional[int] = None) -> None:
    from .database import Provider
    q = Provider.query.filter(db.func.lower(Provider.name) == name.lower())
    if exclude_id is not None:
        q = q.filter(Provider.id != exclude_id)
    if q.first() is not None:
        raise ValueError(f'A provider named {name} already exists.')


def _drop_shares_on_leaving(account, old_provider_id: Optional[int]) -> list:
    """Inside the caller's unit: the account is leaving `old_provider_id`, so it stops
    holding any login another account on that provider still holds (design §3.2). Logins
    only it holds stay. Returns the names of the logins it let go."""
    from .database import Account, AccountLogin, Login
    if old_provider_id is None:
        return []
    mine = db.session.query(AccountLogin).filter(AccountLogin.account_id == account.id).all()
    if not mine:
        return []
    others = {aid for (aid,) in db.session.query(Account.id).filter(
        Account.provider_id == old_provider_id, Account.id != account.id).all()}
    if not others:
        return []
    holders = login_holders(r.login_id for r in mine)
    dropped = []
    for row in mine:
        if set(holders.get(row.login_id, [])) & others:
            login = db.session.get(Login, row.login_id)
            dropped.append(login.name if login else f'#{row.login_id}')
            db.session.delete(row)
    return dropped


def _refold_duplicates() -> None:
    """Recompute the duplicate fold inside the caller's commit unit. An account joining or
    leaving a provider changes the key of every channel on it, and a human made the change,
    so it shows at once rather than after the next sync (design §8.1). A recompute, so a
    lock retry of the enclosing closure redoing it is harmless."""
    from . import duplicate_streams
    duplicate_streams.recompute()


def create_provider(name: str, account_ids: Iterable[int]):
    """Create a provider and put `account_ids` on it, in one unit. An account already on
    another provider moves, dropping the shares it held there. Returns (provider, {account_id:
    [dropped login names]}). Raises ValueError for a bad or taken name or an unknown
    account."""
    from .database import Account, Provider
    name = normalize_provider_name(name)
    ids = list(dict.fromkeys(int(a) for a in account_ids))

    @retry_on_locked()
    def _create_and_commit():
        _check_provider_name(name)
        accounts = Account.query.filter(Account.id.in_(ids)).all() if ids else []
        if len(accounts) != len(ids):
            raise ValueError('One of the chosen accounts no longer exists.')
        now = datetime.utcnow()
        provider = Provider(name=name, created_at=now, updated_at=now)
        db.session.add(provider)
        db.session.flush()
        dropped = {}
        for account in accounts:
            gone = _drop_shares_on_leaving(account, account.provider_id)
            if gone:
                dropped[account.id] = gone
            account.provider_id = provider.id
        if accounts:
            _refold_duplicates()
        db.session.commit()
        return provider, dropped

    provider, dropped = _create_and_commit()
    log.info('Provider "%s" created with accounts %s', provider.name, ids)
    return provider, dropped


def rename_provider(provider_id: int, name: str):
    """Rename a provider. Raises LookupError / ValueError."""
    from .database import Provider
    name = normalize_provider_name(name)

    @retry_on_locked()
    def _rename_and_commit():
        provider = db.session.get(Provider, provider_id)
        if provider is None:
            raise LookupError('Provider not found.')
        _check_provider_name(name, exclude_id=provider_id)
        old = provider.name
        provider.name = name
        provider.updated_at = datetime.utcnow()
        db.session.commit()
        return old, provider

    old, provider = _rename_and_commit()
    log.info('Provider "%s" renamed to "%s"', old, provider.name)
    return provider


def delete_provider(provider_id: int) -> tuple[str, int]:
    """Delete a provider: its accounts go back to being on none. Their hosts and logins are
    left alone, and a login two of them share stays shared (design §3.2) - the seat is one
    real seat whatever the label says, and the account page keeps naming the share. Returns
    (name, accounts unlinked). Raises LookupError."""
    from .database import Account, Provider

    @retry_on_locked()
    def _delete_and_commit():
        provider = db.session.get(Provider, provider_id)
        if provider is None:
            raise LookupError('Provider not found.')
        name = provider.name
        accounts = Account.query.filter(Account.provider_id == provider_id).all()
        for account in accounts:
            account.provider_id = None
        db.session.delete(provider)
        if accounts:
            _refold_duplicates()
        db.session.commit()
        return name, len(accounts)

    name, count = _delete_and_commit()
    log.info('Provider "%s" deleted; %d accounts unlinked', name, count)
    return name, count


def set_account_provider(account_id: int, provider_id: Optional[int]) -> list:
    """Put the account on `provider_id`, or on none. Leaving or moving drops the account's
    hold on any login another account on the old provider still holds (design §3.2).
    Returns the names of the logins it let go. Raises LookupError for an unknown account or
    provider."""
    from .database import Account, Provider

    @retry_on_locked()
    def _set_and_commit():
        account = db.session.get(Account, account_id)
        if account is None:
            raise LookupError('Account not found.')
        if provider_id is not None and db.session.get(Provider, provider_id) is None:
            raise LookupError('Provider not found.')
        if account.provider_id == provider_id:
            return []
        dropped = _drop_shares_on_leaving(account, account.provider_id)
        account.provider_id = provider_id
        _refold_duplicates()
        db.session.commit()
        return dropped

    dropped = _set_and_commit()
    log.info('Account %d: provider set to %s%s', account_id, provider_id,
             f' (stopped sharing {", ".join(dropped)})' if dropped else '')
    return dropped


def share_login(login_id: int, account_id: int):
    """Add a login another account holds to `account_id`'s list too, so the two count its
    seats once (design §3.1 case 3). Only within one provider: every account holding it and
    the target must be on the same one. Appended at the end of the target's list; nothing
    is copied. Returns the Login. Raises LookupError / ValueError."""
    from .database import Account, AccountLogin, Login

    @retry_on_locked()
    def _share_and_commit():
        login = db.session.get(Login, login_id)
        target = db.session.get(Account, account_id)
        holders = login_holders([login_id]).get(login_id, [])
        if login is None or not holders:
            raise LookupError('Login not found.')
        if target is None:
            raise LookupError('Account not found.')
        if account_id in holders:
            raise ValueError(f'{target.name} already holds login "{login.name}".')
        providers = {pid for (pid,) in db.session.query(Account.provider_id)
                     .filter(Account.id.in_(holders)).all()}
        if target.provider_id is None or providers != {target.provider_id}:
            raise ValueError(f'A login can be shared only between accounts on the same '
                             f'provider. Put {target.name} on the same provider first.')
        existing = logins_for_account(account_id)
        if any(lg.username == login.username for lg in existing):
            raise ValueError(f'{target.name} already has a login with the username '
                             f'{login.username}.')
        if any(lg.name.lower() == login.name.lower() for lg in existing):
            raise ValueError(f'{target.name} already has a login named {login.name}.')
        positions = [p for (p,) in db.session.query(AccountLogin.position)
                     .filter(AccountLogin.account_id == account_id).all()]
        db.session.add(AccountLogin(account_id=account_id, login_id=login_id,
                                    position=max(positions, default=-1) + 1))
        db.session.commit()
        return login

    login = _share_and_commit()
    log.info('Login "%s" shared with account %d', login.name, account_id)
    return login


def unshare_login(login_id: int, account_id: int) -> str:
    """`account_id` stops holding a login another account still holds. The other account
    keeps it, with its seats. Returns the login's name. Raises LookupError, or ValueError
    when nobody else holds it - that is Remove on the account page, not a share."""
    holders = login_holders([login_id]).get(login_id, [])
    if account_id not in holders:
        raise LookupError('Login not found.')
    if len(holders) < 2:
        raise ValueError('This login is not shared. Remove it from the account page instead.')
    return remove_login(account_id, login_id)
