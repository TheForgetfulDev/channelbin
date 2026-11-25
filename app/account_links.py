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
shared seats from both (`account_blocks.pool_taken()`). Sharing is only within a provider:
leaving one, or joining one the other holder is not on, drops the share. Nothing links by
itself.

**The playlist's own rows and the switches** (dev/changelog/1670). Every sync lists the host
and the username/password most of the playlist's stream URLs carry as rows on the account
(`AccountHost.from_playlist`, `Login.playlist_account_id`), beside the ones the user added,
and every row has an on/off switch (`AccountHost.enabled`, `AccountLogin.enabled`, the latter
per account so a shared login can be off on one holder only). Listing the playlist's rows is
what lets the render reach every URL: `render_host()` swaps only a listed host and
`render_login()` only a listed pair. A row that is off is never used for a new stream: an off
host is never the active one, and an off login takes no seat. With every switch on, an
account streams exactly as it did before its playlist's rows were listed - its playlist login's
seats are the account's own Max connections. The row follows the playlist and the switch
follows the user: a sync that finds the playlist's host or login changed rewrites the row,
never its switch, and says so on the account's Activity card.

An account with no host rows, no login rows and no provider behaves byte for byte as it did
before the tables existed.

This module is the ONE writer of the lists, the switches and `Account.provider_id` (CLAUDE.md,
participation-switch rule): the rows are the user's judgment about which names reach the same
server and which credentials it accepts, and nothing adds to, edits or removes from a list
but the functions here, called from the explicit user action - or, for the playlist's own
rows, from the sync, which records what the playlist says and never moves a switch the user
set. `AccountHost.is_active` / `activated_at` and
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
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import or_, update

from . import db
from .db_utils import chunked, retry_on_locked

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
    from .host_checks import split_host
    name, port = split_host(text)
    if port is None and ':' in name:
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


def dominant_host(urls: Iterable[Optional[str]]) -> Optional[str]:
    """The host most of `urls` are on; None when none of them has one. Pure, so the
    migration that backfills Account.stream_host counts exactly as current_host() does."""
    counts: Counter = Counter()
    for url in urls:
        host = host_of(url)
        if host:
            counts[host] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def current_host(account_id: int) -> Optional[str]:
    """The host most of the account's stream URLs carry right now - what the first entry
    in a new list is seeded from, so the list always starts from what is already true, and
    what a sync stamps on Account.stream_host for the Hosts card. None for an account with
    no channels. One columns-only query; the count is in Python because SQLite cannot pull
    a netloc out of a URL. It reads every channel on the account (about 250ms at 58,000),
    so a page reads the stamp instead (dev/changelog/1176)."""
    from .database import Channel
    return dominant_host(url for (url,) in db.session.query(Channel.stream_url).filter(
        Channel.account_id == account_id).yield_per(2000))


def url_pair(url: Optional[str]) -> Optional[tuple]:
    """The (username, password) a stream URL's user/pass/id triplet carries, or None for a URL
    without one (a radio mount, a third-party feed). The same parse render_login() splices."""
    if not url:
        return None
    from urllib.parse import urlsplit
    from .accounts import _STREAM_URL_RE
    m = _STREAM_URL_RE.match(urlsplit(url).path)
    return None if m is None else (m.group('user'), m.group('password'))


def playlist_facts(account_id: int) -> dict:
    """{'host', 'pair', 'stream_host'} in one pass over the account's channels: the host and
    the (username, password) most of the playlist's own stream URLs carry - `raw_stream_url`,
    what the provider sent, since `stream_url` already carries the rendered host - and the
    host most of the stream URLs carry now, which a sync stamps on Account.stream_host. Each
    is None when no URL has one. About as costly as current_host(), which it replaces in the
    sync."""
    from .database import Channel
    hosts: Counter = Counter()
    pairs: Counter = Counter()
    rendered: Counter = Counter()
    for stream_url, raw_url in (db.session.query(Channel.stream_url, Channel.raw_stream_url)
                                .filter(Channel.account_id == account_id).yield_per(2000)):
        source = raw_url or stream_url
        host = host_of(source)
        if host:
            hosts[host] += 1
        pair = url_pair(source)
        if pair:
            pairs[pair] += 1
        now_host = host_of(stream_url)
        if now_host:
            rendered[now_host] += 1

    def top(counts):
        return counts.most_common(1)[0][0] if counts else None
    return {'host': top(hosts), 'pair': top(pairs), 'stream_host': top(rendered)}


#: A change list_playlist_rows() reports: which row, the old and new value (a host, or a
#: username - never a password), and whether the row is turned on.
PLAYLIST_HOST, PLAYLIST_LOGIN = 'host', 'login'


def list_playlist_rows(account_id: int, host: Optional[str], pair: Optional[tuple]) -> list:
    """List the playlist's host and login as rows on the account, or bring the rows in line
    with what the playlist says now (dev/changelog/1670). Called by the sync once its channels
    are written, with playlist_facts()'s answer, and by the first manual add on an account no
    sync has listed yet. The row follows the playlist; the switch beside it never moves here.

    - **Host.** The first listing marks the row already carrying that host as the playlist's,
      or adds one, on (the active host when it is the only one). A changed host rewrites the
      playlist's row - or, when the new host is already a row of the user's, marks that one
      and leaves the old row as an ordinary host the user may remove.
    - **Login.** The first listing adopts a login the account already holds with that
      username and password (it keeps its own name and seats), or adds one whose seats are the
      account's own Max connections. Added beside logins of the user's that the playlist's is
      not one of, it starts off: turning it on would add seats the user never set. A changed
      username or password rewrites the playlist's row and clears its refusal stamp.
    - Any change to the host list re-renders the account's stream URLs, and an active host
      that is off hands the role to the first host that is on.

    Returns [{'what', 'old', 'new', 'on'}] for each change to a row already listed (a first
    listing is not a change), in one commit; the caller says each one aloud."""
    from .database import AccountHost

    @retry_on_locked()
    def _list_and_commit():
        now = datetime.utcnow()
        changes = []
        if host:
            rows = (AccountHost.query.filter_by(account_id=account_id)
                    .order_by(AccountHost.position, AccountHost.id).all())
            mine = [r for r in rows if r.from_playlist]
            for extra in mine[1:]:
                extra.from_playlist = False
            cur = mine[0] if mine else None
            existing = next((r for r in rows if r.host == host), None)
            relisted = False
            if cur is None:
                if existing is not None:
                    existing.from_playlist = True
                else:
                    db.session.add(AccountHost(
                        account_id=account_id, host=host, from_playlist=True,
                        position=max((r.position for r in rows), default=-1) + 1,
                        is_active=not rows, created_at=now))
                    relisted = bool(rows)
            elif cur.host != host:
                old = cur.host
                if existing is not None:
                    cur.from_playlist = False
                    existing.from_playlist = True
                    on = existing.enabled
                else:
                    cur.host = host
                    cur.last_resolved_at = None
                    cur.last_resolve_error = None
                    # The user's flip was of the old host: the row now names another, and the
                    # Activity card must not say the new one was turned off (dev/changelog/1671).
                    cur.switched_at = None
                    on = cur.enabled
                changes.append({'what': PLAYLIST_HOST, 'old': old, 'new': host, 'on': on})
                relisted = True
            if relisted:
                db.session.flush()
                rows = (AccountHost.query.filter_by(account_id=account_id)
                        .order_by(AccountHost.position, AccountHost.id).all())
                active = next((r for r in rows if r.is_active), None)
                target = active
                if active is None or not active.enabled:
                    target = next((r for r in rows if r.enabled), active)
                if target is not None:
                    _apply_active(account_id, target.host, now)
        if pair:
            changes.extend(_list_playlist_login(account_id, pair, now))
        db.session.commit()
        return changes

    had_logins = bool(logins_for_account(account_id)) if pair else True
    active_before = _active_host_name(account_id) if host else None
    changes = _list_and_commit()
    if host and _active_host_name(account_id) != active_before:
        # The active host is now another name (a renamed row, or the role handed on); its
        # warning follows. Only then, or a read warning comes back unread (dev/changelog/1732).
        from .host_checks import reassess_active
        reassess_active(account_id)
    if not had_logins:
        from . import connection_limits as connlim
        connlim.fold_account_pool(account_id)
    return changes


