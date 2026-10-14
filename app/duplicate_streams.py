"""The duplicate fold: which channels are one stream, and which copy of each is kept.

Two channels are the same stream when they share a KEY, and a channel has exactly one:

    provider key   (the account's provider, the channel's provider_stream_id)
                   when the account is on a provider AND the channel has an id
    URL key        the channel's stream_url
                   otherwise

Everything here is a derived cache with one writer, this module, the same kind of column as
`Channel.hidden`:

* `Channel.duplicate_cluster_id` - the lowest channel id among the channels sharing this
  channel's key, NULL when it shares its key with nobody. `Channel.is_duplicate_stream` is
  that column read as a yes/no, not a second stored fact.
* `Channel.is_duplicate_loser` - True on every member of a cluster except the one kept.
  "Show duplicates" off is this column and nothing else.

**The keep rule is a CASCADE** - each rung only gets a say when the one above it ties: not
hidden, then still in the provider's feed, then already in the guide, then in a channel
group, then the best health score, then the lowest channel id. `keep_order()` is its one
spelling, ranked over `_ranking_inputs()`. `channel_search_rows._keep_reason()` names the rung that decided a cluster and
must move with it.

**Stored, not ranked per search, and that is measured** (dev/changelog/1172). Ranking in the
search query was fine while a URL match flagged ~1,600 rows; with accounts on a provider
the flagged set was 154,733 of 201,057 channels and one channel search went from 0.8s to
6.6s, because the window is re-ranked in the row query and again in every facet aggregate.

**What keeps the stored answer current.** Cluster membership and the feed rung move only at
a sync, a provider change, a re-normalization or a channel delete, and each of those runs
`recompute()` over the whole table. The other four rungs move one channel at a time, and
those re-rank only the clusters involved through `rerank()`:

* hidden, guide and group changes, from `channel_hiding.recompute()`, which every one of
  them already passes through;
* health score and manual adjustment, guide flag and group membership written through the
  ORM, from the session's after-flush listener below.

A bulk `UPDATE channels SET health_score/in_guide ...` reaches neither and must call
`rerank()` itself; `tests/test_static_invariants.py::DuplicateFoldWriteTests` fails on
one that does not say so.

The worst a missed re-rank can do is show the wrong copy as kept until the next sync: one
member of every cluster is always the one not flagged, so nothing leaves the search.

Design: dev/docs/DESIGN-account-providers.md §8.
"""
import contextlib
import contextvars
import logging

from sqlalchemy import and_, case, event, func, inspect, null, or_, select, update

from . import db
from .database import Account, Channel, ChannelGroupMember, Provider
from .db_utils import retry_on_locked

log = logging.getLogger(__name__)

#: The Channel attributes whose ORM change re-ranks that channel's cluster.
_RANK_INPUTS = ('health_score', 'manual_health_adjustment', 'in_guide')

_suspended = contextvars.ContextVar('duplicate_fold_suspended', default=False)


@contextlib.contextmanager
def suspended():
    """Run a block with the fold's writers switched off.

    For the migration runner alone: its steps run ORM code against a database whose channels
    table does not have these columns yet, and a re-rank there is a "no such column" that
    aborts the upgrade. Migration 88 registers the obligation to compute everything once the
    schema is current, so nothing is lost by skipping the writes before it."""
    token = _suspended.set(True)
    try:
        yield
    finally:
        _suspended.reset(token)


# ---------------------------------------------------------------------------
# The key and the cascade, as SQL
# ---------------------------------------------------------------------------

def in_any_group_expr():
    """"Is this channel in any channel group at all", as a correlated EXISTS. ANY group,
    deliberately - a channel in a group nobody records from is still one the user curated
    (dev/changelog/759)."""
    return select(ChannelGroupMember.id).where(
        ChannelGroupMember.channel_id == Channel.id).correlate(Channel).exists()


def effective_health():
    """The score the badges show: the observed score plus the manual adjustment.

    Deliberately unclamped. `health_score_badge` clamps to 0-100 for display, but clamping
    cannot move a value across the 80 or 50 cut points, so banding on the raw sum is the same
    answer with one less expression in every query.
    """
    return Channel.health_score + Channel.manual_health_adjustment


def _missing_days_cfg(cfg):
    if cfg is None:
        from .config import load_config
        cfg = load_config()
    return cfg


def missing_expr(cfg=None, joined: bool = False):
    """The 'missing from the feed' condition the Missing badge uses - one definition, in
    accounts.missing_channel_expr()."""
    from .accounts import missing_channel_expr
    return missing_channel_expr(_missing_days_cfg(cfg), joined=joined)


