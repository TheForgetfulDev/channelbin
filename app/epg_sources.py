"""EPG sources: where a channel's guide comes from (DESIGN-epg-sources.md).

Canonical home for what a source is, a channel's key into a source's file, which source
wins a channel, the source directory, and moving a channel's listings between the active
table (`epg_entries`) and the alternates table when the winner changes. The import itself
is `accounts.import_source()`, which calls in here; nothing in this module fetches or
parses a feed.

The two user-owned EPG facts - `EpgChannelKey` and `Channel.epg_source_override_id` - are
read here and never written here by anything a sync reaches (DESIGN-epg-sources.md §6).
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta
from typing import NamedTuple
from urllib.parse import urlsplit

from sqlalchemy import delete, func, insert, literal, select, union_all, update

from . import db
from .database import (Account, Channel, ChannelEvent, CHANNEL_EPG_KEY_CHANGED,
                       CHANNEL_EPG_SOURCE_CHANGED, CHANNEL_EPG_SOURCE_OVERRIDE_CHANGED, EPGEntry, EpgAlternateEntry, EpgChannelKey,
                       EpgSource, EpgSourceChannel, EpgSourceSubscription, EPG_KEY_ACCEPTED,
                       EPG_KEY_ORIGIN_MANUAL, EPG_KEY_ORIGIN_NAME_MATCH, EPG_KEY_REJECTED,
                       EPG_SOURCE_PROVIDER, EPG_SOURCE_URL, EPG_STATUS_FAILED, EPG_STATUS_OK,
                       EPG_STATUS_REFUSED, EPG_STATUS_TRUNCATED)
from .db_utils import retry_on_locked

log = logging.getLogger(__name__)

#: A source's coverage of a channel is a real schedule at this many distinct titles in the
#: import window (dev/changelog/806's threshold, re-measured in DESIGN-epg-sources.md §2.3).
REAL_SCHEDULE_MIN_TITLES = 3
#: The directory stores distinct titles capped here: the only questions asked of the
#: number are "one?" and "three or more?", and the cap bounds the import's memory.
DISTINCT_TITLE_CAP = 20
#: Programs per channel the directory keeps for the review page to show (§7.5).
UPCOMING_TITLES = 3

#: Chunk size for any IN (...) over channel ids built in Python - well under SQLite's
#: SQLITE_MAX_VARIABLE_NUMBER, and small enough that one retry_on_locked unit stays short.
_CHUNK = 500

# Why a channel's guide comes from its source - resolve_active_source()'s second value.
REASON_OVERRIDE = 'override'            # the user's per-channel override
REASON_PRIORITY = 'priority'            # first in priority order with a real schedule
REASON_REAL_SCHEDULE = 'real-schedule'  # a higher-priority source only listed one title
REASON_ONLY_LISTING = 'only-listing'    # nothing has a real schedule; first that covers it
REASON_NONE = 'none'                    # no subscribed source covers the channel
RESOLVE_REASONS = (REASON_OVERRIDE, REASON_PRIORITY, REASON_REAL_SCHEDULE,
                   REASON_ONLY_LISTING, REASON_NONE)


class Coverage(NamedTuple):
    """One source's coverage of one channel, read from the source directory."""
    entry_count: int
    distinct_titles: int

    @property
    def real_schedule(self) -> bool:
        return self.distinct_titles >= REAL_SCHEDULE_MIN_TITLES


_NAME_SEPARATORS = re.compile(r'[|:/\\\-_.,()\[\]#*]+')
_WHITESPACE = re.compile(r'\s+')


def normalize_name(name: str | None) -> str:
    """The channel-name normalization name matching compares under (DESIGN-epg-sources.md
    §7.3): casefold, each run of the separators ``| : / \\ - _ . , ( ) [ ] # *`` to one space,
    whitespace collapsed and stripped.

    Measured against account 4 and the external file (§2.4):

    ============================================  =======  ==============================
    rule                                          matches  false-match hazard seen
    ============================================  =======  ==============================
    casefold + collapse whitespace (exact)             32  none
    casefold + keep only ``[a-z0-9]`` (loose)       3,247  ``ΕΛΛΗΝΙΚΟΣ FM`` -> ``FM``
    this rule (separators to a space)               2,394  none found
    ============================================  =======  ==============================

    Do not "improve" this into the loose rule: stripping to ``[a-z0-9]`` deletes every
    non-Latin letter, so a Greek station becomes "FM" and matches a channel called FM. The
    separators are the delimiters providers disagree on (``US| A&E HD`` vs ``US: A&E HD``);
    letters of every script survive.
    """
    if not name:
        return ''
    text = _NAME_SEPARATORS.sub(' ', name.casefold())
    return _WHITESPACE.sub(' ', text).strip()


def norm_key(key: str, case_sensitive: bool) -> str:
    """How an XMLTV channel id is compared: exact, or lower-cased unless
    sync.epg_case_sensitive_matching. Applied once at map build and once at lookup, never
    as an exact-then-fallback chain."""
    return key if case_sensitive else key.lower()


def default_source_name(account_name: str, kind: str) -> str:
    if kind == EPG_SOURCE_PROVIDER:
        return f'{account_name} provider guide'
    if kind == EPG_SOURCE_URL:
        return f'{account_name} XMLTV'
    raise ValueError(f'unknown EPG source kind {kind!r}')


def provider_xmltv_url(account: Account) -> str:
    """The Xtream `xmltv.php` URL for a provider source, built at fetch time from the owner's
    credentials so a credential change needs no second edit (DESIGN-epg-sources.md §3). The
    API endpoint, never server_info - EPG is account data. Secret in full."""
    base = (account.base_url or '').rstrip('/')
    return f'{base}/xmltv.php?username={account.username}&password={account.password}'


def _next_priority(account_id: int) -> int:
    top = db.session.query(func.max(EpgSourceSubscription.priority)).filter(
        EpgSourceSubscription.account_id == account_id).scalar()
    return (top or 0) + 1


def ensure_provider_source(account: Account) -> EpgSource:
    """The Xtream account's one provider source, created with the owner subscribed at the
    end of its priority order if it has none. Does not commit - the caller owns the unit.

    Creating it is not writing a user's answer: every Xtream account has exactly one
    (DESIGN-epg-sources.md §3). An existing source is returned untouched, subscribed or not
    - an unsubscribed provider source is the user's choice and is never re-subscribed."""
    source = EpgSource.query.filter_by(owner_account_id=account.id,
                                       kind=EPG_SOURCE_PROVIDER).first()
    if source is not None:
        return source
    source = EpgSource(kind=EPG_SOURCE_PROVIDER, owner_account_id=account.id,
                       name=default_source_name(account.name, EPG_SOURCE_PROVIDER))
    db.session.add(source)
    db.session.flush()
    db.session.add(EpgSourceSubscription(source_id=source.id, account_id=account.id,
                                         priority=_next_priority(account.id)))
    return source


def sources_refreshed_by_sync(account_id: int) -> list[EpgSource]:
    """The sources an account's sync refreshes: owned, enabled, and on no interval of their
    own (DESIGN-epg-sources.md §8.1), in id order so a sync's work is reproducible."""
    return (EpgSource.query.filter(EpgSource.owner_account_id == account_id,
                                   EpgSource.enabled.is_(True),
                                   EpgSource.refresh_interval_hours.is_(None))
            .order_by(EpgSource.id).all())


def subscriptions_for(account_ids) -> dict[int, list[int]]:
    """{account_id: [source_id, ...]} in priority order. One query."""
    out: dict[int, list[int]] = {aid: [] for aid in account_ids}
    if not out:
        return out
    rows = (db.session.query(EpgSourceSubscription.account_id, EpgSourceSubscription.source_id)
            .filter(EpgSourceSubscription.account_id.in_(list(out)))
            .order_by(EpgSourceSubscription.account_id, EpgSourceSubscription.priority))
    for account_id, source_id in rows:
        out[account_id].append(source_id)
    return out


def subscriber_ids(source_id: int) -> list[int]:
    return [aid for (aid,) in db.session.query(EpgSourceSubscription.account_id)
            .filter(EpgSourceSubscription.source_id == source_id)]


def accepted_keys(source_ids) -> dict[tuple[int, int], str]:
    """{(channel_id, source_id): key} for every accepted user key on these sources.

    Queried by source rather than by channel: the table holds only the user's own answers,
    so it is small, where a channel IN (...) over a large account would not be."""
    source_ids = list(source_ids)
    if not source_ids:
        return {}
    rows = (db.session.query(EpgChannelKey.channel_id, EpgChannelKey.source_id,
                             EpgChannelKey.key)
            .filter(EpgChannelKey.source_id.in_(source_ids),
                    EpgChannelKey.status == EPG_KEY_ACCEPTED,
                    EpgChannelKey.key.isnot(None), EpgChannelKey.key != ''))
    return {(ch, src): key for ch, src, key in rows}


def channel_key(epg_channel_id: str | None, user_key: str | None) -> str | None:
    """key(channel, source) (DESIGN-epg-sources.md §7.1): the user's accepted key for this
    source if there is one, else the provider's id, else None (never matched)."""
    return user_key or epg_channel_id or None


def directory_coverage(source_ids, case_sensitive: bool) -> dict[int, dict[str, Coverage]]:
    """{source_id: {normalized xml_id: Coverage}} from the source directory - never a count
    over epg_entries (DESIGN-epg-sources.md §7.4). Case-variant ids that fold together under
    case-insensitive matching are merged, exactly as the importer's channel map merges them."""
    out: dict[int, dict[str, Coverage]] = {sid: {} for sid in source_ids}
    if not out:
        return out
    rows = (db.session.query(EpgSourceChannel.source_id, EpgSourceChannel.xml_id,
                             EpgSourceChannel.entry_count, EpgSourceChannel.distinct_titles)
            .filter(EpgSourceChannel.source_id.in_(list(out)),
                    EpgSourceChannel.entry_count > 0))
    for source_id, xml_id, entries, titles in rows:
        k = norm_key(xml_id, case_sensitive)
        prev = out[source_id].get(k)
        if prev is None:
            out[source_id][k] = Coverage(entries, titles)
        else:
            out[source_id][k] = Coverage(prev.entry_count + entries,
                                         max(prev.distinct_titles, titles))
    return out


def resolve_active_source(override_id: int | None, ordered_source_ids,
                          coverage: dict[int, Coverage]) -> tuple[int | None, str]:
    """Which source a channel's guide comes from, and why (DESIGN-epg-sources.md §5.1).

    `ordered_source_ids` is the channel's account's subscriptions in priority order;
    `coverage` is {source_id: Coverage} for the sources that cover the channel at all.
    Whole-channel only - one winner, never a spliced timeline.

    1. The override, if the account reads that source and it covers the channel right now.
       If not, fall through; the override is never cleared here (it is the user's, and
       comes back into force the moment both hold again).
    2. The first subscribed source, in priority order, with a real schedule.
    3. Otherwise the first that covers the channel at all.
    4. Otherwise none.

    An override on a source the account stopped reading must not win: Stop using deleted
    that source's listings for the account's channels, and the source's directory - which
    is what `coverage` is read from - outlives the subscription (dev/changelog/1107).
    """
    if (override_id is not None and override_id in coverage
            and override_id in ordered_source_ids):
        return override_id, REASON_OVERRIDE
    covering = [sid for sid in ordered_source_ids if sid in coverage]
    if not covering:
        return None, REASON_NONE
    for sid in covering:
        if coverage[sid].real_schedule:
            return sid, (REASON_PRIORITY if sid == covering[0] else REASON_REAL_SCHEDULE)
    return covering[0], REASON_ONLY_LISTING


# ── Directory ─────────────────────────────────────────────────────────────────


class DirectoryRow(NamedTuple):
    xml_id: str
    display_names: list
    entry_count: int
    distinct_titles: int
    sole_title: str | None
    horizon_until: object   # datetime | None
    upcoming: tuple = ()    # ((start datetime, title), ...), soonest first