def _active_host_name(account_id: int) -> Optional[str]:
    from .database import AccountHost
    row = (db.session.query(AccountHost.host)
           .filter_by(account_id=account_id, is_active=True, enabled=True).first())
    return row[0] if row else None


def _is_sync_listed(login) -> bool:
    """A playlist login the sync added itself - no seat count of its own, so its seats are the
    account's - as against one the user typed that the sync adopted, which keeps the user's
    number. The sync rewrites only the first kind; the second stays the user's
    (dev/changelog/1670)."""
    return login.playlist_account_id is not None and login.max_connections is None


def _list_playlist_login(account_id: int, pair: tuple, now: datetime) -> list:
    """list_playlist_rows()'s login half, inside its transaction (no commit). Returns the
    change list (empty for a first listing or no change)."""
    from .database import AccountLogin, Login
    username, password = pair

    def first_listing():
        """Adopt a held login carrying the pair, or add one; returns whether it is on."""
        held = logins_for_account(account_id)
        match = next((lg for lg in held if (lg.username, lg.password) == pair), None)
        if match is not None:
            if match.playlist_account_id is None:
                match.playlist_account_id = account_id
            return bool(db.session.query(AccountLogin.enabled).filter_by(
                account_id=account_id, login_id=match.id).scalar())
        row = Login(name=username[:100], username=username, password=password,
                    max_connections=None, playlist_account_id=account_id,
                    created_at=now, updated_at=now)
        db.session.add(row)
        db.session.flush()
        positions = [p for (p,) in db.session.query(AccountLogin.position)
                     .filter(AccountLogin.account_id == account_id).all()]
        db.session.add(AccountLogin(account_id=account_id, login_id=row.id, enabled=not held,
                                    position=max(positions, default=-1) + 1))
        return not held

    own = Login.query.filter_by(playlist_account_id=account_id).first()
    if own is None:
        first_listing()
        return []
    if db.session.query(AccountLogin).filter_by(account_id=account_id,
                                                login_id=own.id).first() is None:
        positions = [p for (p,) in db.session.query(AccountLogin.position)
                     .filter(AccountLogin.account_id == account_id).all()]
        db.session.add(AccountLogin(account_id=account_id, login_id=own.id,
                                    position=max(positions, default=-1) + 1))
        db.session.flush()
    if (own.username, own.password) == pair:
        return []
    old_user = own.username
    change = {'what': PLAYLIST_LOGIN, 'old': old_user, 'new': username,
              'password_only': old_user == username}
    taken = next((lg for lg in logins_for_account(account_id)
                  if lg.id != own.id and (lg.username, lg.password) == pair), None)
    if _is_sync_listed(own) and taken is None:
        if own.name == old_user:
            own.name = username[:100]
        own.username = username
        own.password = password
        own.last_refused_at = None
        own.last_refused_detail = None
        own.updated_at = now
        # As for a rewritten host: the flip was of the old login (dev/changelog/1671).
        db.session.query(AccountLogin).filter(AccountLogin.login_id == own.id).update(
            {AccountLogin.switched_at: None}, synchronize_session=False)
        # And so was a block: it kept ChannelBin off the old username for whatever was
        # watching on it, which the new one is not, and left in place the Activity card would
        # name the new username as the one blocked (dev/changelog/1672). A new password on
        # the same username is the same login, so its block stays.
        if not change['password_only']:
            from .account_blocks import drop_login_blocks, login_blocks_at
            change['block_ended'] = bool(login_blocks_at(None, [own.id]))
            drop_login_blocks([own.id])
        change['on'] = bool(db.session.query(AccountLogin.enabled).filter_by(
            account_id=account_id, login_id=own.id).scalar())
        return [change]
    # The row stops being the playlist's. One the user typed stays theirs, untouched; one the
    # sync added is superseded by a login of the user's carrying the new pair, and goes - or,
    # when another account still holds it, stays with that account with a seat count of its own.
    if _is_sync_listed(own):
        _release_sync_listed(own, account_id)
    else:
        own.playlist_account_id = None
    db.session.flush()
    change['on'] = first_listing()
    return [change]


def _release_sync_listed(login, account_id: int) -> None:
    """Inside the caller's unit: a sync-added playlist login that no longer stands for the
    playlist leaves `account_id`; it is deleted when no other account holds it, and otherwise
    stays with them as an ordinary login carrying the seats it had. A seat held on it keeps its
    pool until it releases, as for any removed login."""
    from .database import AccountLogin
    seats = configured_seats([login])[login.id]
    db.session.query(AccountLogin).filter_by(account_id=account_id, login_id=login.id).delete(
        synchronize_session=False)
    still_held = db.session.query(AccountLogin).filter(AccountLogin.login_id == login.id).count()
    if still_held:
        login.max_connections = seats
        login.playlist_account_id = None
    else:
        from .account_blocks import drop_login_blocks
        drop_login_blocks([login.id])
        db.session.delete(login)


def playlist_change_line(change: dict) -> str:
    """The Activity line for one change list_playlist_rows() reported, e.g. "The playlist's
    login changed from k8x2expired to zz91old. It stays turned off, so streams won't use it."
    Hosts and usernames only, never a password."""
    noun = 'host' if change['what'] == PLAYLIST_HOST else 'login'
    if change.get('password_only'):
        head = f'The playlist\'s login {change["new"]} has a new password.'
    else:
        head = f'The playlist\'s {noun} changed from {change["old"]} to {change["new"]}.'
    if change['on']:
        tail = f'It\'s turned on, so new streams can use the new {noun}.'
    else:
        tail = 'It stays turned off, so streams won\'t use it.'
    if change.get('block_ended'):
        tail += f' The block on {change["old"]} ended with it.'
    return f'{head} {tail}'


def note_playlist_changes(account_id: int, account_name: str, changes: list) -> None:
    """One alert per change, so each lands on the account's Activity card (its source is the
    account's own). A row that is on raises the warning - new streams go out with a host or
    login the user did not pick - and one that is off the information. Never raises: the sync
    calling this has already written the rows."""
    from .alerts import ACCOUNT_PLAYLIST_CHANGED, ACCOUNT_PLAYLIST_CHANGED_UNUSED, create_alert
    for change in changes:
        line = playlist_change_line(change)
        log.warning('Account %d (%s): %s', account_id, account_name, line)
        try:
            create_alert(ACCOUNT_PLAYLIST_CHANGED if change['on'] else ACCOUNT_PLAYLIST_CHANGED_UNUSED,
                         title=f'{account_name}: {line}',
                         body=(f'A sync of {account_name} found its playlist\'s stream URLs on a '
                               f'different {"host" if change["what"] == PLAYLIST_HOST else "login"}. '
                               f'{line} The switch is where you left it; change it on the '
                               f'account page.'),
                         source=f'account:{account_id}:playlist-changed')
        except Exception:
            log.exception('Account %d: could not raise the playlist-changed alert', account_id)


# ── The writer: the list is the user's ──────────────────────────────────────────────────