def _ranking_inputs(cfg, *where):
    """One row per channel carrying its key and everything the keep rule reads, as a
    subquery joined to its account.

    Evaluated here once per row and ranked in the query around it, rather than spelled as
    correlated expressions inside the window: on 201,057 channels the same recompute took
    3.4s with the account lookups inside the PARTITION BY and ORDER BY and 1.2s with them
    resolved first (dev/changelog/1172)."""
    keyed = and_(Channel.provider_stream_id.isnot(None), Account.provider_id.isnot(None))
    stmt = select(
        Channel.id.label('id'),
        # The key: (provider, stream id) for a channel on a provider, its URL otherwise.
        case((keyed, Account.provider_id), else_=null()).label('key_provider'),
        case((keyed, Channel.provider_stream_id), else_=Channel.stream_url).label('key'),
        # A URL with no scheme is malformed or truncated, not a duplicate signal: it keys
        # on its own id and so never shares a partition.
        case((Channel.stream_url.contains('://'), null()), else_=Channel.id).label('key_own'),
        Channel.duplicate_cluster_id.label('cluster'),
        Channel.hidden.label('hidden'),
        missing_expr(cfg, joined=True).label('missing'),
        Channel.in_guide.label('in_guide'),
        in_any_group_expr().label('in_group'),
        effective_health().label('health'),
    ).join(Account, Account.id == Channel.account_id)
    if where:
        stmt = stmt.where(*where)
    return stmt.subquery()


def keep_order(inputs) -> list:
    """The keep-rule cascade as an ORDER BY over `_ranking_inputs()` - rank 1 is the copy
    kept. Its one spelling.

    **The hidden rung is at the top and is not optional**: a hidden channel that won a
    cluster would take every visible copy out of the search with it (dev/changelog/775).
    The feed rung sits directly under it for the same reason one step removed - a copy the
    provider stopped listing must not hold a live copy folded behind it. SQLite sorts NULL
    lowest, so DESC puts a never-scored channel behind a scored one."""
    c = inputs.c
    return [c.hidden.asc(), c.missing.asc(), c.in_guide.desc(), c.in_group.desc(),
            c.health.desc(), c.id.asc()]


# ---------------------------------------------------------------------------
# The writers
# ---------------------------------------------------------------------------

def recompute(cfg=None) -> int:
    """Rewrite `duplicate_cluster_id` and `is_duplicate_loser` for the whole table.

    Derives every value from the current rows rather than from a diff, so running it twice
    is always safe. One UPDATE ... FROM that touches only the rows whose answer moved: no id
    list crosses into Python, and there is no state in which the cluster column is new and
    the loser column is old.

    Does NOT commit - the caller owns the commit (`recompute_and_commit()` is the bare
    closure most callers want). Returns the number of rows whose answer changed."""
    if _suspended.get():
        return 0
    db.session.flush()
    inputs = _ranking_inputs(cfg)
    partition = [inputs.c.key_provider, inputs.c.key, inputs.c.key_own]
    ranked = select(
        inputs.c.id,
        func.count().over(partition_by=partition).label('n'),
        func.min(inputs.c.id).over(partition_by=partition).label('first_id'),
        func.row_number().over(partition_by=partition,
                               order_by=keep_order(inputs)).label('rn'),
    ).subquery()
    cluster = case((ranked.c.n > 1, ranked.c.first_id), else_=null())
    loser = case((and_(ranked.c.n > 1, ranked.c.rn > 1), True), else_=False)
    result = db.session.execute(
        update(Channel)
        .where(Channel.id == ranked.c.id,
               or_(Channel.duplicate_cluster_id.is_distinct_from(cluster),
                   Channel.is_duplicate_loser != loser))
        .values(duplicate_cluster_id=cluster, is_duplicate_loser=loser),
        execution_options={'synchronize_session': False})
    return result.rowcount


@retry_on_locked()
def recompute_and_commit() -> None:
    """`recompute()` as its own retried commit unit. Kept bare: whatever else shares a
    `retry_on_locked` closure with a whole-table write is redone on every lock retry and
    holds the write lock while it runs (dev/changelog/685)."""
    recompute()
    db.session.commit()