def write_directory(source_id: int, rows) -> None:
    """Replace a source's directory in full (DESIGN-epg-sources.md §7.4) - it is a cache of
    the file's <channel> block plus the counts the import pass already makes."""
    mappings = [{'source_id': source_id, 'xml_id': r.xml_id[:255],
                 'display_names': json.dumps(r.display_names) if r.display_names else None,
                 'entry_count': r.entry_count, 'distinct_titles': r.distinct_titles,
                 'sole_title': (r.sole_title or '')[:512] or None,
                 'horizon_until': r.horizon_until,
                 'upcoming_titles': json.dumps(
                     [[start.isoformat(), (title or '')[:512]] for start, title in r.upcoming])
                 if r.upcoming else None} for r in rows]

    @retry_on_locked()
    def _rewrite_and_commit():
        EpgSourceChannel.query.filter_by(source_id=source_id).delete(
            synchronize_session=False)
        if mappings:
            db.session.bulk_insert_mappings(EpgSourceChannel, mappings)
        db.session.commit()

    _rewrite_and_commit()


# ── Moving a channel's listings between the two tables ───────────────────────

_ENTRY_COLUMNS = ('channel_id', 'title', 'sub_title', 'description', 'start_time',
                  'stop_time', 'category', 'rating', 'source_id')


def _chunks(ids):
    ids = list(ids)
    for i in range(0, len(ids), _CHUNK):
        yield ids[i:i + _CHUNK]


def _of_source(model, source_id: int):
    """`model.source_id == source_id`, for a statement that also names its channels.

    On epg_entries the source is compared as `source_id + 0`, which keeps SQLite off
    ix_epg_entries_source: given both an equal source and an equal channel it picks that
    index, and walks every row the source holds - 195,589 for one provider feed - once
    per statement, where a channel index reaches only the channels' own rows.
    Measured copying one feed's listings to 3,795 channels: 682 s through the source
    index (dev/changelog/1106). The alternates table's index leads with the source and
    then the channel, so it is used as is. Never for a statement over a whole source."""
    if model is EPGEntry:
        return (model.source_id + 0) == source_id
    return model.source_id == source_id


def _move_rows(channel_ids, source_id: int, src_model, dst_model) -> int:
    """The statements of one move, without committing: INSERT ... SELECT then DELETE."""
    src_cols = [getattr(src_model, c) for c in _ENTRY_COLUMNS]
    dst_cols = [getattr(dst_model, c) for c in _ENTRY_COLUMNS]
    where = (_of_source(src_model, source_id), src_model.channel_id.in_(list(channel_ids)))
    db.session.execute(insert(dst_model).from_select(dst_cols, select(*src_cols).where(*where)))
    return db.session.execute(delete(src_model).where(*where)).rowcount


def _move(channel_ids, source_id: int, src_model, dst_model) -> int:
    """Move one source's rows for these channels from one table to the other. One
    retry_on_locked unit per chunk: the INSERT ... SELECT and the DELETE commit together, so
    a lock retry re-runs both and a row is never in both tables or in neither."""
    moved = 0
    for chunk in _chunks(channel_ids):
        @retry_on_locked()
        def _move_chunk_and_commit(chunk=chunk):
            n = _move_rows(chunk, source_id, src_model, dst_model)
            db.session.commit()
            return n
        moved += _move_chunk_and_commit()
    return moved


def _holding_alternates(source_ids, channel_ids) -> set[tuple[int, int]]:
    """{(source_id, channel_id)} for the pairs where the alternates hold at least one of
    the source's rows for the channel. One query per source per chunk, on the table's
    (source_id, channel_id) index."""
    out: set[tuple[int, int]] = set()
    for sid in source_ids:
        for chunk in _chunks(channel_ids):
            out.update((sid, cid) for (cid,) in db.session.query(EpgAlternateEntry.channel_id)
                       .filter(EpgAlternateEntry.source_id == sid,
                               EpgAlternateEntry.channel_id.in_(chunk)).distinct())
    return out


def sources_without_directory(source_ids) -> set[int]:
    """The sources among these with no directory: never refreshed successfully.

    Asked of the directory itself, never of `last_status`. A source whose every refresh
    failed has a status and no directory, and read as "refreshed" it covered nothing, so
    borrowing a guide moved 616 channels off a provider whose feed had started answering
    404 (dev/docs/BUGS.md 2026-09-23 @ 02:31:03 PM)."""
    source_ids = [s for s in source_ids if s is not None]
    if not source_ids:
        return set()
    return {sid for (sid,) in db.session.query(EpgSource.id).filter(
        EpgSource.id.in_(source_ids),
        ~select(EpgSourceChannel.id).where(EpgSourceChannel.source_id == EpgSource.id)
        .exists())}


def has_directory(source_id: int) -> bool:
    return not sources_without_directory([source_id])


def held_coverage(source_ids, channel_ids) -> dict[int, dict[int, Coverage]]:
    """{source_id: {channel_id: Coverage}} counted from the rows the database holds, in
    either table - for sources with no directory yet (sources_without_directory()).

    An empty directory says nothing about what such a source covers: every provider source
    migrated by m072 holds its listings but has no directory until its first successful
    refresh, which with syncing switched off, or a feed that has stopped answering, may be
    never. Read as "covers nothing", resolution moved
    those channels onto any other source, or left them with no guide
    (dev/changelog/1107). Rows are the only evidence there is, and they are what the
    guide shows. A statement per source per chunk per table."""
    out: dict[int, dict[int, Coverage]] = {sid: {} for sid in source_ids}
    for sid in out:
        for chunk in _chunks(channel_ids):
            for model in (EPGEntry, EpgAlternateEntry):
                for cid, n, titles in (
                        db.session.query(model.channel_id, func.count(model.id),
                                         func.count(func.distinct(model.title)))
                        .filter(_of_source(model, sid), model.channel_id.in_(chunk))
                        .group_by(model.channel_id)):
                    prev = out[sid].get(cid)
                    out[sid][cid] = (Coverage(n, titles) if prev is None else Coverage(
                        prev.entry_count + n, max(prev.distinct_titles, titles)))
    return out


def add_held_coverage(cov: dict[int, Coverage], channel_id: int,
                      held: dict[int, dict[int, Coverage]], allowed) -> None:
    """Add a channel's held-rows coverage (held_coverage()) for the sources in `allowed`
    to its per-source coverage, in place. The directory, where there is one, wins."""
    for sid, per in held.items():
        if sid in allowed and channel_id in per:
            cov.setdefault(sid, per[channel_id])


def _channel_coverage(channel: Channel, source_ids, directory) -> dict[int, Coverage]:
    """One channel's {source_id: Coverage}: the directory, plus held rows for the sources
    with no directory yet."""
    cov = _coverage_of(directory)
    unrefreshed = sources_without_directory(source_ids)
    if unrefreshed:
        add_held_coverage(cov, channel.id, held_coverage(unrefreshed, [channel.id]),
                          unrefreshed)
    return cov


def demote(channel_ids, source_id: int) -> int:
    """A source stopped being these channels' winner: its rows go to the alternates."""
    return _move(channel_ids, source_id, EPGEntry, EpgAlternateEntry)


def promote(channel_ids, source_id: int) -> int:
    """A source became these channels' winner: its kept rows come back to epg_entries."""
    return _move(channel_ids, source_id, EpgAlternateEntry, EPGEntry)


def drop_alternates(channel_ids, source_id: int) -> None:
    """Alternates are kept only for channels with an active source (DESIGN-epg-sources.md
    §4): a channel with no winner would otherwise collect every source's leftovers."""
    for chunk in _chunks(channel_ids):
        @retry_on_locked()
        def _drop_and_commit(chunk=chunk):
            EpgAlternateEntry.query.filter(
                EpgAlternateEntry.source_id == source_id,
                EpgAlternateEntry.channel_id.in_(chunk)).delete(synchronize_session=False)
            db.session.commit()
        _drop_and_commit()


def _source_change_event(ch_id: int, old: int, new: int, source_names: dict[int, str],
                         reason: str) -> dict:
    return {'channel_id': ch_id, 'event_type': CHANNEL_EPG_SOURCE_CHANGED,
            'detail': (f'Guide now from {source_names.get(new, f"source {new}")} '
                       f'(was {source_names.get(old, f"source {old}")}): {reason}')}


def apply_winners(changes: dict[int, tuple[int | None, int | None]],
                  source_names: dict[int, str], reasons: dict[int, str]) -> None:
    """Write `Channel.epg_source_id` for every channel whose winner changed, and log the
    moves between two sources (DESIGN-epg-sources.md §5.4). `changes` is
    {channel_id: (old_source_id, new_source_id)}; the row moves are the caller's, since only
    it knows which table the incoming source's rows are about to land in."""
    by_new: dict[int | None, list[int]] = {}
    for ch_id, (_old, new) in changes.items():
        by_new.setdefault(new, []).append(ch_id)
    for new, ids in by_new.items():
        for chunk in _chunks(ids):
            @retry_on_locked()
            def _set_and_commit(chunk=chunk, new=new):
                db.session.execute(update(Channel).where(Channel.id.in_(chunk))
                                   .values(epg_source_id=new)
                                   .execution_options(synchronize_session=False))
                db.session.commit()
            _set_and_commit()

    events = [_source_change_event(ch_id, old, new, source_names,
                                   reasons.get(ch_id, REASON_PRIORITY))
              for ch_id, (old, new) in changes.items() if old is not None and new is not None]
    for i in range(0, len(events), _CHUNK):
        batch = events[i:i + _CHUNK]

        @retry_on_locked()
        def _log_and_commit(batch=batch):
            db.session.bulk_insert_mappings(ChannelEvent, batch)
            db.session.commit()
        _log_and_commit()


# ── The user's key (DESIGN-epg-sources.md §6.1) ──────────────────────────────

#: What saving a key did to the channel's listings, recorded on its CHANNEL_EPG_KEY_CHANGED
#: event. The first three are done; the rest take effect at the source's next refresh.
KEY_COPIED = 'copied'              # another channel already held the key's listings
KEY_SAME_LISTINGS = 'same'         # the new key matches what the old one did
KEY_HIDDEN = 'hidden'              # hidden channels import nothing from any source
KEY_WAITS = 'waits'                # in the file, but no channel holds its listings
KEY_NOT_IN_FILE = 'not-in-file'    # not in the source's last refresh
KEY_NO_KEY = 'no-key'              # cleared with no provider id to fall back to
KEY_UNREFRESHED = 'unrefreshed'    # the source has no directory yet
KEY_PENDING_OUTCOMES = (KEY_WAITS, KEY_NOT_IN_FILE, KEY_NO_KEY, KEY_UNREFRESHED)


class KeyChange(NamedTuple):
    outcome: str    # KEY_*
    detail: str     # the CHANNEL_EPG_KEY_CHANGED event's text


def _key_outcome_text(outcome: str, source_name: str, donor_name: str | None = None) -> str:
    return {
        KEY_COPIED: f'listings copied from {donor_name}, which already carries it',
        KEY_SAME_LISTINGS: 'it matches the same listings as before',
        KEY_HIDDEN: 'the channel is hidden, so no listings are imported for it',
        KEY_WAITS: f'listings arrive at the next refresh of {source_name}',
        KEY_NOT_IN_FILE: (f"not in {source_name}'s last refresh, so it matches nothing "
                          'until it appears there'),
        KEY_NO_KEY: f'the channel matches nothing in {source_name} after its next refresh',
        KEY_UNREFRESHED: f'{source_name} has no successful refresh yet; applies at its first one',
    }[outcome]