def add_host(account_id: int, raw_host: str):
    """Append a host to the account's list. On an account no sync has listed the playlist's
    host for yet, the first add lists it first, as the playlist's row and the active host
    (design §3.2), so the list always contains what is already true; the host just added
    becomes active only when it IS that host, or when the account has no channels yet to read
    one from. Returns the new row. Raises ValueError for a bad name or one already listed."""
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
            # and "active since <when the list was made>" would say otherwise. It is the
            # playlist's own host, read from what the provider sent, as a sync lists it.
            seed = playlist_facts(account_id)['host']
            if seed and seed != host:
                db.session.add(AccountHost(account_id=account_id, host=seed, position=position,
                                           is_active=True, from_playlist=True, created_at=now))
                position += 1
                row = AccountHost(account_id=account_id, host=host, position=position,
                                  created_at=now)
            else:
                row = AccountHost(account_id=account_id, host=host, position=position,
                                  is_active=True, from_playlist=seed == host, created_at=now)
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
        if row.from_playlist:
            raise ValueError(f'{row.host} comes from the playlist, so it can\'t be removed - '
                             f'the next sync would list it again. Turn it off instead.')
        others = AccountHost.query.filter(AccountHost.account_id == account_id,
                                          AccountHost.id != host_id).count()
        if row.is_active and others:
            raise ValueError(f'{row.host} is the active host. Make another host active first.')
        name, was_active = row.host, row.is_active
        db.session.delete(row)
        db.session.commit()
        return name, was_active

    name, was_active = _remove_and_commit()
    _clear_rolled_alert(account_id)
    if was_active:
        # The last (active) host can go; its warning must not stay naming it. Only then: a
        # standby's removal would bring a read warning back unread (dev/changelog/1732).
        from .host_checks import reassess_active
        reassess_active(account_id)
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
    if not row.enabled:
        raise ValueError(f'{row.host} is turned off. Turn it on first.')
    summary = _activate(account_id, row.host)
    _clear_rolled_alert(account_id)
    log.info('Account %d: host %s made active by hand (%d stream URLs rewritten)',
             account_id, row.host, summary['channels'])
    return summary


def set_host_enabled(account_id: int, host_id: int, enabled: bool) -> dict:
    """The user's switch on one host (dev/changelog/1670). Turning the active host off hands
    the active role to the next host that is on, and rewrites the account's URLs onto it, in
    the same unit; with no other host on, the account can stream nothing until one is turned
    on (connection_limits refuses every new seat and unusable_reason() says why) and the
    URLs stay where they are. Turning a host on while the active one is off makes it active.
    A stream already running keeps going; a recording's next segment reads the URL the
    handover wrote. Returns {'host', 'enabled', 'active', 'channels', 'moved'} - `moved` when
    the active role changed hands. Raises LookupError."""
    from .database import AccountHost
    enabled = bool(enabled)

    @retry_on_locked()
    def _switch_and_commit():
        rows = (AccountHost.query.filter_by(account_id=account_id)
                .order_by(AccountHost.position, AccountHost.id).all())
        row = next((r for r in rows if r.id == host_id), None)
        if row is None:
            raise LookupError('Host not found.')
        active = next((r for r in rows if r.is_active), None)
        out = {'host': row.host, 'enabled': enabled, 'active': active.host if active else None,
               'channels': 0, 'moved': False}
        if row.enabled == enabled:
            return out
        now = datetime.utcnow()
        row.enabled = enabled
        row.switched_at = now
        target = None
        if not enabled and row.is_active:
            target = _next_on_host(rows, row)
        elif enabled and (active is None or not active.enabled):
            target = row
        if target is not None:
            summary = _apply_active(account_id, target.host, now)
            out.update({'active': target.host, 'channels': summary['channels'],
                        'moved': active is None or target.host != active.host})
        db.session.commit()
        return out

    out = _switch_and_commit()
    _clear_rolled_alert(account_id)
    # Only when the active role moved or the active host itself went off: re-asking over a
    # standby host's switch would bring a read warning back unread (dev/changelog/1732).
    if out['moved'] or (not enabled and out['host'] == out['active']):
        from .host_checks import reassess_active
        reassess_active(account_id)
    log.info('Account %d: host %s turned %s%s', account_id, out['host'],
             'on' if enabled else 'off',
             f' - {out["active"]} is now the active host ({out["channels"]} stream URLs '
             f'rewritten)' if out['moved'] else '')
    return out


def delete_hosts_for_account(account_id: int) -> int:
    """Drop the account's host rows inside the caller's delete unit (no commit here): the
    account is going, and a row left behind would list hosts for an account that no longer
    exists. Returns the count removed."""
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
    """Move the account onto the next listed host that is turned on and resolves, because the
    active one stopped resolving (design §4.4). Returns the summary dict when it rolled - or when the
    active host resolves but stream URLs were found stranded on another listed host and
    moved back (`repaired: True`, `old_host: None`) - and None when it changed nothing:
    nothing listed to roll to, inside the cooldown, or no listed host resolves.

    Order: the hosts after the active one in list order, wrapping around, the active one
    last - a resolver hiccup on the active host still gets it re-checked, after the others.
    Never removes a host: the dead one stays, marked with its resolve error, for the user.
    """
    from .database import Account, AccountHost
    from .config import load_config
    cfg = cfg or load_config()
    cooldown = timedelta(minutes=float((cfg.get('hosts') or {}).get('roll_cooldown_minutes', 10)))

    # A host the user turned off is never rolled onto (dev/changelog/1670), and an active
    # host that is off means every host is: there is nothing to roll to.
    rows = (AccountHost.query.filter_by(account_id=account_id, enabled=True)
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
    # A host that answered at its last full check is tried before one never tested, and that
    # before one that did not answer (app/host_checks.py, dev/changelog/1674). The trigger and
    # the lookups are unchanged: this only decides which resolving host the roll reaches first.
    # sorted() is stable, so list order still breaks ties; the active one stays last.
    from .host_checks import rank
    ranks = rank([r.host for r in rows])
    others = sorted(rows[idx + 1:] + rows[:idx], key=lambda r: ranks.get(r.host, 1))
    order = others + [active]
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
        # URL stranded on a listed host that is not the active one. The second case is real:
        # a re-normalize or a sync that raced a roll can leave rows on the dead host while
        # the row says the live one is active, and the next failure lands here again
        # forever. So the active host is re-rendered onto every listed URL anyway (a no-op
        # for a row already there, one scan of the account's channels, bounded by the
        # cooldown), and a repair is said out loud like a roll (dev/changelog/1243).
        summary = _activate(account_id, active.host)
        if not summary['channels'] and not summary['account_urls']:
            log.info('Account %d (%s): %s - active host %s resolves; not rolling',
                     account_id, name, trigger, active.host)
            return None
        summary.update({'old_host': None, 'trigger': trigger, 'repaired': True})
        log.warning('Account %d (%s): %s - active host %s resolves, but %d stream URLs were '
                    'on another listed host; moved them back onto %s', account_id, name,
                    trigger, active.host, summary['channels'], active.host)
        _raise_rolled_alert(
            account_id, name,
            title=f'{name}: {summary["channels"]:,} stream URLs moved back onto {active.host}',
            body=(f'{trigger} failed to resolve its host, but {active.host}, the active host, '
                  f'resolves. {summary["channels"]:,} stream URLs on {name} were on another '
                  f'host from the list'
                  + (', as was the account\'s own URL' if summary['account_urls'] else '')
                  + f' - usually a URL re-normalize or a sync that landed during a host '
                  f'swap - and now use {active.host} again. A running recording picks the '
                  f'repaired URL up at its next segment.'))
        if recording_id is not None:
            _log_recording_roll(recording_id, None, active.host, summary['channels'])
        return summary

    passed = [r.host for r in rows[idx + 1:] + rows[:idx]
              if r is not chosen and ranks.get(r.host, 1) == 2 and ranks.get(chosen.host, 1) < 2]
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
              f'keep. A running recording picks the new host up at its next segment.'
              + (f' Passed over because they did not answer at their last check: '
                 f'{", ".join(passed)}.' if passed else '')
              + (f' {chosen.host} did not answer at its last check either, but it was the only '
                 f'host left that resolves, so streams may fail.'
                 if ranks.get(chosen.host, 1) == 2 else '')))
    if recording_id is not None:
        _log_recording_roll(recording_id, old, chosen.host, summary['channels'])
    return summary


