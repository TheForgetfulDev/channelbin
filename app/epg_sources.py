"""EPG sources: where a channel's guide comes from (DESIGN-epg-sources.md).

Canonical home for what a source is, a channel's key into a source's file, which source
a channel's guide starts with and which continue it, the source directory, and putting a
channel's listings in the guide table (`epg_entries`) or the alternates table
(place_stretches()). The import itself
is `accounts.import_source()`, which calls in here; nothing in this module fetches or
parses a feed.

The two user-owned EPG facts - `EpgChannelKey` and `Channel.epg_source_override_id` - are
read here and never written here by anything a sync reaches (DESIGN-epg-sources.md §6).
"""
from __future__ import annotations

import bisect
import difflib
import functools
import json
import logging
import math
import re
import time as time_mod
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from typing import NamedTuple
from urllib.parse import urlsplit

from sqlalchemy import and_, case, delete, func, insert, literal, select, union_all, update

from . import db
from .database import (Account, Channel, ChannelEvent, CHANNEL_EPG_FILL_CHANGED,
                       CHANNEL_EPG_KEY_CHANGED, EpgChannelStretch, EPG_FINDING_SHARED_ID,
                       EPG_STRETCH_FILLS, EPG_STRETCH_NOTHING_PAST_END, EPG_STRETCH_NO_OVERLAP,
                       EPG_STRETCH_NO_SCHEDULE, EPG_STRETCH_SHARED_ID,
                       EPG_STRETCH_UNRELATED, EPG_STRETCH_WAITING,
                       CHANNEL_EPG_SOURCE_CHANGED, CHANNEL_EPG_SOURCE_OVERRIDE_CHANGED, EPGEntry, EpgAlternateEntry, EpgChannelKey,
                       EpgFinding, EpgSource, EpgSourceAgreement, EpgSourceChannel, EpgSourceRefresh,
                       EpgSourceSubscription, EPG_KEY_ACCEPTED, EPG_ORDER_FOLLOW, EPG_ORDER_MODES,
                       EPG_ORDER_OWN, PROVIDER_ORDER_OWN_GUIDE, PROVIDER_ORDER_SOURCE, Provider,
                       ProviderEpgOrder,
                       EPG_KEY_IGNORED, EPG_REFRESH_HISTORY_LIMIT, EPG_KEY_ORIGIN_MANUAL, EPG_KEY_ORIGIN_NAME_MATCH, EPG_KEY_REJECTED,
                       EPG_SOURCE_PROVIDER, EPG_SOURCE_URL, EPG_STATUS_FAILED, EPG_STATUS_OK,
                       EPG_STATUS_REFUSED, EPG_STATUS_TRUNCATED)
from .db_utils import chunked, retry_on_locked
from .fmt_utils import counted, join_labels, plural
from .program_titles import strip_marks

log = logging.getLogger(__name__)

#: A source's coverage of a channel is a real schedule at this many distinct titles in the
#: import window (dev/changelog/806's threshold, re-measured in DESIGN-epg-sources.md §2.3).
REAL_SCHEDULE_MIN_TITLES = 3
#: The directory stores distinct titles capped here: the only questions asked of the
#: number are "one?" and "three or more?", and the cap bounds the import's memory.
DISTINCT_TITLE_CAP = 20


def directory_row_summary(entry_count, distinct_titles) -> str:
    """What a source's file holds for one channel, in one sentence: 'no listings',
    '1,234 listings, one program all day' or '1,234 listings, 20+ titles'. Every row view
    (search_directory, channel_guide_view, the review page) carries it as `summary`, so the
    three surfaces say the same thing and DISTINCT_TITLE_CAP has one spelling. A sole title
    is rendered by the surface after it, since two of the three set it in <em>."""
    entry_count = entry_count or 0
    distinct_titles = distinct_titles or 0
    if not entry_count:
        return 'no listings'
    if distinct_titles == 1:
        return f'{entry_count:,} listings, one program all day'
    cap = '+' if distinct_titles >= DISTINCT_TITLE_CAP else ''
    return f'{entry_count:,} listings, {distinct_titles}{cap} titles'
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


def case_sensitive_matching(cfg) -> bool:
    """`sync.epg_case_sensitive_matching` off a loaded config - the one reader, handed the
    config the caller already holds (never a load_config() of its own)."""
    return (cfg or {}).get('sync', {}).get('epg_case_sensitive_matching', False)


def norm_key(key: str, case_sensitive: bool) -> str:
    """How an XMLTV channel id is compared: exact, or lower-cased unless
    sync.epg_case_sensitive_matching. Applied once at map build and once at lookup, never
    as an exact-then-fallback chain."""
    return key if case_sensitive else key.lower()


def file_id_for(key: str, variants: dict[str, int]) -> str | None:
    """The one id in a file that a channel keyed `key` reads its listings from, given
    `variants` = {xml_id: in-window program count} for the file's ids that fold to the
    key's normalized form (only ids with programs). None when there are none.

    A file may list a channel under ids that differ only in case, each with its own
    schedule, and on the provider guides they are usually different feeds told apart by
    case alone (`4Seven.uk` is 4Seven, `4seven.uk` is London Live). Reading all of them
    gave the channel every program twice (dev/changelog/1139). So: the id spelled exactly
    as the key, else the one with the most programs, ties to the lowest id so every import
    picks the same one. This is a choice among ids already gathered under one normalized
    key, not an exact-then-fallback lookup - every variant is found either way."""
    if not variants:
        return None
    if key in variants:
        return key
    return max(sorted(variants), key=variants.__getitem__)


def folded_variants(source_id: int, case_sensitive: bool,
                    norm_keys=None) -> dict[str, dict[str, int]]:
    """{normalized key: {xml_id: entry_count}} over the source directory's ids with
    listings, the input file_id_for() chooses from. `norm_keys` limits it to a handful of
    keys (bound as one IN list); None reads the whole directory. One query."""
    q = (db.session.query(EpgSourceChannel.xml_id, EpgSourceChannel.entry_count)
         .filter(EpgSourceChannel.source_id == source_id, EpgSourceChannel.entry_count > 0))
    if norm_keys is not None:
        wanted = {k.lower() for k in norm_keys}
        if not wanted:
            return {}
        q = q.filter(func.lower(EpgSourceChannel.xml_id).in_(wanted))
    out: dict[str, dict[str, int]] = {}
    for xml_id, n in q:
        k = norm_key(xml_id, case_sensitive)
        if norm_keys is None or k in norm_keys:
            out.setdefault(k, {})[xml_id] = n
    return out


class KeyRead(NamedTuple):
    source_id: int | None   # the channel's active source, Channel.epg_source_id
    key: str | None         # channel_key() into that source
    file_id: str | None     # the id of its file the import reads for the key, or None


def keys_read(channels, case_sensitive: bool) -> dict[int, KeyRead]:
    """{channel id: KeyRead} - which schedule each channel's guide listings are: its active
    source, its key into it (the accepted user key, else the provider's id, §7.1), and the
    file id that key reads (file_id_for()). Two channels hold the same listings exactly
    when their (source_id, file_id) are equal, because the import maps every channel
    reading one file id onto the same programs. Keys differing only in case are NOT
    enough: a file listing both spellings gives each its own schedule (dev/changelog/1139).

    `file_id` is None when the directory lists no programs under any spelling of the key
    (a source never refreshed, or an id the file does not carry). Two queries whatever the
    channel count."""
    channels = list(channels)
    reads = keys_read_into([(ch, ch.epg_source_id) for ch in channels], case_sensitive)
    return {ch.id: reads[(ch.id, ch.epg_source_id)] for ch in channels}


def keys_read_into(pairs, case_sensitive: bool) -> dict[tuple[int, int | None], KeyRead]:
    """{(channel id, source id): KeyRead} - keys_read() for a stated source per channel
    rather than the channel's active one, so the listings a source holds for a channel as
    an alternate (epg_alternate_entries) can be named by the schedule they are: the same
    (source, file id) identity, whichever table the rows sit in. `pairs` is an iterable of
    (Channel, source id or None); a channel may appear with several sources. Two queries
    whatever the pair count; the directory is read in one IN list over every key wanted
    (chunked when the list is long), so a guide-wide caller pays it once."""
    pairs = list(pairs)
    sids = {sid for _ch, sid in pairs if sid is not None}
    user = key_answers(sids)
    out: dict[tuple[int, int | None], KeyRead] = {}
    wanted: dict[int, set] = {}
    for ch, sid in pairs:
        key = channel_key(ch.epg_channel_id, user.get((ch.id, sid))) if sid is not None \
            else (ch.epg_channel_id or None)
        out[(ch.id, sid)] = KeyRead(sid, key, None)
        if sid is not None and key:
            wanted.setdefault(sid, set()).add(key.lower())
    if not wanted:
        return out
    variants: dict[tuple[int, str], dict[str, int]] = {}
    lowered = sorted(set().union(*wanted.values()))
    for piece in chunked(lowered, 500):
        for source_id, xml_id, n in (
                db.session.query(EpgSourceChannel.source_id, EpgSourceChannel.xml_id,
                                 EpgSourceChannel.entry_count)
                .filter(EpgSourceChannel.source_id.in_(list(wanted)),
                        EpgSourceChannel.entry_count > 0,
                        func.lower(EpgSourceChannel.xml_id).in_(piece))):
            if xml_id.lower() in wanted[source_id]:
                variants.setdefault((source_id, norm_key(xml_id, case_sensitive)), {})[xml_id] = n
    for unit, kr in out.items():
        if kr.source_id is not None and kr.key:
            fid = file_id_for(kr.key, variants.get((kr.source_id, norm_key(kr.key, case_sensitive)), {}))
            out[unit] = kr._replace(file_id=fid)
    return out


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
    db.session.flush()
    # On a provider whose order it follows, its own guide belongs in the slot (§15.3).
    apply_provider_order(account.id)
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


class IgnoredId(NamedTuple):
    """A user's "not the provider's id" for one channel and source (EPG_KEY_IGNORED): the
    provider id they stopped using. Never a str, so a reader that takes a key answer for a
    key without going through channel_key() fails instead of matching on it."""
    provider_id: str


def key_answers(source_ids, channel_ids=None) -> dict[tuple[int, int], str | IgnoredId]:
    """{(channel_id, source_id): answer} for every user answer about a channel's key on
    these sources: the accepted key (a str), or an IgnoredId for a provider id the user
    stopped using. Every reader hands the answer to channel_key(), the one place that turns
    it into the key a channel matches on. Rejected rows are not answers about the key.

    Queried by source rather than by channel: the table holds only the user's own answers,
    so it is small, where a channel IN (...) over a large account would not be. A caller
    asking about a handful of channels passes `channel_ids`: a bulk accept of name matches
    grows the table by thousands of rows, and reading all of them per channel made that
    accept quadratic (dev/changelog/1718)."""
    source_ids = list(source_ids)
    if not source_ids:
        return {}
    q = (db.session.query(EpgChannelKey.channel_id, EpgChannelKey.source_id,
                          EpgChannelKey.key, EpgChannelKey.status)
         .filter(EpgChannelKey.source_id.in_(source_ids),
                 EpgChannelKey.status.in_((EPG_KEY_ACCEPTED, EPG_KEY_IGNORED)),
                 EpgChannelKey.key.isnot(None), EpgChannelKey.key != ''))
    if channel_ids is None:
        rows = q.all()
    else:
        rows = [r for piece in chunked(sorted(set(channel_ids)), 500)
                for r in q.filter(EpgChannelKey.channel_id.in_(piece))]
    return {(ch, src): (IgnoredId(key) if status == EPG_KEY_IGNORED else key)
            for ch, src, key, status in rows}


def ignores(epg_channel_id: str | None, answer) -> bool:
    """Whether a key answer is an IgnoredId still in force: the provider gives the channel
    the id the user stopped using. Compared case-folded whatever the matching setting, so a
    provider re-spelling the id's case does not bring it back."""
    return (isinstance(answer, IgnoredId) and bool(epg_channel_id)
            and epg_channel_id.lower() == answer.provider_id.lower())


def channel_key(epg_channel_id: str | None, user_key) -> str | None:
    """key(channel, source) (DESIGN-epg-sources.md §7.1): the user's accepted key for this
    source if there is one, else the provider's id, else None (never matched). An IgnoredId
    still in force (ignores()) matches nothing; one whose id the provider has since changed
    falls through to the provider's new id (dev/changelog/1716)."""
    if isinstance(user_key, IgnoredId):
        return None if ignores(epg_channel_id, user_key) else (epg_channel_id or None)
    return user_key or epg_channel_id or None


def directory_coverage(source_ids, case_sensitive: bool) -> dict[int, dict[str, Coverage]]:
    """{source_id: {normalized xml_id: Coverage}} from the source directory - never a count
    over epg_entries (DESIGN-epg-sources.md §7.4). Where case-variant ids fold together
    under case-insensitive matching, each number is the largest of the variants', never
    their sum: the importer reads one variant per channel (file_id_for()), and which one
    depends on the channel's own spelling, which a per-key answer cannot see."""
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
            out[source_id][k] = Coverage(max(prev.entry_count, entries),
                                         max(prev.distinct_titles, titles))
    return out