def _channel_directory(channel: Channel, source_ids, case_sensitive: bool):
    """For one channel over these sources: the user's accepted keys {source_id: (key,
    origin)}, the key each source is matched on {source_id: key} (§7.1), and the directory
    row that key hits in each source {source_id: EpgSourceChannel} - the largest, when
    case-variant ids fold together. Three queries whatever the source count."""
    source_ids = list(source_ids)
    keys = {k.source_id: (k.key, k.origin) for k in EpgChannelKey.query.filter(
        EpgChannelKey.channel_id == channel.id, EpgChannelKey.status == EPG_KEY_ACCEPTED,
        EpgChannelKey.source_id.in_(source_ids))} if source_ids else {}
    wanted = {}
    for sid in source_ids:
        k = channel_key(channel.epg_channel_id, keys.get(sid, (None,))[0])
        if k:
            wanted[sid] = k
    directory = {}
    if wanted:
        rows = EpgSourceChannel.query.filter(EpgSourceChannel.source_id.in_(list(wanted))).filter(
            func.lower(EpgSourceChannel.xml_id).in_({k.lower() for k in wanted.values()})).all()
        for r in rows:
            if norm_key(r.xml_id, case_sensitive) == norm_key(wanted[r.source_id],
                                                              case_sensitive):
                prev = directory.get(r.source_id)
                if prev is None or r.entry_count > prev.entry_count:
                    directory[r.source_id] = r
    return keys, wanted, directory


def _coverage_of(directory) -> dict[int, Coverage]:
    return {sid: Coverage(r.entry_count, r.distinct_titles)
            for sid, r in directory.items() if r.entry_count > 0}


def _key_donor(channel: Channel, source: EpgSource, key: str, case_sensitive: bool):
    """A visible channel on an account reading `source` whose key into it is `key` and which
    holds that source's listings right now, as (channel id, name, table model), or None.

    Its rows are exactly what the next import would write for `channel`: the importer maps
    every channel sharing a key onto the same programs (§7.1)."""
    subscribers = subscriber_ids(source.id)
    if not subscribers:
        return None
    lowered = key.lower()
    by_provider = {cid for (cid,) in db.session.query(Channel.id).filter(
        Channel.account_id.in_(subscribers), Channel.hidden.is_(False), Channel.id != channel.id,
        func.lower(Channel.epg_channel_id) == lowered)}
    by_user = {cid for (cid,) in db.session.query(EpgChannelKey.channel_id).filter(
        EpgChannelKey.source_id == source.id, EpgChannelKey.status == EPG_KEY_ACCEPTED,
        EpgChannelKey.channel_id != channel.id, func.lower(EpgChannelKey.key) == lowered)}
    ids = sorted(by_provider | by_user)
    if not ids:
        return None
    user_keys = {ch: k for (ch, _src), k in accepted_keys([source.id]).items() if ch in ids}
    wanted = norm_key(key, case_sensitive)
    subscribed = set(subscribers)
    for cid, name, provider_id, hidden, account_id in (
            db.session.query(Channel.id, Channel.name, Channel.epg_channel_id, Channel.hidden,
                             Channel.account_id)
            .filter(Channel.id.in_(ids)).order_by(Channel.id)):
        if hidden or account_id not in subscribed:
            continue
        k = channel_key(provider_id, user_keys.get(cid))
        if not k or norm_key(k, case_sensitive) != wanted:
            continue
        for model in (EPGEntry, EpgAlternateEntry):
            if db.session.query(model.id).filter(_of_source(model, source.id),
                                                 model.channel_id == cid).first():
                return cid, name, model
    return None


def _apply_key_now(channel: Channel, source: EpgSource, old_key: str | None,
                   new_key: str | None, case_sensitive: bool) -> tuple[str, str | None]:
    """Bring the channel's listings from `source` in line with its new key where the
    database already holds them, without committing. Returns (outcome, donor name).

    Only a copy is done here, never a fetch: programs for an id no visible channel carries
    are dropped at import, so those arrive at the source's next refresh - the user may be
    rekeying many channels, and one refresh then serves them all. Until then the old
    listings stay, and the channel page says the key is waiting."""
    if channel.hidden:
        return KEY_HIDDEN, None
    if (old_key and new_key
            and norm_key(old_key, case_sensitive) == norm_key(new_key, case_sensitive)):
        return KEY_SAME_LISTINGS, None
    if not new_key:
        return KEY_NO_KEY, None
    if not has_directory(source.id):
        return KEY_UNREFRESHED, None
    order = subscriptions_for([channel.account_id]).get(channel.account_id, [])
    _keys, _wanted, directory = _channel_directory(channel, order, case_sensitive)
    coverage = _channel_coverage(channel, order, directory)
    if source.id not in coverage:
        return KEY_NOT_IN_FILE, None
    donor = _key_donor(channel, source, new_key, case_sensitive)
    if donor is None:
        return KEY_WAITS, None
    donor_id, donor_name, donor_model = donor

    old_winner = channel.epg_source_id
    winner, why = resolve_active_source(channel.epg_source_override_id, order, coverage)
    if old_winner is not None and old_winner not in (winner, source.id):
        _move_rows([channel.id], old_winner, EPGEntry, EpgAlternateEntry)
    for model in (EPGEntry, EpgAlternateEntry):
        db.session.execute(delete(model).where(_of_source(model, source.id),
                                               model.channel_id == channel.id))
    dst = EPGEntry if winner == source.id else EpgAlternateEntry
    src_cols = [literal(channel.id) if c == 'channel_id' else getattr(donor_model, c)
                for c in _ENTRY_COLUMNS]
    db.session.execute(insert(dst).from_select(
        [getattr(dst, c) for c in _ENTRY_COLUMNS],
        select(*src_cols).where(_of_source(donor_model, source.id),
                                donor_model.channel_id == donor_id)))
    if winner is not None and winner not in (old_winner, source.id):
        _move_rows([channel.id], winner, EpgAlternateEntry, EPGEntry)
    _set_winner(channel, old_winner, winner, why)
    return KEY_COPIED, f'{donor_name} (#{donor_id})'


def _set_winner(channel: Channel, old_winner: int | None, winner: int | None,
                why: str) -> None:
    """Store a single channel's new winner and log a move between two sources (§5.4), after
    the caller has moved the rows. Does not commit."""
    if winner == old_winner:
        return
    channel.epg_source_id = winner
    if old_winner is not None and winner is not None:
        names = dict(db.session.query(EpgSource.id, EpgSource.name).filter(
            EpgSource.id.in_([old_winner, winner])))
        db.session.add(ChannelEvent(**_source_change_event(
            channel.id, old_winner, winner, names, why)))


def set_channel_key(channel: Channel, source: EpgSource, key: str | None, *,
                    case_sensitive: bool, origin: str = EPG_KEY_ORIGIN_MANUAL,
                    matched_on: str | None = None) -> KeyChange | None:
    """The one writer of the user's per-channel, per-source key (DESIGN-epg-sources.md
    §6.1). A blank `key` clears it, and the channel goes back to the provider's id. Writes
    CHANNEL_EPG_KEY_CHANGED and applies what it can at once (`_apply_key_now`). Does not
    commit - the caller owns the unit, as with channel_groups.set_participation().

    Returns the outcome (KEY_*) and the event's text, or None when nothing changed. A rejected row is left alone
    by a clear: it records a name-match refusal, not a key."""
    key = (key or '').strip() or None
    row = EpgChannelKey.query.filter_by(channel_id=channel.id, source_id=source.id).first()
    user_key = row.key if row is not None and row.status == EPG_KEY_ACCEPTED else None
    if key == user_key and (key is None or row.origin == origin):
        return None
    old_key = channel_key(channel.epg_channel_id, user_key)
    if key is None:
        db.session.delete(row)
    elif row is None:
        db.session.add(EpgChannelKey(channel_id=channel.id, source_id=source.id, key=key,
                                     origin=origin, status=EPG_KEY_ACCEPTED,
                                     matched_on=matched_on))
    else:
        row.key = key
        row.origin, row.status, row.matched_on = origin, EPG_KEY_ACCEPTED, matched_on
    db.session.flush()
    new_key = channel_key(channel.epg_channel_id, key)
    outcome, donor = _apply_key_now(channel, source, old_key, new_key, case_sensitive)

    was = (f'your key "{user_key}"' if user_key
           else f'the provider id "{channel.epg_channel_id}"' if channel.epg_channel_id
           else 'no key')
    if key is None:
        now = (f'back to the provider id "{channel.epg_channel_id}"' if channel.epg_channel_id
               else 'no key left')
        what = f'EPG key for {source.name} cleared, {now} (was {was})'
    else:
        what = f'EPG key for {source.name} set to "{key}" (was {was})'
    detail = f'{what}: {_key_outcome_text(outcome, source.name, donor)}'
    db.session.add(ChannelEvent(
        channel_id=channel.id, event_type=CHANNEL_EPG_KEY_CHANGED, detail=detail,
        extra_data=json.dumps({'source_id': source.id, 'key': key, 'previous': user_key,
                               'origin': origin, 'outcome': outcome})))
    return KeyChange(outcome, detail)


# ── The user's source override (DESIGN-epg-sources.md §6.2) ────────────────────

#: Where an override stands, for the channel page (channel_guide_view()).
OVERRIDE_IN_FORCE = 'in-force'        # it decides the channel's guide right now
OVERRIDE_WAITING = 'waiting'          # it will; the source's rows arrive at its next refresh
OVERRIDE_NO_LISTINGS = 'no-listings'  # the source has nothing for this channel right now
OVERRIDE_NOT_READ = 'not-read'        # the account stopped reading the source


class OverrideChange(NamedTuple):
    state: str | None   # OVERRIDE_*, None once cleared
    winner: int | None  # where the channel's guide comes from after the change
    detail: str         # the CHANNEL_EPG_SOURCE_OVERRIDE_CHANGED event's text


def _override_state(override_id: int | None, why: str, stored: int | None,
                    order) -> str | None:
    if override_id is None:
        return None
    if why == REASON_OVERRIDE:
        return OVERRIDE_IN_FORCE if stored == override_id else OVERRIDE_WAITING
    return OVERRIDE_NOT_READ if override_id not in order else OVERRIDE_NO_LISTINGS


def set_source_override(channel: Channel, source: EpgSource | None, *,
                        case_sensitive: bool) -> OverrideChange | None:
    """The one writer of the user's per-channel source override (§6.2); `source` None
    clears it. Writes CHANNEL_EPG_SOURCE_OVERRIDE_CHANGED, re-decides the channel's winner
    and moves its listings between the two tables (§5.4) - from rows already held, never a
    fetch. Does not commit - the caller owns the unit, as with set_channel_key(). None when
    nothing changed.

    An override on a source with no listings for the channel is stored all the same and
    falls through (§5.1): it is the user's answer, and it takes over the moment the source
    covers the channel. Where the directory says a source covers the channel but the
    alternates hold none of its rows yet (a key or subscription waiting for its refresh),
    the winner is left alone rather than pointed at a source holding nothing for it; so it
    is when nothing covers the channel at all, since an override is never a reason to take
    a guide away - the source's next import settles both."""
    new_id = source.id if source is not None else None
    old_id = channel.epg_source_override_id
    if new_id == old_id:
        return None
    channel.epg_source_override_id = new_id
    db.session.flush()
    order = subscriptions_for([channel.account_id]).get(channel.account_id, [])
    old_winner = winner = channel.epg_source_id
    why = REASON_NONE
    if not channel.hidden:
        _keys, _wanted, directory = _channel_directory(channel, order, case_sensitive)
        winner, why = resolve_active_source(new_id, order,
                                            _channel_coverage(channel, order, directory))
        if winner is None or (winner != old_winner and not db.session.query(
                EpgAlternateEntry.id).filter(_of_source(EpgAlternateEntry, winner),
                                             EpgAlternateEntry.channel_id == channel.id).first()):
            winner = old_winner
    if winner != old_winner:
        if old_winner is not None:
            _move_rows([channel.id], old_winner, EPGEntry, EpgAlternateEntry)
        if winner is not None:
            _move_rows([channel.id], winner, EpgAlternateEntry, EPGEntry)
        _set_winner(channel, old_winner, winner, why)
    state = _override_state(new_id, why, winner, order)

    names = dict(db.session.query(EpgSource.id, EpgSource.name).filter(
        EpgSource.id.in_([i for i in (new_id, old_id, winner) if i is not None])))

    def _name(sid):
        return names.get(sid, f'source {sid}')
    was = _name(old_id) if old_id is not None else 'none'
    what = (f'EPG source override set to {_name(new_id)} (was {was})' if new_id is not None
            else f'EPG source override cleared (was {was})')
    if winner is None:
        now = 'no source has listings for this channel'
    elif winner != old_winner:
        now = f'guide now from {_name(winner)}'
    else:
        now = f'guide stays from {_name(winner)}'
    if state == OVERRIDE_NO_LISTINGS:
        now += (f'; {_name(new_id)} has no listings for this channel right now and takes '
                'over when it does')
    elif state == OVERRIDE_WAITING:
        now += f"; {_name(new_id)}'s listings for it arrive at its next refresh"
    elif state == OVERRIDE_NOT_READ:
        now += f'; this account does not read {_name(new_id)}'
    detail = f'{what}: {now}'
    db.session.add(ChannelEvent(
        channel_id=channel.id, event_type=CHANNEL_EPG_SOURCE_OVERRIDE_CHANGED, detail=detail,
        extra_data=json.dumps({'source_id': new_id, 'previous': old_id})))
    return OverrideChange(state, winner, detail)