def _activate(account_id: int, host: str) -> dict:
    """Flip `is_active` to `host` and rewrite every stream URL on the account whose host is
    listed, plus the account's own URLs when their host is listed, in one retry unit. Shared
    by the roll and the user's Make active. Returns {'host', 'channels', 'account_urls'}."""

    @retry_on_locked()
    def _activate_and_commit():
        summary = _apply_active(account_id, host, datetime.utcnow())
        db.session.commit()
        return summary

    summary = _activate_and_commit()
    from .host_checks import reassess_active
    reassess_active(account_id)
    return summary


def _apply_active(account_id: int, host: str, now: datetime) -> dict:
    """_activate()'s work inside the caller's transaction (no commit), so a writer that also
    flips a switch moves both in one unit."""
    from .database import Account, AccountHost
    rows = AccountHost.query.filter_by(account_id=account_id).all()
    hosts = frozenset(r.host for r in rows)
    for r in rows:
        if r.host == host:
            if not r.is_active:
                r.is_active = True
                r.activated_at = now
        elif r.is_active:
            r.is_active = False
    changed = rerender_stream_urls(account_id, hosts, host, now)
    account_urls = 0
    account = db.session.get(Account, account_id)
    if account is not None:
        for col in ('base_url', 'm3u_url', 'epg_url'):
            value = getattr(account, col)
            rendered = render_host(value, hosts, host) if value else value
            if rendered != value:
                setattr(account, col, rendered)
                account_urls += 1
        # The URLs on a listed host all moved, so the stamped stream host moves with
        # them; one on a host outside the list did not move and keeps it.
        if account.stream_host in hosts:
            account.stream_host = host
    return {'host': host, 'channels': changed, 'account_urls': account_urls}


def _next_on_host(rows, after) -> Optional[object]:
    """The host to hand the active role to when `after` stops being usable: the next one in
    list order that is turned on, wrapping around, preferring one the DNS probe has not seen
    fail. None when no other host is on. Pure over the rows."""
    if after in rows:
        idx = rows.index(after)
        order = rows[idx + 1:] + rows[:idx]
    else:
        order = list(rows)
    on = [r for r in order if r.enabled and r is not after]
    return next((r for r in on if not r.last_resolve_error), on[0] if on else None)


def rerender_stream_urls(account_id: int, hosts, host: str, now: Optional[datetime] = None) -> int:
    """Inside the caller's transaction (no commit): rewrite every stream URL on the account
    whose host is one of `hosts` onto `host`. Returns the number of rows moved. The one
    rewrite behind the roll, Make active and the sync's end-of-run check, so the three
    writers of `stream_url` cannot disagree on the host (dev/changelog/1243)."""
    from .database import Channel
    now = now or datetime.utcnow()
    changed = []
    for cid, url in db.session.query(Channel.id, Channel.stream_url).filter(
            Channel.account_id == account_id).yield_per(2000):
        rendered = render_host(url, hosts, host)
        if rendered != url:
            changed.append({'b_id': cid, 'b_url': rendered})
    if not changed:
        return 0
    # A Core executemany on the table, on the session's own connection so it rides in
    # the caller's transaction - the ORM's bulk-update path wants primary keys spelled
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
    for batch in chunked(changed, _ROLL_CHUNK):
        conn.execute(stmt, batch)
    return len(changed)


def _log_recording_roll(recording_id: int, old: Optional[str], new: str, channels: int) -> None:
    """The recording's own RECORDING_HOST_ROLLED event. `old` is the host that stopped
    resolving; None means a repair - the active host resolves and stranded URLs were moved
    back onto it."""
    from .database import RECORDING_HOST_ROLLED, add_recording_event
    if old is None:
        detail = (f'{channels:,} stream URLs on the account were on a listed host that is not '
                  f'the active one; moved back onto {new}. The next segment records from the '
                  f'repaired URL.')
    else:
        detail = (f'The stream host {old} stopped resolving; the account now uses {new} '
                  f'({channels:,} channels rewritten). The next segment records from the '
                  f'new URL.')

    @retry_on_locked()
    def _log_and_commit():
        add_recording_event(recording_id, RECORDING_HOST_ROLLED, detail=detail,
                            extra={'old_host': old, 'new_host': new, 'channels': channels})
        db.session.commit()

    _log_and_commit()


# ── The probe: DNS only, never rolls ────────────────────────────────────────────────────

def resolve_host(host: str, timeout: float = RESOLVE_TIMEOUT_SECONDS) -> tuple[bool, Optional[str]]:
    """(resolved, error text). A DNS lookup and nothing else - no connection is opened, so
    this costs the provider nothing and cannot take a seat. The lookup is
    host_checks.lookup(), abandoned past `timeout`."""
    from .host_checks import lookup, split_host
    infos, error = lookup(split_host(host)[0], None, timeout)
    return (True, None) if infos is not None else (False, error)


def _stamp_resolve_verdicts(verdicts: dict, trigger: str = 'roll',
                            ms: Optional[dict] = None) -> None:
    """Write {host_id: (ok, error)} onto the rows. Resolved: last_resolved_at moves and the
    error clears; failed: the error is recorded and the last good time is kept, so the page
    can say "failed since". Each name also gets a lookup row in host_checks (`ms`, by host
    id, is how long it took when the caller timed it), so the card's verdict follows the
    newest thing anything learned about the name."""
    from .database import AccountHost
    from .host_checks import record_lookups
    if not verdicts:
        return
    ms = ms or {}

    @retry_on_locked()
    def _stamp_and_commit():
        now = datetime.utcnow()
        by_host = {}
        for host_id, (ok, error) in verdicts.items():
            row = db.session.get(AccountHost, host_id)
            if row is None:
                continue
            if ok:
                row.last_resolved_at = now
                row.last_resolve_error = None
            else:
                row.last_resolve_error = (error or 'did not resolve')[:255]
            by_host[row.host] = (ok, error, ms.get(host_id))
        db.session.commit()
        return by_host

    record_lookups(_stamp_and_commit(), trigger)


def stamp_lookup(host: str, ok: bool, error: Optional[str]) -> None:
    """A full check looked `host` up: stamp every row listing that name, as the lookup-only
    probe does (app/host_checks.py)."""
    from .database import AccountHost

    @retry_on_locked()
    def _stamp_and_commit():
        now = datetime.utcnow()
        for row in AccountHost.query.filter_by(host=host).all():
            if ok:
                row.last_resolved_at = now
                row.last_resolve_error = None
            else:
                row.last_resolve_error = (error or 'did not resolve')[:255]
        db.session.commit()

    _stamp_and_commit()