def resolve_active_source(override_id: int | None, ordered_source_ids,
                          coverage: dict[int, Coverage]) -> tuple[int | None, str]:
    """Which source a channel's guide starts with, and why (DESIGN-epg-sources.md §5.1).

    `ordered_source_ids` is the channel's account's subscriptions in priority order;
    `coverage` is {source_id: Coverage} for the sources that cover the channel at all.
    One answer per channel. The sources after it may continue the guide past its end, which
    is decide_stretches()'s question and not this one's (§14).

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
    # The scorecard's detail counts (dev/changelog/1717); None = not counted.
    described: int | None = None
    subtitled: int | None = None
    categorized: int | None = None
    rated: int | None = None
    flagged: int | None = None
    doubled: int | None = None


def write_directory(source_id: int, rows) -> None:
    """Replace a source's directory in full (DESIGN-epg-sources.md §7.4) - it is a cache of
    the file's <channel> block plus the counts the import pass already makes."""
    mappings = [{'source_id': source_id, 'xml_id': r.xml_id[:255],
                 'described': r.described, 'subtitled': r.subtitled,
                 'categorized': r.categorized, 'rated': r.rated, 'flagged': r.flagged,
                 'doubled': r.doubled,
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
                  'stop_time', 'category', 'rating', 'is_live', 'is_new', 'source_id')


def _chunks(ids):
    return chunked(list(ids), _CHUNK)


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


def of_sources(model, source_ids):
    """_of_source() for several sources: `model.source_id IN (...)`, compared as
    `source_id + 0` on epg_entries for the same reason, for a statement that also names its
    channels."""
    ids = list(source_ids)
    if model is EPGEntry:
        return (model.source_id + 0).in_(ids)
    return model.source_id.in_(ids)


def _move_rows(channel_ids, source_id: int, src_model, dst_model, *,
               starting_from: datetime | None = None,
               starting_before: datetime | None = None) -> int:
    """The statements of one move, without committing: INSERT ... SELECT then DELETE.
    `starting_from` / `starting_before` narrow it to the listings on one side of a seam
    (§14.4)."""
    src_cols = [getattr(src_model, c) for c in _ENTRY_COLUMNS]
    dst_cols = [getattr(dst_model, c) for c in _ENTRY_COLUMNS]
    where = [_of_source(src_model, source_id), src_model.channel_id.in_(list(channel_ids))]
    if starting_from is not None:
        where.append(src_model.start_time >= starting_from)
    if starting_before is not None:
        where.append(src_model.start_time < starting_before)
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


def holding_rows(source_ids, channel_ids) -> set[tuple[int, int]]:
    """{(source_id, channel_id)} for the pairs where the database holds at least one of the
    source's rows for the channel, in EITHER table - the guard every resolver applies before
    it points a channel at a new winner (reresolve_channels(), set_source_override(), the
    import). Rows in the alternates are what a move promotes; rows already in epg_entries
    mean the move happened and only the pointer is behind, which is what a process death
    between the row moves and apply_winners() leaves - read from the alternates alone, that
    channel was skipped by every later re-resolve and never repaired (dev/changelog/1298).
    One query per source per chunk per table, each on an index that leads with or reaches
    the channel."""
    out: set[tuple[int, int]] = set()
    for sid in source_ids:
        for chunk in _chunks(channel_ids):
            for model in (EpgAlternateEntry, EPGEntry):
                out.update((sid, cid) for (cid,) in db.session.query(model.channel_id)
                           .filter(_of_source(model, sid), model.channel_id.in_(chunk))
                           .distinct())
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


def demote_others(channel_ids, keep_source_id: int | None) -> int:
    """These channels' guide is about to start with another source (or with none, `None`):
    every other source's rows leave epg_entries first - the old first source's and those of
    the sources that continued it (§14). Done before the new one is promoted, so a restart
    between the two leaves a guide that is short, never one holding two sources' programs;
    place_stretches() puts back what still continues it. One retry_on_locked unit a chunk,
    as _move()."""
    cols_src = [getattr(EPGEntry, c) for c in _ENTRY_COLUMNS]
    cols_dst = [getattr(EpgAlternateEntry, c) for c in _ENTRY_COLUMNS]
    moved = 0
    for chunk in _chunks(channel_ids):
        where = [EPGEntry.channel_id.in_(chunk)]
        if keep_source_id is not None:
            where.append((EPGEntry.source_id + 0) != keep_source_id)

        @retry_on_locked()
        def _move_chunk_and_commit(where=where):
            db.session.execute(insert(EpgAlternateEntry).from_select(
                cols_dst, select(*cols_src).where(*where)))
            n = db.session.execute(delete(EPGEntry).where(*where)).rowcount
            db.session.commit()
            return n
        moved += _move_chunk_and_commit()
    return moved


def drop_alternates(channel_ids) -> None:
    """Alternates are kept only for channels with an active source (DESIGN-epg-sources.md
    §4): every source's rows for these channels go, since a channel with no winner would
    otherwise collect every source's leftovers. Called for the channels an import or a
    re-resolve left with no winner. It used to take a source and drop only the OLD winner's
    alternates, which were never there - the old winner's rows were active - so it dropped
    nothing (dev/changelog/1298). On ix_epg_alt_channel."""
    for chunk in _chunks(channel_ids):
        @retry_on_locked()
        def _drop_and_commit(chunk=chunk):
            EpgAlternateEntry.query.filter(
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
    for batch in chunked(events, _CHUNK):

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
KEY_DROPPED = 'dropped'            # the provider's id is ignored: the source's listings went
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
        KEY_DROPPED: f'its listings from {source_name} were removed',
        KEY_WAITS: f'listings arrive at the next refresh of {source_name}',
        KEY_NOT_IN_FILE: (f"not in {source_name}'s last refresh, so it matches nothing "
                          'until it appears there'),
        KEY_NO_KEY: f'the channel matches nothing in {source_name} after its next refresh',
        KEY_UNREFRESHED: f'{source_name} has no successful refresh yet; applies at its first one',
    }[outcome]


def _channel_directory(channel: Channel, source_ids, case_sensitive: bool):
    """For one channel over these sources: the user's key answers {source_id: (answer,
    origin)} - an accepted key, or an IgnoredId (key_answers()) - the key each source is
    matched on {source_id: key} (§7.1), and the directory row that key hits in each source
    {source_id: EpgSourceChannel} - the one the import reads (file_id_for()), when
    case-variant ids fold together. Three queries whatever the source count."""
    source_ids = list(source_ids)
    keys = {k.source_id: ((IgnoredId(k.key) if k.status == EPG_KEY_IGNORED else k.key),
                          k.origin)
            for k in EpgChannelKey.query.filter(
                EpgChannelKey.channel_id == channel.id,
                EpgChannelKey.status.in_((EPG_KEY_ACCEPTED, EPG_KEY_IGNORED)),
                EpgChannelKey.key.isnot(None), EpgChannelKey.key != '',
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
        hits: dict[int, dict[str, EpgSourceChannel]] = {}
        for r in rows:
            if norm_key(r.xml_id, case_sensitive) == norm_key(wanted[r.source_id],
                                                              case_sensitive):
                hits.setdefault(r.source_id, {})[r.xml_id] = r
        for sid, by_id in hits.items():
            listed = {x: r.entry_count for x, r in by_id.items() if r.entry_count > 0}
            pick = (file_id_for(wanted[sid], listed)
                    or (wanted[sid] if wanted[sid] in by_id else min(by_id)))
            directory[sid] = by_id[pick]
    return keys, wanted, directory


def _coverage_of(directory) -> dict[int, Coverage]:
    return {sid: Coverage(r.entry_count, r.distinct_titles)
            for sid, r in directory.items() if r.entry_count > 0}


def _key_donor(channel: Channel, source: EpgSource, key: str, case_sensitive: bool):
    """A visible channel on an account reading `source` whose key into it is `key` and which
    holds that source's listings right now, as (channel id, name), or None. A donor's rows
    may sit in either table or, where the source continues its guide (§14), in both.

    Its rows are exactly what the next import would write for `channel`: the importer maps
    every channel whose key reads the same file id (file_id_for()) onto the same programs
    (§7.1). A key differing only in case can read a different id, so matching the folded key
    alone is not enough (dev/changelog/1139)."""
    subscribers = subscriber_ids(source.id)
    if not subscribers:
        return None
    wanted = norm_key(key, case_sensitive)
    variants = folded_variants(source.id, case_sensitive, {wanted}).get(wanted, {})
    target = file_id_for(key, variants)
    if target is None:
        return None
    lowered = key.lower()
    # By the folded id alone, then filtered here: with the account and hidden filters in the
    # statement SQLite can plan it through ix_channels_standing and scan, which made a key
    # write 23 ms slower per channel; an id is shared by a few hundred channels at most
    # (dev/changelog/1718).
    subscribed_accounts = set(subscribers)
    by_provider = {cid for cid, account_id, hidden in db.session.query(
        Channel.id, Channel.account_id, Channel.hidden).filter(
        func.lower(Channel.epg_channel_id) == lowered)
        if account_id in subscribed_accounts and not hidden and cid != channel.id}
    by_user = {cid for (cid,) in db.session.query(EpgChannelKey.channel_id).filter(
        EpgChannelKey.source_id == source.id, EpgChannelKey.status == EPG_KEY_ACCEPTED,
        EpgChannelKey.channel_id != channel.id, func.lower(EpgChannelKey.key) == lowered)}
    ids = sorted(by_provider | by_user)
    if not ids:
        return None
    user_keys = {ch: k for (ch, _src), k in key_answers([source.id], ids).items()}
    subscribed = set(subscribers)
    for cid, name, provider_id, hidden, account_id in (
            db.session.query(Channel.id, Channel.name, Channel.epg_channel_id, Channel.hidden,
                             Channel.account_id)
            .filter(Channel.id.in_(ids)).order_by(Channel.id)):
        if hidden or account_id not in subscribed:
            continue
        k = channel_key(provider_id, user_keys.get(cid))
        if (not k or norm_key(k, case_sensitive) != wanted
                or file_id_for(k, variants) != target):
            continue
        for model in (EPGEntry, EpgAlternateEntry):
            if db.session.query(model.id).filter(_of_source(model, source.id),
                                                 model.channel_id == cid).first():
                return cid, name
    return None


def _reads_same_id(source_id: int, a: str, b: str, case_sensitive: bool) -> bool:
    """Whether two keys that fold together read the same id of the source's file. Only a
    difference of spelling can make them not: a file listing both spellings gives each its
    own schedule (dev/changelog/1139)."""
    if a == b:
        return True
    k = norm_key(a, case_sensitive)
    variants = folded_variants(source_id, case_sensitive, {k}).get(k, {})
    return file_id_for(a, variants) == file_id_for(b, variants)


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
            and norm_key(old_key, case_sensitive) == norm_key(new_key, case_sensitive)
            and _reads_same_id(source.id, old_key, new_key, case_sensitive)):
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
    donor_id, donor_name = donor

    old_winner = channel.epg_source_id
    winner, why = resolve_active_source(channel.epg_source_override_id, order, coverage)
    if old_winner is not None and old_winner not in (winner, source.id):
        _move_rows([channel.id], old_winner, EPGEntry, EpgAlternateEntry)
    for model in (EPGEntry, EpgAlternateEntry):
        db.session.execute(delete(model).where(_of_source(model, source.id),
                                               model.channel_id == channel.id))
    dst = EPGEntry if winner == source.id else EpgAlternateEntry
    for donor_model in (EPGEntry, EpgAlternateEntry):
        src_cols = [literal(channel.id) if c == 'channel_id' else getattr(donor_model, c)
                    for c in _ENTRY_COLUMNS]
        db.session.execute(insert(dst).from_select(
            [getattr(dst, c) for c in _ENTRY_COLUMNS],
            select(*src_cols).where(_of_source(donor_model, source.id),
                                    donor_model.channel_id == donor_id)))
    if winner is not None and winner not in (old_winner, source.id):
        _move_rows([channel.id], winner, EpgAlternateEntry, EPGEntry)
    _set_winner(channel, old_winner, winner, why)
    place_channel_now(channel, case_sensitive, coverage)
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


def _drop_source_now(channel: Channel, source: EpgSource, case_sensitive: bool) -> str:
    """The channel stopped matching `source` (its provider id is ignored there): delete what
    the source holds for it in both tables now, and when the source was its winner, hand the
    guide to the next source that holds listings for it. Without committing; nothing is
    fetched. A next source the directory names but whose rows are not here yet leaves the
    channel with no winner until that source's import, which picks it up - the source just
    removed is not left as the winner of nothing."""
    if channel.hidden:
        return KEY_HIDDEN
    for model in (EPGEntry, EpgAlternateEntry):
        db.session.execute(delete(model).where(_of_source(model, source.id),
                                               model.channel_id == channel.id))
    old_winner = channel.epg_source_id
    if old_winner != source.id:
        place_channel_now(channel, case_sensitive)
        return KEY_DROPPED
    order = subscriptions_for([channel.account_id]).get(channel.account_id, [])
    _keys, _wanted, directory = _channel_directory(channel, order, case_sensitive)
    winner, why = resolve_active_source(channel.epg_source_override_id, order,
                                        _channel_coverage(channel, order, directory))
    if winner is not None and (winner, channel.id) not in holding_rows([winner], [channel.id]):
        winner, why = None, REASON_NONE
    if winner is not None:
        _move_rows([channel.id], winner, EpgAlternateEntry, EPGEntry)
    else:
        db.session.execute(delete(EpgAlternateEntry).where(
            EpgAlternateEntry.channel_id == channel.id))
    _set_winner(channel, old_winner, winner, why)
    place_channel_now(channel, case_sensitive)
    return KEY_DROPPED


def set_channel_key(channel: Channel, source: EpgSource, key: str | None, *,
                    case_sensitive: bool, origin: str = EPG_KEY_ORIGIN_MANUAL,
                    matched_on: str | None = None,
                    ignore_provider_id: bool = False) -> KeyChange | None:
    """The one writer of the user's per-channel, per-source key (DESIGN-epg-sources.md
    §6.1). A blank `key` clears it, and the channel goes back to the provider's id. Writes
    CHANNEL_EPG_KEY_CHANGED and applies what it can at once (`_apply_key_now`). Does not
    commit - the caller owns the unit, as with channel_groups.set_participation().

    `ignore_provider_id` (with no `key`) stores the user's "not the provider's id": an
    EPG_KEY_IGNORED row holding the channel's provider id, so the channel matches nothing in
    this source while the provider gives it that id, and the source's listings for it go
    now (_drop_source_now()). A clear takes it away again (dev/changelog/1716).

    Returns the outcome (KEY_*) and the event's text, or None when nothing changed. A
    rejected row is left alone by a clear: it records a name-match refusal, not a key; so
    is the refusal an ignored row carries in `matched_on`, which a clear turns back into a
    rejected row."""
    key = (key or '').strip() or None
    if ignore_provider_id and (key is not None or not channel.epg_channel_id):
        raise ValueError('ignoring needs a channel with a provider id and no key')
    row = EpgChannelKey.query.filter_by(channel_id=channel.id, source_id=source.id).first()
    user_key = row.key if row is not None and row.status == EPG_KEY_ACCEPTED else None
    ignored = (row.key if row is not None and row.status == EPG_KEY_IGNORED
               and ignores(channel.epg_channel_id, IgnoredId(row.key or '')) else None)
    if ignore_provider_id:
        if ignored is not None or user_key is not None:
            # Already ignored, or the user's own key is what this source reads.
            return None
    elif key == user_key and ignored is None and (key is None or row.origin == origin):
        return None
    old_key = channel_key(channel.epg_channel_id,
                          IgnoredId(ignored) if ignored is not None else user_key)
    if ignore_provider_id:
        if row is None:
            db.session.add(EpgChannelKey(channel_id=channel.id, source_id=source.id,
                                         key=channel.epg_channel_id, origin=origin,
                                         status=EPG_KEY_IGNORED))
        else:
            # A rejected row's refusal survives in matched_on (§7.5).
            refused = row.matched_on if row.status != EPG_KEY_ACCEPTED else None
            row.key, row.origin, row.status = channel.epg_channel_id, origin, EPG_KEY_IGNORED
            row.matched_on = refused
    elif key is None:
        if row.status == EPG_KEY_IGNORED and row.matched_on:
            row.key, row.status = None, EPG_KEY_REJECTED
        else:
            db.session.delete(row)
    elif row is None:
        db.session.add(EpgChannelKey(channel_id=channel.id, source_id=source.id, key=key,
                                     origin=origin, status=EPG_KEY_ACCEPTED,
                                     matched_on=matched_on))
    else:
        row.key = key
        row.origin, row.status, row.matched_on = origin, EPG_KEY_ACCEPTED, matched_on
    db.session.flush()
    if ignore_provider_id:
        outcome, donor = _drop_source_now(channel, source, case_sensitive), None
    else:
        new_key = channel_key(channel.epg_channel_id, key)
        outcome, donor = _apply_key_now(channel, source, old_key, new_key, case_sensitive)

    was = (f'your key "{user_key}"' if user_key
           else f'not using the provider id "{ignored}"' if ignored is not None
           else f'the provider id "{channel.epg_channel_id}"' if channel.epg_channel_id
           else 'no key')
    if ignore_provider_id:
        what = (f'EPG key for {source.name}: the provider id "{channel.epg_channel_id}" '
                f'is no longer used (was {was})')
    elif key is None:
        now = (f'back to the provider id "{channel.epg_channel_id}"' if channel.epg_channel_id
               else 'no key left')
        what = f'EPG key for {source.name} cleared, {now} (was {was})'
    else:
        what = f'EPG key for {source.name} set to "{key}" (was {was})'
    detail = f'{what}: {_key_outcome_text(outcome, source.name, donor)}'
    db.session.add(ChannelEvent(
        channel_id=channel.id, event_type=CHANNEL_EPG_KEY_CHANGED, detail=detail,
        extra_data=json.dumps({'source_id': source.id, 'key': key, 'previous': user_key,
                               'origin': origin, 'outcome': outcome,
                               'ignored': channel.epg_channel_id if ignore_provider_id
                               else None,
                               'previous_ignored': ignored})))
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
        if winner is None or (winner != old_winner
                              and (winner, channel.id) not in holding_rows([winner],
                                                                            [channel.id])):
            winner = old_winner
    if winner != old_winner:
        if old_winner is not None:
            _move_rows([channel.id], old_winner, EPGEntry, EpgAlternateEntry)
        if winner is not None:
            _move_rows([channel.id], winner, EpgAlternateEntry, EPGEntry)
        _set_winner(channel, old_winner, winner, why)
        place_channel_now(channel, case_sensitive)
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
             'sole_title': r.sole_title if r.distinct_titles == 1 else None,
             'summary': directory_row_summary(r.entry_count, r.distinct_titles)}
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


def prune_refresh_history(source_id: int) -> None:
    """Keep a source's last EPG_REFRESH_HISTORY_LIMIT refreshes. Its own commit, separate from
    the one that wrote the refresh, so a retry here never writes a refresh twice."""
    @retry_on_locked()
    def _prune_and_commit():
        stale = [i for (i,) in db.session.query(EpgSourceRefresh.id)
                 .filter(EpgSourceRefresh.source_id == source_id)
                 .order_by(EpgSourceRefresh.finished_at.desc(), EpgSourceRefresh.id.desc())
                 .offset(EPG_REFRESH_HISTORY_LIMIT)]
        if stale:
            db.session.execute(delete(EpgSourceRefresh).where(EpgSourceRefresh.id.in_(stale)))
            db.session.commit()

    _prune_and_commit()


def active_counts_by_account(account_id: int) -> dict[int, int]:
    """{source_id: channels on this account whose guide comes from it}. One grouped query."""
    return active_counts_by_accounts([account_id]).get(account_id, {})


def active_counts_by_accounts(account_ids) -> dict[int, dict[int, int]]:
    """{account_id: active_counts_by_account()} for several accounts in one grouped query
    (0.14s over prod's 136,130 channels). An account guiding nothing has no entry."""
    account_ids = [a for a in account_ids if a is not None]
    if not account_ids:
        return {}
    out: dict[int, dict[int, int]] = {}
    for aid in chunked(account_ids, 500):
        rows = (db.session.query(Channel.account_id, Channel.epg_source_id, func.count(Channel.id))
                .filter(Channel.account_id.in_(aid), Channel.epg_source_id.isnot(None))
                .group_by(Channel.account_id, Channel.epg_source_id))
        for account_id, sid, n in rows:
            out.setdefault(account_id, {})[sid] = n
    return out


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
        _delete_source_rows(owned)
        for col in (Channel.epg_source_id, Channel.epg_source_override_id):
            db.session.execute(update(Channel).where(col.in_(owned)).values({col: None})
                               .execution_options(synchronize_session=False))
        db.session.execute(delete(EpgSource).where(EpgSource.id.in_(owned)))
    db.session.execute(delete(EpgSourceSubscription)
                       .where(EpgSourceSubscription.account_id == account_id))
    db.session.execute(delete(EpgFinding).where(EpgFinding.account_id == account_id))


def _delete_source_rows(source_ids) -> None:
    """Every row keyed on these sources, short of the sources themselves: listings in both
    tables, directory, user keys, subscriptions, refresh history and agreements on either
    side, findings. The one list both source deletes share, so a table added to one cannot be missed
    by the other. Does not commit."""
    ids = list(source_ids)
    for model in (EPGEntry, EpgAlternateEntry, EpgSourceChannel, EpgChannelKey,
                  EpgSourceSubscription, EpgSourceRefresh, EpgFinding, EpgChannelStretch):
        db.session.execute(delete(model).where(model.source_id.in_(ids)))
    db.session.execute(update(EpgChannelStretch)
                       .where(EpgChannelStretch.after_source_id.in_(ids))
                       .values(after_source_id=None)
                       .execution_options(synchronize_session=False))
    db.session.execute(delete(EpgSourceAgreement).where(db.or_(
        EpgSourceAgreement.source_id.in_(ids), EpgSourceAgreement.other_source_id.in_(ids))))
    # A provider's list names it; the gap left in the positions is closed by the list's
    # next write (_renumber_list()).
    db.session.execute(delete(ProviderEpgOrder).where(ProviderEpgOrder.source_id.in_(ids)))


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
    expires_on = None
    raw_expiry = _text('expires_on')
    if raw_expiry:
        try:
            expires_on = date.fromisoformat(raw_expiry)
        except ValueError:
            errors.append('The expiration date must be a date like 2026-12-31.')
    return errors, {'name': name or default_source_name(account_name, EPG_SOURCE_URL),
                    'url': url, 'refresh_interval_hours': interval,
                    'enabled': bool(enabled), 'expires_on': expires_on}


#: How many days before a source's expiration date the heads-up starts (dev/changelog/1724).
#: Both source dialogs say "a week" in their Expires on help text; change them with it.
EXPIRY_NOTICE_DAYS = 7

#: Where a source stands against its expiration date (expiry_state()).
EXPIRY_LATER = 'later'      # more than EXPIRY_NOTICE_DAYS away
EXPIRY_SOON = 'soon'        # within EXPIRY_NOTICE_DAYS: the heads-up
EXPIRY_PASSED = 'passed'    # the day has come: expired


def expiry_state(expires_on: date | None, today: date) -> str | None:
    """None when no date is set, else one EXPIRY_*. `today` is the local date in the display
    timezone: a source expires at the start of its day there."""
    if expires_on is None:
        return None
    if today >= expires_on:
        return EXPIRY_PASSED
    if (expires_on - today).days <= EXPIRY_NOTICE_DAYS:
        return EXPIRY_SOON
    return EXPIRY_LATER


def expiry_view(expires_on: date | None, now: datetime, tz) -> dict | None:
    """A source row's expiration line: {text, warn}, or None when no date is set."""
    from .tz_utils import full_date_label
    today = to_local_date(now, tz)
    state = expiry_state(expires_on, today)
    if state is None:
        return None
    if state == EXPIRY_PASSED:
        return {'text': f'Expired {full_date_label(expires_on)}', 'warn': True, 'state': state}
    if state == EXPIRY_SOON:
        days = (expires_on - today).days
        text = ('Expires tomorrow' if days == 1
                else f'Expires {full_date_label(expires_on)}, in {days} days')
        return {'text': text, 'warn': True, 'state': state}
    if state == EXPIRY_LATER:
        return {'text': f'Expires {full_date_label(expires_on)}', 'warn': False, 'state': state}
    raise ValueError(f'unknown expiry state {state!r}')


def add_url_source(account: Account, fields: dict, *, account_guide: bool = False) -> EpgSource:
    """A new url source owned by `account`, subscribed last in its priority order
    (move_source() reorders it). Does not commit. Its directory is empty until its
    first refresh, so the caller starts one (accounts.start_source_refresh).

    `account_guide` marks it as an M3U account's own guide (§15.3): the add-account form's
    EPG URL, which fills the slot when the account is on a provider."""
    source = EpgSource(kind=EPG_SOURCE_URL, owner_account_id=account.id,
                       is_account_guide=account_guide, **fields)
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


def resolve_row(r, order: list[int], keys: dict, coverage: dict,
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

    A channel whose new winner holds none of its rows in either table is left as it is:
    the directory can say a source covers a channel before its listings are here (a key or
    a borrowed channel waiting for the source's next refresh), and moving the guide there
    would empty it until then (dev/changelog/1107). That source's import settles it. A
    source with no directory yet covers what it holds rows for (held_coverage()). A channel
    left with no winner keeps no alternates (§4).

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
    keys = key_answers(candidates)
    coverage = directory_coverage(candidates, case_sensitive)

    unrefreshed = sources_without_directory(candidates)
    held_rows = held_coverage(unrefreshed, [r.id for r in rows]) if unrefreshed else None
    decided = {}
    for r in rows:
        new, why = resolve_row(r, order.get(r.account_id, []), keys, coverage,
                                case_sensitive, held_rows)
        if new != r.epg_source_id:
            decided[r.id] = (new, why)
    held = holding_rows({new for new, _why in decided.values() if new is not None},
                         list(decided))

    changes: dict[int, tuple[int | None, int | None]] = {}
    reasons: dict[int, str] = {}
    demotions: dict[int, list[int]] = {}
    promotions: dict[int, list[int]] = {}
    leaving: dict[int | None, list[int]] = {}
    orphaned: list[int] = []
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
        leaving.setdefault(new, []).append(r.id)
        if new is not None:
            promotions.setdefault(new, []).append(r.id)
        else:
            orphaned.append(r.id)
    # Out before in: the old first source's rows and those of the sources that continued it.
    for new, ids in leaving.items():
        demote_others(ids, new)
    for sid, ids in promotions.items():
        promote(ids, sid)
    if orphaned:
        drop_alternates(orphaned)
    names = dict(source_names or {})
    names.update(db.session.query(EpgSource.id, EpgSource.name).filter(
        EpgSource.id.in_(list(promotions) + list(demotions))))
    apply_winners(changes, names, reasons)
    # Every channel asked about, not only the ones whose first source moved: a change of
    # order changes which sources continue a guide whose start stays where it was (§14.4).
    place_stretches([r.id for r in rows], case_sensitive)

    @retry_on_locked()
    def _recount_and_commit():
        for sid in set(promotions) | set(demotions):
            refresh_source_counts(sid)
        db.session.commit()

    _recount_and_commit()
    return {ch: new for ch, (_old, new) in changes.items()}


class RanOut(NamedTuple):
    """What retire_expired_listings() found for one source."""
    channel_ids: list[int]          # channels first found with nothing left from the source
    outcome: dict[int, int | None]  # reresolve_channels()' answer for the ones that moved
    # Since the source's last good refresh, not only in this call: the channels its listings
    # ran out for that have no guide from any source now, and how many it was guiding in
    # all. A source whose channels run out a few per failed refresh is judged on the total.
    dark_total: int
    guided: int


def _retired_rows(source_id: int):
    """The source's directory rows whose listings ran out: no entries, and a horizon. A row
    the file listed with no programs has no horizon, so the two are told apart."""
    return db.session.query(EpgSourceChannel.xml_id).filter(
        EpgSourceChannel.source_id == source_id, EpgSourceChannel.entry_count == 0,
        EpgSourceChannel.horizon_until.isnot(None))


RAN_OUT_ALL = 'all'     # every listing the source kept has passed
RAN_OUT_SOME = 'some'   # some channels' kept listings have passed, others are still ahead
#: What the Sources card says for each, as the page prints it.
RAN_OUT_NOTES = {
    RAN_OUT_ALL: 'Every listing it kept has passed. It has none until a refresh works.',
    RAN_OUT_SOME: ('The listings it kept have passed for some channels. It has none for those '
                   'until a refresh works.'),
}


def ran_out_states(source_ids) -> dict[int, str]:
    """{source_id: RAN_OUT_*} for the sources whose kept listings have run out, in whole or
    in part. A word and not a count: the directory counts the ids in the source's file,
    which says nothing about how many of an account's channels read them. One query."""
    source_ids = list(source_ids)
    if not source_ids:
        return {}
    retired = case((and_(EpgSourceChannel.entry_count == 0,
                         EpgSourceChannel.horizon_until.isnot(None)), 1), else_=0)
    live = case((EpgSourceChannel.entry_count > 0, 1), else_=0)
    return {sid: (RAN_OUT_SOME if n_live else RAN_OUT_ALL)
            for sid, n_retired, n_live in db.session.query(
                EpgSourceChannel.source_id, func.sum(retired), func.sum(live))
            .filter(EpgSourceChannel.source_id.in_(source_ids))
            .group_by(EpgSourceChannel.source_id) if n_retired}


def retire_expired_listings(source_id: int, case_sensitive: bool,
                            now: datetime | None = None) -> RanOut:
    """A refresh that kept a source's previous listings calls this: every directory row
    whose horizon has passed describes listings that no longer exist, so it stops counting
    as coverage, and the channels that took their guide from it are re-resolved - onto a
    source that still holds listings for them (a move from the alternates, never a fetch),
    or onto none.

    The directory is otherwise only rewritten by a successful import, so a source that
    keeps failing went on "covering" channels it held nothing for: 291 on prod, 61 of them
    with another source's listings unused in the alternates (dev/docs/BUGS.md 2026-11-05 @ 07:25 PM).
    Retiring is a recorded fact rather than a test of the clock inside the resolver, so
    every resolver reads the same answer and a healthy source whose file ends before its
    next refresh is left alone. The next successful import rewrites the directory.

    Every channel still on the source with a retired row is re-resolved on each call, not
    only the rows retired by this one, so a run that died between the two commits is
    finished by the next. `channel_ids` names a channel once: when its row is retired, or
    when its winner moves. `Channel.epg_source_override_id` is never touched."""
    now = now or datetime.utcnow()
    due = db.session.query(EpgSourceChannel.id, EpgSourceChannel.xml_id).filter(
        EpgSourceChannel.source_id == source_id, EpgSourceChannel.entry_count > 0,
        EpgSourceChannel.horizon_until < now).all()
    for chunk in _chunks([row_id for row_id, _xml in due]):
        @retry_on_locked()
        def _retire_and_commit(chunk=chunk):
            db.session.execute(
                update(EpgSourceChannel).where(EpgSourceChannel.id.in_(chunk))
                .values(entry_count=0, distinct_titles=0, sole_title=None,
                        upcoming_titles=None)
                .execution_options(synchronize_session=False))
            db.session.commit()
        _retire_and_commit()

    retired = {norm_key(x, case_sensitive) for (x,) in _retired_rows(source_id)}
    if not retired:
        return RanOut([], {}, 0, 0)
    # Ids differing only in case fold to one key, and the key is covered while any of them
    # still has listings (directory_coverage()).
    retired -= {norm_key(x, case_sensitive) for (x,) in db.session.query(
        EpgSourceChannel.xml_id).filter(EpgSourceChannel.source_id == source_id,
                                        EpgSourceChannel.entry_count > 0)}
    just_now = {norm_key(x, case_sensitive) for _id, x in due} & retired
    on_source = db.session.query(Channel.id, Channel.epg_channel_id).filter(
        Channel.epg_source_id == source_id, Channel.hidden.is_(False)).all()
    keys = key_answers([source_id])

    def retired_key(ch_id, epg_channel_id):
        k = channel_key(epg_channel_id, keys.get((ch_id, source_id)))
        k = norm_key(k, case_sensitive) if k else None
        return k if k in retired else None

    dark, new = [], set()
    for ch_id, epg_channel_id in on_source:
        k = retired_key(ch_id, epg_channel_id)
        if k is not None:
            dark.append(ch_id)
            if k in just_now:
                new.add(ch_id)
    outcome = reresolve_channels(dark, case_sensitive) if dark else {}
    # A channel whose guide this source continued has nothing left from it either.
    filled = stretch_channel_ids(source_id)
    if filled:
        place_stretches(filled, case_sensitive, now)

    # Everything this source has left dark since its last good refresh: the channels still
    # pointing at it with nothing to show, and the ones a call like this one left with no
    # source at all. Read back from the tables, so it needs no tally kept between calls.
    waiting = sum(1 for c in dark if c not in outcome)
    left_now = sum(1 for c in dark if c in outcome and outcome[c] is None)
    readers = subscriber_ids(source_id)
    no_source = 0
    if readers:
        no_source = sum(1 for ch_id, epg_channel_id in db.session.query(
            Channel.id, Channel.epg_channel_id).filter(
            Channel.account_id.in_(readers), Channel.epg_source_id.is_(None),
            Channel.hidden.is_(False)) if retired_key(ch_id, epg_channel_id) is not None)
    return RanOut([c for c in dark if c in new or c in outcome], outcome,
                  dark_total=waiting + no_source,
                  guided=len(on_source) + max(no_source - left_now, 0))


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
    filled = stretch_channel_ids(source_id)

    @retry_on_locked()
    def _delete_and_commit():
        _delete_source_rows([source_id])
        for col in (Channel.epg_source_id, Channel.epg_source_override_id):
            db.session.execute(update(Channel).where(col == source_id).values({col: None})
                               .execution_options(synchronize_session=False))
        db.session.execute(delete(EpgSource).where(EpgSource.id == source_id))
        db.session.commit()

    _delete_and_commit()
    outcome = reresolve_channels(affected, case_sensitive,
                                 previous={cid: source_id for cid in affected},
                                 source_names={source_id: name})
    # The channels it continued: a source behind it may reach past the end now.
    place_stretches(filled, case_sensitive)
    return _removal_result(affected, outcome)


def _disable_if_unread(source_id: int) -> None:
    """§3: a source nobody subscribes to any more is not fetched either (`enabled = 0`).
    Does not commit."""
    source = db.session.get(EpgSource, source_id)
    if source is not None and not subscriber_ids(source_id):
        source.enabled = False


def stop_using_source(account_id: int, source_id: int, case_sensitive: bool, *,
                      own_order_ok: bool = False) -> SourceRemoval | None:
    """The account stops reading a source it subscribes to (§3: "no subscription for
    ignoring the result"). Its listings for this account's channels go - the next import
    would not write them anyway - and each channel it was the guide for moves to its next
    source. When nobody reads the source any more it is also disabled, so the fetch is
    skipped too (§3: "enabled = 0 for skipping the fetch"). The source, its directory and
    the users' keys into it are kept, so Use again restores it. None if not subscribed.

    On an account following its provider's order, stopping a source the provider's list
    placed gives the account its own order (§15.3): refused with NeedsOwnOrder unless the
    caller says the user confirmed that (`own_order_ok`)."""
    sub = EpgSourceSubscription.query.filter_by(account_id=account_id,
                                                source_id=source_id).first()
    if sub is None:
        return None
    goes_own = source_id in placed_for_account(account_id)
    if goes_own and not own_order_ok:
        raise NeedsOwnOrder(own_order_sentence(account_id))
    affected = [cid for (cid,) in db.session.query(Channel.id).filter(
        Channel.account_id == account_id, Channel.epg_source_id == source_id)]
    filled = stretch_channel_ids(source_id, account_id)
    mine = select(Channel.id).where(Channel.account_id == account_id)

    @retry_on_locked()
    def _unsubscribe_and_commit():
        if goes_own:
            set_epg_order_mode(db.session.get(Account, account_id), EPG_ORDER_OWN,
                               'stopped using a source its provider placed')
        db.session.execute(delete(EpgSourceSubscription).where(
            EpgSourceSubscription.account_id == account_id,
            EpgSourceSubscription.source_id == source_id))
        db.session.execute(delete(EpgFinding).where(EpgFinding.account_id == account_id,
                                                    EpgFinding.source_id == source_id))
        for model in (EPGEntry, EpgAlternateEntry):
            db.session.execute(delete(model).where(_of_source(model, source_id),
                                                   model.channel_id.in_(mine)))
        db.session.execute(delete(EpgChannelStretch).where(
            EpgChannelStretch.source_id == source_id,
            EpgChannelStretch.channel_id.in_(mine)))
        db.session.execute(update(Channel).where(
            Channel.account_id == account_id, Channel.epg_source_id == source_id)
            .values(epg_source_id=None).execution_options(synchronize_session=False))
        _disable_if_unread(source_id)
        refresh_source_counts(source_id)
        db.session.commit()

    _unsubscribe_and_commit()
    outcome = reresolve_channels(affected, case_sensitive,
                                 previous={cid: source_id for cid in affected})
    place_stretches(filled, case_sensitive)
    return _removal_result(affected, outcome)


def use_source_again(account_id: int, source_id: int) -> bool:
    """Undo stop_using_source: subscribe at the end of the account's priority order and
    enable the source. Does not commit. Its listings come back at its next refresh. False
    when already subscribed.

    A source the account does not own (its provider's, §15.2) is switched on only when
    nobody reads it - off because unread - never when its owner turned it off by hand and
    other accounts still read it."""
    if EpgSourceSubscription.query.filter_by(account_id=account_id,
                                             source_id=source_id).first() is not None:
        return False
    source = db.session.get(EpgSource, source_id)
    if source is None:
        return False
    if source.owner_account_id == account_id or not subscriber_ids(source_id):
        source.enabled = True
    db.session.add(EpgSourceSubscription(source_id=source_id, account_id=account_id,
                                         priority=_next_priority(account_id)))
    return True


def moved_order(items: list, item, step: int) -> list | None:
    """`items` with `item` one place up (`step` -1) or down (+1): the same list when it is
    already first or last, None when it is not there. Pure; the move and its preview both
    take their new order from here."""
    if item not in items:
        return None
    i = items.index(item)
    j = i + step
    if not 0 <= j < len(items):
        return list(items)
    out = list(items)
    out[i], out[j] = out[j], out[i]
    return out


class Reorder(NamedTuple):
    """What moving a source in an account's priority order did."""
    moved: bool      # False: already first (or last), nothing changed
    switched: int    # channels whose guide now comes from a different source
    went_own: bool = False   # the move gave the account its own order (§15.3)


def move_source(account_id: int, source_id: int, step: int,
                case_sensitive: bool, *, own_order_ok: bool = False) -> Reorder | None:
    """Move a source one place up (`step` -1) or down (+1) in the account's priority order
    (§5.2), then re-decide every visible channel's winner and move the listings (§5.4) -
    from rows already held, since every source the account reads keeps its listings for
    these channels in one table or the other. Priorities are renumbered 1..n, closing the
    gaps Stop using leaves. None when the account does not read the source. Commits.

    On an account following its provider's order, a move that changes what the provider's
    list placed - including putting one of the account's own sources above it - gives the
    account its own order (§15.3): refused with NeedsOwnOrder unless `own_order_ok`."""
    if step not in (-1, 1):
        raise ValueError(f'step must be -1 or 1, not {step!r}')
    placed = placed_for_account(account_id)

    @retry_on_locked()
    def _reorder_and_commit():
        subs = (EpgSourceSubscription.query.filter_by(account_id=account_id)
                .order_by(EpgSourceSubscription.priority).all())
        ids = [s.source_id for s in subs]
        trial = moved_order(ids, source_id, step)
        if trial is None:
            return None, False
        if trial == ids:
            return False, False
        went_own = bool(placed) and not still_following(placed, trial)
        if went_own and not own_order_ok:
            raise NeedsOwnOrder(own_order_sentence(account_id))
        if went_own:
            set_epg_order_mode(db.session.get(Account, account_id), EPG_ORDER_OWN,
                               'moved a source its provider placed')
        subs.sort(key=lambda s: trial.index(s.source_id))
        # uq_epg_sub_account_priority is checked row by row, so the new numbers are reached
        # through negative ones nobody holds.
        for n, sub in enumerate(subs, 1):
            sub.priority = -n
        db.session.flush()
        for n, sub in enumerate(subs, 1):
            sub.priority = n
        db.session.commit()
        return True, went_own

    moved, went_own = _reorder_and_commit()
    if moved is None:
        return None
    if not moved:
        return Reorder(False, 0)
    return Reorder(True, len(reresolve_account(account_id, case_sensitive)), went_own)


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
    this account reads that has no directory yet.

    Another provider's source is offered like another account's (§15.2). This account's own
    provider's sources are not: they come with following its order, or Use again on their
    row (account_sources_view() lists them)."""
    order = subscriptions_for([account_id])[account_id]
    account = db.session.get(Account, account_id)
    mine = account.provider_id if account is not None else None
    others = db.or_(EpgSource.owner_account_id != account_id,
                    and_(EpgSource.owner_account_id.is_(None),
                         EpgSource.owner_provider_id != mine) if mine is not None
                    else EpgSource.owner_account_id.is_(None))
    cands = [s for s in EpgSource.query.filter(others)
             .order_by(EpgSource.owner_account_id, EpgSource.id) if s.id not in order]
    if not cands:
        return []
    rows = db.session.query(Channel.id, Channel.epg_channel_id, Channel.hidden,
                            Channel.epg_source_override_id, Channel.epg_source_id).filter(
        Channel.account_id == account_id, Channel.hidden.is_(False)).all()
    ids = set(order) | {s.id for s in cands} | {
        r.epg_source_override_id for r in rows if r.epg_source_override_id}
    keys = key_answers(ids)
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
                new, _why = resolve_row(r, trial, keys, coverage, case_sensitive,
                                         held_rows)
                if new == s.id:
                    if r.epg_source_id is None:
                        fills += 1
                    else:
                        switches += 1
        out.append({'id': s.id, 'name': s.name, 'kind': KIND_LABELS.get(s.kind, s.kind),
                    'is_url': s.kind == EPG_SOURCE_URL, 'enabled': bool(s.enabled),
                    'owner_id': s.owner_account_id,
                    'owner_name': owner_label(s),
                    'last_success_at': s.last_success_at,
                    'covers': covers, 'fills': fills, 'switches': switches})
    return out


def _held_listings(source_id: int, case_sensitive: bool,
                   variants: dict[str, dict[str, int]]) -> dict[str, int]:
    """{file id: channel id} - for each id of the source's file, one visible
    channel on an account reading it that holds the source's listings for it now. Any one
    will do: the importer maps every channel whose key reads that id onto the same programs
    (§7.1), so their rows are identical. Keyed on the id read (file_id_for() over
    `variants`, folded_variants()), not the folded key: two keys differing only in case can
    read different ids (dev/changelog/1139)."""
    readers = subscriber_ids(source_id)
    if not readers:
        return {}
    # In either table: a donor whose guide the source continues holds it in both (§14).
    holding: set[int] = set()
    for model in (EPGEntry, EpgAlternateEntry):
        holding.update(cid for (cid,) in db.session.query(model.channel_id).filter(
            model.source_id == source_id).distinct())
    if not holding:
        return {}
    user = {ch: k for (ch, _sid), k in key_answers([source_id]).items()}
    out: dict[str, int] = {}
    for cid, provider_id in (db.session.query(Channel.id, Channel.epg_channel_id)
                             .filter(Channel.account_id.in_(readers),
                                     Channel.hidden.is_(False)).order_by(Channel.id)):
        if cid not in holding:
            continue
        k = channel_key(provider_id, user.get(cid))
        fid = file_id_for(k, variants.get(norm_key(k, case_sensitive), {})) if k else None
        if fid is not None:
            out.setdefault(fid, cid)
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
    # The donor ids are named in the WHERE as well as the join: given only the join, SQLite
    # read the source's whole run of rows once per chunk (1.2 s a chunk, 3 minutes to give
    # one provider guide to two accounts on prod's data, dev/changelog/1721).
    donors = sorted({d for _t, d in pairs})
    db.session.execute(insert(EpgAlternateEntry).from_select(
        [getattr(EpgAlternateEntry, c) for c in _ENTRY_COLUMNS],
        select(*cols).join(mapping, model.channel_id == mapping.c.donor)
        .where(_of_source(model, source_id), model.channel_id.in_(donors))))


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
    covered, copied = copy_held_listings(account_id, source_id, case_sensitive)
    outcome = reresolve_channels(copied, case_sensitive, source_names={source_id: name})

    @retry_on_locked()
    def _recount_and_commit():
        refresh_source_counts(source_id)
        db.session.commit()

    _recount_and_commit()
    guided = sum(1 for new in outcome.values() if new == source_id)
    return Borrowed(refreshed, covered, len(copied), guided, turned_on)


def copy_pairs(account_id: int, source_id: int,
               case_sensitive: bool) -> tuple[int, list[tuple[int, int]]]:
    """What copy_held_listings() would copy, without copying it: (channels of the account
    the source covers, [(channel, donor channel whose rows it gets)]). Reads only; the
    reorder preview (preview_orders()) reads the donors' rows in the target's place."""
    variants = folded_variants(source_id, case_sensitive)
    donors = _held_listings(source_id, case_sensitive, variants)
    coverage = directory_coverage([source_id], case_sensitive)[source_id]
    user = {ch: k for (ch, _sid), k in key_answers([source_id]).items()}
    covered, pairs = 0, []
    for cid, provider_id in db.session.query(Channel.id, Channel.epg_channel_id).filter(
            Channel.account_id == account_id, Channel.hidden.is_(False)).order_by(Channel.id):
        k = channel_key(provider_id, user.get(cid))
        nk = norm_key(k, case_sensitive) if k else None
        if nk is None or nk not in coverage:
            continue
        covered += 1
        fid = file_id_for(k, variants.get(nk, {}))
        donor = donors.get(fid)
        if donor is not None and donor != cid:
            pairs.append((cid, donor))
    return covered, pairs


def copy_held_listings(account_id: int, source_id: int,
                       case_sensitive: bool) -> tuple[int, list[int]]:
    """For an account that has just started reading a source: copy into the alternates what
    the database already holds of it for each of the account's visible channels - the rule
    set_channel_key() follows (dev/changelog/1102). A channel on another reader whose key
    into the source reads the same id holds exactly what the next import would write. A
    covered key no reader carries waits for the source's next refresh. Returns (channels
    the source covers, channels copied); the caller re-resolves. Commits, in chunks."""
    covered, pairs = copy_pairs(account_id, source_id, case_sensitive)
    for pairs_batch in chunked(pairs, _COPY_CHUNK):
        @retry_on_locked()
        def _copy_and_commit(batch=pairs_batch):
            targets = [t for t, _d in batch]
            for model in (EPGEntry, EpgAlternateEntry):
                db.session.execute(delete(model).where(_of_source(model, source_id),
                                                       model.channel_id.in_(targets)))
            for model in (EPGEntry, EpgAlternateEntry):
                _copy_listings(batch, source_id, model)
            db.session.commit()
        _copy_and_commit()
    return covered, [t for t, _d in pairs]


def reresolve_account(account_id: int, case_sensitive: bool) -> dict[int, int | None]:
    """reresolve_channels() over every visible channel of one account - what a change to its
    order owes. Commits."""
    visible = [cid for (cid,) in db.session.query(Channel.id).filter(
        Channel.account_id == account_id, Channel.hidden.is_(False))]
    return reresolve_channels(visible, case_sensitive)


# ── Provider-level sources (DESIGN-epg-sources.md §15, dev/changelog/1721) ─────
#
# A provider may own url sources, and has an ordered list in which one entry is a slot,
# "Provider's EPG", that each account's own guide fills. epg_source_subscriptions stays the
# one thing every resolver reads: a following account's rows are written from the provider's
# list by apply_provider_order(), so no resolver, import or page learns a second place to
# look. Account.epg_order_mode is the user's answer and has one writer, set_epg_order_mode();
# provider_epg_order is written here and nowhere else.

class NeedsOwnOrder(Exception):
    """The action would give a following account its own order, and the user has not been
    told. The message is the sentence the confirmation shows (§15.3)."""


def owner_label(source: EpgSource) -> str | None:
    """The name of the account or provider that owns a source."""
    if source.owner_provider_id is not None:
        return source.owner_provider.name if source.owner_provider else None
    return source.owner.name if source.owner else None


def own_guide_ids(account_ids) -> dict[int, int]:
    """{account_id: source_id} of each account's own guide (§15.3): its provider-kind source,
    else the url source marked is_account_guide. Accounts with neither are absent. One
    query."""
    ids = list(account_ids)
    if not ids:
        return {}
    out: dict[int, int] = {}
    marked: dict[int, int] = {}
    for sid, aid, kind in (db.session.query(EpgSource.id, EpgSource.owner_account_id,
                                            EpgSource.kind)
                           .filter(EpgSource.owner_account_id.in_(ids),
                                   db.or_(EpgSource.kind == EPG_SOURCE_PROVIDER,
                                          EpgSource.is_account_guide.is_(True)))
                           .order_by(EpgSource.id)):
        if kind == EPG_SOURCE_PROVIDER:
            out.setdefault(aid, sid)
        else:
            marked.setdefault(aid, sid)
    for aid, sid in marked.items():
        out.setdefault(aid, sid)
    return out


def provider_list(provider_id: int) -> list[ProviderEpgOrder]:
    return (ProviderEpgOrder.query.filter_by(provider_id=provider_id)
            .order_by(ProviderEpgOrder.position).all())


def placed_by_provider(entries, own_guide: int | None) -> list[int]:
    """The source ids a provider's list places for one account, in order: each source, and
    the slot as the account's own guide, left out when it has none. Pure."""
    out: list[int] = []
    for e in entries:
        sid = own_guide if e.entry == PROVIDER_ORDER_OWN_GUIDE else e.source_id
        if sid is not None and sid not in out:
            out.append(sid)
    return out


def follow_order(placed: list[int], present: list[int]) -> list[int]:
    """A following account's subscriptions: what the provider's list placed, then the
    account's other sources in the order it already has them (§15.3). Pure."""
    return placed + [s for s in present if s not in placed]


def still_following(placed: list[int], order: list[int]) -> bool:
    """Whether an account order keeps what the provider placed first and in its order, with
    only the account's own sources after it. Pure."""
    return order[:len(placed)] == placed


def follows(account: Account | None) -> bool:
    return (account is not None and account.provider_id is not None
            and account.epg_order_mode == EPG_ORDER_FOLLOW)


def placed_for_account(account_id: int) -> list[int]:
    """What the account's provider's list places for it, or [] when it does not follow one."""
    account = db.session.get(Account, account_id)
    if not follows(account):
        return []
    return placed_by_provider(provider_list(account.provider_id),
                              own_guide_ids([account_id]).get(account_id))


def own_order_sentence(account_id: int) -> str:
    account = db.session.get(Account, account_id)
    provider = (db.session.get(Provider, account.provider_id)
                if account is not None and account.provider_id else None)
    who = account.name if account is not None else 'this account'
    whose = f"{provider.name}'s" if provider is not None else "its provider's"
    return f'This gives {who} its own EPG order; it will stop following {whose}.'


def set_epg_order_mode(account: Account, mode: str | None, why: str) -> bool:
    """The one writer of Account.epg_order_mode (§15.3): the user's answer, moved only by a
    user action and named in the log with what did it. NULL means the account is on no
    provider. Does not commit. Returns True when it changed."""
    if mode is not None and mode not in EPG_ORDER_MODES:
        raise ValueError(f'unknown EPG order mode {mode!r}')
    if account.epg_order_mode == mode:
        return False
    old = account.epg_order_mode
    account.epg_order_mode = mode
    log.info('Account %d (%s): EPG order %s -> %s (%s)', account.id, account.name, old, mode, why)
    return True


def _write_order(account_id: int, order: list[int]) -> list[int]:
    """Make the account's subscriptions `order`, priority 1..n, subscribing to any source in
    it the account does not read yet. A subscription `order` leaves out keeps its place after
    it - nothing is unsubscribed here. A source nobody read (switched off by
    _disable_if_unread()) is switched back on. Returns the source ids newly subscribed. Does
    not commit."""
    subs = (EpgSourceSubscription.query.filter_by(account_id=account_id)
            .order_by(EpgSourceSubscription.priority).all())
    by_sid = {s.source_id: s for s in subs}
    full = list(dict.fromkeys(order)) + [s.source_id for s in subs if s.source_id not in order]
    if full == [s.source_id for s in subs]:
        return []
    added = [sid for sid in full if sid not in by_sid]
    for sid in added:
        src = db.session.get(EpgSource, sid)
        if src is not None and not src.enabled and not subscriber_ids(sid):
            src.enabled = True
    # uq_epg_sub_account_priority is checked row by row: reach the new numbers through
    # negative ones nobody holds, as move_source() does.
    for n, sub in enumerate(subs, 1):
        sub.priority = -n
    for k, sid in enumerate(added, len(subs) + 1):
        sub = EpgSourceSubscription(source_id=sid, account_id=account_id, priority=-k)
        db.session.add(sub)
        by_sid[sid] = sub
    db.session.flush()
    for n, sid in enumerate(full, 1):
        by_sid[sid].priority = n
    db.session.flush()
    return added


def apply_provider_order(account_id: int) -> list[int]:
    """Write a following account's subscriptions from its provider's list (§15.3): the
    list in order with the slot as the account's own guide, then its other sources in the
    order it had them. A no-op for an account that does not follow a provider. Returns the
    source ids newly subscribed, whose held listings the caller copies after the commit
    (copy_held_listings()) before re-resolving. Does not commit."""
    account = db.session.get(Account, account_id)
    if not follows(account):
        return []
    placed = placed_by_provider(provider_list(account.provider_id),
                                own_guide_ids([account_id]).get(account_id))
    present = subscriptions_for([account_id])[account_id]
    return _write_order(account_id, follow_order(placed, present))


def _append_to_own_order(account_id: int, source_id: int) -> list[int]:
    """A source the provider gained goes at the end of an own-order account's list (§15.3).
    Does not commit."""
    present = subscriptions_for([account_id])[account_id]
    if source_id in present:
        return []
    return _write_order(account_id, present + [source_id])


def ensure_provider_list(provider_id: int) -> None:
    """Every provider's list holds the slot (§15.3); a provider created after migration 148
    gets [Provider's EPG] here. Does not commit."""
    if ProviderEpgOrder.query.filter_by(provider_id=provider_id).first() is None:
        db.session.add(ProviderEpgOrder(provider_id=provider_id, position=1,
                                        entry=PROVIDER_ORDER_OWN_GUIDE))
        db.session.flush()


def _renumber_list(entries: list[ProviderEpgOrder]) -> None:
    """Positions 1..n in the given order, through negatives (uq_provider_epg_order_position
    is checked row by row). Does not commit."""
    for n, e in enumerate(entries, 1):
        e.position = -n
    db.session.flush()
    for n, e in enumerate(entries, 1):
        e.position = n
    db.session.flush()


def _list_append(provider_id: int, source_id: int) -> None:
    ensure_provider_list(provider_id)
    top = db.session.query(func.max(ProviderEpgOrder.position)).filter(
        ProviderEpgOrder.provider_id == provider_id).scalar() or 0
    db.session.add(ProviderEpgOrder(provider_id=provider_id, position=top + 1,
                                    entry=PROVIDER_ORDER_SOURCE, source_id=source_id))
    db.session.flush()


def provider_account_ids(provider_id: int) -> list[int]:
    return [aid for (aid,) in db.session.query(Account.id).filter(
        Account.provider_id == provider_id).order_by(Account.id)]


def _give_to_provider_accounts(provider_id: int, source_id: int) -> dict[int, list[int]]:
    """Every account on the provider reads a source its list just gained: a following account
    through its list, an own-order one at the end of its own (§15.3). Returns {account_id:
    sources newly subscribed}. Does not commit."""
    out = {}
    for aid in provider_account_ids(provider_id):
        account = db.session.get(Account, aid)
        out[aid] = (apply_provider_order(aid) if follows(account)
                    else _append_to_own_order(aid, source_id))
    return out


def settle_accounts(added: dict[int, list[int]], case_sensitive: bool,
                    moved: dict[int, set[int]] | None = None) -> int:
    """After an order change has committed: copy what the database holds for each source an
    account newly reads, then re-decide each account's channels from rows held (§5.4).
    Returns how many channels' guide changed source. Commits.

    `moved` ({account_id: source ids}) says the change only joined or moved those sources in
    an account's order, the others keeping their order among themselves. Then only a channel
    holding one of their listings can change winner - a channel switches only to a source it
    holds rows of, or away from one it holds - so only those are re-decided: on prod's
    largest account a few thousand channels, where a whole-account re-resolve is 28,000. An
    account absent from `moved` (or `moved` None) is re-decided whole."""
    switched = 0
    for aid, sources in added.items():
        for sid in sources:
            copy_held_listings(aid, sid, case_sensitive)
        if moved is None or aid not in moved:
            switched += len(reresolve_account(aid, case_sensitive))
        else:
            ids = sorted({cid for sid in moved[aid] for cid in _holding_channels(aid, sid)})
            switched += len(reresolve_channels(ids, case_sensitive))
        for sid in sources:
            @retry_on_locked()
            def _recount_and_commit(sid=sid):
                refresh_source_counts(sid)
                db.session.commit()
            _recount_and_commit()
    return switched


def _holding_channels(account_id: int, source_id: int) -> list[int]:
    """The account's visible channels holding any of the source's listings, in either
    table. A statement over the whole source, so it walks the source index rather than
    going through _of_source() (whose channel-index plan suits a statement naming a few
    channels)."""
    out: set[int] = set()
    for model in (EPGEntry, EpgAlternateEntry):
        out.update(cid for (cid,) in db.session.query(model.channel_id).join(
            Channel, Channel.id == model.channel_id).filter(
            model.source_id == source_id, Channel.account_id == account_id,
            Channel.hidden.is_(False)).distinct())
    return sorted(out)


def clean_provider_source_fields(data: dict, provider_name: str) -> tuple[list[str], dict]:
    """clean_url_source_fields() for a provider's source, which has no account sync to ride
    and so must refresh on an interval of its own (§15.2)."""
    errors, fields = clean_url_source_fields(data, provider_name)
    if not errors and not fields['refresh_interval_hours']:
        errors.append("Choose how often to refresh it: a provider's source has no account "
                      'sync to refresh with.')
    return errors, fields


def add_provider_source(provider_id: int, fields: dict) -> tuple[EpgSource, dict[int, list[int]]]:
    """A new url source owned by the provider, last on its list and read by every account on
    it (§15.2, §15.3). Nothing is held for it yet, so nothing needs re-resolving until its
    first import, which the caller starts. Returns (source, {account_id: newly subscribed}).
    Does not commit."""
    source = EpgSource(kind=EPG_SOURCE_URL, owner_provider_id=provider_id, **fields)
    db.session.add(source)
    db.session.flush()
    _list_append(provider_id, source.id)
    return source, _give_to_provider_accounts(provider_id, source.id)


class ProviderMove(NamedTuple):
    """What moving a source between an account and its provider did."""
    readers: int     # accounts reading it now
    switched: int    # channels whose guide changed source


def move_source_to_provider(source_id: int, provider_id: int, interval: int | None,
                            case_sensitive: bool) -> ProviderMove:
    """An account's url source becomes its provider's (§15.4, "Move to Strong"): ownership
    changes, the list gains it at the end and every account on the provider reads it.
    Nothing is fetched: the other accounts get what the database already holds for it. A
    source that refreshed with its account's sync takes `interval` (§15.2). The caller has
    checked the source is a url source owned by an account on this provider, and not that
    account's own guide. Commits."""
    @retry_on_locked()
    def _move_and_commit():
        src = db.session.get(EpgSource, source_id)
        src.owner_account_id = None
        src.owner_provider_id = provider_id
        src.is_account_guide = False
        if not src.refresh_interval_hours:
            src.refresh_interval_hours = interval
        db.session.flush()
        _list_append(provider_id, source_id)
        added = _give_to_provider_accounts(provider_id, source_id)
        db.session.commit()
        return added

    added = _move_and_commit()
    switched = settle_accounts(added, case_sensitive,
                               moved={aid: {source_id} for aid in added})
    return ProviderMove(len(subscriber_ids(source_id)), switched)


def move_source_to_account(source_id: int, account_id: int,
                           case_sensitive: bool) -> SourceRemoval:
    """A provider's source becomes one account's alone (§15.3, the other way to take it off
    the list): ownership changes, it leaves the list, and every other account reading it
    stops - their listings from it go and their channels re-resolve. Its interval is kept.
    The caller has checked the account is on the provider. Returns what the other accounts'
    channels lost. Commits."""
    @retry_on_locked()
    def _move_and_commit():
        src = db.session.get(EpgSource, source_id)
        pid = src.owner_provider_id
        src.owner_provider_id = None
        src.owner_account_id = account_id
        db.session.execute(delete(ProviderEpgOrder).where(ProviderEpgOrder.source_id == source_id))
        db.session.flush()
        if pid is not None:
            _renumber_list(provider_list(pid))
        present = subscriptions_for([account_id])[account_id]
        added = [] if source_id in present else _write_order(account_id, present + [source_id])
        added += apply_provider_order(account_id)
        db.session.commit()
        return added

    added = _move_and_commit()
    # Copied before the others stop: their rows are what it copies.
    settle_accounts({account_id: added}, case_sensitive)
    affected, switched = 0, 0
    for aid in subscriber_ids(source_id):
        if aid == account_id:
            continue
        done = stop_using_source(aid, source_id, case_sensitive, own_order_ok=True)
        if done is not None:
            affected += done.affected
            switched += done.switched
    return SourceRemoval(affected, switched, affected - switched)


def _traded(entries, new_ids: list) -> list:
    """The entries whose place `new_ids` changes: the two a one-place move traded."""
    return [e for i, e in enumerate(entries) if new_ids[i] != e.id]


def _entry_sources(entries, own_guide: int | None) -> set[int]:
    """The source ids these list entries stand for on one account: the slot is its own
    guide, left out when it has none."""
    return {own_guide if e.entry == PROVIDER_ORDER_OWN_GUIDE else e.source_id
            for e in entries} - {None}


def move_provider_entry(provider_id: int, entry_id: int, step: int,
                        case_sensitive: bool) -> Reorder | None:
    """Move one entry of the provider's list up (-1) or down (+1) (§15.3, "Changing the
    provider's list"): every following account's subscriptions are rewritten at once and
    its channels re-resolved from rows held. Own-order accounts are untouched. None when the
    entry is not on this provider's list. Commits."""
    if step not in (-1, 1):
        raise ValueError(f'step must be -1 or 1, not {step!r}')

    @retry_on_locked()
    def _move_and_commit():
        entries = provider_list(provider_id)
        ids = [e.id for e in entries]
        new = moved_order(ids, entry_id, step)
        if new is None:
            return None
        if new == ids:
            return False
        swapped = _traded(entries, new)
        entries.sort(key=lambda e: new.index(e.id))
        _renumber_list(entries)
        following = [aid for aid in provider_account_ids(provider_id)
                     if follows(db.session.get(Account, aid))]
        guides = own_guide_ids(following)
        added, moved = {}, {}
        for aid in following:
            added[aid] = apply_provider_order(aid)
            moved[aid] = _entry_sources(swapped, guides.get(aid))
        db.session.commit()
        return added, moved

    result = _move_and_commit()
    if result is None:
        return None
    if result is False:
        return Reorder(False, 0)
    added, moved = result
    return Reorder(True, settle_accounts(added, case_sensitive, moved))


def follow_provider_again(account_id: int, case_sensitive: bool) -> int:
    """"Follow <provider>'s order again" (§15.3): the order mode back to follow and the
    provider's list re-applied, re-subscribing to any of its sources the account had dropped,
    its own sources after. Returns how many channels' guide changed source. Commits."""
    @retry_on_locked()
    def _follow_and_commit():
        account = db.session.get(Account, account_id)
        set_epg_order_mode(account, EPG_ORDER_FOLLOW, "chose to follow its provider's order again")
        ensure_provider_list(account.provider_id)
        added = apply_provider_order(account_id)
        db.session.commit()
        return added

    return settle_accounts({account_id: _follow_and_commit()}, case_sensitive)


def set_account_guide(account_id: int, source_id: int, case_sensitive: bool) -> int:
    """Make one of an M3U account's own url sources its own guide (§15.3), the one that
    fills the provider's slot; the mark leaves whichever source held it. On a following
    account the slot moves with it. The caller has checked the source is a url source the
    account owns. Returns how many channels changed source. Commits."""
    @retry_on_locked()
    def _mark_and_commit():
        for src in EpgSource.query.filter_by(owner_account_id=account_id).all():
            src.is_account_guide = src.id == source_id
        db.session.flush()
        added = apply_provider_order(account_id)
        db.session.commit()
        return added

    added = _mark_and_commit()
    return settle_accounts({account_id: added}, case_sensitive) if placed_for_account(account_id) else 0


class ProviderChange(NamedTuple):
    """What an account joining or leaving a provider did to its guide."""
    switched: int    # channels whose guide changed source (joining)
    removed: SourceRemoval | None   # what leaving the old provider's sources cost


def provider_changed(account_id: int, old_provider_id: int | None,
                     new_provider_id: int | None, case_sensitive: bool) -> ProviderChange:
    """The EPG half of an account moving between providers (§15.5), after the move itself
    has committed (account_links.set_account_provider()). Leaving: the account stops using
    the old provider's sources - their listings for its channels go and the channels
    re-resolve from rows held - and its own sources are untouched. Joining: it follows the
    new provider's order. Commits."""
    removed = None
    if old_provider_id is not None and old_provider_id != new_provider_id:
        theirs = [sid for (sid,) in db.session.query(EpgSourceSubscription.source_id)
                  .join(EpgSource, EpgSource.id == EpgSourceSubscription.source_id)
                  .filter(EpgSourceSubscription.account_id == account_id,
                          EpgSource.owner_provider_id == old_provider_id)]
        affected = switched = 0
        for sid in theirs:
            done = stop_using_source(account_id, sid, case_sensitive, own_order_ok=True)
            if done is not None:
                affected += done.affected
                switched += done.switched
        removed = SourceRemoval(affected, switched, affected - switched)
    switched = 0
    if new_provider_id is not None and old_provider_id != new_provider_id:
        @retry_on_locked()
        def _join_and_commit():
            account = db.session.get(Account, account_id)
            set_epg_order_mode(account, EPG_ORDER_FOLLOW, 'joined a provider')
            ensure_provider_list(new_provider_id)
            added = apply_provider_order(account_id)
            db.session.commit()
            return added
        switched = settle_accounts({account_id: _join_and_commit()}, case_sensitive)
    elif new_provider_id is None:
        @retry_on_locked()
        def _leave_and_commit():
            set_epg_order_mode(db.session.get(Account, account_id), None, 'left its provider')
            db.session.commit()
        _leave_and_commit()
    return ProviderChange(switched, removed)


class ProviderRelease(NamedTuple):
    """What deleting a provider did with its sources (§15.5)."""
    handed: list      # [(source name, account name)] kept for the accounts reading them
    deleted: list     # [source id] deleted with the provider
    removed: SourceRemoval


def release_provider_sources(provider_id: int, keep: bool,
                             case_sensitive: bool) -> ProviderRelease:
    """Before a provider is deleted (§15.5, §17 Q3). Kept, each source is handed to the
    lowest-numbered account still reading it with every subscription kept, so no guide
    changes; turned off, the sources are deleted and their channels re-resolve. Either way a
    source no account reads is deleted, and the provider's list goes. The caller removes the
    deleted sources' scheduler jobs and alerts. Commits."""
    sources = EpgSource.query.filter_by(owner_provider_id=provider_id).order_by(EpgSource.id).all()
    plan = [(s.id, s.name, sorted(subscriber_ids(s.id))) for s in sources]
    handed, deleted = [], []
    affected = switched = 0
    for sid, name, readers in plan:
        if keep and readers:
            @retry_on_locked()
            def _hand_and_commit(sid=sid, to=readers[0]):
                src = db.session.get(EpgSource, sid)
                src.owner_provider_id = None
                src.owner_account_id = to
                db.session.commit()
            _hand_and_commit()
            account = db.session.get(Account, readers[0])
            handed.append((name, account.name if account else f'account {readers[0]}'))
        else:
            done = delete_url_source(sid, case_sensitive)
            deleted.append(sid)
            if done is not None:
                affected += done.affected
                switched += done.switched

    @retry_on_locked()
    def _drop_list_and_commit():
        db.session.execute(delete(ProviderEpgOrder).where(
            ProviderEpgOrder.provider_id == provider_id))
        for account in Account.query.filter(Account.provider_id == provider_id).all():
            set_epg_order_mode(account, None, 'its provider was deleted')
        db.session.commit()

    _drop_list_and_commit()
    return ProviderRelease(handed, deleted, SourceRemoval(affected, switched, affected - switched))


def provider_release_preview(provider_id: int) -> dict:
    """What the delete dialog counts (§15.5): the provider's sources, how many no account
    reads, and the channels whose guide comes from them now."""
    sources = [sid for (sid,) in db.session.query(EpgSource.id).filter(
        EpgSource.owner_provider_id == provider_id)]
    unread = sum(1 for sid in sources if not subscriber_ids(sid))
    guided = (db.session.query(func.count(Channel.id)).filter(
        Channel.epg_source_id.in_(sources)).scalar() or 0) if sources else 0
    extended = sum(extended_counts(sources).values())
    return {'sources': len(sources), 'unread': unread, 'guided': guided,
            'extended': extended}


def provider_lists_view(provider_ids, tz=None, now: datetime | None = None) -> dict[int, list[dict]]:
    """{provider_id: [entry, ...]} for the Providers section's cards (§15.4): each provider's
    list in order, the slot and each source with its status, interval and readers. A fixed
    number of queries whatever the count of providers or sources."""
    from .url_utils import mask_url_path
    ids = list(provider_ids)
    if not ids:
        return {}
    entries = (ProviderEpgOrder.query.filter(ProviderEpgOrder.provider_id.in_(ids))
               .order_by(ProviderEpgOrder.provider_id, ProviderEpgOrder.position).all())
    sources = {s.id: s for s in EpgSource.query.filter(EpgSource.owner_provider_id.in_(ids))}
    readers: dict[int, int] = dict(db.session.query(
        EpgSourceSubscription.source_id, func.count(EpgSourceSubscription.id))
        .filter(EpgSourceSubscription.source_id.in_(list(sources) or [-1]))
        .group_by(EpgSourceSubscription.source_id))
    guided: dict[int, int] = dict(db.session.query(Channel.epg_source_id, func.count(Channel.id))
                                  .filter(Channel.epg_source_id.in_(list(sources) or [-1]))
                                  .group_by(Channel.epg_source_id))
    extended = extended_counts(sources)
    if tz is None:
        from .tz_utils import get_display_tz
        tz = get_display_tz()
    now = now or datetime.utcnow()
    out: dict[int, list[dict]] = {pid: [] for pid in ids}
    for e in entries:
        if e.entry == PROVIDER_ORDER_OWN_GUIDE:
            out[e.provider_id].append({'entry_id': e.id, 'slot': True,
                                       'name': "Provider's EPG"})
            continue
        s = sources.get(e.source_id)
        if s is None:
            continue
        badge, label = STATUS_DISPLAY.get(s.last_status, STATUS_DISPLAY[None])
        out[e.provider_id].append({
            'entry_id': e.id, 'slot': False, 'id': s.id, 'name': s.name,
            'url': mask_url_path(s.url), 'enabled': bool(s.enabled),
            'refresh_interval_hours': s.refresh_interval_hours,
            'refreshing': s.refresh_started_at is not None,
            'last_success_at': s.last_success_at,
            'status_badge': badge, 'status_label': label, 'last_error': s.last_error,
            'readers': readers.get(s.id, 0), 'guided': guided.get(s.id, 0),
            'extended': extended.get(s.id, 0),
            'expiry': expiry_view(s.expires_on, now, tz),
        })
    return out



def account_order_view(account: Account) -> dict | None:
    """The Sources card's order line (§15.3, §15.4) for an account on a provider: the
    provider's id and name and whether the account follows its order. None on no provider."""
    if account.provider_id is None:
        return None
    provider = db.session.get(Provider, account.provider_id)
    if provider is None:
        return None
    return {'provider_id': provider.id, 'provider_name': provider.name,
            'follows': follows(account)}

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


def refreshing_source_names(account_ids=(), source_ids=()) -> list[str]:
    """The names of the sources with a refresh in flight among `source_ids` and every
    source an account in `account_ids` reads - the one question every user action that
    re-resolves channels or moves their rows asks before it writes (Set key, Clear key, the
    review page's Accept, Use this source, Move up/down, Delete source, Stop using,
    Subscribe). An import of any source the account reads decides the same channels'
    winners and moves the same rows, so a write landing beside it can leave one channel
    with two schedules from one source in one table (dev/changelog/1298). Six routes each
    asked a narrower version of this; now there is one, and `refresh_busy_message()` is
    its one wording. Ordered by name so the message is reproducible."""
    account_ids = [a for a in account_ids if a is not None]
    source_ids = [s for s in source_ids if s is not None]
    q = EpgSource.query.filter(EpgSource.refresh_started_at.isnot(None))
    clauses = []
    if source_ids:
        clauses.append(EpgSource.id.in_(source_ids))
    if account_ids:
        clauses.append(EpgSource.id.in_(
            select(EpgSourceSubscription.source_id)
            .where(EpgSourceSubscription.account_id.in_(account_ids))))
    if not clauses:
        return []
    return [name for (name,) in q.filter(db.or_(*clauses)).with_entities(EpgSource.name)
            .order_by(EpgSource.name)]


def refresh_busy_message(names, then: str = 'Try again once the refresh has finished.') -> str:
    """The 409 a route answers while refreshing_source_names() is non-empty. `then` is what
    the user does next, where the action has its own verb ("Delete it once ...")."""
    return f'"{names[0]}" is being refreshed right now. {then}'


# ---------------------------------------------------------------------------
# How far ahead a guide reaches (dev/changelog/1714)
# ---------------------------------------------------------------------------

class Reach(NamedTuple):
    """How far ahead one source's listings run, in local days. `day` is the last day the
    typical channel's listings run through - the median, so at least half its channels reach
    it - because the single latest listing is one channel's: on prod 3,570 of a provider's
    channels ended Nov 25 and one ran to Nov 29. `latest` is that furthest day and `later`
    how many channels run past `day`. `days` is ((day, channels), ...) latest first, counted
    over every channel in the source's directory, so a percentage of it is a share of the
    guide file, not of any one account's channels. A source with no directory is measured
    from the listings it holds: `from_directory` is False and `days` is empty."""
    day: date
    latest: date
    later: int
    channels: int
    days: tuple
    from_directory: bool


def latest_listing_start(source_id: int) -> datetime | None:
    """The latest listing start the guide holds from this source. Two index reads: the
    listing that ends last fixes a lower bound on the latest start, and only listings ending
    after that bound can start later - so the exact answer reads the guide's last few hours,
    not the whole table (a GROUP BY over it measured 2.6s on 340,678 listings)."""
    last = db.session.query(EPGEntry.start_time).filter(
        EPGEntry.source_id == source_id).order_by(EPGEntry.stop_time.desc()).limit(1).scalar()
    if last is None:
        return None
    return db.session.query(func.max(EPGEntry.start_time)).filter(
        EPGEntry.source_id == source_id, EPGEntry.stop_time >= last).scalar()


def to_local_date(dt: datetime, tz) -> date:
    from .tz_utils import to_local
    return to_local(dt, tz).date()


def last_local_day(until: datetime, tz) -> date:
    # A listing ending at midnight leaves the next day empty: the day it runs through is the
    # one before.
    return to_local_date(until - timedelta(seconds=1), tz)


def reach_from_horizons(horizons, tz) -> Reach | None:
    """Reach from [(horizon_until, channels), ...] - pure. None when there is nothing."""
    per_day: dict[date, int] = {}
    for until, n in horizons:
        if until is None or not n:
            continue
        day = last_local_day(until, tz)
        per_day[day] = per_day.get(day, 0) + n
    if not per_day:
        return None
    total = sum(per_day.values())
    seen, median = 0, None
    for day in sorted(per_day):
        seen += per_day[day]
        if seen * 2 >= total:
            median = day
            break
    days = tuple(sorted(per_day.items(), reverse=True))
    later = sum(n for d, n in per_day.items() if d > median)
    return Reach(median, days[0][0], later, total, days, True)


def source_reach(source_ids, tz) -> dict[int, Reach]:
    """{source_id: Reach} in one grouped directory read, whatever the source count; a source
    with no directory adds latest_listing_start()'s two reads. A source whose directory rows
    have all run out (retire_expired_listings() zeroes their counts) has no entry.

    Grouped by the hour of `horizon_until` and each group's latest instant taken, so the
    rows read are a few hundred, not the directory's 76,016 (measured 0.1s on prod). A
    half-hour timezone can put a group's earliest channel on the day before its latest; it
    is counted with the latest."""
    ids = sorted({s for s in source_ids if s is not None})
    if not ids:
        return {}
    hour = func.substr(EpgSourceChannel.horizon_until, 1, 13)
    rows = (db.session.query(EpgSourceChannel.source_id, func.max(EpgSourceChannel.horizon_until),
                             func.count(EpgSourceChannel.id))
            .filter(EpgSourceChannel.source_id.in_(ids), EpgSourceChannel.entry_count > 0,
                    EpgSourceChannel.horizon_until.isnot(None))
            .group_by(EpgSourceChannel.source_id, hour))
    horizons: dict[int, list] = {}
    for sid, until, n in rows:
        horizons.setdefault(sid, []).append((until, n))
    out = {}
    for sid, values in horizons.items():
        reach = reach_from_horizons(values, tz)
        if reach is not None:
            out[sid] = reach
    for sid in sources_without_directory([s for s in ids if s not in horizons]):
        start = latest_listing_start(sid)
        if start is not None:
            day = to_local_date(start, tz)
            out[sid] = Reach(day, day, 0, 0, (), False)
    return out


class AccountReach(NamedTuple):
    """An account's guide reach from the sources that guide its channels: `first` and `last`
    are the shortest and longest of their typical days, `most` the day at least half of the
    account's guided channels reach, each counted at its source's typical day - the one date
    a list row has room for. A channel whose guide a later source continues (§14) is counted
    at the later of the two sources' days."""
    first: date
    last: date
    most: date


def account_reach(active: dict, days: dict, filled: dict | None = None) -> AccountReach | None:
    """AccountReach for one account - `active` is {source_id: channels guided from it here},
    `days` {source_id: Reach.day}, `filled` {(first source, last filling source): channels}
    for the ones whose guide is continued (filled_counts_by_accounts()). A source the
    account reads that guides none of its channels says nothing about its guide."""
    plain = dict(active)
    found = []
    for (first, last), n in (filled or {}).items():
        reached = [days[s] for s in (first, last) if days.get(s) is not None]
        if n and reached and first in plain:
            plain[first] -= n
            found.append((max(reached), n))
    found = sorted(found + [(days[sid], n) for sid, n in plain.items()
                            if n > 0 and days.get(sid) is not None])
    if not found:
        return None
    total, seen, most = sum(n for _d, n in found), 0, found[-1][0]
    for day, n in found:
        seen += n
        if seen * 2 >= total:
            most = day
            break
    return AccountReach(found[0][0], found[-1][0], most)


def accounts_reach(account_ids, tz) -> dict[int, AccountReach]:
    """{account_id: account_reach()} for a list page: two grouped queries whatever the
    account count, plus source_reach()'s reads for a source with no directory."""
    active = active_counts_by_accounts(account_ids)
    filled = filled_counts_by_accounts(account_ids)
    reach = source_reach({sid for counts in active.values() for sid in counts}
                         | {last for pairs in filled.values() for _first, last in pairs}, tz)
    days = {sid: r.day for sid, r in reach.items()}
    out = {}
    for aid, counts in active.items():
        span = account_reach(counts, days, filled.get(aid))
        if span is not None:
            out[aid] = span
    return out


def _days_ahead(day: date, today: date) -> str:
    ahead = (day - today).days
    return f'{ahead:,} days' if ahead >= 2 else ''


_RELATIVE_DAYS = ('Today', 'Tomorrow', 'Yesterday')


def reach_words(span, now: datetime, tz, with_days: bool = True) -> dict | None:
    """{'ran_out': bool, 'text': ..., 'inline': ...} for a (first, last, ...) day span:
    'Fri Nov 28 (4 days)', 'Tue Nov 25 - Thu Nov 27 (up to 3 days)', or the day it ran out
    when even the longest is behind today. `inline` is `text` for the middle of a sentence
    ("guide to tomorrow"); `with_days=False` leaves out the count, for a cell with room for
    a date only. None for no span."""
    from .tz_utils import date_label
    if span is None:
        return None
    first, last = span[0], span[1]
    today = to_local_date(now, tz)
    if last < today:
        text, ran_out = date_label(last, now, tz), True
    else:
        ahead, ran_out = (_days_ahead(last, today) if with_days else ''), False
        if first == last:
            text = date_label(last, now, tz) + (f' ({ahead})' if ahead else '')
        else:
            text = (f'{date_label(first, now, tz)} - {date_label(last, now, tz)}'
                    + (f' (up to {ahead})' if ahead else ''))
    return {'ran_out': ran_out, 'text': text, 'inline': _inline(text)}


def _inline(text: str) -> str:
    for word in _RELATIVE_DAYS:
        text = text.replace(word, word.lower())
    return text


def _share(n: int, total: int) -> str:
    pct = n * 100 / total
    return '<1%' if 0 < pct < 1 else f'{round(pct)}%'


def reach_breakdown(reach: Reach, now: datetime, tz) -> str:
    """The Reaches cell's tooltip: the source's channels by the last day their listings run
    through, latest first, days already behind today counted together."""
    from .tz_utils import date_label
    if not reach.from_directory:
        return ('Measured from the listings it holds.\nIt has not refreshed successfully yet, '
                'so there is no directory to count its channels from.')
    today = to_local_date(now, tz)
    lines = [f'{counted(reach.channels, "channel")} in this guide, by the last day their '
             'listings run through.']
    gone = 0
    for day, n in reach.days:
        if day < today:
            gone += n
            continue
        ahead = _days_ahead(day, today)
        when = date_label(day, now, tz) + (f' ({ahead})' if ahead else '')
        lines.append(f'{when}: {n:,} ({_share(n, reach.channels)})')
    if gone:
        lines.append(f'Already ran out: {gone:,} ({_share(gone, reach.channels)})')
    return '\n'.join(lines)


def source_reach_view(reach: Reach | None, now: datetime, tz) -> dict | None:
    """What a sources-card row shows of a source's reach: the typical day, a line for the
    channels that run past it, and the tooltip's breakdown."""
    from .tz_utils import date_label
    if reach is None:
        return None
    later = None
    if reach.later and reach.latest > reach.day:
        later = (f'{counted(reach.later, "channel")} {plural(reach.later, "runs", "run")} later, to '
                 f'{_inline(date_label(reach.latest, now, tz))}')
    return {'words': reach_words((reach.day, reach.day), now, tz), 'later': later,
            'tip': reach_breakdown(reach, now, tz)}


def account_sources_view(account_id: int, schedule: dict | None = None, tz=None,
                         now: datetime | None = None) -> list[dict]:
    """The Sources card's rows (DESIGN-epg-sources.md §9.2): every source the account
    subscribes to, in priority order, then any source it owns and has stopped using (so
    Use again has a row to sit on). A fixed number of queries whatever the count; `reach` is
    source_reach_view() and `reach_day` the source's typical last day, from which the
    account's own span is account_reach() over the rows. A `url` source's
    address is account-owned and secret in full, so it is masked whole. `ran_out_note` is
    the sentence for a source whose listings, kept through failed refreshes, have passed
    in whole or in part (retire_expired_listings()), else None.

    `schedule` is scheduler.epg_source_schedule() - {source_id: {'next': ..., 'retry': ...}}
    read off the jobs that will really fire - or None on an app with no scheduler."""
    from .url_utils import mask_url_path
    schedule = schedule or {}
    subs = (EpgSourceSubscription.query.filter_by(account_id=account_id)
            .order_by(EpgSourceSubscription.priority).all())
    subscribed = {sub.source_id for sub in subs}
    account = db.session.get(Account, account_id)
    provider_id = account.provider_id if account is not None else None
    # Its own sources and its provider's (§15.4) it has stopped using, so Use again has a row.
    mine = (db.or_(EpgSource.owner_account_id == account_id,
                   EpgSource.owner_provider_id == provider_id) if provider_id is not None
            else EpgSource.owner_account_id == account_id)
    idle = EpgSource.query.filter(mine)
    if subscribed:
        idle = idle.filter(EpgSource.id.notin_(subscribed))
    idle = idle.order_by(EpgSource.id).all()
    placed = set(placed_for_account(account_id))
    own_guide = own_guide_ids([account_id]).get(account_id)
    active_here = active_counts_by_account(account_id)
    extends_here = extends_counts(account_id)
    readers: dict[int, list[ForeignReader]] = {}
    owned = [s.id for s in [sub.source for sub in subs] + idle
             if s.owner_account_id == account_id]
    for fr in foreign_readers(owned, account_id):
        readers.setdefault(fr.source_id, []).append(fr)
    listed = [(sub.source, sub.priority) for sub in subs] + [(s, None) for s in idle]
    ran_out = ran_out_states(s.id for s, _priority in listed)
    if tz is None:
        from .tz_utils import get_display_tz
        tz = get_display_tz()
    now = now or datetime.utcnow()
    reach = source_reach([s.id for s, _priority in listed], tz)
    rows = []
    for s, priority in listed:
        badge, label = STATUS_DISPLAY.get(s.last_status, STATUS_DISPLAY[None])
        when = schedule.get(s.id, {})
        rows.append({
            'id': s.id, 'name': s.name, 'priority': priority, 'subscribed': priority is not None,
            'kind': KIND_LABELS.get(s.kind, s.kind), 'is_url': s.kind == EPG_SOURCE_URL,
            'url': mask_url_path(s.url) if s.kind == EPG_SOURCE_URL else None,
            'owned': s.owner_account_id == account_id,
            'owner_id': s.owner_account_id, 'owner_name': owner_label(s),
            'owner_provider_id': s.owner_provider_id,
            # The provider's own source (§15.2), and whether the provider's list placed this
            # row on a following account - what moving it would change (§15.3).
            'provider_owned': provider_id is not None and s.owner_provider_id == provider_id,
            'placed': s.id in placed, 'own_guide': s.id == own_guide,
            'enabled': s.enabled, 'refresh_interval_hours': s.refresh_interval_hours,
            'refreshing': s.refresh_started_at is not None,
            'next_refresh_at': when.get('next'), 'retry_at': when.get('retry'),
            'last_refresh_at': s.last_refresh_at, 'last_success_at': s.last_success_at,
            'status_badge': badge, 'status_label': label, 'last_error': s.last_error,
            'entry_count': s.entry_count or 0, 'channel_count': s.channel_count or 0,
            'active_here': active_here.get(s.id, 0),
            # Channels whose guide it continues past the end of an earlier source (§14.6).
            'extends_here': extends_here.get(s.id, 0),
            'ran_out_note': RAN_OUT_NOTES.get(ran_out.get(s.id)),
            'expiry': expiry_view(s.expires_on, now, tz),
            'reach': source_reach_view(reach.get(s.id), now, tz),
            'reach_day': reach[s.id].day if s.id in reach else None,
            'readers': [{'id': fr.account_id, 'name': fr.account_name, 'guided': fr.guided}
                        for fr in readers.get(s.id, [])],
        })
    return rows


def stretch_note(outcome: str, after_name: str | None) -> str | None:
    """Why a source after the first does not continue a channel's guide, for the channel
    page. None for one that does: the page says where its part starts instead."""
    other = f"{after_name}'s" if after_name else "the earlier source's"
    if outcome == EPG_STRETCH_FILLS:
        return None
    if outcome == EPG_STRETCH_NOTHING_PAST_END:
        return 'Reaches no further than the guide already does.'
    if outcome == EPG_STRETCH_UNRELATED:
        return f'Its listings do not match {other}, so it is not used to continue the guide.'
    if outcome == EPG_STRETCH_NO_OVERLAP:
        return (f'Too few listings overlap {other} to tell whether they match, so it is not '
                'used to continue the guide.')
    if outcome == EPG_STRETCH_NO_SCHEDULE:
        return 'Lists one program all day, so it is not used to continue a full schedule.'
    if outcome == EPG_STRETCH_WAITING:
        return 'Its listings for this channel arrive at its next refresh.'
    if outcome == EPG_STRETCH_SHARED_ID:
        return ('Channels with different names share this id, so it is not used to continue '
                'the guide.')
    raise ValueError(f'unknown EPG stretch outcome {outcome!r}')


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
    """The channel page's Guide section (DESIGN-epg-sources.md §9.3, §14.6): the source the
    guide starts with and why, the sources that continue it (`chain`, each with where its
    part ends), then one row per source the channel's account subscribes to, saying what
    each does past the end of the one before. A fixed number of queries per page, whatever
    the source count.

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
    comparable = bool(active and others and any(db.session.query(
        select(model.id).where(of_sources(model, others), model.channel_id == channel.id)
        .exists()).scalar() for model in (EpgAlternateEntry, EPGEntry)))
    stretches = {r.source_id: r for r in EpgChannelStretch.query.filter_by(
        channel_id=channel.id).order_by(EpgChannelStretch.position)}
    # Where each source's part of the guide runs, read from the listings held (§14.4).
    part = {sid: (lo, hi) for sid, lo, hi in db.session.query(
        EPGEntry.source_id, func.min(EPGEntry.start_time), func.max(EPGEntry.stop_time))
        .filter(EPGEntry.channel_id == channel.id).group_by(EPGEntry.source_id)}
    chain = []
    if active is not None and not channel.hidden:
        chain = [{'name': by_id[sid].name, 'until': part[sid][1]}
                 for sid in [active.id] + [x for x, r in stretches.items()
                                           if r.outcome == EPG_STRETCH_FILLS]
                 if sid in part and sid in by_id]
    rows = []
    for sid in source_ids + [s for s in by_id if s not in source_ids]:
        s = by_id[sid]
        d = directory.get(sid) or coverage.get(sid)
        stretch = stretches.get(sid) if not channel.hidden else None
        after = by_id.get(stretch.after_source_id) if stretch is not None else None
        user_key, origin = keys.get(sid, (None, None))
        ignored_id = (user_key.provider_id if ignores(channel.epg_channel_id, user_key)
                      else None)
        if isinstance(user_key, IgnoredId):
            user_key = None
        rows.append({
            'id': sid, 'name': s.name, 'kind': KIND_LABELS.get(s.kind, s.kind),
            'subscribed': sid in source_ids,
            'key': wanted.get(sid), 'key_origin': origin if user_key else 'provider',
            'user_key': user_key, 'ignored_id': ignored_id, 'key_pending': pending.get(sid),
            'owner_id': s.owner_account_id, 'owner_name': owner_label(s),
            'owner_provider_id': s.owner_provider_id,
            'covers': sid in coverage,
            'entry_count': d.entry_count if d else 0,
            'distinct_titles': d.distinct_titles if d else 0,
            'sole_title': getattr(d, 'sole_title', None) if d and d.distinct_titles == 1 else None,
            'summary': directory_row_summary(d.entry_count if d else 0,
                                             d.distinct_titles if d else 0),
            'horizon_until': getattr(d, 'horizon_until', None),
            # The source's refreshes kept failing until what it held for this channel
            # had all passed (retire_expired_listings()).
            'ran_out': bool(d is not None and not d.entry_count
                            and getattr(d, 'horizon_until', None)),
            'refreshed': sid not in no_directory,
            'active': sid == channel.epg_source_id,
            'override': sid == channel.epg_source_override_id,
            # What it does for the guide past the end of the source before it (§14.6).
            'stretch': stretch.outcome if stretch is not None else None,
            'stretch_from': (part[sid][0] if stretch is not None and sid in part
                             and stretch.outcome == EPG_STRETCH_FILLS else None),
            'stretch_note': (stretch_note(stretch.outcome, after.name if after else None)
                             if stretch is not None else None),
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
        'comparable': comparable, 'chain': chain,
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


def _load_directory(source_id: int, lowered_ids=None) -> list[DirectoryEntry]:
    """The source's directory rows with listings, or only those whose id lower-cased is in
    `lowered_ids` (bound as IN lists of 500)."""
    def _query(piece=None):
        q = (db.session.query(EpgSourceChannel.xml_id, EpgSourceChannel.display_names,
                              EpgSourceChannel.entry_count, EpgSourceChannel.distinct_titles,
                              EpgSourceChannel.sole_title, EpgSourceChannel.horizon_until,
                              EpgSourceChannel.upcoming_titles)
             .filter(EpgSourceChannel.source_id == source_id,
                     EpgSourceChannel.entry_count > 0))
        if piece is not None:
            q = q.filter(func.lower(EpgSourceChannel.xml_id).in_(piece))
        return q.all()
    if lowered_ids is None:
        rows = _query()
    else:
        rows = [r for piece in chunked(sorted(lowered_ids), 500) for r in _query(piece)]
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


def _refused(row: EpgChannelKey) -> bool:
    """A key row that is a person's answer to name matching: an accepted or rejected key,
    or an ignored provider id that also carries a rejected name (matched_on). An ignored
    row alone is not one - name matching is how such a channel finds its right id."""
    return row.status != EPG_KEY_IGNORED or bool(row.matched_on)


def name_match_review(source: EpgSource, case_sensitive: bool,
                      include_single: bool = False) -> NameMatchResult:
    """name_match_proposals() over what the database holds for `source`. Four queries,
    whatever the channel count. A channel whose provider id is ignored here is matched as
    one with no id, so its proposals are new matches, not disagreements."""
    rows = _key_rows(source.id)
    channels = [ch._replace(epg_channel_id=None)
                if ch.id in rows and rows[ch.id].status == EPG_KEY_IGNORED
                and ignores(ch.epg_channel_id, IgnoredId(rows[ch.id].key or '')) else ch
                for ch in _reader_channels(source.id)]
    decided = {cid for cid, row in rows.items() if _refused(row)}
    return name_match_proposals(_load_directory(source.id), channels, decided,
                                case_sensitive, include_single)


def rejected_name_matches(source_id: int) -> list[dict]:
    """The channels whose proposal for this source a person rejected, for the review page's
    Rejected view. One query."""
    rows = (db.session.query(EpgChannelKey.channel_id, EpgChannelKey.matched_on,
                             EpgChannelKey.updated_at, Channel.name, Channel.account_id)
            .join(Channel, Channel.id == EpgChannelKey.channel_id)
            .filter(EpgChannelKey.source_id == source_id,
                    db.or_(EpgChannelKey.status == EPG_KEY_REJECTED,
                           and_(EpgChannelKey.status == EPG_KEY_IGNORED,
                                EpgChannelKey.matched_on.isnot(None))))
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
    for chunk in chunked(valid, 100):

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
    sets `key`: a refusal is not an answer to "which id is this channel". On a channel
    whose provider id is ignored here the refusal is kept on that row's matched_on, since
    one row holds both answers (dev/changelog/1716). Does not commit."""
    row = EpgChannelKey.query.filter_by(channel_id=channel_id, source_id=source_id).first()
    if row is not None:
        if row.status == EPG_KEY_IGNORED:
            row.matched_on = (matched_on or '')[:512] or None
        return
    db.session.add(EpgChannelKey(channel_id=channel_id, source_id=source_id, key=None,
                                 origin=EPG_KEY_ORIGIN_NAME_MATCH, status=EPG_KEY_REJECTED,
                                 matched_on=(matched_on or '')[:512] or None))


def propose_again(source_id: int, channel_ids) -> int:
    """Undo rejections: delete the rejected rows, and forget the refusal an ignored row
    carries, so the channels are proposed again if their names still match. Returns how
    many were undone."""
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
            n += EpgChannelKey.query.filter(
                EpgChannelKey.source_id == source_id,
                EpgChannelKey.status == EPG_KEY_IGNORED, EpgChannelKey.matched_on.isnot(None),
                EpgChannelKey.channel_id.in_(chunk)).update(
                {EpgChannelKey.matched_on: None}, synchronize_session=False)
        db.session.commit()
        return n

    return _delete_and_commit()


# ── One id shared by unrelated channels (dev/changelog/1716) ──────────────────
#
# A provider can give one guide id to many channels that are not that channel: 180 on one
# prod account, BBC 3 and ITV London among them, all on a Greek radio station's id. Only
# the names can tell, never the count - 23 copies of one sports channel share an id and are
# right. A finding is only ever shown; a person stops using the id through
# set_channel_key(ignore_provider_id=True).

#: Words that say how a copy is delivered, not which channel it is.
_DELIVERY_WORDS = frozenset(
    'hd fhd uhd sd 4k 8k hevc h265 hdr raw backup alt 50fps 60fps 1080p 720p 1080p50 '
    '1080p25 720p25 576p25'.split())
#: A leading country or language tag (`UK:`, `US|`, `CA EN:`, `PT - `).
_COUNTRY_TAG = re.compile(r'^\s*[A-Za-z]{2,3}(?:\s+[A-Za-z]{2})?\s*[:|]\s*'
                          r'|^\s*[A-Za-z]{2,3}\s+-\s+')
#: Two cores this alike (difflib's ratio) are one channel spelled two ways: "premier
#: league" against an id's "premiere league", a "sport" against a "sports".
_SAME_NAME_RATIO = 0.8


@functools.lru_cache(maxsize=65536)
def name_core(name: str | None) -> str:
    """What a channel name says about WHICH channel it is, for comparing two names: the
    country tag, accents, marks and delivery words gone, and what is left run together
    (`UK: SKY SPORTS F1 ᴴᴰ ◉` -> `skysportsf1`). NFKD turns a superscript `ᴿᴬᵂ` into the
    word `raw` so it drops with the rest. Letters of every script survive, as in
    normalize_name(), whose separators it uses."""
    text = unicodedata.normalize('NFKD', _COUNTRY_TAG.sub('', name or ''))
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return ''.join(t for t in normalize_name(text).split()
                   if t not in _DELIVERY_WORDS and any(c.isalnum() for c in t))


def _id_core(xml_id: str) -> str:
    """An XMLTV id as a name: `skysportsf1.uk` -> `skysportsf1`."""
    stem = xml_id.rsplit('.', 1)[0] if '.' in xml_id else xml_id
    return ''.join(c for c in stem.casefold() if c.isalnum())


def same_channel_name(a: str, b: str) -> bool:
    """Whether two name_core()s name one channel: one holds the other (`nflnetwork` in
    `nflnetworkn507savannah`), or they differ by a spelling (_SAME_NAME_RATIO). Cores
    under three characters only match exactly."""
    return _alike(a, b, Counter(a), Counter(b))


def _alike(a: str, b: str, chars_a: Counter, chars_b: Counter) -> bool:
    """same_channel_name() with each core's letter counts already made, for a caller
    comparing every pair of a group's cores."""
    if not a or not b:
        return False
    if len(a) >= 3 and len(b) >= 3 and (a in b or b in a):
        return True
    # difflib's two quick ratios, worked out without building a SequenceMatcher: both are
    # upper bounds of ratio() and rule out nearly every pair, where building the matcher
    # for each pair cost 2.1 s over one prod account's shared ids (dev/changelog/1716).
    total = len(a) + len(b)
    if 2 * min(len(a), len(b)) < _SAME_NAME_RATIO * total:
        return False
    if 2 * sum((chars_a & chars_b).values()) < _SAME_NAME_RATIO * total:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= _SAME_NAME_RATIO


class SharedId(NamedTuple):
    source_id: int
    account_id: int
    entry: DirectoryEntry       # what the id lists in the source
    channels: tuple             # every visible channel on the account reading the id
    wrong: tuple                # those whose name is not the id's: the ones misled by it
    named: tuple                # those whose name is the id's; the rest had no name to compare


def _names_mostly_agree(cores) -> bool:
    """Whether at least half of `cores` share a name with each other: one group linked by
    same_channel_name() holds half of them (union-find over the distinct cores, weighted by
    how many channels carry each, stopping as soon as one group is big enough)."""
    weight: dict[str, int] = {}
    for c in cores:
        weight[c] = weight.get(c, 0) + 1
    need = (len(cores) + 1) // 2
    if max(weight.values()) >= need:
        return True
    distinct = sorted(weight)
    chars = {c: Counter(c) for c in distinct}
    parent = {c: c for c in distinct}

    def root(c):
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c
    for i, a in enumerate(distinct):
        for b in distinct[i + 1:]:
            ra, rb = root(a), root(b)
            if ra != rb and _alike(a, b, chars[a], chars[b]):
                parent[ra] = rb
                weight[rb] += weight[ra]
                if weight[rb] >= need:
                    return True
    return False


def find_shared_ids(source_id: int, channels, keys: dict, directory,
                    case_sensitive: bool) -> list[SharedId]:
    """The ids of one source that many channels share while their names disagree. Pure:
    no database, so the review page, the account page and the findings work run one rule.

    `channels` are ChannelForMatch rows of the accounts reading the source, `keys`
    {channel id: the key it reads in this source} (channel_key()), `directory` the
    source's DirectoryEntry rows. Per account and folded id, with the id listing a real
    schedule (an id listing one program all day misleads nobody about a program):
      - fewer than half the channels whose name says anything have a name the id's own
        names (its display names, the id itself) agree with, and
      - no group of channels whose names agree with each other holds half of them.
    Either test alone flagged mostly spellings and local stations on prod (60 and 12 ids);
    the two together flagged one id, the wrong one (dev/changelog/1716). Ordered by how
    many channels each misleads."""
    rows = _folded_rows(directory, case_sensitive)
    groups: dict[tuple[int, str], list] = {}
    for ch in channels:
        k = keys.get(ch.id)
        if k:
            nk = norm_key(k, case_sensitive)
            if nk in rows:
                groups.setdefault((ch.account_id, nk), []).append(ch)
    out = []
    for (account_id, nk), members in groups.items():
        entry = rows[nk]
        if len(members) < 2 or entry.distinct_titles < REAL_SCHEDULE_MIN_TITLES:
            continue
        cores = {ch.id: name_core(ch.name) for ch in members}
        judged = [ch for ch in members if cores[ch.id]]
        if len(judged) < 2:
            continue
        guide = {name_core(n) for n in entry.display_names or ()} | {_id_core(entry.xml_id)}
        wrong = [ch for ch in judged
                 if not any(same_channel_name(cores[ch.id], g) for g in guide)]
        if (len(judged) - len(wrong)) * 2 >= len(judged):
            continue
        if _names_mostly_agree([cores[ch.id] for ch in judged]):
            continue
        wrong_ids = {ch.id for ch in wrong}
        out.append(SharedId(source_id, account_id, entry, tuple(members), tuple(wrong),
                            tuple(ch for ch in judged if ch.id not in wrong_ids)))
    out.sort(key=lambda f: (-len(f.wrong), f.account_id, f.entry.xml_id))
    return out


def shared_ids(source_ids, account_ids, case_sensitive: bool) -> list[SharedId]:
    """find_shared_ids() over what the database holds: each of `source_ids` against the
    visible channels of whichever `account_ids` read it. One channel query, one key-answer
    query, and per source a directory read of only the ids two or more channels share."""
    account_ids = list(account_ids)
    order = subscriptions_for(account_ids)
    wanted_sources = set(source_ids)
    readers: dict[int, list[int]] = {}
    for aid, sids in order.items():
        for sid in sids:
            if sid in wanted_sources:
                readers.setdefault(sid, []).append(aid)
    if not readers:
        return []
    channels = [ChannelForMatch(*r) for r in db.session.query(
        Channel.id, Channel.name, Channel.account_id, Channel.epg_channel_id,
        Channel.epg_source_id)
        .filter(Channel.account_id.in_({a for aids in readers.values() for a in aids}),
                Channel.hidden.is_(False))
        .order_by(Channel.account_id, func.lower(Channel.name), Channel.id)]
    answers = key_answers(list(readers))
    out = []
    for sid in sorted(readers):
        accounts = set(readers[sid])
        mine = [ch for ch in channels if ch.account_id in accounts]
        keys = {}
        counts: dict[tuple[int, str], int] = {}
        for ch in mine:
            k = channel_key(ch.epg_channel_id, answers.get((ch.id, sid)))
            if k:
                keys[ch.id] = k
                unit = (ch.account_id, k.lower())
                counts[unit] = counts.get(unit, 0) + 1
        lowered = {k for (_aid, k), n in counts.items() if n > 1}
        if not lowered:
            continue
        shared = [ch for ch in mine if ch.id in keys
                  and counts[(ch.account_id, keys[ch.id].lower())] > 1]
        out += find_shared_ids(sid, shared, keys, _load_directory(sid, lowered),
                               case_sensitive)
    return out


class SharedIdIgnored(NamedTuple):
    applied: int        # channels that stopped using the id
    skipped: int        # shown, but no longer misled by it through the provider's id


def ignore_shared_id(source_id: int, account_id: int, xml_id: str, channel_ids,
                     case_sensitive: bool) -> SharedIdIgnored:
    """Stop using a shared id for the channels a person was shown (§16): each one gets an
    ignored row on every source its account reads, through set_channel_key(), and those
    sources' listings for it go now. Each id is checked against the finding as it stands -
    a channel no longer misled by the id, or one reading it through its own key rather than
    the provider's id, is skipped and counted - and nothing outside the shown ids is
    touched. Commits a chunk of channels at a time, each its own retry_on_locked unit."""
    finding = next((f for f in shared_ids([source_id], [account_id], case_sensitive)
                    if f.account_id == account_id
                    and norm_key(f.entry.xml_id, case_sensitive) == norm_key(xml_id,
                                                                               case_sensitive)),
                   None)
    # Only a channel reading the id through the provider's id: one whose own key on this
    # source names it was put there by the user, and an ignore would not reach it.
    own_keys = key_answers([source_id])
    wrong = ({ch.id for ch in finding.wrong
              if ch.epg_channel_id
              and norm_key(ch.epg_channel_id, case_sensitive) == norm_key(xml_id,
                                                                          case_sensitive)
              and not isinstance(own_keys.get((ch.id, source_id)), str)}
             if finding else set())
    shown = list(dict.fromkeys(int(c) for c in channel_ids))
    valid = [cid for cid in shown if cid in wrong]
    order = subscriptions_for([account_id]).get(account_id, [])
    applied = 0
    for chunk in chunked(valid, 100):

        @retry_on_locked()
        def _ignore_and_commit(chunk=chunk):
            sources = {s.id: s for s in (db.session.get(EpgSource, sid) for sid in order)
                       if s is not None}
            done = 0
            for cid in chunk:
                channel = db.session.get(Channel, cid)
                if channel is None or not channel.epg_channel_id:
                    continue
                # The source the guide comes from goes last: ignored first, it would hand
                # the guide to a source the next step then takes away, and the channel's
                # timeline would name a move that never stood.
                last = channel.epg_source_id
                ordered = ([sid for sid in order if sid != last]
                           + ([last] if last in sources else []))
                for sid in ordered:
                    set_channel_key(channel, sources[sid], None,
                                    case_sensitive=case_sensitive, ignore_provider_id=True)
                done += 1
            db.session.commit()
            return done

        applied += _ignore_and_commit()

    if valid:
        @retry_on_locked()
        def _recount_and_commit():
            for sid in order:
                refresh_source_counts(sid)
            db.session.commit()
        _recount_and_commit()
    return SharedIdIgnored(applied, len(shown) - len(valid))


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
#: The verdicts that say two sources list the same channel's schedule (§14.3's "agree").
CMP_AGREEING = frozenset({CMP_IDENTICAL, CMP_DESCRIPTIONS, CMP_MOSTLY, CMP_SHIFTED})

ROW_SAME = 'same'
ROW_MOVED = 'moved'
ROW_DIFFERENT = 'different'         # neither has a partner, and they start together
ROW_ONLY_ACTIVE = 'only-active'
ROW_ONLY_OTHER = 'only-other'
#: A second listing at a moment one side already paired: that side lists the channel twice,
#: which a file giving two programs the same start time produces. Shown, and left out of the
#: verdict, which would otherwise call two agreeing guides unrelated.
ROW_DOUBLED_ACTIVE = 'doubled-active'
ROW_DOUBLED_OTHER = 'doubled-other'
#: A listing titled with the channel's own name and matching nothing on the other side: the
#: source has no program there. Shown, and left out of the verdict, so its slot partner is
#: "only in the other source" rather than a different program (dev/changelog/1725).
ROW_PLACEHOLDER_ACTIVE = 'placeholder-active'
ROW_PLACEHOLDER_OTHER = 'placeholder-other'


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
    #: ROW_SAME only: paired as one program under another name - one title the other plus
    #: more words, or _another_name() - not on equal comparison titles.
    renamed: bool = False

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
    renamed: int             # of `same`, the pairs judged one program under another name
    placeholder_active: int  # ROW_PLACEHOLDER_ACTIVE rows
    placeholder_other: int


#: The accents NFKD splits off Latin, Greek and Cyrillic letters (`š`, `ț`, `ё`). Only this
#: block: a kana voicing mark or an Indic vowel sign is part of the letter, and dropping one
#: would make two different words equal.
_ACCENTS = re.compile('[̀-ͯ]+')


@functools.lru_cache(maxsize=65536)
def comparison_title(title: str | None) -> str:
    """A listing's title as the comparison matches it: the marks providers write into
    titles dropped - the superscript tags (`ᴺᵉʷ`, `ᴸᶦᵛᵉ`, `ᴿᴬᵂ`, 131,000 of them in the live
    guide) that one feed carries and another does not, and a leading `Live:`, the other
    feeds' spelling of the same tag (`U.S. Senate ᴸᶦᵛᵉ` / `Live: U.S. Senate`) - then
    casefolded, accents folded (`Ţ` / `ț`) and every run of punctuation, symbols and spaces
    one space (`šéfe!` / `sefe`), keeping the letters, marks and digits of every script.

    Measured on prod's 2026-11-25 copy (dev/changelog/1725): folding found 235 of 6,900
    listings one account's two guides called different programs the same, and none of
    20,006 pairs from unrelated channels. A title that folds to nothing (`⚽`) keeps its
    §7.3 normalize_name() form, so two symbol-only titles are not equal by both being
    empty.

    The marks are program_titles.strip_marks(), the one definition auto-record rules use
    too (DESIGN-autoschedule.md §3.3). The folding is this comparison's own."""
    bare = strip_marks(title)
    text = _ACCENTS.sub('', unicodedata.normalize('NFKD', bare).casefold())
    text = ''.join(ch if unicodedata.category(ch)[0] in 'LMN' else ' ' for ch in text)
    folded = unicodedata.normalize('NFC', ' '.join(text.split()))
    return folded or normalize_name(bare)


#: A word spelled two ways differs by at most this many letters (`проклятья` / `проклятия`,
#: `Zvezdice` / `zvjezdice`), and only in words this long: `Bachelor` / `Bachelorette` is
#: four apart, `Friends` / `Fiends` a one-word title (dev/changelog/1725).
_SPELLING_EDITS = 2
_SPELLING_MIN_LETTERS = 4
_NUMBERS = re.compile(r'\d+')


def _word_prefix(x: str, y: str) -> bool:
    return bool(x and y) and (x.startswith(y + ' ') or y.startswith(x + ' '))


def _edits_within(a: str, b: str, cap: int) -> bool:
    """Whether `a` becomes `b` in at most `cap` single-letter insertions, deletions or
    substitutions."""
    if abs(len(a) - len(b)) > cap:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return False
        prev = cur
    return prev[-1] <= cap


@functools.lru_cache(maxsize=65536)
def _spelled_alike(x: str, y: str) -> bool:
    """Whether two comparison titles are one spelled two ways: as many words, at least two,
    fewer than half of them different, and each different word long and a letter or two
    apart. A one-word title is never a misspelling of another, and neither is a number:
    `Formula 1` / `Formula 2` and `episode 3` / `episode 4` stay apart."""
    xw, yw = x.split(), y.split()
    if len(xw) != len(yw) or len(xw) < 2 or _NUMBERS.findall(x) != _NUMBERS.findall(y):
        return False
    differ = [(p, q) for p, q in zip(xw, yw) if p != q]
    return (0 < len(differ) <= len(xw) // 2
            and all(min(len(p), len(q)) >= _SPELLING_MIN_LETTERS
                    and _edits_within(p, q, _SPELLING_EDITS) for p, q in differ))


class _Titles(NamedTuple):
    """What the comparison reads of one listing's names, worked out once per listing."""
    title: str
    sub_title: str
    words: frozenset


def _titles(x: Listing) -> _Titles:
    t = comparison_title(x.title)
    return _Titles(t, comparison_title(x.sub_title) if x.sub_title else '', frozenset(t.split()))


def _named_as(name: str, title: str) -> bool:
    """An episode title standing for the other side's title: equal, or one the other plus
    more words, and the shorter of the two at least two words."""
    shorter = name if len(name) <= len(title) else title
    return (len(shorter.split()) >= 2
            and (name == title or _word_prefix(name, title)))


def _another_name(x: _Titles, y: _Titles) -> bool:
    """Whether two listings starting together are one program named two ways, past the
    matching titles compare_listings() tries first. Each rule was measured on prod's
    2026-11-25 copy, account 5's two guides, against the 6,900 pairs called different and
    20,006 same-slot pairs from unrelated channels (dev/changelog/1725):

    - one side's episode title is the other's title (`Aces` with episode `Primo Per Sempre`
      / `Primo Per Sempre`): 29 pairs, none unrelated;
    - every word of a title of two words or more is in the other (`Nové Město` / `Epreuve de
      Nove Mesto`, `PARTIZAN - DUBAI` / `Košarka - Aba Liga: Partizan - Dubai`): 659, one;
    - the same title spelled two ways (_spelled_alike()): 127, none.

    A one-word title never stands for a longer one or an episode: `News` is in `BBC World
    News` and `Football` in `Monday Night Football`, and in a slot two guides fill differently
    that says nothing. That leaves `Weltcup` / `Ski alpin: Weltcup Kitzbühel Abfahrt` apart.
    A 60% word overlap, which called `FOX SPORTS News` and `FOX 10 News at 4PM` one program
    16 times, was left out, as was difflib's ratio, which joins `The Bachelor` / `The
    Bachelorette`."""
    if not x.title or not y.title:
        return False
    if x.sub_title and _named_as(x.sub_title, y.title):
        return True
    if y.sub_title and _named_as(y.sub_title, x.title):
        return True
    smaller, larger = (x.words, y.words) if len(x.words) <= len(y.words) else (y.words, x.words)
    if len(smaller) >= 2 and smaller <= larger:
        return True
    return _spelled_alike(x.title, y.title)


def placeholder_names(names) -> frozenset:
    """The name_core()s of the channel names a comparison treats as placeholder titles: a
    listing titled with its channel's own name (`talkSPORT` on `UK: TALKSPORT`) says the
    source has no program there. Cores under three characters are left out, as
    same_channel_name() leaves them out."""
    return frozenset(c for c in (name_core(n) for n in names if n) if len(c) >= 3)


def _pair(a, b, used_a, used_b, window, match, b_starts) -> list[tuple[int, int]]:
    """Closest-first pairing of unused listings starting within `window` of each other for
    which match(i, j) holds. `b_starts` is [(start, j)] over `b`, sorted, so each listing
    looks only at the other side's listings inside its window."""
    cands = []
    keys = [s for s, _j in b_starts]
    for i, x in enumerate(a):
        if i in used_a:
            continue
        lo = bisect.bisect_left(keys, x.start - window)
        hi = bisect.bisect_right(keys, x.start + window)
        for start, j in b_starts[lo:hi]:
            if j not in used_b and match(i, j):
                cands.append((abs(start - x.start), i, j))
    cands.sort()
    out = []
    for _gap, i, j in cands:
        if i not in used_a and j not in used_b:
            used_a.add(i)
            used_b.add(j)
            out.append((i, j))
    return out


def compare_listings(active, other, *, placeholders) -> Comparison:
    """Line up one channel's listings from two sources and name the difference, under the
    rule recorded in DESIGN-epg-sources.md §12 - the same program starts within five
    minutes under a matching title - with the title match widened by measurement on the
    live guide (dev/changelog/1108, 1725): comparison_title() drops providers' superscript
    tags, accents and punctuation; a title that is the other plus more words (`Hot Bench` /
    `Hot Bench - Timesharing Is Caring`) is the same program when the two start together;
    and so is one named two ways (_another_name()). Pure; both lists are the listings from
    now on.

    `placeholders` is placeholder_names() of the channel or channels the listings are for,
    required so that no caller judges without them: a listing titled with one of those
    names that nothing pairs with by title is a placeholder, the source having nothing
    there, never a different program.

    Pairing runs closest first: matching titles within five minutes, then one program under
    another name within five minutes, then equal titles within six hours (moved - a rain
    delay), then any two leftovers starting together that are not placeholders (a different
    program in the same slot). Only the stretch both sources list is judged, so a source
    listing 20 hours is not called unrelated to one listing 72."""
    a, b = list(active), list(other)
    an, bn = [_titles(x) for x in a], [_titles(x) for x in b]
    at, bt = [x.title for x in an], [x.title for x in bn]
    b_starts = sorted((x.start, j) for j, x in enumerate(b))
    used_a: set[int] = set()
    used_b: set[int] = set()
    same = _pair(a, b, used_a, used_b, SAME_START, lambda i, j: at[i] == bt[j], b_starts)
    renamed = _pair(a, b, used_a, used_b, SAME_START,
                    lambda i, j: _word_prefix(at[i], bt[j]), b_starts)
    renamed += _pair(a, b, used_a, used_b, SAME_START,
                     lambda i, j: _another_name(an[i], bn[j]), b_starts)
    moved = _pair(a, b, used_a, used_b, MOVED_WITHIN, lambda i, j: at[i] == bt[j], b_starts)
    held_a = {i for i, x in enumerate(a)
              if i not in used_a and placeholders and name_core(x.title) in placeholders}
    held_b = {j for j, x in enumerate(b)
              if j not in used_b and placeholders and name_core(x.title) in placeholders}
    different = _pair(a, b, used_a | held_a, used_b | held_b, SAME_START, lambda i, j: True,
                      b_starts)
    used_a.update(i for i, _j in different)
    used_b.update(j for _i, j in different)
    since = max(min(x.start for x in a), min(x.start for x in b)) if a and b else None
    until = min(max(x.stop for x in a), max(x.stop for x in b)) if a and b else None

    def inside(x):
        return until is not None and since < until and x.stop > since and x.start < until

    pairs = same + renamed + moved + different
    paired_a = {a[i].start for i, _j in pairs}
    paired_b = {b[j].start for _i, j in pairs}
    rows = ([CompareRow(ROW_SAME, a[i], b[j], inside(a[i])) for i, j in same]
            + [CompareRow(ROW_SAME, a[i], b[j], inside(a[i]), renamed=True)
               for i, j in renamed]
            + [CompareRow(ROW_MOVED, a[i], b[j], inside(a[i])) for i, j in moved]
            + [CompareRow(ROW_DIFFERENT, a[i], b[j], inside(a[i]))
               for i, j in different])
    for i, x in enumerate(a):
        if i in held_a:
            rows.append(CompareRow(ROW_PLACEHOLDER_ACTIVE, x, None, False))
        elif i not in used_a:
            rows.append(CompareRow(ROW_DOUBLED_ACTIVE, x, None, False) if x.start in paired_a
                        else CompareRow(ROW_ONLY_ACTIVE, x, None, inside(x)))
    for j, x in enumerate(b):
        if j in held_b:
            rows.append(CompareRow(ROW_PLACEHOLDER_OTHER, None, x, False))
        elif j not in used_b:
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
        doubled_other=sum(1 for r in rows if r.kind == ROW_DOUBLED_OTHER),
        renamed=sum(1 for r in same_rows if r.renamed),
        placeholder_active=len(held_a), placeholder_other=len(held_b))


# How one listing stands against a reference schedule, for a display that draws both
# (the group page's What's On card, dev/changelog/1324). Derived from the comparator's
# ROW_* kinds, never decided a second way.
AGREE_SAME = 'same'
AGREE_MOVED = 'moved'
AGREE_DIFFERENT = 'different'
AGREE_ONLY_HERE = 'only-here'       # nothing in the reference starts within five minutes
AGREE_DOUBLED = 'doubled'           # a second listing at a moment already listed
AGREE_OUTSIDE = 'outside'           # past the stretch both list, so not judged
AGREE_PAST = 'past'                 # over before now; the comparison starts from now
AGREE_REFERENCE = 'reference'       # a listing of the reference itself
AGREE_PLACEHOLDER = 'placeholder'   # titled with the channel's own name: no program there
AGREEMENTS = (AGREE_SAME, AGREE_MOVED, AGREE_DIFFERENT, AGREE_ONLY_HERE, AGREE_DOUBLED,
              AGREE_OUTSIDE, AGREE_PAST, AGREE_REFERENCE, AGREE_PLACEHOLDER)
#: The side of a same-program pair that has the only description.
GAP_REFERENCE = 'reference'
GAP_OTHER = 'other'


class Agreement(NamedTuple):
    kind: str
    partner: Listing | None   # the reference's listing it was paired with (same/moved/different)
    gap: str | None           # GAP_* on a same pair described on one side only
    renamed: bool = False     # a same pair named two ways (CompareRow.renamed)


def listing_agreement(reference, other, now, *, placeholders):
    """(Comparison, [Agreement] parallel to `other`, [reference Listing other lacks]) -
    `other` judged listing by listing against `reference`, both from now on, by
    compare_listings(), with `placeholders` the placeholder_names() of both channels. The
    lacking ones are the reference's counted listings with no
    partner that overlap nothing `other` lists, so a display can outline them without
    drawing one over a real listing. Pure."""
    ref_live = [x for x in reference if x.stop > now]
    oth_live = [x for x in other if x.stop > now]
    cmp = compare_listings(ref_live, oth_live, placeholders=placeholders)
    by_other = {id(r.other): r for r in cmp.rows if r.other is not None}
    out = []
    for x in other:
        if x.stop <= now:
            out.append(Agreement(AGREE_PAST, None, None))
            continue
        r = by_other[id(x)]
        if r.kind == ROW_DOUBLED_OTHER:
            out.append(Agreement(AGREE_DOUBLED, None, None))
        elif r.kind == ROW_PLACEHOLDER_OTHER:
            out.append(Agreement(AGREE_PLACEHOLDER, None, None))
        elif not r.counted:
            out.append(Agreement(AGREE_OUTSIDE, None, None))
        elif r.kind == ROW_SAME:
            gap = None
            if r.active.described != r.other.described:
                gap = GAP_REFERENCE if r.active.described else GAP_OTHER
            out.append(Agreement(AGREE_SAME, r.active, gap, r.renamed))
        elif r.kind == ROW_MOVED:
            out.append(Agreement(AGREE_MOVED, r.active, None))
        elif r.kind == ROW_DIFFERENT:
            out.append(Agreement(AGREE_DIFFERENT, r.active, None))
        elif r.kind == ROW_ONLY_OTHER:
            out.append(Agreement(AGREE_ONLY_HERE, None, None))
        else:
            raise ValueError(f'comparator row kind {r.kind!r} on the other side')
    lacking = [r.active for r in cmp.rows
               if r.kind == ROW_ONLY_ACTIVE and r.counted
               and not any(y.start < r.active.stop and r.active.start < y.stop for y in other)]
    return cmp, out, lacking


class SourceComparison(NamedTuple):
    source: EpgSource
    last_stop: datetime       # the end of this source's listings for the channel
    comparison: Comparison


_LISTING_COLUMNS = ('title', 'sub_title', 'description', 'start_time', 'stop_time')


def channel_source_comparisons(channel: Channel, now: datetime | None = None):
    """(the first source's listings, [SourceComparison]) for one channel, from now on: the
    source its guide starts with against every other source its account reads that holds
    listings for it, in priority order. Four queries whatever the listing or source count,
    and nothing written.

    The alternates are kept only for a channel with an active source (§4), so a channel
    with none has nothing to compare."""
    now = now or datetime.utcnow()
    if channel.epg_source_id is None:
        return [], []

    def listing(r):
        return Listing(r.title, r.sub_title, r.description, r.start_time, r.stop_time)

    order = [sid for sid in subscriptions_for([channel.account_id]).get(channel.account_id, [])
             if sid != channel.epg_source_id]
    # By source over both tables: a source that continues the guide (§14) holds its later
    # listings in epg_entries and its earlier ones in the alternates.
    held: dict[int, list[Listing]] = {}
    for model in (EPGEntry, EpgAlternateEntry):
        for r in db.session.query(model.source_id,
                                  *(getattr(model, c) for c in _LISTING_COLUMNS)
                                  ).filter(model.channel_id == channel.id,
                                           of_sources(model, order + [channel.epg_source_id]),
                                           model.stop_time > now):
            held.setdefault(r.source_id, []).append(listing(r))
    for rows in held.values():
        rows.sort(key=lambda x: x.start)
    active = held.pop(channel.epg_source_id, [])
    if not held:
        return active, []
    sources = {s.id: s for s in EpgSource.query.filter(EpgSource.id.in_(list(held)))}
    placeholders = placeholder_names([channel.name])
    return active, [SourceComparison(sources[sid], max(x.stop for x in held[sid]),
                                     compare_listings(active, held[sid],
                                                      placeholders=placeholders))
                    for sid in order if sid in held]


# ── Filling a guide past the end of its first source (DESIGN-epg-sources.md §14,
#    dev/changelog/1722) ─────────────────────────────────────────────────────
#
# Channel.epg_source_id is the source a channel's guide STARTS with. Each later source in
# the account's order may continue it: its listings starting at or after the last stop of
# the stretch before go to epg_entries, the rest stay in the alternates. place_stretches()
# is the one function that decides this and puts every row where it says, and every path
# that moves or copies listings ends in it (tests/test_epg_fill.py holds the list).

#: A recorded `fills` or `unrelated` is replaced only by a comparison over at least this
#: many of the earlier source's listings, and a first judgment over fewer is `no_overlap`
#: (§14.3). Judged over its first n listings, a pair of sources got a different answer than
#: over all of them on 5.4% of 2,153 channels at 2, 2.0% at 6 and 1.5% at 10.
FILL_MIN_JUDGED = 6

#: Channels placed per commit.
_PLACE_CHUNK = 100


class Span(NamedTuple):
    """What one source holds for one channel, over both tables."""
    last_start: datetime
    last_stop: datetime


class Stretch(NamedTuple):
    """The decision for one source after a channel's first."""
    source_id: int
    position: int
    outcome: str                # EPG_STRETCH_*
    after: int | None           # the source of the stretch it was judged against
    seam: datetime | None       # EPG_STRETCH_FILLS: its listings starting at or after this
    #                             are the guide's; None with fills = all of them


def decide_stretches(first: int, later, covers: dict, spans: dict, flagged, stored: dict,
                     listings: dict, placeholders) -> list[Stretch]:
    """Which of `later` (the account's other sources, in its order) continue a channel's
    guide past `first`, and why not for the rest (§14.3, §14.4). Pure.

    `covers` {source_id: Coverage} for the sources that cover the channel; `spans`
    {source_id: Span} for the ones holding listings; `flagged` the sources through which the
    channel reads an id unrelated channels share; `stored` {source_id: (outcome, after)} as
    last decided; `listings` {source_id: [Listing] from now on, by start}; `placeholders`
    the channel's placeholder_names().

    A source is judged against the source of the stretch before it, and a refused one is
    passed over: the next is judged against the same stretch. The checks run cheapest
    first, and each names its outcome."""
    out: list[Stretch] = []
    prev = first
    end = spans[first].last_stop if first in spans else None
    for sid in later:
        if sid == first or sid not in covers:
            continue
        span = spans.get(sid)
        if sid in flagged or prev in flagged:
            outcome = EPG_STRETCH_SHARED_ID
        elif span is None:
            outcome = EPG_STRETCH_WAITING
        elif end is not None and span.last_start < end:
            outcome = EPG_STRETCH_NOTHING_PAST_END
        elif (prev in covers and covers[prev].real_schedule
              and not covers[sid].real_schedule):
            outcome = EPG_STRETCH_NO_SCHEDULE
        else:
            cmp = compare_listings(listings.get(prev, ()), listings.get(sid, ()),
                                   placeholders=placeholders)
            kept = stored.get(sid)
            if cmp.compared < FILL_MIN_JUDGED:
                judged = None
            elif cmp.verdict in CMP_AGREEING:
                judged = EPG_STRETCH_FILLS
            elif cmp.verdict == CMP_UNRELATED:
                judged = EPG_STRETCH_UNRELATED
            elif cmp.verdict == CMP_NO_OVERLAP:
                judged = None
            else:
                # A verdict this does not know is never read as agreement.
                raise ValueError(f'unknown comparison verdict {cmp.verdict!r}')
            if judged is not None:
                outcome = judged
            elif (kept is not None and kept[1] == prev
                  and kept[0] in (EPG_STRETCH_FILLS, EPG_STRETCH_UNRELATED)):
                outcome = kept[0]
            else:
                outcome = EPG_STRETCH_NO_OVERLAP
        out.append(Stretch(sid, len(out) + 1, outcome, prev,
                           end if outcome == EPG_STRETCH_FILLS else None))
        if outcome == EPG_STRETCH_FILLS:
            end = span.last_stop if end is None else max(end, span.last_stop)
            prev = sid
    return out


class _Held(NamedTuple):
    """One source's rows for one channel in one table."""
    first_start: datetime
    last_start: datetime
    last_stop: datetime
    count: int
    titles: int


def _held_by_table(channel_ids) -> tuple[dict, dict]:
    """({channel_id: {source_id: _Held}} in epg_entries, the same in the alternates). Two
    grouped statements per chunk, each reaching the channels' own rows by a channel index."""
    out: tuple[dict, dict] = ({}, {})
    for chunk in _chunks(channel_ids):
        for held, model in zip(out, (EPGEntry, EpgAlternateEntry)):
            for cid, sid, lo, hi, stop, n, titles in (
                    db.session.query(model.channel_id, model.source_id,
                                     func.min(model.start_time), func.max(model.start_time),
                                     func.max(model.stop_time), func.count(model.id),
                                     func.count(func.distinct(model.title)))
                    .filter(model.channel_id.in_(chunk))
                    .group_by(model.channel_id, model.source_id)):
                held.setdefault(cid, {})[sid] = _Held(lo, hi, stop, n, titles)
    return out


def _future_listings(channel_ids, now: datetime) -> dict[int, dict[int, list]]:
    """{channel_id: {source_id: [Listing] from now on, by start}} over both tables. Titles,
    episode titles and times: all a verdict is made from (compare_listings() pairs one
    side's episode title with the other's title, dev/changelog/1725)."""
    out: dict[int, dict[int, list]] = {}
    for chunk in _chunks(channel_ids):
        for model in (EPGEntry, EpgAlternateEntry):
            for cid, sid, title, sub_title, start, stop in db.session.query(
                    model.channel_id, model.source_id, model.title, model.sub_title,
                    model.start_time, model.stop_time).filter(model.channel_id.in_(chunk),
                                                              model.stop_time > now):
                out.setdefault(cid, {}).setdefault(sid, []).append(
                    Listing(title, sub_title, None, start, stop))
    for by_source in out.values():
        for rows in by_source.values():
            rows.sort(key=lambda x: x.start)
    return out


def _shared_id_flags(account_ids, case_sensitive: bool) -> dict[tuple[int, int], set[str]]:
    """{(account_id, source_id): folded ids} the EPG check last found shared by channels
    whose names disagree (find_shared_ids(), stored as findings). Read from the stored
    finding, never judged here: the name comparison costs half a second an account, and a
    key write places one channel at a time."""
    out: dict[tuple[int, int], set[str]] = {}
    account_ids = list(account_ids)
    if not account_ids:
        return out
    for aid, sid, subject in db.session.query(
            EpgFinding.account_id, EpgFinding.source_id, EpgFinding.subject).filter(
            EpgFinding.account_id.in_(account_ids),
            EpgFinding.kind == EPG_FINDING_SHARED_ID, EpgFinding.subject.isnot(None)):
        out.setdefault((aid, sid), set()).add(norm_key(subject, case_sensitive))
    return out


class _Placement(NamedTuple):
    """Where one channel's listings belong, and what has to change to get there."""
    channel_id: int
    first: int | None
    stretches: list             # [Stretch], as decided
    stored: dict                # {source_id: (position, outcome, after, updated_at)}
    moves: list                 # [(source_id, into the guide?, starting_from, starting_before)]
    end: datetime | None = None   # the last stop of the guide as decided, fills included


def _plan_moves(first: int, stretches, in_guide: dict, kept: dict) -> list:
    """The moves that put one channel's rows where the decision says, from what it holds in
    each table ({source_id: _Held}): the first source's all in epg_entries, a filling source's from its seam on, everything else in the alternates.
    Only moves a row needs are planned, so a channel already in place costs nothing."""
    fills = {s.source_id: s.seam for s in stretches if s.outcome == EPG_STRETCH_FILLS}
    moves = []
    for sid, held in kept.items():
        if sid == first or (sid in fills and fills[sid] is None):
            moves.append((sid, True, None, None))
        elif sid in fills and held.last_start >= fills[sid]:
            moves.append((sid, True, fills[sid], None))
    for sid, held in in_guide.items():
        if sid == first:
            continue
        if sid not in fills:
            moves.append((sid, False, None, None))
        elif fills[sid] is not None and held.first_start < fills[sid]:
            moves.append((sid, False, None, fills[sid]))
    return moves


class _PlanContext(NamedTuple):
    """What a placement reads once for all its channels, however many chunks it plans in."""
    order: dict                 # {account_id: [source ids]}
    keys: dict                  # key_answers()
    flags: dict                 # _shared_id_flags()
    directory: dict | None      # directory_coverage(); None when the caller has the coverage
    unrefreshed: set


def _plan_context(guided, case_sensitive: bool, with_directory: bool) -> _PlanContext:
    order = subscriptions_for({r.account_id for r in guided})
    candidates = {sid for sids in order.values() for sid in sids}
    candidates |= {r.epg_source_id for r in guided}
    keys = key_answers(candidates, [r.id for r in guided] if len(guided) <= _CHUNK else None)
    flags = _shared_id_flags(order, case_sensitive)
    if not with_directory or not guided:
        return _PlanContext(order, keys, flags, None, set())
    return _PlanContext(order, keys, flags, directory_coverage(candidates, case_sensitive),
                        sources_without_directory(candidates))


def _is_guided(r) -> bool:
    return r.epg_source_id is not None and not r.hidden


def _plan_stretches(rows, case_sensitive: bool, now: datetime, coverage_of=None,
                    ctx: _PlanContext | None = None, held_by_table=None,
                    future_listings=None) -> list[_Placement]:
    """Decide every channel in `rows` (id, account_id, epg_channel_id, hidden,
    epg_source_id, name) from what the database holds. Reads only.

    `coverage_of` {channel_id: {source_id: Coverage}} is passed by a caller that already
    has one channel's coverage (a key write); otherwise it is read from the directory, with
    held rows standing in for a source that has none yet (held_coverage()'s rule). `ctx` is
    the caller's _plan_context() when it plans in chunks, so the directory is read once.
    `held_by_table` and `future_listings` stand in for _held_by_table() and
    _future_listings(): the reorder preview hands in what the rows would be after the
    change (preview_orders()), so it decides with this function rather than a copy of it."""
    held_by_table = held_by_table or _held_by_table
    future_listings = future_listings or _future_listings
    guided = [r for r in rows if _is_guided(r)]
    if ctx is None:
        ctx = _plan_context(guided, case_sensitive, coverage_of is None)
    order, keys, flags, directory, unrefreshed = ctx
    in_guide, kept = held_by_table([r.id for r in guided])
    stored: dict[int, dict] = {}
    for chunk in _chunks([r.id for r in rows]):
        for cid, sid, pos, outcome, after, at in db.session.query(
                EpgChannelStretch.channel_id, EpgChannelStretch.source_id,
                EpgChannelStretch.position, EpgChannelStretch.outcome,
                EpgChannelStretch.after_source_id, EpgChannelStretch.updated_at).filter(
                EpgChannelStretch.channel_id.in_(chunk)):
            stored.setdefault(cid, {})[sid] = (pos, outcome, after, at)

    def held_span(cid, sid):
        a, b = in_guide.get(cid, {}).get(sid), kept.get(cid, {}).get(sid)
        if a is None and b is None:
            return None
        return Span(max(x.last_start for x in (a, b) if x), max(x.last_stop for x in (a, b) if x))

    facts = {}
    for r in guided:
        mine = [r.epg_source_id] + [s for s in order.get(r.account_id, [])
                                    if s != r.epg_source_id]
        covers: dict[int, Coverage] = {}
        flagged = set()
        for sid in mine:
            k = channel_key(r.epg_channel_id, keys.get((r.id, sid)))
            nk = norm_key(k, case_sensitive) if k else None
            if nk and nk in flags.get((r.account_id, sid), ()):
                flagged.add(sid)
            if coverage_of is not None:
                continue
            hit = directory.get(sid, {}).get(nk) if nk else None
            if hit is None and (sid in unrefreshed or sid == r.epg_source_id):
                # No directory yet, or the stored first source's row has been retired: what
                # is held is the evidence there is (held_coverage()).
                held = [x for x in (in_guide.get(r.id, {}).get(sid),
                                    kept.get(r.id, {}).get(sid)) if x]
                if held:
                    hit = Coverage(sum(x.count for x in held), max(x.titles for x in held))
            if hit is not None:
                covers[sid] = hit
        if coverage_of is not None:
            covers = dict(coverage_of.get(r.id, {}))
        spans = {sid: sp for sid in mine if (sp := held_span(r.id, sid)) is not None}
        facts[r.id] = (mine, covers, spans, flagged)

    # Listings are read only for a channel where some later source lists past the first's
    # end, which the grouped read above already says.
    def may_fill(cid):
        mine, covers, spans, _flagged = facts[cid]
        end = spans[mine[0]].last_stop if mine[0] in spans else None
        return any(s in covers and s in spans and (end is None or spans[s].last_start >= end)
                   for s in mine[1:])

    listings = future_listings([cid for cid in facts if may_fill(cid)], now)

    out = []
    for r in rows:
        was = stored.get(r.id, {})
        if r.id not in facts:
            out.append(_Placement(r.id, None, [], was, []))
            continue
        mine, covers, spans, flagged = facts[r.id]
        decided = decide_stretches(mine[0], mine[1:], covers, spans, flagged,
                                   {sid: (o, after) for sid, (_p, o, after, _at) in was.items()},
                                   listings.get(r.id, {}), placeholder_names([r.name]))
        ends = [spans[s].last_stop for s in [mine[0]] + [x.source_id for x in decided
                                                           if x.outcome == EPG_STRETCH_FILLS]
                if s in spans]
        out.append(_Placement(r.id, mine[0], decided, was,
                              _plan_moves(mine[0], decided, in_guide.get(r.id, {}),
                                          kept.get(r.id, {})),
                              max(ends) if ends else None))
        time_mod.sleep(0)   # yield the GIL to request threads between channels
    return out


def _fill_event(p: _Placement, names: dict) -> dict | None:
    """The CHANNEL_EPG_FILL_CHANGED row for a channel whose filling sources changed, or
    None. A seam that only moved writes nothing (§14.6)."""
    def name(sid):
        return names.get(sid, f'source {sid}')
    old = [sid for sid, (_pos, outcome, _after, _at) in
           sorted(p.stored.items(), key=lambda x: x[1][0]) if outcome == EPG_STRETCH_FILLS]
    new = [s.source_id for s in p.stretches if s.outcome == EPG_STRETCH_FILLS]
    if old == new or p.first is None:
        return None
    if new:
        detail = (f'Guide now continues from {join_labels([name(s) for s in new])} past the '
                  f'end of {name(p.first)}')
        if old:
            detail += f' (was {join_labels([name(s) for s in old])})'
    else:
        detail = (f'Guide no longer continues from {join_labels([name(s) for s in old])} '
                  f'past the end of {name(p.first)}')
    return {'channel_id': p.channel_id, 'event_type': CHANNEL_EPG_FILL_CHANGED,
            'detail': detail,
            'extra_data': json.dumps({'first': p.first, 'fills': new, 'previous': old})}


def _apply_placements(placements, names: dict, now: datetime) -> tuple[int, int]:
    """Put the rows where the plan says, rewrite the stretch rows that changed and log the
    channels whose filling sources changed. Does not commit. Returns (listings moved into
    the guide, listings moved out). Every statement names its own rows, so run twice it
    moves nothing the second time."""
    moved_in = moved_out = 0
    whole: dict[tuple[int, bool], list[int]] = {}
    for p in placements:
        for sid, into, starting_from, starting_before in p.moves:
            src, dst = ((EpgAlternateEntry, EPGEntry) if into
                        else (EPGEntry, EpgAlternateEntry))
            if starting_from is None and starting_before is None:
                whole.setdefault((sid, into), []).append(p.channel_id)
                continue
            n = _move_rows([p.channel_id], sid, src, dst, starting_from=starting_from,
                           starting_before=starting_before)
            moved_in, moved_out = moved_in + (n if into else 0), moved_out + (0 if into else n)
    for (sid, into), ids in whole.items():
        src, dst = (EpgAlternateEntry, EPGEntry) if into else (EPGEntry, EpgAlternateEntry)
        n = _move_rows(ids, sid, src, dst)
        moved_in, moved_out = moved_in + (n if into else 0), moved_out + (0 if into else n)

    events = []
    for p in placements:
        want = _wanted(p)
        if want == _stored(p):
            continue
        db.session.execute(delete(EpgChannelStretch).where(
            EpgChannelStretch.channel_id == p.channel_id))
        if want:
            db.session.bulk_insert_mappings(EpgChannelStretch, [
                {'channel_id': p.channel_id, 'source_id': sid, 'position': pos,
                 'outcome': outcome, 'after_source_id': after,
                 # When the outcome last changed: a row that only changed place keeps its time.
                 'updated_at': (p.stored[sid][3] if sid in p.stored
                                and p.stored[sid][1] == outcome else now)}
                for sid, (pos, outcome, after) in want.items()])
        event = _fill_event(p, names)
        if event:
            events.append(event)
    if events:
        db.session.bulk_insert_mappings(ChannelEvent, events)
    return moved_in, moved_out


def _wanted(p: _Placement) -> dict:
    return {s.source_id: (s.position, s.outcome, s.after) for s in p.stretches}


def _stored(p: _Placement) -> dict:
    return {sid: row[:3] for sid, row in p.stored.items()}


class _LazyNames:
    """{source_id: name}, read the first time an event needs it: most placements change
    no channel's filling sources, and a key write places one channel at a time."""

    def __init__(self):
        self._names = None

    def get(self, sid, default=None):
        if self._names is None:
            self._names = dict(db.session.query(EpgSource.id, EpgSource.name))
        return self._names.get(sid, default)


def _take_write_lock() -> None:
    """Open the write transaction before the reads a placement decides from. A statement
    that changes no row still takes SQLite's write lock, and under pysqlite a SELECT alone
    opens no transaction, so without it the reads would not be part of the unit."""
    db.session.execute(update(EpgChannelStretch).where(EpgChannelStretch.id == -1)
                       .values(position=EpgChannelStretch.position)
                       .execution_options(synchronize_session=False))


class Placed(NamedTuple):
    """What place_stretches() did."""
    channels: int       # channels whose stretch rows or listings changed
    moved_in: int       # listings moved into epg_entries
    moved_out: int      # ... and out of it


def _channel_rows(channel_ids):
    rows = []
    for chunk in _chunks(channel_ids):
        rows += db.session.query(Channel.id, Channel.account_id, Channel.epg_channel_id,
                                 Channel.hidden, Channel.epg_source_id, Channel.name).filter(
            Channel.id.in_(chunk)).all()
    return rows


def place_stretches(channel_ids, case_sensitive: bool,
                    now: datetime | None = None) -> Placed:
    """Put every listing these channels hold where §14 says it belongs, and record what was
    decided for each source after the first. The first source's rows all go to epg_entries;
    a later source that agrees with the stretch before it (decide_stretches()) contributes
    the programs starting at or after that stretch's last stop; every other row stays in
    the alternates. A channel with no first source keeps no stretch rows.

    A function of the rows held and the stored first source, so it can be run again from
    the top at any time and ends in the same rows: where a seam falls is read each time and
    never stored, and a move interrupted by a restart is finished by the next call. Each
    batch of channels is read, decided and moved in one write transaction. Every
    path that moves or copies listings ends here for the channels it touched; one that did
    not would leave a filling source's whole listing beside the first source's, every
    program doubled. Nothing is fetched. Commits, a chunk of channels at a time."""
    now = now or datetime.utcnow()
    rows = _channel_rows(list(dict.fromkeys(channel_ids)))
    # Only a channel with a guide, or with stretch rows to take away, has anything to place:
    # an import hands over every channel of its accounts, most of which have no guide.
    stale = {cid for chunk in _chunks([r.id for r in rows if not _is_guided(r)])
             for (cid,) in db.session.query(EpgChannelStretch.channel_id).filter(
                 EpgChannelStretch.channel_id.in_(chunk)).distinct()}
    rows = [r for r in rows if _is_guided(r) or r.id in stale]
    ctx = _plan_context([r for r in rows if _is_guided(r)], case_sensitive, True)
    changed = moved_in = moved_out = 0
    names = _LazyNames()
    for batch in chunked(rows, _PLACE_CHUNK):
        @retry_on_locked()
        def _place_and_commit(batch=batch):
            # Read, decide and move in one write transaction: a plan read before another
            # writer's commit and applied after it (an import finishing in between) would
            # put a filling source's listings beside the ones that import just wrote.
            _take_write_lock()
            fresh = _channel_rows([r.id for r in batch])
            placements = [p for p in _plan_stretches(fresh, case_sensitive, now, ctx=ctx)
                          if p.moves or _wanted(p) != _stored(p)]
            done = _apply_placements(placements, names, now)
            db.session.commit()
            return (*done, len(placements))
        n_in, n_out, n = _place_and_commit()
        moved_in, moved_out, changed = moved_in + n_in, moved_out + n_out, changed + n
    return Placed(changed, moved_in, moved_out)


def place_channel_now(channel: Channel, case_sensitive: bool, coverage: dict | None = None,
                      now: datetime | None = None) -> None:
    """place_stretches() for one channel inside the caller's own unit: the writers that do
    not commit (set_channel_key(), set_source_override()) call it after their own moves, so
    the key or override and the placement land in one commit. `coverage` is the channel's
    {source_id: Coverage} when the caller has it. Does not commit."""
    now = now or datetime.utcnow()
    db.session.flush()
    row = db.session.query(Channel.id, Channel.account_id, Channel.epg_channel_id,
                           Channel.hidden, Channel.epg_source_id, Channel.name).filter(
        Channel.id == channel.id).one()
    if coverage is None and row.epg_source_id is not None and not row.hidden:
        order = subscriptions_for([row.account_id]).get(row.account_id, [])
        sources = list(dict.fromkeys([row.epg_source_id] + order))
        _keys, _wanted, directory = _channel_directory(channel, sources, case_sensitive)
        coverage = _channel_coverage(channel, sources, directory)
        if row.epg_source_id not in coverage:
            held = held_coverage([row.epg_source_id], [row.id])[row.epg_source_id]
            if row.id in held:
                coverage[row.epg_source_id] = held[row.id]
    placements = _plan_stretches([row], case_sensitive, now,
                                 coverage_of={row.id: coverage or {}})
    _apply_placements(placements, _LazyNames(), now)


def stretch_channel_ids(source_id: int, account_id: int | None = None) -> list[int]:
    """The channels (of one account, when given) with a stretch row on this source: the
    ones whose guide it continues, or was judged for. What a path that takes the source
    away, or changes what it covers, owes a placement beyond the channels it starts."""
    q = db.session.query(EpgChannelStretch.channel_id).filter(
        EpgChannelStretch.source_id == source_id)
    if account_id is not None:
        q = q.filter(EpgChannelStretch.channel_id.in_(
            select(Channel.id).where(Channel.account_id == account_id)))
    return [cid for (cid,) in q]


def make_room_for_first(source_id: int, new_ends: dict[int, datetime]) -> int:
    """Before an import writes a first source's new listings: move out of epg_entries the
    later sources' listings that start before where the new file ends for each channel
    (`new_ends` {channel_id: the file's last stop for it}), so the hours the file grew by
    never hold two sources' programs while the import runs. place_stretches() at the end of
    the import puts back whatever still belongs. Returns the listings moved. Commits, a
    chunk of channels at a time."""
    moved = 0
    firsts = [cid for chunk in _chunks(new_ends) for (cid,) in db.session.query(
        Channel.id).filter(Channel.id.in_(chunk), Channel.epg_source_id == source_id)]
    for chunk in chunked(firsts, _PLACE_CHUNK):
        @retry_on_locked()
        def _move_and_commit(chunk=chunk):
            _take_write_lock()
            early = [(cid, sid) for cid, sid, lo in db.session.query(
                EPGEntry.channel_id, EPGEntry.source_id, func.min(EPGEntry.start_time)).filter(
                EPGEntry.channel_id.in_(chunk), (EPGEntry.source_id + 0) != source_id)
                .group_by(EPGEntry.channel_id, EPGEntry.source_id) if lo < new_ends[cid]]
            n = sum(_move_rows([cid], sid, EPGEntry, EpgAlternateEntry,
                               starting_before=new_ends[cid]) for cid, sid in early)
            db.session.commit()
            return n
        moved += _move_and_commit()
    return moved


def filling_sources(channel_ids) -> dict[int, list[int]]:
    """{channel_id: [the sources continuing its guide, in order]} for the channels that
    have any."""
    out: dict[int, list[int]] = {}
    for chunk in _chunks(channel_ids):
        for cid, sid in db.session.query(
                EpgChannelStretch.channel_id, EpgChannelStretch.source_id).filter(
                EpgChannelStretch.channel_id.in_(chunk),
                EpgChannelStretch.outcome == EPG_STRETCH_FILLS).order_by(
                EpgChannelStretch.channel_id, EpgChannelStretch.position):
            out.setdefault(cid, []).append(sid)
    return out


def account_filling_sources(account_id: int) -> dict[int, list[int]]:
    """filling_sources() for every channel of one account. One query."""
    out: dict[int, list[int]] = {}
    for cid, sid in db.session.query(
            EpgChannelStretch.channel_id, EpgChannelStretch.source_id).join(
            Channel, Channel.id == EpgChannelStretch.channel_id).filter(
            Channel.account_id == account_id,
            EpgChannelStretch.outcome == EPG_STRETCH_FILLS).order_by(
            EpgChannelStretch.channel_id, EpgChannelStretch.position):
        out.setdefault(cid, []).append(sid)
    return out


def filled_counts_by_accounts(account_ids) -> dict[int, dict[tuple[int, int], int]]:
    """{account_id: {(first source, last filling source): channels}} over the channels
    whose guide a later source continues. One grouped query."""
    account_ids = list(account_ids)
    out: dict[int, dict] = {}
    if not account_ids:
        return out
    later = EpgChannelStretch.__table__.alias('later')
    last = ~select(later.c.id).where(
        later.c.channel_id == EpgChannelStretch.channel_id,
        later.c.outcome == EPG_STRETCH_FILLS,
        later.c.position > EpgChannelStretch.position).exists()
    for aid, first, sid, n in db.session.query(
            Channel.account_id, Channel.epg_source_id, EpgChannelStretch.source_id,
            func.count(EpgChannelStretch.id)).join(
            Channel, Channel.id == EpgChannelStretch.channel_id).filter(
            Channel.account_id.in_(account_ids), Channel.epg_source_id.isnot(None),
            EpgChannelStretch.outcome == EPG_STRETCH_FILLS, last).group_by(
            Channel.account_id, Channel.epg_source_id, EpgChannelStretch.source_id):
        out.setdefault(aid, {})[(first, sid)] = n
    return out


def filling_source_ids() -> set[int]:
    """Every source that continues some channel's guide. One query on the stretch index."""
    return {sid for (sid,) in db.session.query(EpgChannelStretch.source_id).filter(
        EpgChannelStretch.outcome == EPG_STRETCH_FILLS).distinct()}


def extended_counts(source_ids) -> dict[int, int]:
    """{source_id: channels, on any account, whose guide it continues}. One grouped query on
    the stretch index."""
    source_ids = list(source_ids)
    if not source_ids:
        return {}
    return dict(db.session.query(EpgChannelStretch.source_id, func.count(EpgChannelStretch.id))
                .filter(EpgChannelStretch.source_id.in_(source_ids),
                        EpgChannelStretch.outcome == EPG_STRETCH_FILLS)
                .group_by(EpgChannelStretch.source_id))


def extends_counts(account_id: int) -> dict[int, int]:
    """{source_id: channels of this account whose guide it continues}. One grouped query."""
    return dict(db.session.query(EpgChannelStretch.source_id, func.count(EpgChannelStretch.id))
                .join(Channel, Channel.id == EpgChannelStretch.channel_id)
                .filter(Channel.account_id == account_id,
                        EpgChannelStretch.outcome == EPG_STRETCH_FILLS)
                .group_by(EpgChannelStretch.source_id))


# ── Reorder preview (DESIGN-epg-sources.md §15.3, dev/changelog/1723) ──────────
#
# What an order change would do, worked out before it happens by the functions that do it:
# resolve_row() for each channel's first source under the new order, with
# reresolve_channels()' rule that a channel moves only to a source it holds rows of, then
# _plan_stretches() for the sources continuing each guide. Nothing is written. A source the
# change would start reading is read through copy_pairs(): each channel sees the rows its
# donor holds, as the copy would give it. A source the change stops reading is left out.
#
# What it reports is what the data that comes first would be, not how far ahead a guide
# reaches: since a later source continues a guide past the end of the one before (§14), an
# order rarely changes reach, so a guide ending sooner is a warning shown when it happens.
# A channel whose first source stays but whose continuing sources change is counted too, so
# "no channel's guide would change" is said only when that is so.

class Trial(NamedTuple):
    """One account's side of an order change."""
    account_id: int
    order: list                     # the subscriptions the change leaves, in priority order
    channels: list | None = None    # the channels the change re-decides; None = every visible one
    refirst: set | None = None      # of those, the ones whose first source may change; None = all


class OrderTrial(NamedTuple):
    """An action's trials, and what the action would say before it runs."""
    trials: list                    # [Trial]
    own_order: str | None = None    # NeedsOwnOrder's sentence, when the action gives one
    noop: bool = False              # already first or last: nothing to preview


class OrderPreview(NamedTuple):
    """What preview_orders() found. Listing counts compare, for each channel whose first
    source changes, the old first source's listings from now on with the new one's, over
    the hours both list (compare_listings())."""
    accounts: int
    changes: dict           # {(old first source, new first source): channels}, None = no guide
    same: int               # listings both sources list as the same program
    moved: int              # ... the same program at another time
    different: int          # a different title in the same slot
    only_before: int        # listed only by the old first source
    only_after: int         # ... only by the new one
    described_before: int   # of `same`, described by the old source
    described_after: int
    episodes_before: int    # of `same`, carrying an episode title in the old source
    episodes_after: int
    unaligned: int          # switched channels where fewer than half the listings pair up
    refilled: int           # channels keeping their first source whose continuing sources change
    shorter: int            # channels whose guide would end sooner
    most_hours_shorter: int # ... by up to this many hours, rounded up
    renamed: int            # of `same`, one program named two ways
    placeholder_before: int # listings of the old first source titled with the channel's name
    placeholder_after: int  # ... of the new one

    def _sum(self, keep) -> int:
        return sum(n for (old, new), n in self.changes.items() if keep(old, new))

    @property
    def switched(self) -> int:
        return self._sum(lambda old, new: old is not None and new is not None)

    @property
    def gained(self) -> int:
        return self._sum(lambda old, new: old is None)

    @property
    def lost(self) -> int:
        return self._sum(lambda old, new: new is None)


_PREVIEW_COUNTS = ('same', 'moved', 'different', 'only_before', 'only_after',
                   'described_before', 'described_after', 'episodes_before',
                   'episodes_after', 'unaligned', 'refilled', 'shorter', 'most_hours_shorter',
                   'renamed', 'placeholder_before', 'placeholder_after')


class _TrialRow(NamedTuple):
    id: int
    account_id: int
    epg_channel_id: str | None
    hidden: bool
    epg_source_id: int | None
    name: str | None


class _ListEntry(NamedTuple):
    """A provider list entry as placed_by_provider() reads it."""
    id: int | None
    entry: str
    source_id: int | None


#: The list of a provider with none yet: ensure_provider_list() would write the slot.
_SLOT_ONLY = [_ListEntry(None, PROVIDER_ORDER_OWN_GUIDE, None)]


def _merge_held(a, b):
    """One source's _Held for one channel over both tables, as a copy into the alternates
    would hold them."""
    parts = [x for x in (a, b) if x is not None]
    if not parts:
        return None
    return _Held(min(x.first_start for x in parts), max(x.last_start for x in parts),
                 max(x.last_stop for x in parts), sum(x.count for x in parts),
                 max(x.titles for x in parts))


def _trial_held(raw, channel_ids, dropped, borrowed) -> tuple[dict, dict]:
    """_held_by_table() as it would read after the change: the stopped sources' rows gone,
    and each copied source's rows replaced by its donor's, in the alternates."""
    in_guide, kept = {}, {}
    for cid in channel_ids:
        g = {s: h for s, h in raw[0].get(cid, {}).items() if s not in dropped}
        k = {s: h for s, h in raw[1].get(cid, {}).items() if s not in dropped}
        for sid, donor in borrowed.get(cid, {}).items():
            g.pop(sid, None)
            k.pop(sid, None)
            merged = _merge_held(raw[0].get(donor, {}).get(sid), raw[1].get(donor, {}).get(sid))
            if merged is not None:
                k[sid] = merged
        if g:
            in_guide[cid] = g
        if k:
            kept[cid] = k
    return in_guide, kept


def _held_coverage_from(held, source_ids) -> dict[int, dict[int, Coverage]]:
    """held_coverage() counted from _held_by_table()-shaped rows."""
    out: dict[int, dict[int, Coverage]] = {sid: {} for sid in source_ids}
    for table in held:
        for cid, per in table.items():
            for sid, h in per.items():
                if sid not in out:
                    continue
                prev = out[sid].get(cid)
                out[sid][cid] = (Coverage(h.count, h.titles) if prev is None else Coverage(
                    prev.entry_count + h.count, max(prev.distinct_titles, h.titles)))
    return out


def _detailed_listings(pairs, now: datetime) -> dict[tuple[int, int], list]:
    """{(channel_id, source_id): [Listing] from now on, by start} over both tables, with the
    sub-title, for the pairs asked. `description` is only whether there is one ('y' or
    None), so a large feed's text is not read to answer a yes or no."""
    by_source: dict[int, set[int]] = {}
    for cid, sid in pairs:
        by_source.setdefault(sid, set()).add(cid)
    out: dict[tuple[int, int], list] = {}
    for sid, cids in by_source.items():
        for chunk in _chunks(sorted(cids)):
            for model in (EPGEntry, EpgAlternateEntry):
                described = func.length(func.trim(func.coalesce(model.description, ''))) > 0
                for cid, title, sub, has, start, stop in db.session.query(
                        model.channel_id, model.title, model.sub_title, described,
                        model.start_time, model.stop_time).filter(
                        _of_source(model, sid), model.channel_id.in_(chunk),
                        model.stop_time > now):
                    out.setdefault((cid, sid), []).append(
                        Listing(title, sub, 'y' if has else None, start, stop))
    for rows in out.values():
        rows.sort(key=lambda x: x.start)
    return out


def _preview_account(t: Trial, case_sensitive: bool, now: datetime,
                     acc: Counter, changes: Counter) -> None:
    aid = t.account_id
    present = subscriptions_for([aid])[aid]
    order = list(dict.fromkeys(t.order))
    dropped = set(present) - set(order)
    borrowed: dict[int, dict[int, int]] = {}
    for sid in order:
        if sid not in present:
            _covered, pairs = copy_pairs(aid, sid, case_sensitive)
            for target, donor in pairs:
                borrowed.setdefault(target, {})[sid] = donor

    q = db.session.query(Channel.id, Channel.account_id, Channel.epg_channel_id, Channel.hidden,
                         Channel.epg_source_id, Channel.epg_source_override_id,
                         Channel.name).filter(
        Channel.account_id == aid, Channel.hidden.is_(False))
    rows = (q.all() if t.channels is None
            else [r for chunk in _chunks(sorted(set(t.channels))) for r in q.filter(
                Channel.id.in_(chunk))])
    if not rows:
        return
    ids = [r.id for r in rows]
    donors = {d for per in borrowed.values() for d in per.values()}
    raw = _held_by_table(sorted(set(ids) | donors))
    held = _trial_held(raw, ids, dropped, borrowed)

    candidates = set(order) | set(present) | {r.epg_source_override_id for r in rows
                                              if r.epg_source_override_id}
    keys = key_answers(candidates)
    coverage = directory_coverage(candidates, case_sensitive)
    unrefreshed = sources_without_directory(candidates)
    held_cov = _held_coverage_from(held, unrefreshed) if unrefreshed else None

    first: dict[int, int | None] = {}
    for r in rows:
        now_first = None if r.epg_source_id in dropped else r.epg_source_id
        if t.refirst is not None and r.id not in t.refirst:
            first[r.id] = now_first
            continue
        new, _why = resolve_row(r, order, keys, coverage, case_sensitive, held_cov)
        if new is not None and not (new in held[0].get(r.id, {}) or new in held[1].get(r.id, {})):
            new = now_first     # reresolve_channels(): never onto a source holding nothing here
        first[r.id] = new

    cache: dict[int, dict] = {}

    def future(cids, when):
        want = [c for c in cids if c not in cache]
        if want:
            got = _future_listings(sorted(set(want) | {d for c in want
                                                       for d in borrowed.get(c, {}).values()}),
                                   when)
            for c in want:
                mine = {s: v for s, v in got.get(c, {}).items() if s not in dropped}
                for sid, donor in borrowed.get(c, {}).items():
                    mine.pop(sid, None)
                    if sid in got.get(donor, {}):
                        mine[sid] = got[donor][sid]
                cache[c] = mine
        return {c: cache[c] for c in cids}

    ctx = _PlanContext({aid: order}, keys, _shared_id_flags([aid], case_sensitive), coverage,
                       unrefreshed)
    trial_rows = [_TrialRow(r.id, aid, r.epg_channel_id, False, first[r.id], r.name)
                  for r in rows]
    planned = {p.channel_id: p for p in _plan_stretches(
        trial_rows, case_sensitive, now, ctx=ctx, held_by_table=lambda _ids: held,
        future_listings=future)}
    fills_now = filling_sources(ids)

    switched = []
    for r in rows:
        old, new = r.epg_source_id, first[r.id]
        p = planned.get(r.id)
        if old != new:
            changes[(old, new)] += 1
            if old is not None and new is not None:
                switched.append((r.id, old, new))
        elif old is not None and p is not None and fills_now.get(r.id, []) != [
                s.source_id for s in p.stretches if s.outcome == EPG_STRETCH_FILLS]:
            acc['refilled'] += 1
        before = max((h.last_stop for h in raw[0].get(r.id, {}).values()), default=None)
        after = p.end if p is not None else None
        if new is not None and before is not None and after is not None and after < before:
            acc['shorter'] += 1
            acc['most_hours_shorter'] = max(acc['most_hours_shorter'],
                                            math.ceil((before - after).total_seconds() / 3600))

    reads = {(cid, old) for cid, old, _new in switched}
    reads |= {(borrowed.get(cid, {}).get(new, cid), new) for cid, _old, new in switched}
    detail = _detailed_listings(reads, now)
    name_of = {r.id: r.name for r in rows}
    for cid, old, new in switched:
        cmp = compare_listings(detail.get((cid, old), []),
                               detail.get((borrowed.get(cid, {}).get(new, cid), new), []),
                               placeholders=placeholder_names([name_of[cid]]))
        acc['same'] += cmp.same
        acc['renamed'] += cmp.renamed
        acc['placeholder_before'] += cmp.placeholder_active
        acc['placeholder_after'] += cmp.placeholder_other
        acc['moved'] += cmp.moved
        acc['different'] += cmp.different
        acc['only_before'] += cmp.only_active
        acc['only_after'] += cmp.only_other
        acc['described_before'] += cmp.described_active
        acc['described_after'] += cmp.described_other
        for row in cmp.rows:
            if row.kind == ROW_SAME and row.counted:
                acc['episodes_before'] += bool(row.active.sub_title)
                acc['episodes_after'] += bool(row.other.sub_title)
        if cmp.verdict == CMP_UNRELATED:
            acc['unaligned'] += 1
        time_mod.sleep(0)   # yield the GIL to request threads between channels


def preview_orders(trials, case_sensitive: bool,
                   now: datetime | None = None) -> OrderPreview:
    """What these order changes would do to each channel's guide, without doing it. Reads
    only. A channel's first source is decided as the change would decide it, its guide's
    end with the sources that would continue it, and the old and new first source's
    listings are compared for every channel whose first source changes."""
    now = now or datetime.utcnow()
    acc: Counter = Counter()
    changes: Counter = Counter()
    for t in trials:
        _preview_account(t, case_sensitive, now, acc, changes)
    return OrderPreview(len(trials), dict(changes), *(acc[f] for f in _PREVIEW_COUNTS))


def _holders(account_id: int, source_ids) -> list[int]:
    """The account's visible channels holding any of these sources' listings or starting
    their guide with one: the only ones whose guide a move of those sources can change
    (settle_accounts()' argument)."""
    out = set()
    for sid in source_ids:
        out.update(_holding_channels(account_id, sid))
    if source_ids:
        out.update(cid for (cid,) in db.session.query(Channel.id).filter(
            Channel.account_id == account_id, Channel.hidden.is_(False),
            Channel.epg_source_id.in_(list(source_ids))))
    return sorted(out)


def move_trial(account_id: int, source_id: int, step: int) -> OrderTrial | None:
    """move_source()'s change as a trial. None when the account does not read the source."""
    present = subscriptions_for([account_id])[account_id]
    new = moved_order(present, source_id, step)
    if new is None:
        return None
    if new == present:
        return OrderTrial([], noop=True)
    placed = placed_for_account(account_id)
    own = (own_order_sentence(account_id)
           if placed and not still_following(placed, new) else None)
    traded = {s for i, s in enumerate(present) if new[i] != s}
    return OrderTrial([Trial(account_id, new, _holders(account_id, traded))], own)


def stop_trial(account_id: int, source_id: int) -> OrderTrial | None:
    """stop_using_source()'s change as a trial: the channels it starts are re-decided, the
    ones it continues are placed again. None when the account does not read the source."""
    present = subscriptions_for([account_id])[account_id]
    if source_id not in present:
        return None
    affected = {cid for (cid,) in db.session.query(Channel.id).filter(
        Channel.account_id == account_id, Channel.epg_source_id == source_id)}
    filled = stretch_channel_ids(source_id, account_id)
    own = (own_order_sentence(account_id) if source_id in placed_for_account(account_id)
           else None)
    return OrderTrial([Trial(account_id, [s for s in present if s != source_id],
                             sorted(affected | set(filled)), affected)], own)


def _following_order(account_id: int, entries, present: list) -> list:
    return follow_order(placed_by_provider(entries, own_guide_ids([account_id]).get(account_id)),
                        present)


def follow_trial(account_id: int) -> OrderTrial | None:
    """follow_provider_again()'s change as a trial. None when the account is on no
    provider."""
    account = db.session.get(Account, account_id)
    if account is None or account.provider_id is None:
        return None
    present = subscriptions_for([account_id])[account_id]
    order = _following_order(account_id, provider_list(account.provider_id) or _SLOT_ONLY,
                             present)
    return OrderTrial([Trial(account_id, order)])


def provider_move_trial(provider_id: int, entry_id: int, step: int) -> OrderTrial | None:
    """move_provider_entry()'s change as a trial over every account following the list.
    None when the entry is not on it."""
    entries = provider_list(provider_id)
    ids = [e.id for e in entries]
    new = moved_order(ids, entry_id, step)
    if new is None:
        return None
    if new == ids:
        return OrderTrial([], noop=True)
    traded = _traded(entries, new)
    reordered = sorted(entries, key=lambda e: new.index(e.id))
    following = [aid for aid in provider_account_ids(provider_id)
                 if follows(db.session.get(Account, aid))]
    guides = own_guide_ids(following)
    present = subscriptions_for(following)
    # The channels move_provider_entry() re-decides: settle_accounts()' holders, no more.
    return OrderTrial([Trial(aid, _following_order(aid, reordered, present[aid]),
                             sorted({cid for sid in _entry_sources(traded, guides.get(aid))
                                     for cid in _holding_channels(aid, sid)}))
                       for aid in following])


def join_trial(account_ids, provider_id: int | None) -> OrderTrial:
    """provider_changed()'s change for accounts joining a provider, as a trial: each stops
    using its old provider's sources and follows the new list. `provider_id` None is a
    provider not created yet, whose list is the slot alone. An account already on the
    provider changes nothing and is left out."""
    trials = []
    for aid in account_ids:
        account = db.session.get(Account, aid)
        if account is None or (provider_id is not None and account.provider_id == provider_id):
            continue
        present = subscriptions_for([aid])[aid]
        if account.provider_id is not None:
            theirs = {sid for (sid,) in db.session.query(EpgSource.id).filter(
                EpgSource.owner_provider_id == account.provider_id)}
            present = [s for s in present if s not in theirs]
        entries = (provider_list(provider_id) if provider_id is not None else []) or _SLOT_ONLY
        trials.append(Trial(aid, _following_order(aid, entries, present)))
    return OrderTrial(trials)


def _names(source_ids, names: dict) -> str:
    return join_labels([names.get(s, f'source {s}') for s in source_ids])


def preview_lines(p: OrderPreview, names: dict) -> list[str]:
    """The preview in plain sentences, one per paragraph. `names` {source_id: name}. Says
    what the comparison can stand behind: a different title in the same slot is not called
    a different program, since two guides often name one program differently."""
    if not p.changes and not p.refilled and not p.shorter:
        return ["No channel's guide would change."]
    lines = []
    if p.switched:
        moves = sorted(((n, old, new) for (old, new), n in p.changes.items()
                        if old is not None and new is not None), key=lambda x: (-x[0], x[1], x[2]))
        parts = [f'{n:,} from {_names([old], names)} to {_names([new], names)}'
                 for n, old, new in moves[:3]]
        if len(moves) > 3:
            parts.append(f'{sum(n for n, _o, _n in moves[3:]):,} between other sources')
        lines.append(f'{counted(p.switched, "channel")} would start '
                     f'{plural(p.switched, "its", "their")} guide with a different source: '
                     f'{join_labels(parts)}.')
    if p.gained:
        lines.append(f'{counted(p.gained, "channel")} with no guide now would get one.')
    if p.lost:
        lines.append(f'{counted(p.lost, "channel")} would have no guide.')
    if p.switched:
        judged = p.same + p.moved + p.different + p.only_before + p.only_after
        if judged:
            renamed = f' ({p.renamed:,} of them named differently)' if p.renamed else ''
            lines.append(
                f'Over the hours both list for those channels, {counted(p.same, "listing")} '
                f'{plural(p.same, "is", "are")} the same program{renamed}, {p.moved:,} the same '
                f'program at another time, and {p.different:,} '
                f'{plural(p.different, "has", "have")} a different title in the same slot (some '
                f'of those may still be one program named two ways). {p.only_before:,} '
                f'{plural(p.only_before, "is", "are")} listed only by the source used now and '
                f'{p.only_after:,} only by the new one.')
        else:
            lines.append('The two sources list no hours in common for those channels, so '
                         'their listings could not be compared.')
        if p.placeholder_before or p.placeholder_after:
            lines.append(
                f'{p.placeholder_before:,} {plural(p.placeholder_before, "listing", "listings")} '
                f'from the source used now and {p.placeholder_after:,} from the new one are only '
                f"the channel's name, not a program, so they are not counted above.")
        if p.same:
            lines.append(
                f'Of the same programs, {_share(p.described_before, p.same)} have a '
                f'description now and {_share(p.described_after, p.same)} would after; '
                f'{_share(p.episodes_before, p.same)} have an episode title now and '
                f'{_share(p.episodes_after, p.same)} would after.')
        if p.unaligned:
            lines.append(f'On {counted(p.unaligned, "channel")} fewer than half the listings '
                         'line up between the two sources.')
    if p.refilled:
        lines.append(f'{counted(p.refilled, "channel")} would keep the source '
                     f'{plural(p.refilled, "its guide starts", "their guides start")} with but '
                     f'be continued past its end by different sources.')
    if p.shorter:
        whose = (f'{counted(p.shorter, "channel")}\'s guide' if p.shorter == 1
                 else f'{p.shorter:,} channels\' guides')
        lines.append(f'{whose} would end sooner, by up to '
                     f'{counted(p.most_hours_shorter, "hour")}.')
    return lines


def preview_json(t: OrderTrial, case_sensitive: bool) -> dict:
    """The API answer for one action's preview: the sentences, the counts behind them, and
    the own-order sentence the action would need confirmed."""
    if t.noop:
        return {'success': True, 'noop': True, 'lines': [], 'own_order': None}
    p = preview_orders(t.trials, case_sensitive)
    ids = {s for pair in p.changes for s in pair if s is not None}
    names = dict(db.session.query(EpgSource.id, EpgSource.name).filter(
        EpgSource.id.in_(list(ids)))) if ids else {}
    return {'success': True, 'noop': False, 'lines': preview_lines(p, names),
            'own_order': t.own_order, 'accounts': p.accounts, 'switched': p.switched,
            'gained': p.gained, 'lost': p.lost, 'refilled': p.refilled, 'shorter': p.shorter,
            'counts': {f: getattr(p, f) for f in _PREVIEW_COUNTS}}