def search_directory(source_id: int, q: str, limit: int = 20) -> list[dict]:
    """The Set key dialog's lookup: one source's channels whose id or a display name
    contains `q`, most listings first. The directory holds every channel in the file,
    including those whose programs no channel carries, so this finds an id before any
    channel uses it."""
    q = (q or '').strip()
    if not q:
        return []

    def _like(text):
        return '%' + re.sub(r'([\\%_])', r'\\\1', text) + '%'
    # display_names is stored as json.dumps() output, so the needle is escaped the same way
    # to find a non-ASCII name.
    json_needle = json.dumps(q)[1:-1]
    rows = (EpgSourceChannel.query
            .filter(EpgSourceChannel.source_id == source_id,
                    db.or_(EpgSourceChannel.xml_id.ilike(_like(q), escape='\\'),
                           EpgSourceChannel.display_names.ilike(_like(json_needle),
                                                                escape='\\')))
            .order_by(EpgSourceChannel.entry_count.desc(), EpgSourceChannel.xml_id)
            .limit(limit).all())
    return [{'xml_id': r.xml_id,
             'display_names': json.loads(r.display_names) if r.display_names else [],
             'entry_count': r.entry_count or 0, 'distinct_titles': r.distinct_titles or 0,
             'sole_title': r.sole_title if r.distinct_titles == 1 else None}
            for r in rows]


# ── Numbers and teardown ─────────────────────────────────────────────────────


def refresh_source_counts(source_id: int, covered_channels: int | None = None) -> None:
    """Recompute a source's stored counts - a recompute, never an increment. Does not
    commit. `covered_channels` is passed by an import, which is the only place that knows
    it; other callers leave channel_count as it is."""
    source = db.session.get(EpgSource, source_id)
    if source is None:
        return
    source.entry_count = (
        (db.session.query(func.count(EPGEntry.id))
         .filter(EPGEntry.source_id == source_id).scalar() or 0)
        + (db.session.query(func.count(EpgAlternateEntry.id))
           .filter(EpgAlternateEntry.source_id == source_id).scalar() or 0))
    source.active_channel_count = db.session.query(func.count(Channel.id)).filter(
        Channel.epg_source_id == source_id).scalar() or 0
    if covered_channels is not None:
        source.channel_count = covered_channels


def active_counts_by_account(account_id: int) -> dict[int, int]:
    """{source_id: channels on this account whose guide comes from it}. One grouped query."""
    rows = (db.session.query(Channel.epg_source_id, func.count(Channel.id))
            .filter(Channel.account_id == account_id, Channel.epg_source_id.isnot(None))
            .group_by(Channel.epg_source_id))
    return dict(rows)


def delete_sources_for_account(account_id: int) -> None:
    """Take everything EPG-source-shaped that an account deletion must take, ahead of the
    account's own delete: the sources it owns with their rows in both tables, their
    directories, subscriptions and user keys, and the account's own subscriptions to other
    accounts' sources. Bulk statements, not the ORM cascade - a source can hold hundreds of
    thousands of rows. Does not commit; the caller's delete unit does.

    A channel on ANOTHER account that was taking its guide from a deleted source has the
    pointer cleared here, in the same commit, so a crash before the re-resolve leaves it
    with no winner rather than one that no longer exists. The caller collects those
    channels first (foreign_guided_channels), re-resolves them afterward and raises
    EPG_SOURCE_REMOVED (§9.5)."""
    owned = [sid for (sid,) in db.session.query(EpgSource.id)
             .filter(EpgSource.owner_account_id == account_id)]
    if owned:
        for model in (EPGEntry, EpgAlternateEntry, EpgSourceChannel, EpgChannelKey,
                      EpgSourceSubscription):
            db.session.execute(delete(model).where(model.source_id.in_(owned)))
        for col in (Channel.epg_source_id, Channel.epg_source_override_id):
            db.session.execute(update(Channel).where(col.in_(owned)).values({col: None})
                               .execution_options(synchronize_session=False))
        db.session.execute(delete(EpgSource).where(EpgSource.id.in_(owned)))
    db.session.execute(delete(EpgSourceSubscription)
                       .where(EpgSourceSubscription.account_id == account_id))


# ── Managing sources from the account page (DESIGN-epg-sources.md §9.2) ────────

#: A url source's own refresh interval. None (the default) refreshes it with its owner
#: account's sync (§8.1).
REFRESH_HOURS_CHOICES = (1, 2, 4, 6, 12, 24, 48)
_SOURCE_NAME_MAX = 255
_SOURCE_URL_MAX = 2048


def clean_url_source_fields(data: dict, account_name: str) -> tuple[list[str], dict]:
    """Validate the Add / Edit source form. Returns (errors, fields); `fields` is only
    meaningful when `errors` is empty. A blank name takes the default for the account."""
    def _text(key):
        v = data.get(key)
        return v.strip() if isinstance(v, str) else ''

    errors = []
    name, url = _text('name'), _text('url')
    if len(name) > _SOURCE_NAME_MAX:
        errors.append(f'The name can be at most {_SOURCE_NAME_MAX} characters.')
    if not url:
        errors.append('An XMLTV URL is required.')
    elif len(url) > _SOURCE_URL_MAX:
        errors.append(f'The XMLTV URL can be at most {_SOURCE_URL_MAX} characters.')
    else:
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            errors.append('The XMLTV URL must start with http:// or https://.')
    raw = data.get('refresh_interval_hours')
    interval = None
    if raw not in (None, '', 0, '0'):
        try:
            interval = int(raw)
        except (TypeError, ValueError):
            interval = -1
        if interval not in REFRESH_HOURS_CHOICES:
            errors.append('Choose a refresh interval from the list.')
    enabled = data.get('enabled', True)
    if not isinstance(enabled, bool):
        errors.append('"enabled" must be true or false.')
    return errors, {'name': name or default_source_name(account_name, EPG_SOURCE_URL),
                    'url': url, 'refresh_interval_hours': interval,
                    'enabled': bool(enabled)}


def add_url_source(account: Account, fields: dict) -> EpgSource:
    """A new url source owned by `account`, subscribed last in its priority order
    (move_source() reorders it). Does not commit. Its directory is empty until its
    first refresh, so the caller starts one (accounts.start_source_refresh)."""
    source = EpgSource(kind=EPG_SOURCE_URL, owner_account_id=account.id, **fields)
    db.session.add(source)
    db.session.flush()
    db.session.add(EpgSourceSubscription(source_id=source.id, account_id=account.id,
                                         priority=_next_priority(account.id)))
    return source


def update_url_source(source: EpgSource, fields: dict) -> bool:
    """Apply the Edit source form. Does not commit. Returns True when the URL changed: the
    directory then describes a different file, so the caller refreshes it."""
    url_changed = fields['url'] != source.url
    for k, v in fields.items():
        setattr(source, k, v)
    return url_changed


def _resolve_row(r, order: list[int], keys: dict, coverage: dict,
                 case_sensitive: bool, held=None) -> tuple[int | None, str]:
    """resolve_active_source() for one channel row (id, epg_channel_id, hidden,
    epg_source_override_id) over its account's `order`, from preloaded keys and directory
    coverage, plus `held` (held_coverage()) for sources with no directory yet. Hidden
    channels take nothing from any source."""
    if r.hidden:
        return None, REASON_NONE
    mine = {}
    allowed = set(order) | ({r.epg_source_override_id} if r.epg_source_override_id
                            else set())
    for sid in allowed:
        k = channel_key(r.epg_channel_id, keys.get((r.id, sid)))
        hit = coverage.get(sid, {}).get(norm_key(k, case_sensitive)) if k else None
        if hit is not None:
            mine[sid] = hit
    if held:
        add_held_coverage(mine, r.id, held, allowed)
    return resolve_active_source(r.epg_source_override_id, order, mine)


def reresolve_channels(channel_ids, case_sensitive: bool, *,
                       previous: dict[int, int] | None = None,
                       source_names: dict[int, str] | None = None) -> dict[int, int | None]:
    """Re-decide the winner for these channels from the directory, move the listings to
    match and write Channel.epg_source_id - the §5.4 moves, for a change that is not an
    import (a source deleted, or an account no longer reading one). Nothing is fetched: the
    next source's listings are already in the alternates table.

    A channel whose new winner holds none of its rows in the alternates is left as it is:
    the directory can say a source covers a channel before its listings are here (a key or
    a borrowed channel waiting for the source's next refresh), and moving the guide there
    would empty it until then (dev/changelog/1107). That source's import settles it. A
    source with no directory yet covers what it holds rows for (held_coverage()).

    `previous` names the winner a channel had when the caller has already cleared its
    pointer (the source is gone), so the CHANNEL_EPG_SOURCE_CHANGED event can still say
    what the guide came from. Returns {channel_id: new winner} for every channel whose
    stored winner changed. Commits, in chunks."""
    previous = previous or {}
    rows = []
    for chunk in _chunks(channel_ids):
        rows += db.session.query(
            Channel.id, Channel.account_id, Channel.epg_channel_id, Channel.hidden,
            Channel.epg_source_id, Channel.epg_source_override_id).filter(
            Channel.id.in_(chunk)).all()
    if not rows:
        return {}
    order = subscriptions_for({r.account_id for r in rows})
    candidates = {sid for sids in order.values() for sid in sids}
    candidates |= {r.epg_source_override_id for r in rows if r.epg_source_override_id}
    keys = accepted_keys(candidates)
    coverage = directory_coverage(candidates, case_sensitive)

    unrefreshed = sources_without_directory(candidates)
    held_rows = held_coverage(unrefreshed, [r.id for r in rows]) if unrefreshed else None
    decided = {}
    for r in rows:
        new, why = _resolve_row(r, order.get(r.account_id, []), keys, coverage,
                                case_sensitive, held_rows)
        if new != r.epg_source_id:
            decided[r.id] = (new, why)
    held = _holding_alternates({new for new, _why in decided.values() if new is not None},
                               list(decided))

    changes: dict[int, tuple[int | None, int | None]] = {}
    reasons: dict[int, str] = {}
    demotions: dict[int, list[int]] = {}
    promotions: dict[int, list[int]] = {}
    for r in rows:
        if r.id not in decided:
            continue
        new, why = decided[r.id]
        if new is not None and (new, r.id) not in held:
            continue
        changes[r.id] = (previous.get(r.id, r.epg_source_id), new)
        reasons[r.id] = why
        if r.epg_source_id is not None:
            demotions.setdefault(r.epg_source_id, []).append(r.id)
        if new is not None:
            promotions.setdefault(new, []).append(r.id)
    for sid, ids in demotions.items():
        demote(ids, sid)
    for sid, ids in promotions.items():
        promote(ids, sid)
    names = dict(source_names or {})
    names.update(db.session.query(EpgSource.id, EpgSource.name).filter(
        EpgSource.id.in_(list(promotions) + list(demotions))))
    apply_winners(changes, names, reasons)

    @retry_on_locked()
    def _recount_and_commit():
        for sid in set(promotions) | set(demotions):
            refresh_source_counts(sid)
        db.session.commit()

    _recount_and_commit()
    return {ch: new for ch, (_old, new) in changes.items()}