def check_hosts(account_ids: Optional[Iterable[int]] = None, app=None) -> dict:
    """The `host_resolve_check` job (design §4.5). With `hosts.scheduled_check: lookup` (the
    default) it resolves every listed host and stamps the verdict - DNS only, nothing reaches
    a provider. With `full`, and an `app` to run the worker in, it puts every listed host name
    in the one host-check line instead (app/host_checks.py), paced like any other check, and
    returns {'queued', ...}. It never rolls either way - its one job is the account page's
    list, so a list going bad is visible before a recording finds out. Returns
    {'checked', 'failed', 'accounts'}."""
    from .config import load_config
    from .database import AccountHost
    q = AccountHost.query
    if account_ids is not None:
        ids = {a for a in account_ids if a is not None}
        if not ids:
            return {'checked': 0, 'failed': 0, 'accounts': 0}
        q = q.filter(AccountHost.account_id.in_(ids))
    rows = q.order_by(AccountHost.account_id, AccountHost.position).all()
    mode = (load_config().get('hosts') or {}).get('scheduled_check', 'lookup')
    if mode == 'full' and app is not None:
        from .host_checks import TRIGGER_SCHEDULED, request
        result = request(app, [row.host for row in rows], TRIGGER_SCHEDULED)
        log.info('Host check (full): %d host(s) put in line, %d left out as checked in the last '
                 '10 minutes', len(result['added']), len(result['recent']))
        return {'checked': 0, 'failed': 0, 'accounts': len({row.account_id for row in rows}),
                'queued': len(result['added'])}
    if mode not in ('lookup', 'full'):
        log.warning('hosts.scheduled_check is %r, which is neither lookup nor full; looking '
                    'the hosts up only', mode)
    verdicts, ms = {}, {}
    for row in rows:
        started = time.perf_counter()
        verdicts[row.id] = resolve_host(row.host)
        ms[row.id] = round((time.perf_counter() - started) * 1000)
    _stamp_resolve_verdicts(verdicts, 'scheduled', ms)
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


def _default_max_connections(cfg: Optional[dict]) -> int:
    if cfg is None:
        from .config import load_config
        cfg = load_config()
    return (cfg.get('accounts') or {}).get('default_max_connections', 1)


def login_seats_for_accounts(account_ids: Iterable[int], cfg: Optional[dict] = None) -> dict:
    """{account_id: [(login_id, usable seats, last_refused_at), ...] in position order}
    - what the connection counter needs to size and walk an account's pools, columns only.
    An account with no logins maps to [] (one pool, sized by the account, as before).

    The ONE reader of a login's seat count. A playlist login with no number of its own
    (`max_connections` NULL) has the seats of the account whose playlist it is - that
    account's Max connections, or the global default - so an account whose only login is its
    playlist's counts exactly what it counted before the row was listed. A login that is off
    on an account is listed for it with 0 seats, never left out: left out, an account whose
    logins are all off would read as holding none and fall back to its own pool, which
    launches the playlist's credentials (dev/changelog/1670)."""
    from sqlalchemy.orm import aliased
    from .database import Account, AccountLogin, Login
    ids = {a for a in account_ids if a is not None}
    out: dict = {a: [] for a in ids}
    if not ids:
        return out
    owner = aliased(Account)
    rows = (db.session.query(AccountLogin.account_id, Login.id, Login.max_connections,
                             Login.last_refused_at, AccountLogin.enabled, owner.max_connections)
            .join(Login, Login.id == AccountLogin.login_id)
            .outerjoin(owner, owner.id == Login.playlist_account_id)
            .filter(AccountLogin.account_id.in_(ids))
            .order_by(AccountLogin.account_id, AccountLogin.position, Login.id).all())
    default = None
    for account_id, login_id, seats, refused_at, enabled, owner_max in rows:
        if not enabled:
            seats = 0
        elif seats is None:
            if owner_max is None and default is None:
                default = _default_max_connections(cfg)
            seats = owner_max or default
        out[account_id].append((login_id, seats, refused_at))
    return out


def configured_seats(logins: Iterable, cfg: Optional[dict] = None) -> dict:
    """{login_id: seats} for Login rows, as configured and before any account's switch - what
    a page says a login is good for. A playlist login's is its account's Max connections or
    the global default, as login_seats_for_accounts() resolves it. One query at most."""
    from .database import Account
    rows = list(logins)
    owners = {lg.playlist_account_id for lg in rows
              if lg.max_connections is None and lg.playlist_account_id is not None}
    limits = dict(db.session.query(Account.id, Account.max_connections)
                  .filter(Account.id.in_(owners)).all()) if owners else {}
    default = None
    out = {}
    for lg in rows:
        seats = lg.max_connections
        if seats is None:
            seats = limits.get(lg.playlist_account_id)
            if not seats:
                if default is None:
                    default = _default_max_connections(cfg)
                seats = default
        out[lg.id] = seats
    return out


def hosts_all_off(account_ids: Iterable[int]) -> set:
    """Of `account_ids`, the ones that list hosts and have every one of them turned off: no
    stream may go out on such an account (dev/changelog/1670). One grouped query."""
    from sqlalchemy import case, func
    from .database import AccountHost
    ids = {a for a in account_ids if a is not None}
    if not ids:
        return set()
    on = func.sum(case((AccountHost.enabled.is_(True), 1), else_=0))
    return {aid for aid, n_on in (db.session.query(AccountHost.account_id, on)
                                  .filter(AccountHost.account_id.in_(ids))
                                  .group_by(AccountHost.account_id).all())
            if not n_on}


def logins_all_off(account_ids: Iterable[int]) -> set:
    """Of `account_ids`, the ones that hold logins and have every one of them turned off for
    that account. One grouped query."""
    from sqlalchemy import case, func
    from .database import AccountLogin
    ids = {a for a in account_ids if a is not None}
    if not ids:
        return set()
    on = func.sum(case((AccountLogin.enabled.is_(True), 1), else_=0))
    return {aid for aid, n_on in (db.session.query(AccountLogin.account_id, on)
                                  .filter(AccountLogin.account_id.in_(ids))
                                  .group_by(AccountLogin.account_id).all())
            if not n_on}


#: unusable_reasons()'s two answers: what is turned off on an account that can stream nothing.
UNUSABLE_HOSTS = 'hosts'
UNUSABLE_LOGINS = 'logins'


def unusable_reasons(account_ids: Iterable[int]) -> dict:
    """{account_id: UNUSABLE_HOSTS | UNUSABLE_LOGINS} for the accounts among `account_ids`
    that cannot take a new stream because the user turned every host, or every login, off.
    Hosts are named first: with no host on, which login is on does not matter. Two queries."""
    ids = {a for a in account_ids if a is not None}
    hosts = hosts_all_off(ids)
    out = {a: UNUSABLE_HOSTS for a in hosts}
    out.update({a: UNUSABLE_LOGINS for a in logins_all_off(ids - hosts)})
    return out


def describe_unusable(account_name: str, which: str, capital: bool = False) -> str:
    """The one phrase every surface uses for an account with every host or every login off.
    `capital` for the start of a sentence."""
    noun = 'host' if which == UNUSABLE_HOSTS else 'login'
    head = 'Every' if capital else 'every'
    return f'{head} {noun} on account "{account_name}" is turned off'


def unusable_reason(account_id: int, account_name: str, capital: bool = False) -> Optional[str]:
    """describe_unusable() for an account that can stream nothing because every host or
    every login on it is turned off, else None - for a refusal that has to say why rather
    than blame the connection limit, checked before the block's and the limit's wording."""
    which = unusable_reasons([account_id]).get(account_id)
    return None if which is None else describe_unusable(account_name, which, capital)