def rerank(channel_ids, cfg=None) -> int:
    """Re-rank the clusters these channels belong to, after something the cascade reads
    changed on them. Membership is not re-derived - that is `recompute()`'s job - so this
    is an index seek per cluster and costs nothing for a channel in none.

    Does NOT commit, and does not flush: the caller's change must already be on the
    connection (`channel_hiding.recompute()` and the after-flush listener both guarantee
    it)."""
    if _suspended.get():
        return 0
    ids = {int(cid) for cid in channel_ids if cid is not None}
    if not ids:
        return 0
    return _rerank_ids(db.session, ids, cfg)


def _rerank_ids(session, ids, cfg=None) -> int:
    touched = (select(Channel.duplicate_cluster_id)
               .where(Channel.id.in_(sorted(ids)),
                      Channel.duplicate_cluster_id.isnot(None)))
    inputs = _ranking_inputs(cfg, Channel.duplicate_cluster_id.in_(touched))
    ranked = select(
        inputs.c.id,
        func.row_number().over(partition_by=inputs.c.cluster,
                               order_by=keep_order(inputs)).label('rn'),
    ).subquery()
    loser = case((ranked.c.rn > 1, True), else_=False)
    result = session.execute(
        update(Channel)
        .where(Channel.id == ranked.c.id, Channel.is_duplicate_loser != loser)
        .values(is_duplicate_loser=loser),
        execution_options={'synchronize_session': False})
    return result.rowcount


def _rerank_after_flush(session, flush_context):
    """Re-rank the clusters of every channel whose cascade inputs this flush wrote."""
    if _suspended.get():
        return
    ids = set()
    for obj in session.dirty:
        if isinstance(obj, Channel):
            attrs = inspect(obj).attrs
            if any(attrs[name].history.has_changes() for name in _RANK_INPUTS):
                ids.add(obj.id)
    for obj in list(session.new) + list(session.deleted):
        if isinstance(obj, ChannelGroupMember) and obj.channel_id is not None:
            ids.add(obj.channel_id)
    if ids:
        _rerank_ids(session, ids)


def install_listener() -> None:
    """Attach the after-flush re-rank to the app's session. Idempotent - create_app() runs
    once per test app in one process."""
    if not event.contains(db.session, 'after_flush', _rerank_after_flush):
        event.listen(db.session, 'after_flush', _rerank_after_flush)


def backfill_fold(cfg) -> None:
    """Discharge migration 88's obligation: compute both columns for a database that just
    gained them. Run from create_app() rather than inside the step because the ranking reads
    `sync.channel_missing_after_days`, and a migration step has no business reading config.

    Gated on the ledger and stamped in the same commit as the work; a recompute, so a retry
    after a crash between the two is harmless."""
    from .migrations import _BF_DUPLICATE_FOLD, finish_obligation, obligation_pending
    if not obligation_pending(_BF_DUPLICATE_FOLD):
        return

    @retry_on_locked()
    def _fold_and_commit():
        changed = recompute(cfg)
        finish_obligation(_BF_DUPLICATE_FOLD)
        db.session.commit()
        return changed

    changed = _fold_and_commit()
    log.info('Backfill: duplicate fold computed (%d channel(s) updated)', changed)



# ---------------------------------------------------------------------------
# Naming a cluster's key
# ---------------------------------------------------------------------------

KEY_PROVIDER = 'provider'
KEY_URL = 'url'


def key_payload(provider_stream_id, provider_name) -> dict:
    """What a cluster is keyed on, for a badge or a page to put into words. `provider_name`
    is None for a channel whose account is on no provider."""
    if provider_name is not None and provider_stream_id is not None:
        return {'kind': KEY_PROVIDER, 'stream_id': provider_stream_id,
                'provider': provider_name}
    return {'kind': KEY_URL}


def describe_key(channel) -> dict:
    """`key_payload()` for one loaded channel. Two primary-key reads - for a page showing
    one channel, never a row loop."""
    account = db.session.get(Account, channel.account_id)
    provider = (db.session.get(Provider, account.provider_id)
                if account is not None and account.provider_id is not None else None)
    return key_payload(channel.provider_stream_id, provider.name if provider else None)


# ---------------------------------------------------------------------------
# Duplicate sets inside one group
# ---------------------------------------------------------------------------

COPY_SAME_URL = 'same_url'
COPY_OTHER_LOGIN = 'other_login'
COPY_SAME_LOGIN = 'same_login'
COPY_SAME_ACCOUNT = 'same_account'


def _url_key(url):
    """The fold's URL rule: a URL with no scheme is malformed, not a duplicate signal."""
    return url if url and '://' in url else None