class SourceRemoval(NamedTuple):
    """What taking a source away from channels did to their guide."""
    affected: int    # channels whose guide came from it
    switched: int    # ... now on another source
    lost: int        # ... with no guide from any source now


def _removal_result(affected: list[int], outcome: dict[int, int | None]) -> SourceRemoval:
    switched = sum(1 for cid in affected if outcome.get(cid))
    return SourceRemoval(len(affected), switched, len(affected) - switched)


def delete_url_source(source_id: int, case_sensitive: bool) -> SourceRemoval | None:
    """Delete a url source by hand (DESIGN-epg-sources.md §9.5): its listings in both
    tables, its directory, the keys users set into it and every subscription, then move
    each channel it was the guide for onto its next source. None if it is already gone or
    is not a url source (a provider source goes with its account).

    The affected channels are collected BEFORE the delete, which is what finds them - the
    rows that answer the question are gone after it (dev/changelog/763). Their pointer is
    cleared in the same commit as the delete, so a crash before the re-resolve leaves them
    with no winner, which their next source's import resolves, never pointing at a source
    that no longer exists. The scheduler jobs and alerts are the caller's."""
    source = db.session.get(EpgSource, source_id)
    if source is None or source.kind != EPG_SOURCE_URL:
        return None
    name = source.name
    affected = [cid for (cid,) in db.session.query(Channel.id)
                .filter(Channel.epg_source_id == source_id)]

    @retry_on_locked()
    def _delete_and_commit():
        for model in (EPGEntry, EpgAlternateEntry, EpgSourceChannel, EpgChannelKey,
                      EpgSourceSubscription):
            db.session.execute(delete(model).where(model.source_id == source_id))
        for col in (Channel.epg_source_id, Channel.epg_source_override_id):
            db.session.execute(update(Channel).where(col == source_id).values({col: None})
                               .execution_options(synchronize_session=False))
        db.session.execute(delete(EpgSource).where(EpgSource.id == source_id))
        db.session.commit()

    _delete_and_commit()
    outcome = reresolve_channels(affected, case_sensitive,
                                 previous={cid: source_id for cid in affected},
                                 source_names={source_id: name})
    return _removal_result(affected, outcome)


def _disable_if_unread(source_id: int) -> None:
    """§3: a source nobody subscribes to any more is not fetched either (`enabled = 0`).
    Does not commit."""
    source = db.session.get(EpgSource, source_id)
    if source is not None and not subscriber_ids(source_id):
        source.enabled = False


def stop_using_source(account_id: int, source_id: int,
                      case_sensitive: bool) -> SourceRemoval | None:
    """The account stops reading a source it subscribes to (§3: "no subscription for
    ignoring the result"). Its listings for this account's channels go - the next import
    would not write them anyway - and each channel it was the guide for moves to its next
    source. When nobody reads the source any more it is also disabled, so the fetch is
    skipped too (§3: "enabled = 0 for skipping the fetch"). The source, its directory and
    the users' keys into it are kept, so Use again restores it. None if not subscribed."""
    sub = EpgSourceSubscription.query.filter_by(account_id=account_id,
                                                source_id=source_id).first()
    if sub is None:
        return None
    affected = [cid for (cid,) in db.session.query(Channel.id).filter(
        Channel.account_id == account_id, Channel.epg_source_id == source_id)]
    mine = select(Channel.id).where(Channel.account_id == account_id)

    @retry_on_locked()
    def _unsubscribe_and_commit():
        db.session.execute(delete(EpgSourceSubscription).where(
            EpgSourceSubscription.account_id == account_id,
            EpgSourceSubscription.source_id == source_id))
        for model in (EPGEntry, EpgAlternateEntry):
            db.session.execute(delete(model).where(model.source_id == source_id,
                                                   model.channel_id.in_(mine)))
        db.session.execute(update(Channel).where(
            Channel.account_id == account_id, Channel.epg_source_id == source_id)
            .values(epg_source_id=None).execution_options(synchronize_session=False))
        _disable_if_unread(source_id)
        refresh_source_counts(source_id)
        db.session.commit()

    _unsubscribe_and_commit()
    outcome = reresolve_channels(affected, case_sensitive,
                                 previous={cid: source_id for cid in affected})
    return _removal_result(affected, outcome)


def use_source_again(account_id: int, source_id: int) -> bool:
    """Undo stop_using_source: subscribe at the end of the account's priority order and
    enable the source. Does not commit. Its listings come back at its next refresh. False
    when already subscribed."""
    if EpgSourceSubscription.query.filter_by(account_id=account_id,
                                             source_id=source_id).first() is not None:
        return False
    source = db.session.get(EpgSource, source_id)
    if source is None:
        return False
    source.enabled = True
    db.session.add(EpgSourceSubscription(source_id=source_id, account_id=account_id,
                                         priority=_next_priority(account_id)))
    return True


class Reorder(NamedTuple):
    """What moving a source in an account's priority order did."""
    moved: bool      # False: already first (or last), nothing changed
    switched: int    # channels whose guide now comes from a different source


def move_source(account_id: int, source_id: int, step: int,
                case_sensitive: bool) -> Reorder | None:
    """Move a source one place up (`step` -1) or down (+1) in the account's priority order
    (§5.2), then re-decide every visible channel's winner and move the listings (§5.4) -
    from rows already held, since every source the account reads keeps its listings for
    these channels in one table or the other. Priorities are renumbered 1..n, closing the
    gaps Stop using leaves. None when the account does not read the source. Commits."""
    if step not in (-1, 1):
        raise ValueError(f'step must be -1 or 1, not {step!r}')

    @retry_on_locked()
    def _reorder_and_commit():
        subs = (EpgSourceSubscription.query.filter_by(account_id=account_id)
                .order_by(EpgSourceSubscription.priority).all())
        ids = [s.source_id for s in subs]
        if source_id not in ids:
            return None
        i = ids.index(source_id)
        j = i + step
        if not 0 <= j < len(subs):
            return False
        subs[i], subs[j] = subs[j], subs[i]
        # uq_epg_sub_account_priority is checked row by row, so the new numbers are reached
        # through negative ones nobody holds.
        for n, sub in enumerate(subs, 1):
            sub.priority = -n
        db.session.flush()
        for n, sub in enumerate(subs, 1):
            sub.priority = n
        db.session.commit()
        return True

    moved = _reorder_and_commit()
    if moved is None:
        return None
    if not moved:
        return Reorder(False, 0)
    visible = [cid for (cid,) in db.session.query(Channel.id).filter(
        Channel.account_id == account_id, Channel.hidden.is_(False))]
    return Reorder(True, len(reresolve_channels(visible, case_sensitive)))


# ── Another account's source (DESIGN-epg-sources.md §3, dev/changelog/1106) ────
#
# Reading a source another account owns is one subscription row - nothing is fetched twice
# and no credentials are copied. The owner refreshes it and the owner's deletion removes it;
# a reader only reads.

class ForeignReader(NamedTuple):
    """An account reading a source it does not own."""
    source_id: int
    account_id: int
    account_name: str
    guided: int      # its channels whose guide comes from the source right now


def foreign_readers(source_ids, owner_account_id: int) -> list[ForeignReader]:
    """Every account other than the owner subscribed to these sources, with how many of
    its channels take their guide from each. Two queries."""
    source_ids = list(source_ids)
    if not source_ids:
        return []
    guided = {(sid, aid): n for sid, aid, n in db.session.query(
        Channel.epg_source_id, Channel.account_id, func.count(Channel.id))
        .filter(Channel.epg_source_id.in_(source_ids),
                Channel.account_id != owner_account_id)
        .group_by(Channel.epg_source_id, Channel.account_id)}
    rows = (db.session.query(EpgSourceSubscription.source_id, Account.id, Account.name)
            .join(Account, Account.id == EpgSourceSubscription.account_id)
            .filter(EpgSourceSubscription.source_id.in_(source_ids),
                    EpgSourceSubscription.account_id != owner_account_id)
            .order_by(EpgSourceSubscription.source_id, Account.name))
    return [ForeignReader(sid, aid, name, guided.get((sid, aid), 0))
            for sid, aid, name in rows]


def foreign_guided_channels(source_ids, owner_account_id: int) -> dict[int, int]:
    """{channel_id: source_id} for channels on other accounts whose guide comes from these
    sources. A delete collects this BEFORE it runs: the pointer is what answers the
    question, and the delete clears it (dev/changelog/763)."""
    source_ids = list(source_ids)
    if not source_ids:
        return {}
    return dict(db.session.query(Channel.id, Channel.epg_source_id).filter(
        Channel.epg_source_id.in_(source_ids), Channel.account_id != owner_account_id))


def forget_reader(source_ids) -> None:
    """After an account reading these sources is deleted: recount each, and stop fetching
    any that nobody reads now (§3). Commits."""
    for sid in source_ids:
        @retry_on_locked()
        def _recount_and_commit(sid=sid):
            _disable_if_unread(sid)
            refresh_source_counts(sid)
            db.session.commit()
        _recount_and_commit()


def borrowable_sources(account_id: int, case_sensitive: bool) -> list[dict]:
    """The sources other accounts own that this one does not read, for Add source's
    "another account's guide" choice, each with what reading it would do here today: the
    channels it covers, how many would get a guide where they have none, and how many would
    switch to it from a source listing one program all day (§5.3). Resolved from the
    directories exactly as an import resolves, with the source appended last in priority
    as subscribe_to_source() appends it. A source with no directory (never refreshed
    successfully) has unknown numbers (None), not zero. Five queries, plus held_coverage()'s for a source
    this account reads that has no directory yet."""
    order = subscriptions_for([account_id])[account_id]
    cands = [s for s in EpgSource.query.filter(EpgSource.owner_account_id != account_id)
             .order_by(EpgSource.owner_account_id, EpgSource.id) if s.id not in order]
    if not cands:
        return []
    rows = db.session.query(Channel.id, Channel.epg_channel_id, Channel.hidden,
                            Channel.epg_source_override_id, Channel.epg_source_id).filter(
        Channel.account_id == account_id, Channel.hidden.is_(False)).all()
    ids = set(order) | {s.id for s in cands} | {
        r.epg_source_override_id for r in rows if r.epg_source_override_id}
    keys = accepted_keys(ids)
    coverage = directory_coverage(ids, case_sensitive)
    unrefreshed = sources_without_directory(ids)
    held_rows = held_coverage(unrefreshed, [r.id for r in rows]) if unrefreshed else None
    out = []
    for s in cands:
        covers = fills = switches = None
        if s.id not in unrefreshed:
            covers = fills = switches = 0
            trial = order + [s.id]
            for r in rows:
                k = channel_key(r.epg_channel_id, keys.get((r.id, s.id)))
                if not k or norm_key(k, case_sensitive) not in coverage[s.id]:
                    continue
                covers += 1
                new, _why = _resolve_row(r, trial, keys, coverage, case_sensitive,
                                         held_rows)
                if new == s.id:
                    if r.epg_source_id is None:
                        fills += 1
                    else:
                        switches += 1
        out.append({'id': s.id, 'name': s.name, 'kind': KIND_LABELS.get(s.kind, s.kind),
                    'is_url': s.kind == EPG_SOURCE_URL, 'enabled': bool(s.enabled),
                    'owner_id': s.owner_account_id,
                    'owner_name': s.owner.name if s.owner else None,
                    'last_success_at': s.last_success_at,
                    'covers': covers, 'fills': fills, 'switches': switches})
    return out