def playlist_rows_off(account_ids: Iterable[int]) -> dict:
    """{account_id: {'host': bool, 'login': bool}} for the accounts among `account_ids` whose
    playlist host or playlist login row is turned off - what the Accounts list's badge reads
    (dev/changelog/1671). An account with both on, or with neither listed yet, is absent. Two
    queries however many accounts."""
    from .database import AccountHost, AccountLogin, Login
    ids = {a for a in account_ids if a is not None}
    out: dict = {}
    if not ids:
        return out
    for (aid,) in (db.session.query(AccountHost.account_id)
                   .filter(AccountHost.account_id.in_(ids), AccountHost.from_playlist.is_(True),
                           AccountHost.enabled.is_(False)).all()):
        out.setdefault(aid, {'host': False, 'login': False})['host'] = True
    for (aid,) in (db.session.query(AccountLogin.account_id)
                   .join(Login, Login.id == AccountLogin.login_id)
                   .filter(AccountLogin.account_id.in_(ids),
                           Login.playlist_account_id == AccountLogin.account_id,
                           AccountLogin.enabled.is_(False)).all()):
        out.setdefault(aid, {'host': False, 'login': False})['login'] = True
    return out


#: next_stream()'s answers: a seat is free; every seat is taken; the account is blocked; the
#: user turned every host or every login off.
NEXT_FREE, NEXT_FULL, NEXT_BLOCKED, NEXT_UNUSABLE = 'free', 'full', 'blocked', 'unusable'


def next_stream(account, cfg: Optional[dict] = None) -> Optional[dict]:
    """What the next stream on `account` would be launched with right now - for the account
    page's Logins card and the channel page (dev/changelog/1671). None for an account that
    lists no host and no login, whose streams go out exactly as the playlist says.

    {'state': NEXT_*, 'login': the Login row or None, 'host': the active host or None,
     'reason': the sentence for NEXT_UNUSABLE and NEXT_BLOCKED - describe_unusable()'s and
     account_blocks.blocked_reason()'s, the words every refusal uses, 'copy_login': the login
     the channel page's Copy renders - the next seat's, or while no seat is free the first
     login that is on and not blocked, or None when there is none}. The login is the one
    connection_limits.try_acquire() would seat it on (next_seat(), the same pick). Advisory:
    a seat can be taken between this read and a launch. A fixed handful of queries."""
    from . import connection_limits as connlim
    logins = logins_for_account(account.id)
    hosts, active = host_list(account.id)
    if not logins and not hosts:
        return None
    # The streams' own host when it is not on the list: a swap does not move those URLs, so
    # they go out where they are, not on the active host.
    unlisted = account.stream_host and account.stream_host not in hosts
    out = {'state': NEXT_FREE, 'login': None, 'copy_login': None,
           'host': account.stream_host if unlisted else (active or account.stream_host),
           'reason': None}
    which = unusable_reasons([account.id]).get(account.id)
    if which is not None:
        out.update(state=NEXT_UNUSABLE, host=None,
                   reason=describe_unusable(account.name, which))
        return out
    free, login_id = connlim.next_seat(account.id, cfg)
    if not free:
        from .account_blocks import blocked_reason, login_blocks_at
        from .database import AccountLogin
        blocked = blocked_reason(account.id, account.name)
        # What Copy hands out while no seat is free: the first login that is on and not
        # blocked, never the playlist's credentials the user turned off, nor a login a TV is
        # watching on (dev/changelog/1672). None when there is no such login, as when every
        # login is off.
        enabled = {lid for lid, on in db.session.query(AccountLogin.login_id, AccountLogin.enabled)
                   .filter(AccountLogin.account_id == account.id).all() if on}
        on = [lg for lg in logins if lg.id in enabled]
        blocked_ids = {b.login_id for b in login_blocks_at(None, enabled)} if on else set()
        free_of_block = [lg for lg in on if lg.id not in blocked_ids]
        out.update(state=NEXT_BLOCKED if blocked else NEXT_FULL, reason=blocked,
                   copy_login=next(iter(free_of_block), None))
        return out
    out['login'] = out['copy_login'] = next((lg for lg in logins if lg.id == login_id), None)
    return out


def next_stream_url(url: Optional[str], account_id: int, nxt: Optional[dict]) -> Optional[str]:
    """`url` (a channel's stored stream URL, already on the active host) with the credentials
    of next_stream()'s `copy_login` rendered in, as a launch would render them - what the
    channel page's Copy hands out. `url` unchanged when there is none."""
    if not url or not nxt or nxt.get('copy_login') is None:
        return url
    return render_login(url, logins_for_account(account_id), nxt['copy_login'])


def login_enabled(account_id: int, login_id: int) -> bool:
    """Whether `login_id` is turned on for `account_id` (False for one it does not hold)."""
    from .database import AccountLogin
    return bool(db.session.query(AccountLogin.enabled).filter(
        AccountLogin.account_id == account_id, AccountLogin.login_id == login_id).scalar())


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

#: Characters a login's username or password may not carry, because render_login() splices
#: both into the stream URL's path raw: "/" would add a path segment, "?" starts the query
#: and "#" the fragment (the stream id after it is lost and the server answers 404, which
#: is not a credential refusal, so the login would be retried on every segment with nothing
#: stamped), "%" starts an escape the server decodes, "@" is read as userinfo by some
#: clients, and whitespace breaks the request line. Refused at the dialog rather than
#: encoded on the way out: encoding would change what the provider sees for a password
#: that already carries %XX (dev/changelog/1243).
_UNRENDERABLE = frozenset('/?#%@')


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
    for value in (username, password or ''):
        bad = next((ch for ch in value if ch in _UNRENDERABLE or ch.isspace()), None)
        if bad is not None:
            shown = 'a space' if bad.isspace() else f'"{bad}"'
            raise ValueError(f'A username or password cannot contain {shown}: it is '
                             f'rendered into the stream URL as a path segment, and this '
                             f'character would change how the URL is read.')
    try:
        seats = int(max_connections)
    except (TypeError, ValueError):
        raise ValueError('Seats must be a whole number of 1 or more.')
    if seats < 1:
        raise ValueError('Seats must be 1 or more.')
    return name, username, password, seats


def _check_login_unique(existing, name: str, username: str, password: str) -> None:
    """Refuse a login whose username AND password another login on the account already has -
    two rows for one credential would count its seats twice - or whose name one of the user's
    own logins already has. A username alone may repeat: the playlist's expired login and the
    working one the user added often share it (dev/changelog/1670). A playlist login's name is
    its username, not the user's choice, so it never blocks a name."""
    if any((lg.username, lg.password) == (username, password) for lg in existing):
        raise ValueError(f'A login with the username {username} and that password is '
                         f'already on the list.')
    if any(lg.name.lower() == name.lower() and lg.playlist_account_id is None
           for lg in existing):
        raise ValueError(f'A login named {name} is already on the list.')