def duplicate_sets(channels, lead_of=None) -> list:
    """The channels in `channels` that are one stream more than once, as SETS - one per
    stream - each with the copy a group keeps (its lead) and, for every other copy, which
    copy it duplicates and how (DESIGN-account-providers.md §9, dev/changelog/1175).

    Two channels are one stream when they share an exact stream URL or a stored
    `duplicate_cluster_id`. A set is the connected whole, so a channel matching one copy by
    URL and another by provider stream id lands in ONE set: a group page that showed it in
    two would offer to keep it in one and remove it in the other.

    `lead_of(set_channels)` picks each set's lead and defaults to its first channel. Sets
    come back in order of their first channel, each set's channels in `channels`' order.

    Returns [{'channels': [...], 'lead': channel, 'provider': name or None,
              'copies': {channel_id: {'of': channel, 'situation': COPY_*,
                                      'provider': name or None}}}],
    `provider` naming the provider when the set holds a copy reached through one.

    No query unless a set holds two different URLs; three batched queries when one does."""
    parent = {ch.id: ch.id for ch in channels}

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_by_key: dict = {}
    for ch in channels:
        for key in (('url', _url_key(ch.stream_url)), ('cluster', ch.duplicate_cluster_id)):
            if key[1] is None:
                continue
            first = first_by_key.setdefault(key, ch.id)
            if first != ch.id:
                parent[root(ch.id)] = root(first)

    grouped: dict = {}
    for ch in channels:
        grouped.setdefault(root(ch.id), []).append(ch)
    members_list = [m for m in grouped.values() if len(m) > 1]
    if not members_list:
        return []

    def same_url(a, b):
        return _url_key(a.stream_url) is not None and a.stream_url == b.stream_url

    def related(a, b):
        return same_url(a, b) or (a.duplicate_cluster_id is not None
                                  and a.duplicate_cluster_id == b.duplicate_cluster_id)

    by_cluster = [m for m in members_list if len({ch.stream_url for ch in m}) > 1]
    accounts, providers, siblings = {}, {}, {}
    if by_cluster:
        account_ids = {ch.account_id for m in by_cluster for ch in m}
        accounts = {a.id: a for a in db.session.query(Account.id, Account.provider_id)
                    .filter(Account.id.in_(account_ids))}
        provider_ids = {a.provider_id for a in accounts.values() if a.provider_id is not None}
        if provider_ids:
            providers = dict(db.session.query(Provider.id, Provider.name)
                             .filter(Provider.id.in_(provider_ids)))
            from .account_links import login_siblings
            siblings = login_siblings(account_ids)

    def provider_of(ch):
        acct = accounts.get(ch.account_id)
        return providers.get(acct.provider_id) if acct is not None else None

    out = []
    for members in members_list:
        lead = (lead_of(members) if lead_of is not None else None) or members[0]
        copies = {}
        set_provider = None
        for ch in members:
            if ch is lead:
                continue
            # Told against the lead when the two match directly; a copy that joined the set
            # through another copy is told against the first one it does match, because
            # "a duplicate of the lead" would then be a claim nothing measured.
            of = lead if related(ch, lead) else next(o for o in members
                                                     if o is not ch and related(ch, o))
            if same_url(ch, of):
                situation, provider = COPY_SAME_URL, None
            else:
                provider = provider_of(ch)
                if ch.account_id == of.account_id:
                    situation = COPY_SAME_ACCOUNT
                elif of.account_id in siblings.get(ch.account_id, {}):
                    situation = COPY_SAME_LOGIN
                else:
                    situation = COPY_OTHER_LOGIN
                set_provider = set_provider or provider
            copies[ch.id] = {'of': of, 'situation': situation, 'provider': provider}
        out.append({'channels': members, 'lead': lead, 'provider': set_provider,
                    'copies': copies})
    return out


def copy_note(situation, provider) -> str:
    """How one copy duplicates another, in a few words - the one spelling, for the group
    page's badge. The situation is kept because it is what tells a real backup (another
    login) from one that covers nothing (the same login or account)."""
    if situation == COPY_SAME_URL:
        return 'Same stream URL.'
    where = f'Same channel on {provider}' if provider else 'Same channel'
    if situation == COPY_OTHER_LOGIN:
        return f'{where}, through a different login.'
    if situation == COPY_SAME_LOGIN:
        return f'{where}, on the same login.'
    if situation == COPY_SAME_ACCOUNT:
        return f'{where}, on the same account.'
    raise ValueError(f'unknown copy situation {situation!r}')