def _held_listings(source_id: int, case_sensitive: bool) -> dict[str, tuple[int, type]]:
    """{normalized key: (channel id, table model)} - for each key into the source, one
    visible channel on an account reading it that holds the source's listings for it now.
    Any one will do: the importer maps every channel sharing a key onto the same programs
    (§7.1), so their rows are identical."""
    readers = subscriber_ids(source_id)
    if not readers:
        return {}
    holding: dict[int, type] = {}
    for model in (EPGEntry, EpgAlternateEntry):
        for (cid,) in db.session.query(model.channel_id).filter(
                model.source_id == source_id).distinct():
            holding.setdefault(cid, model)
    if not holding:
        return {}
    user = {ch: k for (ch, _sid), k in accepted_keys([source_id]).items()}
    out: dict[str, tuple[int, type]] = {}
    for cid, provider_id in (db.session.query(Channel.id, Channel.epg_channel_id)
                             .filter(Channel.account_id.in_(readers),
                                     Channel.hidden.is_(False)).order_by(Channel.id)):
        if cid not in holding:
            continue
        k = channel_key(provider_id, user.get(cid))
        if k:
            out.setdefault(norm_key(k, case_sensitive), (cid, holding[cid]))
    return out


def _copy_listings(pairs, source_id: int, model) -> None:
    """One INSERT ... SELECT copying the source's rows in `model` from each donor channel
    to its target, [(target, donor), ...], into the alternates. Does not commit."""
    if not pairs:
        return
    mapping = union_all(*[select(literal(t).label('target'), literal(d).label('donor'))
                          for t, d in pairs]).subquery('m')
    cols = [mapping.c.target if c == 'channel_id' else getattr(model, c)
            for c in _ENTRY_COLUMNS]
    db.session.execute(insert(EpgAlternateEntry).from_select(
        [getattr(EpgAlternateEntry, c) for c in _ENTRY_COLUMNS],
        select(*cols).join(mapping, model.channel_id == mapping.c.donor)
        .where(_of_source(model, source_id))))


class Borrowed(NamedTuple):
    """What subscribing to another account's source did."""
    refreshed: bool   # False: no successful refresh, so what it covers is not known yet
    covered: int      # this account's channels its last refresh covers
    copied: int       # ... whose listings the database held and were copied at once
    guided: int       # ... for which it is now the guide
    turned_on: bool   # a provider source nobody read was switched back on

    @property
    def waiting(self) -> int:
        """Covered channels whose listings arrive at the source's next refresh."""
        return self.covered - self.copied


#: Channels copied per commit. Each is one INSERT ... SELECT of a day or three of programs.
_COPY_CHUNK = 100


def subscribe_to_source(account_id: int, source_id: int,
                        case_sensitive: bool) -> Borrowed | None:
    """Read another account's source (§3): one subscription, last in this account's
    priority order, so it gives a guide only where nothing above it covers the channel with
    a real schedule (§5.1). Nothing is fetched and no credentials are copied.

    What the database already holds is copied at once - the rule set_channel_key() follows
    (dev/changelog/1102). A channel on an existing reader whose key into the source is this
    channel's key holds exactly what the next import would write, so it is copied to the
    alternates and the channel re-resolved (§5.4 moves the rows if the source wins). A
    covered key no reader carries waits for the source's next refresh, and its channel's
    winner is left alone until then rather than pointed at a source holding nothing for it.

    A provider source is switched back on if it was off, since the only thing that turns
    one off is nobody reading it (stop_using_source); a url source's switch belongs to its
    owner and is left alone. None when the account owns the source or already reads it.
    Commits."""
    source = db.session.get(EpgSource, source_id)
    if source is None or source.owner_account_id == account_id:
        return None
    if EpgSourceSubscription.query.filter_by(account_id=account_id,
                                             source_id=source_id).first() is not None:
        return None
    name, refreshed = source.name, has_directory(source_id)
    donors = _held_listings(source_id, case_sensitive)

    @retry_on_locked()
    def _subscribe_and_commit():
        src = db.session.get(EpgSource, source_id)
        turned_on = src.kind == EPG_SOURCE_PROVIDER and not src.enabled
        if turned_on:
            src.enabled = True
        db.session.add(EpgSourceSubscription(source_id=source_id, account_id=account_id,
                                             priority=_next_priority(account_id)))
        db.session.commit()
        return turned_on

    turned_on = _subscribe_and_commit()
    coverage = directory_coverage([source_id], case_sensitive)[source_id]
    user = {ch: k for (ch, _sid), k in accepted_keys([source_id]).items()}
    covered, pairs = 0, []
    for cid, provider_id in db.session.query(Channel.id, Channel.epg_channel_id).filter(
            Channel.account_id == account_id, Channel.hidden.is_(False)).order_by(Channel.id):
        k = channel_key(provider_id, user.get(cid))
        nk = norm_key(k, case_sensitive) if k else None
        if nk is None or nk not in coverage:
            continue
        covered += 1
        if nk in donors:
            pairs.append((cid, *donors[nk]))

    for i in range(0, len(pairs), _COPY_CHUNK):
        @retry_on_locked()
        def _copy_and_commit(batch=pairs[i:i + _COPY_CHUNK]):
            targets = [t for t, _d, _m in batch]
            for model in (EPGEntry, EpgAlternateEntry):
                db.session.execute(delete(model).where(_of_source(model, source_id),
                                                       model.channel_id.in_(targets)))
            for model in (EPGEntry, EpgAlternateEntry):
                _copy_listings([(t, d) for t, d, m in batch if m is model], source_id, model)
            db.session.commit()
        _copy_and_commit()

    outcome = reresolve_channels([t for t, _d, _m in pairs], case_sensitive,
                                 source_names={source_id: name})

    @retry_on_locked()
    def _recount_and_commit():
        refresh_source_counts(source_id)
        db.session.commit()

    _recount_and_commit()
    guided = sum(1 for new in outcome.values() if new == source_id)
    return Borrowed(refreshed, covered, len(pairs), guided, turned_on)


# ── What the account and channel pages show ─────────────────────────────────

#: last_status -> (badge class, label). None is a source never refreshed on this version.
STATUS_DISPLAY = {
    None: ('b-abort', 'Not refreshed yet'),
    EPG_STATUS_OK: ('b-done', 'OK'),
    EPG_STATUS_FAILED: ('b-fail', 'Fetch failed'),
    EPG_STATUS_REFUSED: ('b-warn', 'Import refused'),
    EPG_STATUS_TRUNCATED: ('b-warn', 'Cut short'),
}
KIND_LABELS = {EPG_SOURCE_PROVIDER: "Provider's guide (xmltv.php)",
               EPG_SOURCE_URL: 'XMLTV URL'}


def account_sources_view(account_id: int, schedule: dict | None = None) -> list[dict]:
    """The Sources card's rows (DESIGN-epg-sources.md §9.2): every source the account
    subscribes to, in priority order, then any source it owns and has stopped using (so
    Use again has a row to sit on). Three queries whatever the count. A `url` source's
    address is account-owned and secret in full, so it is masked whole.

    `schedule` is scheduler.epg_source_schedule() - {source_id: {'next': ..., 'retry': ...}}
    read off the jobs that will really fire - or None on an app with no scheduler."""
    from .url_utils import mask_url_path
    schedule = schedule or {}
    subs = (EpgSourceSubscription.query.filter_by(account_id=account_id)
            .order_by(EpgSourceSubscription.priority).all())
    subscribed = {sub.source_id for sub in subs}
    idle = EpgSource.query.filter(EpgSource.owner_account_id == account_id)
    if subscribed:
        idle = idle.filter(EpgSource.id.notin_(subscribed))
    idle = idle.order_by(EpgSource.id).all()
    active_here = active_counts_by_account(account_id)
    readers: dict[int, list[ForeignReader]] = {}
    owned = [s.id for s in [sub.source for sub in subs] + idle
             if s.owner_account_id == account_id]
    for fr in foreign_readers(owned, account_id):
        readers.setdefault(fr.source_id, []).append(fr)
    rows = []
    for s, priority in [(sub.source, sub.priority) for sub in subs] + [(s, None) for s in idle]:
        badge, label = STATUS_DISPLAY.get(s.last_status, STATUS_DISPLAY[None])
        when = schedule.get(s.id, {})
        rows.append({
            'id': s.id, 'name': s.name, 'priority': priority, 'subscribed': priority is not None,
            'kind': KIND_LABELS.get(s.kind, s.kind), 'is_url': s.kind == EPG_SOURCE_URL,
            'url': mask_url_path(s.url) if s.kind == EPG_SOURCE_URL else None,
            'owned': s.owner_account_id == account_id,
            'owner_id': s.owner_account_id, 'owner_name': s.owner.name if s.owner else None,
            'enabled': s.enabled, 'refresh_interval_hours': s.refresh_interval_hours,
            'refreshing': s.refresh_started_at is not None,
            'next_refresh_at': when.get('next'), 'retry_at': when.get('retry'),
            'last_refresh_at': s.last_refresh_at, 'last_success_at': s.last_success_at,
            'status_badge': badge, 'status_label': label, 'last_error': s.last_error,
            'entry_count': s.entry_count or 0, 'channel_count': s.channel_count or 0,
            'active_here': active_here.get(s.id, 0),
            'readers': [{'id': fr.account_id, 'name': fr.account_name, 'guided': fr.guided}
                        for fr in readers.get(s.id, [])],
        })
    return rows


def _pending_keys(channel_id: int, sources: dict[int, EpgSource]) -> dict[int, str]:
    """{source_id: KEY_* outcome} for each source whose latest key change for this channel
    is still waiting for that source's next refresh. Derived, not stored: the change is
    waiting exactly while its event is newer than the source's last successful import,
    whose timestamp is when that import read the keys (accounts.import_source)."""
    events = (db.session.query(ChannelEvent.timestamp, ChannelEvent.extra_data)
              .filter(ChannelEvent.channel_id == channel_id,
                      ChannelEvent.event_type == CHANNEL_EPG_KEY_CHANGED)
              .order_by(ChannelEvent.timestamp.desc(), ChannelEvent.id.desc()).limit(50))
    seen, out = set(), {}
    for ts, extra in events:
        try:
            data = json.loads(extra or '{}')
        except ValueError:
            continue
        sid = data.get('source_id')
        if sid in seen or sid not in sources:
            continue
        seen.add(sid)
        done_at = sources[sid].last_success_at
        if data.get('outcome') in KEY_PENDING_OUTCOMES and (done_at is None or ts > done_at):
            out[sid] = data['outcome']
    return out