def add_login(account_id: int, name: str, username: str, password: str, max_connections):
    """Append a login to the account's list. Returns the new row. Raises ValueError for a
    missing field, a bad seat count, or a credential or name already listed on this account.

    On an account holding no login yet, the playlist's own login is listed first, on, as a
    sync would list it (dev/changelog/1670): adding a second login must not take away the
    first. When the login typed IS the playlist's, it becomes the playlist's row itself."""
    from .database import AccountLogin, Login
    name, username, password, seats = _validate_login_fields(
        name, username, password, max_connections, password_required=True)
    adopt = False
    if not logins_for_account(account_id) and Login.query.filter_by(
            playlist_account_id=account_id).first() is None:
        pair = playlist_facts(account_id)['pair']
        if pair == (username, password):
            adopt = True
        elif pair is not None:
            list_playlist_rows(account_id, None, pair)

    @retry_on_locked()
    def _add_and_commit():
        existing = logins_for_account(account_id)
        _check_login_unique(existing, name, username, password)
        positions = [p for (p,) in db.session.query(AccountLogin.position)
                     .filter(AccountLogin.account_id == account_id).all()]
        now = datetime.utcnow()
        row = Login(name=name, username=username, password=password, max_connections=seats,
                    playlist_account_id=account_id if adopt else None,
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
    from . import connection_limits as connlim
    connlim.fold_account_pool(account_id)
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
        _check_login_unique(others, name, username, password or row.password)
        credentials_changed = (row.username != username
                               or (password is not None and password != '' and row.password != password))
        if _is_sync_listed(row):
            # The sync rewrites it from the playlist, and its seats are the account's Max
            # connections; an edit would be undone by the next sync, or would pin a number the
            # account form no longer moves (dev/changelog/1670).
            raise ValueError('This login comes from the playlist, so it follows the playlist '
                             'and its seats are the account\'s Max connections. Turn it off '
                             'and add your own login instead.')
        if credentials_changed and row.playlist_account_id is not None:
            # A login the user typed that the sync adopted: new credentials make it no longer
            # the playlist's, and it stays the user's (dev/changelog/1670).
            row.playlist_account_id = None
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
        if row.playlist_account_id == account_id and _is_sync_listed(row):
            raise ValueError(f'Login "{row.name}" comes from the playlist, so it can\'t be '
                             f'removed - the next sync would list it again. Turn it off '
                             f'instead.')
        if row.playlist_account_id == account_id:
            # A login the user typed that the sync adopted: removing it is the user's call,
            # and the next sync lists the playlist's login as a row of its own.
            row.playlist_account_id = None
        name = row.name
        db.session.delete(held)
        still_held = db.session.query(AccountLogin).filter(
            AccountLogin.login_id == login_id, AccountLogin.account_id != account_id).count()
        if not still_held:
            from .account_blocks import drop_login_blocks
            drop_login_blocks([login_id])
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


def set_login_enabled(account_id: int, login_id: int, enabled: bool) -> dict:
    """The user's switch on one login, for this account only (dev/changelog/1670): a shared
    login stays as it was on its other holders. A login that is off takes no new seat on the
    account; a stream already seated on it keeps going, and a recording's next segment moves
    to another login that is on when one has a free seat
    (connection_limits.reseat_before_relaunch). Returns {'login', 'enabled'}. Raises
    LookupError for a login the account does not hold."""
    from .database import AccountLogin, Login
    enabled = bool(enabled)

    @retry_on_locked()
    def _switch_and_commit():
        held = db.session.query(AccountLogin).filter_by(account_id=account_id,
                                                        login_id=login_id).first()
        row = db.session.get(Login, login_id) if held is not None else None
        if row is None:
            raise LookupError('Login not found.')
        if held.enabled != enabled:
            held.enabled = enabled
            held.switched_at = datetime.utcnow()
            db.session.commit()
        return {'login': row.name, 'enabled': enabled}

    out = _switch_and_commit()
    log.info('Account %d: login "%s" turned %s', account_id, out['login'],
             'on' if enabled else 'off')
    return out


def delete_logins_for_account(account_id: int) -> int:
    """Drop the account's login rows inside the caller's delete unit (no commit here): the
    join rows, and every login no other account holds. Returns the count of logins
    deleted.

    The account's playlist login, when another account still holds it, stays with that
    account as an ordinary login carrying the seat count it had (dev/changelog/1670): its
    seats were this account's Max connections, which goes with the account."""
    from .database import AccountLogin, Login
    for row in Login.query.filter_by(playlist_account_id=account_id).all():
        if row.max_connections is None:
            row.max_connections = configured_seats([row])[row.id]
        row.playlist_account_id = None
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
    from .account_blocks import drop_login_blocks
    drop_login_blocks(orphans)
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


def _drop_shares_off_provider(account, provider_id: Optional[int]) -> list:
    """Inside the caller's unit, with `account.provider_id` already set to `provider_id`
    and flushed: the account stops holding any login it shares with an account that is
    not on that same provider (design §3.2 - a share lives within one provider). That is
    the account leaving its provider (every other holder was on it), joining one from
    none (a share kept through a provider's deletion whose other holder stayed off), or
    moving between two. On a move to none every share goes. Logins only it holds stay.
    Returns the names of the logins it let go."""
    from .database import Account, AccountLogin, Login
    mine = db.session.query(AccountLogin).filter(AccountLogin.account_id == account.id).all()
    if not mine:
        return []
    holders = login_holders(r.login_id for r in mine)
    others = {aid for ids in holders.values() for aid in ids if aid != account.id}
    if not others:
        return []
    if provider_id is None:
        with_me = set()
    else:
        with_me = {aid for (aid,) in db.session.query(Account.id).filter(
            Account.provider_id == provider_id, Account.id.in_(others)).all()}
    dropped = []
    for row in mine:
        away = set(holders.get(row.login_id, [])) - {account.id} - with_me
        if not away:
            continue
        login = db.session.get(Login, row.login_id)
        dropped.append(login.name if login else f'#{row.login_id}')
        if login is not None and login.playlist_account_id == account.id:
            # The account's own playlist login stays with it - its next sync would list it
            # again - and the accounts it was shared with let it go (dev/changelog/1670).
            db.session.query(AccountLogin).filter(
                AccountLogin.login_id == row.login_id,
                AccountLogin.account_id.in_(away)).delete(synchronize_session=False)
        else:
            db.session.delete(row)
    return dropped


def _case_sensitive(case_sensitive: Optional[bool]) -> bool:
    # duplicated from app/routes/accounts.py::_case_sensitive - app/ modules do not import
    # route modules; both are case_sensitive_matching() over the one config reader.
    if case_sensitive is not None:
        return case_sensitive
    from .config import load_config
    from .epg_sources import case_sensitive_matching
    return case_sensitive_matching(load_config())


def _refold_duplicates() -> None:
    """Recompute the duplicate fold inside the caller's commit unit. An account joining or
    leaving a provider changes the key of every channel on it, and a human made the change,
    so it shows at once rather than after the next sync (design §8.1). A recompute, so a
    lock retry of the enclosing closure redoing it is harmless."""
    from . import duplicate_streams
    duplicate_streams.recompute()


def create_provider(name: str, account_ids: Iterable[int],
                    case_sensitive: Optional[bool] = None):
    """create_provider_full() without its EPG report: (provider, dropped)."""
    provider, dropped, _changes = create_provider_full(name, account_ids, case_sensitive)
    return provider, dropped


def create_provider_full(name: str, account_ids: Iterable[int],
                         case_sensitive: Optional[bool] = None):
    """Create a provider and put `account_ids` on it, in one unit. An account already on
    another provider moves, dropping the shares it held there. Returns (provider, {account_id:
    [dropped login names]}, {account_id: epg_sources.ProviderChange}). Raises ValueError for
    a bad or taken name or an unknown account.

    The provider starts with the EPG list [Provider's EPG], and each account then follows it
    (DESIGN-epg-sources.md §15.5) - after the commit, since that re-resolves channels and
    commits on its own."""
    from .database import Account, Provider
    from .epg_sources import ensure_provider_list, provider_changed
    name = normalize_provider_name(name)
    ids = list(dict.fromkeys(int(a) for a in account_ids))

    @retry_on_locked()
    def _create_and_commit():
        _check_provider_name(name)
        accounts = Account.query.filter(Account.id.in_(ids)).all() if ids else []
        if len(accounts) != len(ids):
            raise ValueError('One of the chosen accounts no longer exists.')
        before = {a.id: a.provider_id for a in accounts}
        now = datetime.utcnow()
        provider = Provider(name=name, created_at=now, updated_at=now)
        db.session.add(provider)
        db.session.flush()
        ensure_provider_list(provider.id)
        # Every account is put on the provider BEFORE any share is judged: two accounts
        # that kept a share through their old provider's deletion and are now re-created
        # together must keep it, and judging the first while the second is still on none
        # would drop it.
        for account in accounts:
            account.provider_id = provider.id
        db.session.flush()
        dropped = {}
        for account in accounts:
            gone = _drop_shares_off_provider(account, provider.id)
            if gone:
                dropped[account.id] = gone
        if accounts:
            _refold_duplicates()
        db.session.commit()
        return provider, dropped, before

    provider, dropped, before = _create_and_commit()
    log.info('Provider "%s" created with accounts %s', provider.name, ids)
    changes = {}
    if ids:
        cs = _case_sensitive(case_sensitive)
        for account_id in ids:
            changes[account_id] = provider_changed(account_id, before.get(account_id),
                                                   provider.id, cs)
    return provider, dropped, changes


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


def delete_provider(provider_id: int, keep_sources: bool = True,
                    case_sensitive: Optional[bool] = None) -> tuple[str, int]:
    """delete_provider_full() without its EPG report. Returns (name, accounts unlinked)."""
    name, count, _release = delete_provider_full(provider_id, keep_sources, case_sensitive)
    return name, count


def delete_provider_full(provider_id: int, keep_sources: bool,
                         case_sensitive: Optional[bool] = None):
    """Delete a provider: its accounts go back to being on none. Their hosts and logins are
    left alone, and a login two of them share stays shared (design §3.2) - the seat is one
    real seat whatever the label says, and the account page keeps naming the share. The
    share is judged again at the next provider change of either account: re-creating the
    provider with both keeps it, putting one of them on a provider without the other drops
    it. Returns (name, accounts unlinked, epg_sources.ProviderRelease). Raises LookupError.

    Its EPG sources go first (DESIGN-epg-sources.md §15.5): kept, each is handed to the
    lowest-numbered account reading it so no guide changes; not kept, they are deleted and
    their channels re-resolved. The caller removes a deleted source's jobs and alerts."""
    from .database import Account, Provider
    from .epg_sources import release_provider_sources
    if db.session.get(Provider, provider_id) is None:
        raise LookupError('Provider not found.')
    release = release_provider_sources(provider_id, keep_sources,
                                       _case_sensitive(case_sensitive))

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
    log.info('Provider "%s" deleted; %d accounts unlinked; EPG sources handed over %s, '
             'deleted %s', name, count, release.handed, release.deleted)
    return name, count, release


def set_account_provider(account_id: int, provider_id: Optional[int],
                         case_sensitive: Optional[bool] = None) -> list:
    """move_account_provider() without its EPG report: the names of the logins let go."""
    return move_account_provider(account_id, provider_id, case_sensitive)[0]


def move_account_provider(account_id: int, provider_id: Optional[int],
                          case_sensitive: Optional[bool] = None):
    """Put the account on `provider_id`, or on none. Leaving, joining or moving drops the
    account's hold on any login shared with an account that is not on the new provider
    (design §3.2). Returns (names of the logins it let go, epg_sources.ProviderChange or
    None when nothing moved). Raises LookupError for an unknown account or provider.

    Then the EPG half (DESIGN-epg-sources.md §15.5), after the commit since it re-resolves
    channels and commits on its own: leaving stops using the old provider's sources,
    joining follows the new provider's order."""
    from .database import Account, Provider
    from .epg_sources import provider_changed

    @retry_on_locked()
    def _set_and_commit():
        account = db.session.get(Account, account_id)
        if account is None:
            raise LookupError('Account not found.')
        if provider_id is not None and db.session.get(Provider, provider_id) is None:
            raise LookupError('Provider not found.')
        old = account.provider_id
        if old == provider_id:
            return [], old, False
        account.provider_id = provider_id
        db.session.flush()
        dropped = _drop_shares_off_provider(account, provider_id)
        _refold_duplicates()
        db.session.commit()
        return dropped, old, True

    dropped, old, moved = _set_and_commit()
    if not moved:
        return dropped, None
    log.info('Account %d: provider set to %s%s', account_id, provider_id,
             f' (stopped sharing {", ".join(dropped)})' if dropped else '')
    change = provider_changed(account_id, old, provider_id, _case_sensitive(case_sensitive))
    return dropped, change


def share_login(login_id: int, account_id: int):
    """Add a login another account holds to `account_id`'s list too, so the two count its
    seats once (design §3.1 case 3). Only within one provider: every account holding it and
    the target must be on the same one. Appended at the end of the target's list; nothing
    is copied. Returns (Login, replaced) - `replaced` the id of the target's own playlist row
    the share took the place of, or None. Raises LookupError / ValueError.

    One login sold through two services is the case sharing exists for, and there the sync
    has already listed the same username and password on both accounts as each one's
    playlist login. The target's row is then replaced by the shared one, in its place on the
    list with its switch, rather than refused: it cannot be removed (the next sync would list
    it again), so a refusal would leave the two accounts no way to count the seat once. The
    target's sync then adopts the shared row as its playlist login instead of adding another
    (dev/changelog/1692). A row the user typed is never replaced: that is the user's to
    remove."""
    from .database import Account, AccountLogin, Login, LoginBlock

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
        twin = next((lg for lg in existing
                     if (lg.username, lg.password) == (login.username, login.password)), None)
        if twin is not None:
            if not (twin.playlist_account_id == account_id and _is_sync_listed(twin)):
                raise ValueError(f'{target.name} already has a login with the username '
                                 f'{login.username} and that password. Remove it from '
                                 f'{target.name} first, then share this one.')
            if login_holders([twin.id]).get(twin.id, []) != [account_id]:
                raise ValueError(f'{target.name}\'s own login {twin.username} is shared with '
                                 f'another account. Stop sharing it first.')
            now = datetime.utcnow()
            if LoginBlock.query.filter(
                    LoginBlock.login_id == twin.id,
                    or_(LoginBlock.stop_time.is_(None), LoginBlock.stop_time > now)).first():
                raise ValueError(f'{target.name}\'s own login {twin.username} is blocked. '
                                 f'Unblock it first, then share this one.')
        if login.playlist_account_id is None and any(
                lg.name.lower() == login.name.lower() and lg.playlist_account_id is None
                for lg in existing):
            raise ValueError(f'{target.name} already has a login named {login.name}.')
        replaced = None
        if twin is not None:
            held = db.session.get(AccountLogin, (account_id, twin.id))
            row = AccountLogin(account_id=account_id, login_id=login_id, position=held.position,
                               enabled=held.enabled, switched_at=held.switched_at)
            replaced = twin.id
            db.session.delete(held)
            db.session.flush()
            from .account_blocks import drop_login_blocks
            drop_login_blocks([twin.id])
            db.session.delete(twin)
            db.session.add(row)
        else:
            positions = [p for (p,) in db.session.query(AccountLogin.position)
                         .filter(AccountLogin.account_id == account_id).all()]
            db.session.add(AccountLogin(account_id=account_id, login_id=login_id,
                                        position=max(positions, default=-1) + 1))
        db.session.commit()
        return login, replaced

    login, replaced = _share_and_commit()
    if replaced is not None:
        from . import connection_limits as connlim
        connlim.merge_login_pool(replaced, login.id)
        log.info('Login "%s" shared with account %d, in place of its own playlist login %d '
                 '(the same username and password)', login.name, account_id, replaced)
    else:
        log.info('Login "%s" shared with account %d', login.name, account_id)
    return login, replaced


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