def channel_guide_view(channel: Channel, case_sensitive: bool) -> dict:
    """The channel page's Guide section (DESIGN-epg-sources.md §9.3): the active source and
    why, then one row per source the channel's account subscribes to. A fixed number of
    queries per page, whatever the source count.

    The "why" is recomputed from the directory with the same resolve_active_source() the
    import used, rather than stored. When it disagrees with the stored winner - a source not
    yet refreshed since the directory existed - the stored winner is what the guide shows,
    so the page says that and not the recomputation."""
    subs = (EpgSourceSubscription.query.filter_by(account_id=channel.account_id)
            .order_by(EpgSourceSubscription.priority).all())
    source_ids = [sub.source_id for sub in subs]
    by_id = {sub.source_id: sub.source for sub in subs}
    for sid in (channel.epg_source_id, channel.epg_source_override_id):
        if sid and sid not in by_id:
            extra = db.session.get(EpgSource, sid)
            if extra is not None:
                by_id[extra.id] = extra
    keys, wanted, directory = _channel_directory(channel, by_id, case_sensitive)
    coverage = _channel_coverage(channel, by_id, directory)
    pending = _pending_keys(channel.id, by_id)
    no_directory = sources_without_directory(by_id)
    if channel.hidden:
        computed, why = None, REASON_NONE
    else:
        computed, why = resolve_active_source(channel.epg_source_override_id, source_ids,
                                              coverage)
    active = by_id.get(channel.epg_source_id) if channel.epg_source_id else None
    others = [sid for sid in source_ids if sid != channel.epg_source_id]
    comparable = bool(active and others and db.session.query(
        select(EpgAlternateEntry.id).where(EpgAlternateEntry.source_id.in_(others),
                                           EpgAlternateEntry.channel_id == channel.id)
        .exists()).scalar())
    rows = []
    for sid in source_ids + [s for s in by_id if s not in source_ids]:
        s = by_id[sid]
        d = directory.get(sid) or coverage.get(sid)
        user_key, origin = keys.get(sid, (None, None))
        rows.append({
            'id': sid, 'name': s.name, 'kind': KIND_LABELS.get(s.kind, s.kind),
            'subscribed': sid in source_ids,
            'key': wanted.get(sid), 'key_origin': origin if user_key else 'provider',
            'user_key': user_key, 'key_pending': pending.get(sid),
            'owner_id': s.owner_account_id, 'owner_name': s.owner.name if s.owner else None,
            'covers': sid in coverage,
            'entry_count': d.entry_count if d else 0,
            'distinct_titles': d.distinct_titles if d else 0,
            'sole_title': getattr(d, 'sole_title', None) if d and d.distinct_titles == 1 else None,
            'horizon_until': getattr(d, 'horizon_until', None),
            'refreshed': sid not in no_directory,
            'active': sid == channel.epg_source_id,
            'override': sid == channel.epg_source_override_id,
        })
    override = None
    if channel.epg_source_override_id:
        ov = by_id.get(channel.epg_source_override_id)
        override = {'id': channel.epg_source_override_id,
                    'name': ov.name if ov else f'source {channel.epg_source_override_id}',
                    'state': _override_state(channel.epg_source_override_id, why,
                                             channel.epg_source_id, source_ids)}
    return {
        'active': active, 'reason': why if computed == channel.epg_source_id else None,
        'hidden': channel.hidden, 'rows': rows, 'override': override,
        'comparable': comparable,
        'title_cap': DISTINCT_TITLE_CAP,
    }


# ── Name matching (DESIGN-epg-sources.md §7.3 - §7.5, dev/changelog/1105) ────
#
# Never inside an import (§7.2). Proposals are computed from the stored directory for a
# person to review, and only what that person accepts becomes a key, through
# set_channel_key(). The provider's id stays the default match everywhere.

MATCH_NEW = 'new'              # the channel's key matches nothing in the source today
MATCH_DISAGREE = 'disagree'    # its key matches one directory row and its name another


class DirectoryEntry(NamedTuple):
    xml_id: str
    display_names: list
    entry_count: int
    distinct_titles: int
    sole_title: str | None
    horizon_until: object       # datetime | None
    upcoming: list              # [[start ISO, title], ...]


class ChannelForMatch(NamedTuple):
    id: int
    name: str | None
    account_id: int
    epg_channel_id: str | None
    epg_source_id: int | None = None    # the channel's guide today, for the page to show


class NameProposal(NamedTuple):
    kind: str                   # MATCH_NEW | MATCH_DISAGREE
    channel: ChannelForMatch
    entry: DirectoryEntry       # what the name matched
    matched_on: str             # the display name, as the file spells it
    current: DirectoryEntry | None   # what the channel's key matches today (a disagreement)


class NameMatchResult(NamedTuple):
    proposals: list             # [NameProposal], both kinds, in channel order
    ambiguous: int              # channels whose name fits several file channels
    single_hidden: int          # proposals only the single-title toggle would show


def _folded_rows(directory, case_sensitive: bool) -> dict[str, DirectoryEntry]:
    """{normalized xml_id: entry} over rows with listings, the largest when case-variant ids
    fold together - the same fold directory_coverage() and the importer's map make."""
    out: dict[str, DirectoryEntry] = {}
    for e in directory:
        if e.entry_count <= 0 or not e.xml_id:
            continue
        k = norm_key(e.xml_id, case_sensitive)
        if k not in out or e.entry_count > out[k].entry_count:
            out[k] = e
    return out


def name_match_proposals(directory, channels, decided, case_sensitive: bool,
                         include_single: bool = False) -> NameMatchResult:
    """The name matches a source's directory proposes for these channels (§7.3, §7.5).
    Pure: no database, so the review page and a measurement over a file on disk run the
    same code.

    `directory` is DirectoryEntry rows; `channels` the visible channels of the accounts
    reading the source; `decided` the channel ids that already have a key row for this
    source, accepted or rejected - a person has answered for those, so nothing is proposed.

    A name is proposed only when it fits exactly one file channel: among the real-schedule
    rows (three or more distinct titles), or - with `include_single`, and only when no
    real-schedule row carries the name - among the rest, which is how an event channel
    repeating the one game it carries looks. A channel whose key already matches the same
    row is not proposed; one whose key matches a DIFFERENT row is a disagreement.
    """
    rows = _folded_rows(directory, case_sensitive)
    real: dict[str, set] = {}
    other: dict[str, set] = {}
    spelled: dict[tuple[str, str], str] = {}
    for k, e in rows.items():
        index = real if e.distinct_titles >= REAL_SCHEDULE_MIN_TITLES else other
        for display in e.display_names or ():
            n = normalize_name(display)
            if n:
                index.setdefault(n, set()).add(k)
                spelled.setdefault((n, k), display)
    proposals, ambiguous, single_hidden = [], 0, 0
    for ch in channels:
        if ch.id in decided:
            continue
        n = normalize_name(ch.name)
        if not n:
            continue
        hits = real.get(n)
        single = False
        if not hits:
            hits = other.get(n)
            single = True
        if not hits:
            continue
        if len(hits) > 1:
            ambiguous += 1
            continue
        k = next(iter(hits))
        current_key = norm_key(ch.epg_channel_id, case_sensitive) if ch.epg_channel_id else None
        if current_key == k:
            continue
        if single and not include_single:
            single_hidden += 1
            continue
        current = rows.get(current_key) if current_key else None
        proposals.append(NameProposal(MATCH_DISAGREE if current else MATCH_NEW, ch, rows[k],
                                      spelled[(n, k)], current))
    return NameMatchResult(proposals, ambiguous, single_hidden)


def _load_directory(source_id: int) -> list[DirectoryEntry]:
    rows = (db.session.query(EpgSourceChannel.xml_id, EpgSourceChannel.display_names,
                             EpgSourceChannel.entry_count, EpgSourceChannel.distinct_titles,
                             EpgSourceChannel.sole_title, EpgSourceChannel.horizon_until,
                             EpgSourceChannel.upcoming_titles)
            .filter(EpgSourceChannel.source_id == source_id,
                    EpgSourceChannel.entry_count > 0))
    out = []
    for xml_id, names, count, titles, sole, horizon, upcoming in rows:
        try:
            names = json.loads(names) if names else []
            upcoming = json.loads(upcoming) if upcoming else []
        except ValueError:
            log.warning('EPG source %d: unreadable directory row %r', source_id, xml_id)
            continue
        out.append(DirectoryEntry(xml_id, names, count or 0, titles or 0,
                                  sole if titles == 1 else None, horizon, upcoming))
    return out


def _reader_channels(source_id: int) -> list[ChannelForMatch]:
    """The visible channels of every account reading the source, in account then name
    order. Hidden channels import nothing from any source, so a key for one is moot."""
    accounts = subscriber_ids(source_id)
    if not accounts:
        return []
    return [ChannelForMatch(*r) for r in db.session.query(
        Channel.id, Channel.name, Channel.account_id, Channel.epg_channel_id,
        Channel.epg_source_id)
        .filter(Channel.account_id.in_(accounts), Channel.hidden.is_(False))
        .order_by(Channel.account_id, func.lower(Channel.name), Channel.id)]


def _key_rows(source_id: int) -> dict[int, EpgChannelKey]:
    return {k.channel_id: k for k in EpgChannelKey.query.filter_by(source_id=source_id)}


def name_match_review(source: EpgSource, case_sensitive: bool,
                      include_single: bool = False) -> NameMatchResult:
    """name_match_proposals() over what the database holds for `source`. Four queries,
    whatever the channel count."""
    return name_match_proposals(_load_directory(source.id), _reader_channels(source.id),
                                set(_key_rows(source.id)), case_sensitive, include_single)


def rejected_name_matches(source_id: int) -> list[dict]:
    """The channels whose proposal for this source a person rejected, for the review page's
    Rejected view. One query."""
    rows = (db.session.query(EpgChannelKey.channel_id, EpgChannelKey.matched_on,
                             EpgChannelKey.updated_at, Channel.name, Channel.account_id)
            .join(Channel, Channel.id == EpgChannelKey.channel_id)
            .filter(EpgChannelKey.source_id == source_id,
                    EpgChannelKey.status == EPG_KEY_REJECTED)
            .order_by(Channel.account_id, func.lower(Channel.name), Channel.id))
    return [{'channel_id': cid, 'matched_on': matched_on, 'rejected_at': at,
             'channel_name': name, 'account_id': account_id}
            for cid, matched_on, at, name, account_id in rows]


class NameMatchDecision(NamedTuple):
    applied: int        # accepted or rejected as asked
    stale: int          # no longer proposed (the file or the channel changed); left alone
    waiting: int        # accepted, listings arrive at the source's next refresh


def _current_proposals(source: EpgSource, case_sensitive: bool) -> dict[int, NameProposal]:
    """Every proposal the page could have shown, keyed by channel. A decision is checked
    against this rather than trusted from the request: the page may be stale, and a
    channel id and file id arriving together are not proof the name ever matched."""
    result = name_match_review(source, case_sensitive, include_single=True)
    return {p.channel.id: p for p in result.proposals}


def accept_name_matches(source: EpgSource, picks, case_sensitive: bool) -> NameMatchDecision:
    """Accept proposals: each becomes the channel's key for this source through
    set_channel_key() with origin name_match and the display name it matched on, so every
    one writes CHANNEL_EPG_KEY_CHANGED. `picks` is [(channel_id, xml_id)]; a pick that is
    no longer a current proposal is counted stale and not written. Commits in chunks, each
    its own retry_on_locked unit."""
    proposals = _current_proposals(source, case_sensitive)
    valid = [(cid, xml) for cid, xml in picks
             if cid in proposals and proposals[cid].entry.xml_id == xml]
    applied = waiting = 0
    for i in range(0, len(valid), 100):
        chunk = valid[i:i + 100]

        @retry_on_locked()
        def _accept_and_commit():
            done = pending = 0
            src = db.session.get(EpgSource, source.id)
            for cid, xml in chunk:
                channel = db.session.get(Channel, cid)
                if channel is None:
                    continue
                change = set_channel_key(channel, src, xml, case_sensitive=case_sensitive,
                                         origin=EPG_KEY_ORIGIN_NAME_MATCH,
                                         matched_on=proposals[cid].matched_on[:512])
                if change is not None:
                    done += 1
                    pending += change.outcome in KEY_PENDING_OUTCOMES
            db.session.commit()
            return done, pending

        done, pending = _accept_and_commit()
        applied += done
        waiting += pending
    return NameMatchDecision(applied, len(picks) - len(valid), waiting)


def reject_name_matches(source: EpgSource, picks, case_sensitive: bool) -> NameMatchDecision:
    """Reject proposals: a `rejected` key row per channel, so the pair is not proposed again
    (§6.1). The key stays NULL - the channel goes on matching by its provider id exactly as
    before, so no listing moves and no ChannelEvent is written; the Rejected view is where
    the refusal is seen and undone."""
    proposals = _current_proposals(source, case_sensitive)
    valid = [(cid, xml) for cid, xml in picks
             if cid in proposals and proposals[cid].entry.xml_id == xml]

    @retry_on_locked()
    def _reject_and_commit():
        for cid, _xml in valid:
            reject_name_match(cid, source.id, proposals[cid].matched_on)
        db.session.commit()

    if valid:
        _reject_and_commit()
    return NameMatchDecision(len(valid), len(picks) - len(valid), 0)


def reject_name_match(channel_id: int, source_id: int, matched_on: str) -> None:
    """The one writer of a `rejected` key row. Never touches an accepted key, and never
    sets `key`: a refusal is not an answer to "which id is this channel". Does not commit."""
    row = EpgChannelKey.query.filter_by(channel_id=channel_id, source_id=source_id).first()
    if row is not None:
        return
    db.session.add(EpgChannelKey(channel_id=channel_id, source_id=source_id, key=None,
                                 origin=EPG_KEY_ORIGIN_NAME_MATCH, status=EPG_KEY_REJECTED,
                                 matched_on=(matched_on or '')[:512] or None))


def propose_again(source_id: int, channel_ids) -> int:
    """Undo rejections: delete the rejected rows, so the channels are proposed again if their
    names still match. Returns how many were deleted."""
    ids = [int(c) for c in channel_ids]
    if not ids:
        return 0

    @retry_on_locked()
    def _delete_and_commit():
        n = 0
        for chunk in _chunks(ids):
            n += EpgChannelKey.query.filter(
                EpgChannelKey.source_id == source_id,
                EpgChannelKey.status == EPG_KEY_REJECTED,
                EpgChannelKey.channel_id.in_(chunk)).delete(synchronize_session=False)
        db.session.commit()
        return n

    return _delete_and_commit()


# ── Comparing what two sources say about one channel (DESIGN-epg-sources.md §4, §12,
#    dev/changelog/1108) ──────────────────────────────────────────────────────
#
# Read-only, over rows already held: the active listings in epg_entries against each other
# source's in epg_alternate_entries. The losing listings are kept for exactly this (§4).

SAME_START = timedelta(minutes=5)   # §12: the same program starts within five minutes
MOVED_WITHIN = timedelta(hours=6)   # further apart, a same-titled listing is another airing

CMP_IDENTICAL = 'identical'
CMP_DESCRIPTIONS = 'descriptions'   # identical, but only one side describes some programs
CMP_MOSTLY = 'mostly'               # nothing moved, but some listings have no partner
CMP_SHIFTED = 'shifted'             # at least one program starts more than 5 minutes apart
CMP_UNRELATED = 'unrelated'         # fewer than half the active listings have a partner
CMP_NO_OVERLAP = 'no-overlap'       # no stretch of time both list, from now on
COMPARE_VERDICTS = (CMP_IDENTICAL, CMP_DESCRIPTIONS, CMP_MOSTLY, CMP_SHIFTED, CMP_UNRELATED,
                    CMP_NO_OVERLAP)

ROW_SAME = 'same'
ROW_MOVED = 'moved'
ROW_DIFFERENT = 'different'         # neither has a partner, and they start together
ROW_ONLY_ACTIVE = 'only-active'
ROW_ONLY_OTHER = 'only-other'
#: A second listing at a moment one side already paired: that side lists the channel twice,
#: which a file naming it under two ids that differ only in case produces when ids match
#: case-insensitively. Shown, and left out of the verdict, which would otherwise call two
#: agreeing guides unrelated.
ROW_DOUBLED_ACTIVE = 'doubled-active'
ROW_DOUBLED_OTHER = 'doubled-other'


class Listing(NamedTuple):
    title: str
    sub_title: str | None
    description: str | None
    start: datetime
    stop: datetime

    @property
    def described(self) -> bool:
        return bool((self.description or '').strip())


class CompareRow(NamedTuple):
    kind: str
    active: Listing | None
    other: Listing | None
    counted: bool       # judged: inside the stretch both sources list, and not doubled

    @property
    def start(self) -> datetime:
        return (self.active or self.other).start


class Comparison(NamedTuple):
    verdict: str
    rows: list
    until: datetime | None   # the earlier of the two last stops: where comparing ends
    compared: int            # active listings starting before `until`
    same: int
    moved: int
    different: int
    only_active: int
    only_other: int
    described_active: int    # among the `same` pairs
    described_other: int
    description_gaps: int    # `same` pairs where exactly one side has a description
    largest_move: timedelta | None   # other minus active, the largest in size
    doubled_active: int      # ROW_DOUBLED_ACTIVE rows
    doubled_other: int


def comparison_title(title: str | None) -> str:
    """A listing's title as the comparison matches it: §7.3's normalize_name() after
    dropping modifier letters - the superscript tags providers append (`ᴺᵉʷ`, `ᴸᶦᵛᵉ`,
    `ᴿᴬᵂ`, 131,000 of them in the live guide) that one feed carries and another does not -
    and a leading `Live:`, the other feeds' spelling of the same tag
    (`U.S. Senate ᴸᶦᵛᵉ` / `Live: U.S. Senate`)."""
    key = normalize_name(''.join(ch for ch in (title or '')
                                 if unicodedata.category(ch) != 'Lm'))
    return key[5:] if key.startswith('live ') else key


def _word_prefix(x: str, y: str) -> bool:
    return bool(x and y) and (x.startswith(y + ' ') or y.startswith(x + ' '))


def _pair(a, b, used_a, used_b, window, match) -> list[tuple[int, int]]:
    """Closest-first pairing of unused listings starting within `window` of each other for
    which match(i, j) holds."""
    cands = []
    for i, x in enumerate(a):
        if i in used_a:
            continue
        for j, y in enumerate(b):
            if j not in used_b and abs(y.start - x.start) <= window and match(i, j):
                cands.append((abs(y.start - x.start), i, j))
    cands.sort()
    out = []
    for _gap, i, j in cands:
        if i not in used_a and j not in used_b:
            used_a.add(i)
            used_b.add(j)
            out.append((i, j))
    return out


def compare_listings(active, other) -> Comparison:
    """Line up one channel's listings from two sources and name the difference, under the
    rule recorded in DESIGN-epg-sources.md §12 - the same program starts within five
    minutes under a matching title - with the title match widened by measurement on the
    live guide (dev/changelog/1108): comparison_title() drops providers' superscript tags,
    and a title that is the other plus more words (`Hot Bench` / `Hot Bench - Timesharing
    Is Caring`) is the same program when the two start together. Pure; both lists are the
    listings from now on.

    Pairing runs closest first: matching titles within five minutes, then equal titles
    within six hours (moved - a rain delay), then any two leftovers starting together (a
    different program in the same slot). Only the stretch both sources list is judged, so a
    source listing 20 hours is not called unrelated to one listing 72."""
    a, b = list(active), list(other)
    at = [comparison_title(x.title) for x in a]
    bt = [comparison_title(x.title) for x in b]
    used_a: set[int] = set()
    used_b: set[int] = set()
    same = _pair(a, b, used_a, used_b, SAME_START, lambda i, j: at[i] == bt[j])
    same += _pair(a, b, used_a, used_b, SAME_START, lambda i, j: _word_prefix(at[i], bt[j]))
    moved = _pair(a, b, used_a, used_b, MOVED_WITHIN, lambda i, j: at[i] == bt[j])
    different = _pair(a, b, used_a, used_b, SAME_START, lambda i, j: True)
    since = max(min(x.start for x in a), min(x.start for x in b)) if a and b else None
    until = min(max(x.stop for x in a), max(x.stop for x in b)) if a and b else None

    def inside(x):
        return until is not None and since < until and x.stop > since and x.start < until

    paired_a = {a[i].start for i, _j in same + moved + different}
    paired_b = {b[j].start for _i, j in same + moved + different}
    rows = ([CompareRow(ROW_SAME, a[i], b[j], inside(a[i])) for i, j in same]
            + [CompareRow(ROW_MOVED, a[i], b[j], inside(a[i])) for i, j in moved]
            + [CompareRow(ROW_DIFFERENT, a[i], b[j], inside(a[i]))
               for i, j in different])
    for i, x in enumerate(a):
        if i not in used_a:
            rows.append(CompareRow(ROW_DOUBLED_ACTIVE, x, None, False) if x.start in paired_a
                        else CompareRow(ROW_ONLY_ACTIVE, x, None, inside(x)))
    for j, x in enumerate(b):
        if j not in used_b:
            rows.append(CompareRow(ROW_DOUBLED_OTHER, None, x, False) if x.start in paired_b
                        else CompareRow(ROW_ONLY_OTHER, None, x, inside(x)))
    rows.sort(key=lambda r: (r.start, r.active is None))
    counted = [r for r in rows if r.counted]

    def n(kind):
        return sum(1 for r in counted if r.kind == kind)

    same_rows = [r for r in counted if r.kind == ROW_SAME]
    moves = [r.other.start - r.active.start for r in counted if r.kind == ROW_MOVED]
    compared = sum(1 for r in counted if r.active is not None)
    gaps = sum(1 for r in same_rows if r.active.described != r.other.described)
    paired = len(same_rows) + len(moves)
    if not compared:
        verdict = CMP_NO_OVERLAP
    elif paired * 2 < compared:
        verdict = CMP_UNRELATED
    elif moves:
        verdict = CMP_SHIFTED
    elif n(ROW_DIFFERENT) or n(ROW_ONLY_ACTIVE) or n(ROW_ONLY_OTHER):
        verdict = CMP_MOSTLY
    elif gaps:
        verdict = CMP_DESCRIPTIONS
    else:
        verdict = CMP_IDENTICAL
    return Comparison(
        verdict=verdict, rows=rows, until=until, compared=compared, same=len(same_rows),
        moved=len(moves), different=n(ROW_DIFFERENT), only_active=n(ROW_ONLY_ACTIVE),
        only_other=n(ROW_ONLY_OTHER),
        described_active=sum(1 for r in same_rows if r.active.described),
        described_other=sum(1 for r in same_rows if r.other.described),
        description_gaps=gaps, largest_move=max(moves, key=abs) if moves else None,
        doubled_active=sum(1 for r in rows if r.kind == ROW_DOUBLED_ACTIVE),
        doubled_other=sum(1 for r in rows if r.kind == ROW_DOUBLED_OTHER))


class SourceComparison(NamedTuple):
    source: EpgSource
    last_stop: datetime       # the end of this source's listings for the channel
    comparison: Comparison


_LISTING_COLUMNS = ('title', 'sub_title', 'description', 'start_time', 'stop_time')


def channel_source_comparisons(channel: Channel, now: datetime | None = None):
    """(active listings, [SourceComparison]) for one channel, from now on: its guide
    against every other source its account reads that holds listings for it, in priority
    order. Three queries whatever the listing or source count, and nothing written.

    The alternates are kept only for a channel with an active source (§4), so a channel
    with none has nothing to compare."""
    now = now or datetime.utcnow()
    if channel.epg_source_id is None:
        return [], []

    def listing(r):
        return Listing(r.title, r.sub_title, r.description, r.start_time, r.stop_time)

    active = [listing(r) for r in db.session.query(
        *(getattr(EPGEntry, c) for c in _LISTING_COLUMNS)).filter(
        EPGEntry.channel_id == channel.id, EPGEntry.stop_time > now)
        .order_by(EPGEntry.start_time)]
    order = [sid for sid in subscriptions_for([channel.account_id]).get(channel.account_id, [])
             if sid != channel.epg_source_id]
    held: dict[int, list[Listing]] = {}
    if order:
        for r in db.session.query(EpgAlternateEntry.source_id,
                                  *(getattr(EpgAlternateEntry, c) for c in _LISTING_COLUMNS)
                                  ).filter(EpgAlternateEntry.source_id.in_(order),
                                           EpgAlternateEntry.channel_id == channel.id,
                                           EpgAlternateEntry.stop_time > now
                                           ).order_by(EpgAlternateEntry.start_time):
            held.setdefault(r.source_id, []).append(listing(r))
    if not held:
        return active, []
    sources = {s.id: s for s in EpgSource.query.filter(EpgSource.id.in_(list(held)))}
    return active, [SourceComparison(sources[sid], max(x.stop for x in held[sid]),
                                     compare_listings(active, held[sid]))
                    for sid in order if sid in held]
