"""Auto-record rules: a saved Programs search that schedules whatever it finds.

The spec is dev/docs/DESIGN-autoschedule.md, and the section numbers below are its. In one
paragraph (§1): a rule is a Programs search plus recording settings. Each evaluation re-runs
the search through channel_search.search(), judges every upcoming showing it returns against
the rule's ledger (RuleMatch), and schedules the ones that pass as ordinary recordings - planned
by the bulk scheduler's planner and created through the one create path, so failover, pre-record
checks and connection waits all work unchanged. Every showing judged gets a ledger row naming
what was decided and why, which is what makes evaluation idempotent, makes a cancellation stick
and skips replays.

Three things to keep in mind when changing this module:

* **Judging is batched; scheduling is not** (§4.3). Config, the search context, the ledger and
  the library reads happen once per run, and judging a showing does no query - the `/guide`
  11-second lesson. Planning and creating cost queries per scheduled showing by construction,
  bounded by the rule's cap. Everything read once is snapshotted into plain values first:
  every commit expires the session's objects, and touching an expired one is a query.
* **Every write re-checks the rule** (§4.3). A rule can be edited, paused or deleted while a
  run evaluates it, and those paths commit outside admission. Each write unit re-reads the rule
  inside its own closure and writes nothing if it moved; the run then abandons that rule.
* **Nothing here decides a recording is "already done" by inference** (CLAUDE.md). The ledger
  row is written in the same commit as the recording it names, and a deleted pick's fate is
  stamped onto its row by detach_recording_references().
"""
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode

from sqlalchemy import func, or_, update
from sqlalchemy.orm.exc import ObjectDeletedError
from werkzeug.datastructures import MultiDict

from . import admission, db
from .database import (
    AutoRule, CANCEL_BEFORE_START, CANCEL_DURING_ANALYSIS, CANCEL_DURING_CAPTURE,
    CANCEL_DURING_CONVERSION, CANCEL_GROUP_CHANGE, Channel, ChannelGroup, EPGEntry,
    REC_STATUS_ABORTED, REC_STATUS_FAILED, REC_STATUS_SCHEDULED, RECORDING_RULE_PROGRAM_MOVED,
    RECORDING_RULE_RELEASED, RECORDING_RULE_RETRACKED, RECORDING_RULE_SCHEDULED,
    RECORDING_START_NOT_ARMED, Recording,
    RecordingProfile, RULE_MATCH_SCHEDULED,
    RULE_MATCH_SKIPPED_CAP, RULE_MATCH_SKIPPED_EXISTING, RULE_MATCH_SKIPPED_LIMIT,
    RULE_MATCH_SKIPPED_REPEAT, RULE_MATCH_SKIPS, RULE_MATCH_USER_MODIFIED, RuleMatch,
    add_recording_event, detach_recording_references,
)
from .db_utils import retry_on_locked
from .program_titles import title_key

log = logging.getLogger(__name__)

#: The engine reads at most this many upcoming showings per rule - two pages of the search's
#: MAX_PAGE_SIZE. A rule matching more is too broad to be what anyone meant, and is reported
#: broken rather than silently truncated (§4.3 step 2).
MAX_SHOWINGS = 1000
#: Under this, the guide (or a source a rule depends on) is too short to be trusted (§4.5).
#: A constant, not a setting, by decision (§9).
GUIDE_REACH_MIN = timedelta(hours=24)

#: Search parameters that describe a page rather than a result set (§3.1). A stored rule never
#: carries them; the engine sets its own sort and paging.
_PAGE_ONLY = ('sort', 'page', 'per_page', 'facets', 'add_to_group', 'replace_rec', 'rule')

#: A rule recording "spoken for" exactly as the search page's Record button reads it.
_ABORTED_BY_A_PERSON = (CANCEL_BEFORE_START, CANCEL_DURING_CAPTURE,
                        CANCEL_DURING_ANALYSIS, CANCEL_DURING_CONVERSION)
#: An ABORTED pick that never started frees its limit slot (§3.5).
_NEVER_STARTED = (CANCEL_BEFORE_START, CANCEL_GROUP_CHANGE)

TRIGGER_IMPORT = 'guide import'
TRIGGER_SAVE = 'rule saved'
TRIGGER_SWEEP = 'periodic sweep'
TRIGGER_QUEUED = 'queued'


class RuleError(ValueError):
    """A rule the user tried to save that cannot be a rule, with the sentence saying why."""


def describe_error(exc) -> str:
    """An exception as a sentence fit to store on a rule and show a person. A database error's
    own text carries the whole statement and its parameters - a recording's stream URL among
    them, with the account's login in its path - so only the driver's message is kept, and
    whatever is left goes through the credential masker. Kept short: it lands in a table
    cell and a toast."""
    from .url_utils import mask_creds_in_text
    text = str(getattr(exc, 'orig', None) or exc).strip() or type(exc).__name__
    text = mask_creds_in_text(f'{type(exc).__name__}: {text}')
    return text if len(text) <= 300 else text[:297] + '...'


def _spoken_for():
    from .channel_search_rows import REC_DONE, REC_LIVE
    return (REC_STATUS_SCHEDULED,) + tuple(REC_LIVE) + tuple(REC_DONE)


# ---------------------------------------------------------------------------
# The stored search (§3.1)
# ---------------------------------------------------------------------------

def parse_search(params_text: str):
    """The rule's stored spelling as a SearchState, or SearchStateError. Never trusted as text:
    the URL contract has one speller, and this is its reader."""
    from .channel_search import SearchState
    return SearchState.from_params(MultiDict(parse_qsl(params_text or '', keep_blank_values=True)))


def clean_search(raw_params: str):
    """(state, params_text) for a search a person is saving as a rule, or RuleError.

    The page-only parts are dropped and the result re-serialized through to_params(), so what
    is stored is the canonical spelling. The four refusals of §3.1 come with a sentence each,
    and so does a When, Day of week, Time of day or Program length value that does not parse
    - it would match nothing or stop the rule, which is the one outcome a rule running with
    nobody watching must never be allowed."""
    from .channel_search import (
        DEFAULT_PAGE_SIZE, DEFAULT_SORT_BY_GRAIN, DOW_LABELS, DUR_PREFIX, GRAIN_AIRINGS,
        SearchStateError, TOD_PREFIX, WHEN_CUSTOM_PREFIX, WHEN_NEXT_PREFIX, WHEN_NOW,
        WHEN_STATIC_VALUES, parse_duration, parse_time_of_day, parse_when_custom,
        parse_when_next, standing_applied)
    from .tz_utils import get_display_tz
    try:
        state = parse_search(raw_params)
    except SearchStateError as exc:
        raise RuleError(f'That search cannot be read: {exc}.') from exc
    if state.grain != GRAIN_AIRINGS:
        raise RuleError('A rule records showings, so it needs a Programs search - '
                        'a channel has no start time.')
    when = state.filter_for('when')
    if when is not None and WHEN_NOW in when.values:
        raise RuleError('A rule cannot use "On now" - it never joins a program that has '
                        'already started. Pick a window that looks ahead instead.')
    if standing_applied(state.standing, 'firstonly'):
        raise RuleError('A rule cannot use "One row per channel" - it hides later showings '
                        'on the same channel, which is exactly where next week\'s would be.')
    if not standing_applied(state.standing, 'showpast'):
        raise RuleError('A rule cannot use "Show airings that have ended" - it only ever '
                        'acts on the future.')
    tz = get_display_tz()
    if when is not None:
        for value in when.values + when.ex:
            if value in WHEN_STATIC_VALUES:
                continue
            ok = ((value.startswith(WHEN_NEXT_PREFIX) and parse_when_next(value) is not None)
                  or (value.startswith(WHEN_CUSTOM_PREFIX)
                      and parse_when_custom(value, tz) is not None))
            if not ok:
                raise RuleError(f'The When value "{value}" is not a complete window, so the '
                                'rule would match nothing. Fill it in or remove it.')
    duration = state.filter_for('duration')
    if duration is not None:
        for value in duration.values + duration.ex:
            if not value.startswith(DUR_PREFIX) or parse_duration(value) is None:
                raise RuleError(f'The Program length value "{value}" is not complete, so the '
                                'rule would match nothing. Fill it in or remove it.')
    days = state.filter_for('dow')
    if days is not None:
        for value in days.values + days.ex:
            if value not in DOW_LABELS:
                raise RuleError(f'"{value}" is not a day of the week, so the rule cannot '
                                'run. Pick the day again or remove it.')
    clock = state.filter_for('tod')
    if clock is not None:
        for value in clock.values + clock.ex:
            if not value.startswith(TOD_PREFIX) or parse_time_of_day(value) is None:
                raise RuleError(f'The Time of day value "{value}" is not a complete time '
                                'range, so the rule would match nothing. Fill it in or '
                                'remove it.')
    state = replace(state, sort=DEFAULT_SORT_BY_GRAIN[GRAIN_AIRINGS], sort_desc=False, page=1,
                    page_size=DEFAULT_PAGE_SIZE, facets=None, add_to_group=None,
                    replace_rec=None, rule=None)
    return state, urlencode(state.to_params())


# ---------------------------------------------------------------------------
# Names the search depends on (§3.2)
# ---------------------------------------------------------------------------

def _named(state) -> dict:
    """{'tag'|'group'|'cat': set of names, 'chan'|'acct': set of raw values} the search names,
    included or excluded alike - an excluded value that is gone changes the answer too."""
    from .channel_search import GROUP_ANY
    out = {'tag': set(), 'group': set(), 'cat': set(), 'chan': set(), 'acct': set()}
    for f in state.filters:
        if f.key in out:
            out[f.key].update(f.values)
            out[f.key].update(f.ex)
    out['group'].discard(GROUP_ANY)
    return out


@dataclass
class _Known:
    """What exists, read once per run for every rule at once (§3.2)."""
    tags: set
    groups: set
    channels: set
    accounts: set
    categories: set


def known_names(states, ctx=None) -> _Known:
    """What exists among everything `states` name. With `ctx`, its tags and accounts - the
    run-wide read; without, a fresh read of the database, for confirming a name really is
    gone before a rule is called broken (a rename that commits mid-run moves the rule's
    spelling without moving its revision, so the run-wide read can be behind it)."""
    wanted_chan, wanted_cat = set(), set()
    for state in states:
        named = _named(state)
        wanted_chan |= {int(v) for v in named['chan'] if str(v).isdigit()}
        wanted_cat |= named['cat']
    channels = set()
    if wanted_chan:
        channels = {cid for (cid,) in db.session.query(Channel.id).filter(
            Channel.id.in_(sorted(wanted_chan)))}
    categories = set()
    if wanted_cat:
        categories = {c for (c,) in db.session.query(Channel.category_name).filter(
            Channel.category_name.in_(sorted(wanted_cat))).distinct()}
    groups = {n for (n,) in db.session.query(ChannelGroup.name)}
    if ctx is not None:
        tags, accounts = set(ctx.tags_by_name), set(ctx.accounts_by_id)
    else:
        from .database import Account, Tag
        tags = {n for (n,) in db.session.query(Tag.name)}
        accounts = {i for (i,) in db.session.query(Account.id)}
    return _Known(tags=tags, groups=groups, channels=channels, accounts=accounts,
                  categories=categories)


def gone_values(state, known: _Known) -> dict:
    """{dimension key: set of values} the search names that no longer exist - the one
    spelling of "gone", read by missing_names() for the sentence and by the rule page to mark
    each value where it is listed."""
    named = _named(state)
    return {
        'tag': {n for n in named['tag'] if n not in known.tags},
        'group': {n for n in named['group'] if n not in known.groups},
        'cat': {n for n in named['cat'] if n not in known.categories},
        'chan': {v for v in named['chan']
                 if not str(v).isdigit() or int(v) not in known.channels},
        'acct': {v for v in named['acct']
                 if not str(v).isdigit() or int(v) not in known.accounts},
    }


def missing_names(state, known: _Known) -> str:
    """'' when everything the search names still exists, else the sentence saying what is
    gone. A name that is gone matches nothing on the search page; in a rule that would be a
    rule quietly doing nothing, so it is reported instead (§3.2)."""
    gv = gone_values(state, known)
    gone = []
    gone += [f'the tag "{n}"' for n in sorted(gv['tag'])]
    gone += [f'the channel group "{n}"' for n in sorted(gv['group'])]
    gone += [f'the category "{n}"' for n in sorted(gv['cat'])]
    gone += [f'channel #{v}' for v in sorted(gv['chan'])]
    gone += [f'account #{v}' for v in sorted(gv['acct'])]
    if not gone:
        return ''
    return f'Its search names {", ".join(gone)}, which no longer exist{"s" if len(gone) == 1 else ""}.'


def rules_naming(dimension: str) -> list:
    """[(rule name, {values})] for every rule whose search names something in `dimension`
    ('tag' or 'group'), included or excluded - what a delete confirm lists. One query; a rule
    whose spelling no longer parses names nothing it can be held to."""
    from .channel_search import SearchStateError
    out = []
    for name, params in db.session.query(AutoRule.name, AutoRule.search_params).order_by(
            AutoRule.name):
        try:
            values = _named(parse_search(params)).get(dimension, set())
        except SearchStateError:
            continue
        if values:
            out.append((name, values))
    return out


def rename_in_rules(dimension: str, old: str, new: str) -> int:
    """Rewrite every rule whose search names `old` in `dimension` ('tag' or 'group') to name
    `new`. Mutates without committing: the caller is the rename itself, and the rewrite lands
    in the rename's own commit (§3.2). Parsed and re-serialized, never string-replaced, and
    the rule's revision is NOT bumped - this is the user's own rename following through.

    Returns how many rules were rewritten."""
    from .channel_search import DimensionFilter, SearchStateError
    if old == new:
        return 0
    # The write lock before the read, for delete_rule()'s reason: rules read outside the
    # rename's transaction would be written back over an edit that committed in between (and
    # a rule deleted in between would fail the whole rename). A no-op UPDATE takes the lock;
    # what is read after it cannot change before this commit.
    db.session.execute(update(AutoRule).values(revision=AutoRule.revision)
                       .execution_options(synchronize_session=False))
    rewritten = 0
    for rule in AutoRule.query.populate_existing().all():
        try:
            state = parse_search(rule.search_params)
        except SearchStateError:
            continue   # already broken, and reported as such by every evaluation
        changed = False
        filters = []
        for f in state.filters:
            if f.key == dimension and (old in f.values or old in f.ex):
                f = DimensionFilter(f.key,
                                    tuple(new if v == old else v for v in f.values),
                                    tuple(new if v == old else v for v in f.ex))
                changed = True
            filters.append(f)
        if changed:
            rule.search_params = urlencode(replace(state, filters=tuple(filters)).to_params())
            rewritten += 1
    return rewritten


# ---------------------------------------------------------------------------
# Snapshots - plain values read once per run (see the module docstring)
# ---------------------------------------------------------------------------

@dataclass
class _Lib:
    """One recording in the library, as the existing and repeat checks read it."""
    id: int
    name: str
    status: str
    cancel_reason: str | None
    title_key: str
    episode_raw: str
    program_start: datetime | None
    program_stop: datetime | None
    channel_id: int | None
    group_id: int | None
    start: datetime
    stop: datetime
    rule_id: int | None


@dataclass
class _Row:
    """One ledger row of the rule being evaluated."""
    id: int | None
    title_key: str
    episode_key: str
    start: datetime
    stop: datetime
    action: str
    recording_id: int | None
    removed_status: str | None
    removed_cancel_reason: str | None
    channel_id: int | None
    group_id: int | None
    program_title: str | None


@dataclass
class _Showing:
    """One row the rule's search returned, snapshotted before anything commits."""
    entry_id: int
    channel_id: int
    channel_name: str
    group_id: int | None
    group_name: str | None
    title: str
    sub_title: str
    title_key: str
    episode_raw: str
    start: datetime
    stop: datetime
    score: int


@dataclass
class _Program:
    """Every showing of one program at one moment (§3.3, §3.7). `showings[0]` is the one a
    recording is made from; the rest are the channels it was chosen over."""
    identity: tuple
    showings: list

    @property
    def lead(self):
        return self.showings[0]


@dataclass
class _Verdict:
    identity: tuple
    row_id: int | None
    action: str
    detail: str
    lead: _Showing


@dataclass
class RuleOutcome:
    """What one evaluation did to one rule - the save route's summary (§4.1).

    Two kinds of trouble, kept apart because they mean opposite things to the person who
    saved the rule: `stopped` is a check that did not run at all (the rule is broken, or the
    check raised), `problems` names showings a check that DID run could not place or arm -
    alongside the ones it scheduled. `already` counts the programs an earlier check of this
    rule scheduled whose recordings will still happen, so a re-check of a rule whose matches
    are all spoken for does not read as "nothing matches". `retracked` counts the
    recordings this check moved to follow their program (§5)."""
    rule_id: int
    scheduled: int = 0
    skipped: dict = field(default_factory=dict)
    already: int = 0
    capped: int = 0
    retracked: int = 0
    problems: str = ''
    stopped: str = ''
    abandoned: bool = False
    guide_reach: datetime | None = None

    def payload(self):
        return {'rule_id': self.rule_id, 'scheduled': self.scheduled,
                'skipped': dict(self.skipped), 'already_scheduled': self.already,
                'retracked': self.retracked,
                'error': self.stopped or None, 'problems': self.problems or None,
                'abandoned': self.abandoned,
                'guide_reach': (self.guide_reach.strftime('%Y-%m-%dT%H:%M:%S')
                                if self.guide_reach else None)}


def _library(extra_ids) -> list:
    """Every recording that is spoken for, plus the ones the ledger names whatever their
    status - one query, columns only, never an ORM object (Recording joins three tables)."""
    clause = Recording.status.in_(_spoken_for())
    if extra_ids:
        clause = or_(clause, Recording.id.in_(sorted(extra_ids)))
    rows = db.session.query(
        Recording.id, Recording.name, Recording.status, Recording.cancel_reason,
        Recording.program_title, Recording.program_sub_title, Recording.program_start_time,
        Recording.program_stop_time, Recording.channel_id, Recording.group_id,
        Recording.start_time, Recording.stop_time, Recording.rule_id).filter(clause).all()
    return [_Lib(id=r.id, name=r.name, status=r.status, cancel_reason=r.cancel_reason,
                 title_key=title_key(r.program_title) if r.program_title else '',
                 episode_raw=title_key(r.program_sub_title),
                 program_start=r.program_start_time, program_stop=r.program_stop_time,
                 channel_id=r.channel_id, group_id=r.group_id, start=r.start_time,
                 stop=r.stop_time, rule_id=r.rule_id) for r in rows]


def _row_snapshot(m: RuleMatch) -> _Row:
    return _Row(id=m.id, title_key=m.title_key, episode_key=m.episode_key or '',
                start=m.program_start_time, stop=m.program_stop_time, action=m.action_taken,
                recording_id=m.recording_id, removed_status=m.removed_status,
                removed_cancel_reason=m.removed_cancel_reason, channel_id=m.channel_id,
                group_id=m.group_id, program_title=m.program_title)


# ---------------------------------------------------------------------------
# "The same program" (§3.3)
# ---------------------------------------------------------------------------

class _Slots:
    """{(title_key, start): non-empty episode keys} over this run's showings, the rule's
    ledger and the library. An empty episode is the same program as a non-empty one only
    when that one is the ONLY distinct episode at its title and start: six games under one
    series title at one moment are six programs, and an episode-less listing among them is a
    seventh rather than absorbing one of the six."""

    def __init__(self):
        self._eps = {}

    def add(self, tkey, start, episode):
        if tkey and start is not None and episode:
            self._eps.setdefault((tkey, start), set()).add(episode)

    def only_episode(self, tkey, start) -> str:
        eps = self._eps.get((tkey, start), ())
        return next(iter(eps)) if len(eps) == 1 else ''

    def identity(self, tkey, start, episode) -> tuple:
        return (tkey, episode or self.only_episode(tkey, start), start)


def _fate(row: _Row, lib_by_id) -> tuple:
    """(status, cancel_reason) of the recording a `scheduled` row made - read from the
    recording while it exists and from what detach_recording_references() stamped once it
    was deleted. (None, None) when neither is known."""
    if row.recording_id is not None:
        rec = lib_by_id.get(row.recording_id)
        return (rec.status, rec.cancel_reason) if rec is not None else (None, None)
    return row.removed_status, row.removed_cancel_reason


def _pick_is_spoken_for(row: _Row, lib_by_id) -> bool:
    """Does this pick make later airings of its program repeats (§3.4)? Yes while it is
    scheduled, recording or recorded, and when a person called it off - a cancellation sticks
    (R6). No when it FAILED, when a group change aborted it, or when its fate is unknown:
    every "no" here re-opens a replay, and an extra replay is the cheap failure."""
    status, reason = _fate(row, lib_by_id)
    if status is None:
        return False
    if status == REC_STATUS_ABORTED:
        return reason in _ABORTED_BY_A_PERSON
    if status == REC_STATUS_FAILED:
        return False
    if status in _spoken_for():
        return True
    log.error('Auto-record: ledger row %s names a recording in unknown status %r; not '
              'counted as a repeat', row.id, status)
    return False


def _pick_counts_for_limit(row: _Row, lib_by_id) -> bool:
    """Is this pick one of the rule's "at most X within Y" (§3.5)? A pick that never started -
    deleted while SCHEDULED, or ABORTED before it began - frees its slot. A FAILED one keeps
    counting: the limit must never become a retry. An unknown fate does not count."""
    status, reason = _fate(row, lib_by_id)
    if status is None:
        return False
    if status == REC_STATUS_SCHEDULED:
        # Still scheduled counts; deleted while SCHEDULED is a pick that never started.
        return row.recording_id is not None
    if status == REC_STATUS_ABORTED:
        return reason not in _NEVER_STARTED
    if status == REC_STATUS_FAILED or status in _spoken_for():
        return True
    log.error('Auto-record: ledger row %s names a recording in unknown status %r; not '
              'counted toward the limit', row.id, status)
    return False


def _state_phrase(status: str) -> str:
    """How a skip's detail names a spoken-for recording's state - the only statuses the
    existing and repeat checks index."""
    from .channel_search_rows import REC_DONE, REC_LIVE
    if status == REC_STATUS_SCHEDULED:
        return 'scheduled'
    if status in REC_LIVE:
        return 'being recorded'
    if status in REC_DONE:
        return 'recorded'
    log.error('Auto-record: a skip names a recording in unexpected status %r', status)
    return status.lower()


def _aborted_by_group_change(row: _Row, lib_by_id) -> bool:
    status, reason = _fate(row, lib_by_id)
    return status == REC_STATUS_ABORTED and reason == CANCEL_GROUP_CHANGE


# ---------------------------------------------------------------------------
# Guide reach (§4.5)
# ---------------------------------------------------------------------------

@dataclass
class _Reach:
    by_source: dict          # {source id winning a channel: latest listing start or None}
    guide_wide: datetime | None


def _guide_reach() -> _Reach:
    """The latest listing start per EPG source that wins at least one channel, and across the
    whole guide. Two index reads per source: the listing that ends last fixes a lower bound
    on the latest start, and only listings ending after that bound can start later - so the
    exact answer reads the guide's last few hours, not the whole table (a GROUP BY over it
    measured 2.6s on 340,678 listings)."""
    winning = [sid for (sid,) in db.session.query(Channel.epg_source_id).filter(
        Channel.epg_source_id.isnot(None)).distinct()]
    by_source = {}
    for sid in winning:
        last = db.session.query(EPGEntry.start_time).filter(
            EPGEntry.source_id == sid).order_by(EPGEntry.stop_time.desc()).limit(1).scalar()
        if last is None:
            by_source[sid] = None
            continue
        by_source[sid] = db.session.query(func.max(EPGEntry.start_time)).filter(
            EPGEntry.source_id == sid, EPGEntry.stop_time >= last).scalar()
    guide_wide = db.session.query(func.max(EPGEntry.start_time)).scalar()
    return _Reach(by_source=by_source, guide_wide=guide_wide)


def _rule_sources(rows, member_channels, channel_source) -> list:
    """[[source ids]] - one entry per ledger match, the sources that match can be seen
    through: a channel match its channel's source, a group match any of its members'."""
    out = []
    for row in rows:
        if row.group_id is not None and member_channels.get(row.group_id):
            sids = {channel_source.get(c) for c in member_channels[row.group_id]}
        else:
            sids = {channel_source.get(row.channel_id)}
        sids.discard(None)
        if sids:
            out.append(sorted(sids))
    return out


def _reach_for(sources, reach: _Reach):
    """(reach, empty) for one rule: the shortest reach among its matches, a group match
    reaching as far as its longest-reaching member. A rule with no matches yet uses the
    guide-wide figure. `empty` is True when something it depends on holds nothing."""
    if not sources:
        return reach.guide_wide, reach.guide_wide is None
    shortest, empty = None, False
    for sids in sources:
        values = [reach.by_source.get(s) for s in sids]
        known = [v for v in values if v is not None]
        best = max(known) if known else None
        if best is None:
            empty = True
            continue
        shortest = best if shortest is None else min(shortest, best)
    return shortest, empty


def _reach_inputs(rule_ids):
    """Everything the per-rule reach needs, read once: each distinct channel and group the
    rules' ledgers name, every such group's members and every channel's source.

    The whole retained ledger, not a recent window: a source a rule has drawn from keeps
    counting until a refresh makes it fresh again, however long ago the rule last matched
    on it. A window would let a drained source age out of the alert while it is still
    drained - exactly when a weekly rule needs it."""
    rows = db.session.query(RuleMatch.rule_id, RuleMatch.channel_id, RuleMatch.group_id).filter(
        RuleMatch.rule_id.in_(sorted(rule_ids))).distinct().all() if rule_ids else []
    by_rule = {}
    for r in rows:
        by_rule.setdefault(r.rule_id, []).append(r)
    group_ids = {r.group_id for r in rows if r.group_id is not None}
    member_channels = {}
    if group_ids:
        from .database import ChannelGroupMember
        for gid, cid in db.session.query(ChannelGroupMember.group_id,
                                         ChannelGroupMember.channel_id).filter(
                ChannelGroupMember.group_id.in_(sorted(group_ids))):
            member_channels.setdefault(gid, []).append(cid)
    channel_ids = {r.channel_id for r in rows if r.channel_id is not None}
    for cids in member_channels.values():
        channel_ids.update(cids)
    channel_source = {}
    if channel_ids:
        channel_source = dict(db.session.query(Channel.id, Channel.epg_source_id).filter(
            Channel.id.in_(sorted(channel_ids))))
    return by_rule, member_channels, channel_source


def refresh_guide_alert(now=None, reach=None):
    """Raise or clear AUTO_RULES_GUIDE_SHORT across every enabled rule (§6). Run by every
    evaluation, including one that finds no enabled rule, and by pause and delete. `reach`
    is the run's own reading when it has one."""
    from .alerts import AUTO_RULES_GUIDE_SHORT, dismiss_open_alerts
    from .tz_utils import format_local
    now = now or datetime.utcnow()
    enabled = [rid for (rid,) in db.session.query(AutoRule.id).filter(AutoRule.enabled.is_(True))]
    source = 'auto-rules:guide-reach'
    if not enabled:
        dismiss_open_alerts(AUTO_RULES_GUIDE_SHORT, source)
        return None
    reach = reach or _guide_reach()
    by_rule, member_channels, channel_source = _reach_inputs(enabled)
    short = []
    if reach.guide_wide is None:
        short.append('The guide holds no listings at all.')
    elif reach.guide_wide - now < GUIDE_REACH_MIN:
        short.append(f'The guide only reaches {format_local(reach.guide_wide)}.')
    names = {}
    if reach.by_source:
        from .database import EpgSource
        names = dict(db.session.query(EpgSource.id, EpgSource.name).filter(
            EpgSource.id.in_(sorted(reach.by_source))))
    # Judged per match, exactly as a rule's own reach is (_reach_for): a group match sees as
    # far as its longest-reaching member, since the group row has listings while any member
    # does. Checking every member's source on its own would raise this alert for a rule
    # whose page says the guide reaches days ahead.
    empty, short_reach = set(), {}
    for rid in enabled:
        for sids in _rule_sources(by_rule.get(rid, []), member_channels, channel_source):
            known = [(reach.by_source[s], s) for s in sids if reach.by_source.get(s)]
            if not known:
                empty.update(sids)
                continue
            value, sid = max(known)
            if value - now < GUIDE_REACH_MIN:
                short_reach[sid] = value
    for sid in sorted(empty):
        short.append(f'EPG source "{names.get(sid, f"#{sid}")}" holds no listings.')
    for sid, value in sorted(short_reach.items()):
        short.append(f'EPG source "{names.get(sid, f"#{sid}")}" only reaches '
                     f'{format_local(value)}.')
    if not short:
        dismiss_open_alerts(AUTO_RULES_GUIDE_SHORT, source)
        return None
    _raise_standing(AUTO_RULES_GUIDE_SHORT, source,
                    'The guide is too short for auto-record rules',
                    ' '.join(short) + ' Rules can only schedule what the guide lists, so they '
                    'will miss anything past that until the guide is refreshed. This clears '
                    'itself once every source a rule uses reaches a day ahead.')
    return None


def _raise_standing(alert_type, source, title, body):
    """Raise a standing alert once, keeping its body current while it stands. Unlike the
    sync alerts' helper (accounts._raise_or_resolve_standing_alert) this never re-dates or
    re-marks it unread: it is re-evaluated every hour, and a condition that has not changed
    must not resurface as news each time."""
    from .alerts import create_alert
    from .database import Alert

    @retry_on_locked()
    def _refresh():
        rows = Alert.query.filter_by(alert_type=alert_type, source=source,
                                     dismissed_at=None).all()
        for row in rows:
            if row.body != body:
                row.body = body
        db.session.commit()
        return bool(rows)

    if not _refresh():
        create_alert(alert_type, title=title, body=body, source=source)


def _clear_rule_alerts(rule_id):
    from .alerts import AUTO_RULE_BROKEN, AUTO_RULE_CAPPED, dismiss_open_alerts
    dismiss_open_alerts(AUTO_RULE_CAPPED, f'auto-rule:{rule_id}:cap')
    dismiss_open_alerts(AUTO_RULE_BROKEN, f'auto-rule:{rule_id}:broken')


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

@dataclass
class _Run:
    """What one run reads once and shares across its rules."""
    cfg: dict
    ctx: object
    known: _Known
    library: list
    lib_by_id: dict
    user_modified_recs: set
    default_cap: int
    tz: object
    h24: bool
    reach: '_Reach'


def run_evaluation(trigger: str, rule_ids=None):
    """Evaluate the enabled rules (or just `rule_ids`), under an admission ticket (§4.2).

    Returns {rule id: RuleOutcome}, or the admission Refusal when something it yields to is
    running - the caller decides whether to queue (a queued or periodic run always does; a
    save says what it waits behind and queues). Must run inside an app context."""
    label = trigger if rule_ids is None else f'{trigger}: rule {", ".join(map(str, rule_ids))}'
    ticket = admission.try_start(admission.KIND_RULES, label)
    if not ticket.granted:
        return ticket
    try:
        return run_admitted(trigger, rule_ids)
    finally:
        admission.release(ticket)


def waiting_phrase(refusal) -> str:
    """'waiting for an account sync to finish' - what a held-back check is queued behind,
    named by the blocker's kind. Not the refusal's own sentence, which is a whole clause with
    an age in it that would be frozen into wherever this is shown."""
    return f'waiting for {admission.KIND_LABELS.get(refusal.blocked_by, "other work")} to finish'


def run_admitted(trigger, rule_ids=None):
    """run_evaluation() for a caller that already holds the KIND_RULES ticket - Run now on
    /jobs takes it in the request, so a refusal reaches the person who clicked."""
    return _run(trigger, rule_ids)


def _run(trigger, rule_ids):
    from .channel_search import SearchContext
    from .config import load_config
    from .tz_utils import get_display_tz, is_24h

    cfg = load_config()
    settings = cfg.get('auto_scheduling') or {}
    now = datetime.utcnow()
    if trigger == TRIGGER_SWEEP:
        keep = _setting_int(settings, 'history_keep_days', 0)
        pruned = prune_history(keep, now)
        if pruned:
            log.info('Auto-record: pruned %d history rows older than %d days', pruned, keep)
    reach = _guide_reach()
    query = db.session.query(AutoRule.id).filter(AutoRule.enabled.is_(True))
    if rule_ids is not None:
        query = query.filter(AutoRule.id.in_(list(rule_ids)))
    ids = [rid for (rid,) in query.order_by(AutoRule.id)]
    outcomes = {}
    if ids:
        ctx = SearchContext.build(cfg)
        states = []
        for (params,) in db.session.query(AutoRule.search_params).filter(
                AutoRule.id.in_(ids)):
            try:
                states.append(parse_search(params))
            except ValueError:
                continue
        ledger_rec_ids = {rid for (rid,) in db.session.query(RuleMatch.recording_id).filter(
            RuleMatch.rule_id.in_(ids), RuleMatch.recording_id.isnot(None))}
        library = _library(ledger_rec_ids)
        run = _Run(
            cfg=cfg, ctx=ctx, known=known_names(states, ctx), library=library,
            lib_by_id={rec.id: rec for rec in library},
            user_modified_recs={rid for (rid,) in db.session.query(RuleMatch.recording_id).filter(
                RuleMatch.action_taken == RULE_MATCH_USER_MODIFIED,
                RuleMatch.recording_id.isnot(None))},
            default_cap=default_cap(cfg),
            tz=get_display_tz(), h24=is_24h(), reach=reach)
        # Two passes, so every rule's retrack (§5) lands before any rule judges a showing as
        # new: a rule judged first would otherwise find a moved program unclaimed - the rule
        # that owns its recording has not followed it yet - and schedule it a second time.
        prepared = {}
        for rid in ids:
            revision = db.session.query(AutoRule.revision).filter(
                AutoRule.id == rid).scalar()
            prepared[rid] = _guarded(rid, revision, _prepare_rule, rid, run)
        for rid in ids:
            p = prepared[rid]
            outcomes[rid] = (_guarded(rid, p.revision, _judge_rule, p, run)
                             if isinstance(p, _Prepared) else p)
    refresh_guide_alert(now, reach)
    log.info('Auto-record rules evaluated (%s): %s', trigger,
             ', '.join(f'#{o.rule_id} scheduled {o.scheduled}'
                       + (f', moved {o.retracked}' if o.retracked else '')
                       for o in outcomes.values())
             or 'no enabled rule')
    return outcomes


def default_cap(cfg) -> int:
    """The cap a rule with no cap of its own runs under (§3.6) - read here and nowhere else,
    so a page that shows it can never show a different number from the one the engine uses."""
    return _setting_int(cfg.get('auto_scheduling') or {}, 'default_max_upcoming', 10)


def _setting_int(settings, key, fallback) -> int:
    """A whole-number auto_scheduling setting, or `fallback` (said in the log) when a
    hand-edited config.yaml holds something else."""
    raw = settings.get(key, fallback)
    try:
        return max(0, int(raw if raw is not None else fallback))
    except (TypeError, ValueError):
        log.warning('auto_scheduling.%s is %r, not a whole number - using %d', key, raw,
                    fallback)
        return fallback


def _refresh_context(run):
    """Read the run-wide search context and names again - for when a rename or a delete
    committed since the run read them. Rare by construction, so its cost is never per rule."""
    from .channel_search import SearchContext
    run.ctx = SearchContext.build(run.cfg)
    states = []
    for (params,) in db.session.query(AutoRule.search_params).filter(
            AutoRule.enabled.is_(True)):
        try:
            states.append(parse_search(params))
        except ValueError:
            continue
    run.known = known_names(states, run.ctx)


def _guarded(rule_id, revision, step, *args):
    """One pass over one rule, under its own catch: an exception rolls back, is logged naming
    the rule (which the log-error alert surfaces), lands in the rule's last_error, and the
    run moves on to the next rule (§4.3). `revision` is the one the rule had when its check
    began, so a failure is never stamped on a rule that changed since."""
    try:
        return step(*args)
    except Exception as exc:
        db.session.rollback()
        log.exception('Auto-record rule #%d could not be evaluated', rule_id)
        message = f'The last check stopped on an error: {describe_error(exc)}'

        # Claimed like every other write: a rule paused or edited while this check ran
        # must not wear the old revision's failure until it is next checked.
        @retry_on_locked()
        def _note_error():
            if not _claim_rule(rule_id, revision):
                db.session.rollback()
                return
            rule = db.session.get(AutoRule, rule_id)
            rule.last_evaluated_at = datetime.utcnow()
            rule.last_error = message
            rule.last_problems = None
            db.session.commit()

        try:
            _note_error()
        except Exception:
            db.session.rollback()
            log.exception('Auto-record rule #%d: could not record its error', rule_id)
        return RuleOutcome(rule_id=rule_id, stopped=message)


def _claim_rule(rule_id, revision) -> bool:
    """The re-check every write unit makes inside its own closure (§4.3), and the reason it
    is a write: a conditional no-op UPDATE of the rule's own row.

    A plain SELECT would not do. pysqlite runs SELECTs in autocommit, outside the write's
    transaction, so an edit, pause or delete committing between the check and the commit
    would go unseen and the stale write would land anyway. The UPDATE opens the write
    transaction and takes SQLite's write lock in the same statement, so nothing can commit
    under it until this unit does - and it never answers from the identity map the way
    session.get() does. False (the rule is gone, paused, or at another revision) leaves the
    caller to roll back and write nothing."""
    result = db.session.execute(
        update(AutoRule)
        .where(AutoRule.id == rule_id, AutoRule.revision == revision,
               AutoRule.enabled.is_(True))
        .values(revision=AutoRule.revision)
        .execution_options(synchronize_session=False))
    return result.rowcount == 1


def _abandon(rule_id, why) -> RuleOutcome:
    log.info('Auto-record rule #%d: abandoned this run - %s; the evaluation the change '
             'queued judges it afresh', rule_id, why)
    return RuleOutcome(rule_id=rule_id, abandoned=True)


def _mark_broken(rule_id, revision, message, now) -> RuleOutcome:
    from .alerts import AUTO_RULE_BROKEN, AUTO_RULE_CAPPED, dismiss_open_alerts

    @retry_on_locked()
    def _write():
        if not _claim_rule(rule_id, revision):
            db.session.rollback()
            return None
        rule = db.session.get(AutoRule, rule_id)
        rule.last_evaluated_at = now
        rule.last_error = message
        rule.last_problems = None
        name = rule.name
        db.session.commit()
        return name

    name = _write()
    if name is None:
        return _abandon(rule_id, 'it changed while it was being checked')
    log.warning('Auto-record rule #%d ("%s") cannot run: %s', rule_id, name, message)
    _raise_standing(AUTO_RULE_BROKEN, f'auto-rule:{rule_id}:broken',
                    f'Auto-record rule "{name}" cannot run',
                    f'{message} Nothing is scheduled by this rule until that is fixed. This '
                    'clears itself once the rule runs again, or when it is paused, edited or '
                    'deleted.')
    # A rule that cannot run is not judging showings, so it is not holding any at its cap.
    dismiss_open_alerts(AUTO_RULE_CAPPED, f'auto-rule:{rule_id}:cap')
    _unless_rule_moved(rule_id, revision)
    return RuleOutcome(rule_id=rule_id, stopped=message)


def _search_rule(state, run):
    """(entries, group wins) for the rule's search, start order, at most MAX_SHOWINGS - or
    None when it matches more. Readiness is re-read before and after: a search that ran
    while an index it used stopped being ready is run again unindexed and never acted on
    (§4.2, R13)."""
    from .channel_search import MAX_PAGE_SIZE, search
    from .search_index import readiness_map

    pages = MAX_SHOWINGS // MAX_PAGE_SIZE
    base = replace(state, sort='when', sort_desc=False, page_size=MAX_PAGE_SIZE, page=1,
                   facets=())
    while True:
        before = readiness_map()
        run.ctx.readiness = before
        entries, wins = [], {}
        for page in range(1, pages + 1):
            result = search(replace(base, page=page), run.ctx, want_counts=False,
                            want_facets=False)
            entries += [_EntrySnap.of(e) for e in result.rows]
            wins.update(result.airing_group_ids)
            if len(result.rows) < MAX_PAGE_SIZE:
                break
        more = False
        if len(entries) >= MAX_SHOWINGS:
            probe = search(replace(base, page=MAX_SHOWINGS + 1, page_size=1), run.ctx,
                           want_counts=False, want_facets=False)
            more = bool(probe.rows)
        after = readiness_map()
        degraded = [k for k, (ready, _reason) in before.items()
                    if ready and not after.get(k, (False, ''))[0]]
        if degraded:
            log.info('Auto-record: a search index stopped being ready mid-search (%s); '
                     'searching again without it', ', '.join('+'.join(k) for k in degraded))
            continue
        return (None if more else entries), wins


@dataclass
class _EntrySnap:
    id: int
    channel_id: int
    title: str
    sub_title: str
    start: datetime
    stop: datetime

    @classmethod
    def of(cls, e):
        return cls(id=e.id, channel_id=e.channel_id, title=e.title or '',
                   sub_title=e.sub_title or '', start=e.start_time, stop=e.stop_time)


@dataclass
class _Prepared:
    """One rule after its first pass - searched, its episodes filled in and retracked - and
    waiting for the second pass to judge what it found (§4.3 steps 4-5)."""
    rule_id: int
    revision: int
    settings: '_RuleSettings'
    now: datetime
    rows: list
    showings: list
    slots: '_Slots'
    outcome: RuleOutcome
    problems: list
    moved: set


def _prepare_rule(rule_id, run):
    """§4.3 steps 1-3 for one rule: a _Prepared, or the RuleOutcome of a rule that stops here
    (paused, broken, too broad, or changed under a write)."""
    from .channel_groups import effective_score, member_eager_options
    from .channel_search import SearchStateError
    from .channel_search_rows import label_group_id

    now = datetime.utcnow()
    # 1. Prepare (§4.3). populate_existing: get() would otherwise answer from the identity
    # map, and a rule edited since this session last read it would be judged as it was.
    rule = db.session.get(AutoRule, rule_id, populate_existing=True)
    if rule is None or not rule.enabled:
        return _abandon(rule_id, 'it was paused or deleted')
    revision = rule.revision
    settings = _RuleSettings.of(rule, run)
    try:
        state = parse_search(rule.search_params)
    except SearchStateError as exc:
        return _mark_broken(rule_id, revision, f'Its search can no longer be read: {exc}.', now)
    gone = missing_names(state, run.known)
    if gone:
        gone = missing_names(state, known_names([state]))
        if not gone:
            # A tag or group renamed since the run read them: the rule follows the rename,
            # so the run's own context is what is behind. Read it again.
            _refresh_context(run)
    if gone:
        return _mark_broken(rule_id, revision, gone, now)

    # 2. Search.
    try:
        try:
            entries, wins = _search_rule(state, run)
        except ObjectDeletedError:
            # A tag the run read was deleted under it (its own commits expire what it read,
            # and the next touch finds the row gone). Read again and judge the rule afresh -
            # a search naming the deleted tag is then broken in the ordinary way (§3.2).
            _refresh_context(run)
            gone = missing_names(state, run.known)
            if gone:
                return _mark_broken(rule_id, revision, gone, now)
            entries, wins = _search_rule(state, run)
    except SearchStateError as exc:
        return _mark_broken(rule_id, revision, f'Its search can no longer be run: {exc}.', now)
    if entries is None:
        return _mark_broken(
            rule_id, revision,
            f'Its search matches more than {MAX_SHOWINGS:,} upcoming showings, which is too '
            'broad to be what anyone meant. Use Edit search on the rule\'s page to narrow it '
            '(a channel, a group or a tag), then save it to the rule.', now)
    entries = [e for e in entries if e.start is not None and e.start > now]

    won_ids = sorted({gid for e in entries for gid in wins.get(e.id, ())})
    groups = ({g.id: g for g in ChannelGroup.query.options(*member_eager_options())
               .filter(ChannelGroup.id.in_(won_ids))} if won_ids else {})
    group_names = {gid: g.name for gid, g in groups.items()}
    channel_rows = {c.id: c for c in db.session.query(
        Channel.id, Channel.name, Channel.health_score, Channel.manual_health_adjustment
    ).filter(Channel.id.in_(sorted({e.channel_id for e in entries})))} if entries else {}

    showings = []
    for e in entries:
        ch = channel_rows.get(e.channel_id)
        if ch is None:
            continue
        gid = label_group_id(wins[e.id], groups) if wins.get(e.id) else None
        showings.append(_Showing(
            entry_id=e.id, channel_id=e.channel_id, channel_name=ch.name, group_id=gid,
            group_name=group_names.get(gid), title=e.title, sub_title=e.sub_title,
            title_key=title_key(e.title), episode_raw=title_key(e.sub_title),
            start=e.start, stop=e.stop, score=effective_score(ch)))

    rows = [_row_snapshot(m) for m in
            RuleMatch.query.filter_by(rule_id=rule_id).populate_existing()]

    slots = _Slots()
    for s in showings:
        slots.add(s.title_key, s.start, s.episode_raw)
    for r in rows:
        slots.add(r.title_key, r.start, r.episode_key)
    for rec in run.library:
        slots.add(rec.title_key, rec.program_start, rec.episode_raw)

    fills = _episode_fills(rows, slots)
    if fills and not _fill_episodes(rule_id, revision, fills):
        return _abandon(rule_id, 'it changed while it was being checked')

    ledger = {(r.title_key, r.episode_key, r.start): r for r in rows}
    programs = _programs(showings, slots)
    outcome = RuleOutcome(rule_id=rule_id)

    # 3. Retrack (§5), before anything is judged as new, so a moved program's new start
    # already belongs to its row. The judge of the second pass is built from what it left.
    moved = set()
    problems = _retrack(rule_id, revision, rows, programs, ledger, slots, run, now, outcome,
                        moved)
    if problems is _CHANGED:
        return _abandon(rule_id, 'it changed while it was being checked')
    return _Prepared(rule_id=rule_id, revision=revision, settings=settings, now=now, rows=rows,
                     showings=showings, slots=slots, outcome=outcome, problems=problems,
                     moved=moved)


def _episode_fills(rows, slots) -> list:
    """The episode fill-in (§3.3): [(row id, episode)] for each empty-episode row whose slot
    now holds exactly one episode, unless another row of the rule already holds it there.
    The rows are updated in memory; the caller writes the list."""
    held = {(r.title_key, r.episode_key, r.start) for r in rows}
    fills = []
    for r in rows:
        if r.episode_key:
            continue
        only = slots.only_episode(r.title_key, r.start)
        if only and (r.title_key, only, r.start) not in held:
            fills.append((r.id, only))
            held.add((r.title_key, only, r.start))
            r.episode_key = only
    return fills


def _judge_rule(p, run) -> RuleOutcome:
    """§4.3 steps 4-5 for one prepared rule, after every rule in the run has retracked."""
    rule_id, revision, settings, now = p.rule_id, p.revision, p.settings, p.now
    outcome, placement_errors = p.outcome, p.problems

    # Since this rule's first pass, the run's other rules have made and moved recordings,
    # and an episode they carry can change which showings are the same program (§3.3). Read
    # them into the slots, fill in and group again, so this rule is judged exactly as it
    # would be had it run after them in one pass.
    for rec in run.library:
        p.slots.add(rec.title_key, rec.program_start, rec.episode_raw)
    fills = _episode_fills(p.rows, p.slots)
    if fills and not _fill_episodes(rule_id, revision, fills):
        return _abandon(rule_id, 'it changed while it was being checked')
    ledger = {(r.title_key, r.episode_key, r.start): r for r in p.rows}
    programs = _programs(p.showings, p.slots)
    judge = _Judge(rule_id, settings, p.rows, run, p.slots, now)
    verdicts = []

    # 4. Judge, plan and create, one program at a time in start order.
    for program in programs:
        row = ledger.get(program.identity)
        if row is not None:
            if row.action == RULE_MATCH_SCHEDULED:
                if not _aborted_by_group_change(row, run.lib_by_id):
                    # Counted only while its recording will still happen: a pick the user
                    # cancelled stays decided (R6) but is not "already scheduled". One this
                    # check just moved is counted as moved instead, never as both.
                    if (row.recording_id is not None and row.id not in p.moved
                            and _fate(row, run.lib_by_id)[0] in _spoken_for()):
                        outcome.already += 1
                    continue
            elif row.action == RULE_MATCH_USER_MODIFIED:
                continue
            elif row.action in RULE_MATCH_SKIPS:
                pass
            else:
                log.error('Auto-record rule #%d: ledger row %s has unknown action %r; left '
                          'alone', rule_id, row.id, row.action)
                continue
        action, detail = judge.verdict(program)
        if action is not None:
            outcome.skipped[action] = outcome.skipped.get(action, 0) + 1
            if action == RULE_MATCH_SKIPPED_CAP:
                outcome.capped += 1
            if row is None or row.action != action:
                verdicts.append(_Verdict(program.identity, row.id if row else None,
                                         action, detail, program.lead))
            continue
        result = _schedule(rule_id, revision, program, row, settings, run, judge)
        if result is _CHANGED:
            return _abandon(rule_id, 'it changed while it was being checked')
        made, problem = result
        if problem:
            placement_errors.append(problem)
        if made is not None:
            outcome.scheduled += 1
            ledger[program.identity] = made

    if verdicts and not _write_verdicts(rule_id, revision, verdicts):
        return _abandon(rule_id, 'it changed while it was being checked')

    # 5. Record.
    reach = _guide_reach_for_rule(rule_id, now, run.reach)
    outcome.guide_reach = reach
    outcome.problems = ' '.join(placement_errors)
    if not _finish(rule_id, revision, now, reach, outcome.problems or None):
        return _abandon(rule_id, 'it changed while it was being checked')
    _rule_alerts_after_run(rule_id, revision, outcome, settings)
    return outcome


@dataclass
class _RuleSettings:
    name: str
    skip_repeats: bool
    limit_count: int | None
    limit_window: timedelta | None
    cap: int
    profile_id: int | None

    @classmethod
    def of(cls, rule, run):
        limit = None
        if rule.limit_count and rule.limit_window_hours:
            limit = timedelta(hours=rule.limit_window_hours)
        cap = rule.max_upcoming if rule.max_upcoming is not None else run.default_cap
        return cls(name=rule.name, skip_repeats=bool(rule.skip_repeats),
                   limit_count=rule.limit_count if limit else None, limit_window=limit,
                   cap=max(0, int(cap or 0)), profile_id=rule.profile_id)

    def profile_choice(self, rule_id):
        """What the planner is handed: 'default' (each showing's own) or the profile, read
        at the moment of planning. Never a run-wide object: every commit expires it, and a
        profile deleted since would raise on the next touch. A profile that is gone means
        what a deleted profile means everywhere (§8) - each showing's default; its unlink is
        what is committing."""
        if self.profile_id is None:
            return 'default'
        profile = db.session.get(RecordingProfile, self.profile_id)
        if profile is None:
            log.info('Auto-record rule #%d: its recording profile #%d is gone; each showing\'s '
                     'default profile is used', rule_id, self.profile_id)
            return 'default'
        return profile


def _local(dt, run) -> str:
    """A moment as every sentence this engine writes names it, in the display timezone read
    once for the run."""
    from .tz_utils import format_local
    return format_local(dt, 'day_datetime', tz=run.tz, h24=run.h24)


def _programs(showings, slots) -> list:
    """Showings grouped into programs, in start order. Within one program the order is §3.7's
    simulcast order - a group row before a single channel, then the healthier channel - so the
    first is the one a recording is made from and the rest end at its ledger row."""
    by_identity = {}
    for s in showings:
        by_identity.setdefault(slots.identity(s.title_key, s.start, s.episode_raw), []).append(s)
    programs = []
    for identity, group in by_identity.items():
        group.sort(key=lambda s: (0 if s.group_id is not None else 1, -s.score, s.entry_id))
        programs.append(_Program(identity=identity, showings=group))
    programs.sort(key=lambda p: (p.lead.start, p.identity[0], p.identity[1], p.lead.entry_id))
    return programs


class _LibIndex:
    """The library's spoken-for recordings, indexed the ways the existing and repeat checks
    read them. One definition for step 4's judge and for retrack's "is this target already
    recorded" (§5), so the two can never disagree about what counts as recorded."""

    def __init__(self, slots, recs=()):
        self.slots = slots
        self.spoken = set(_spoken_for())
        self.by_identity = {}
        self.by_title_episode = {}
        self.by_channel = {}
        self.by_group = {}
        for rec in recs:
            self.add(rec)

    def add(self, rec) -> bool:
        """Index `rec` if it is spoken for; True when it was."""
        if rec.status not in self.spoken:
            return False
        if rec.title_key and rec.program_start is not None:
            ident = self.slots.identity(rec.title_key, rec.program_start, rec.episode_raw)
            self.by_identity.setdefault(ident, []).append(rec)
            if ident[1]:
                self.by_title_episode.setdefault(ident[:2], []).append((ident[2], rec))
        if rec.channel_id is not None:
            self.by_channel.setdefault(rec.channel_id, []).append(rec)
        if rec.group_id is not None:
            self.by_group.setdefault(rec.group_id, []).append(rec)
        return True

    def same_program(self, identity):
        return self.by_identity.get(identity, ())

    def covering_on(self, channel_id, group_id, start, stop):
        """Recordings on this channel, or on this group, whose window covers start-stop."""
        near = list(self.by_channel.get(channel_id, ()))
        if group_id is not None:
            near += self.by_group.get(group_id, ())
        for rec in near:
            if rec.start <= start and rec.stop >= stop:
                yield rec

    def covering(self, program):
        """(recording, showing) for every showing of `program` some recording on that
        showing's channel or group already covers whole."""
        for s in program.showings:
            for rec in self.covering_on(s.channel_id, s.group_id, s.start, s.stop):
                yield rec, s


class _Judge:
    """The four checks of §4.3 step 4, over in-memory state extended by every recording this
    run commits - so each count includes what the run has already made, and no check queries."""

    def __init__(self, rule_id, settings, rows, run, slots, now):
        self.rule_id = rule_id
        self.settings = settings
        self.run = run
        self.slots = slots
        self.now = now
        self.index = _LibIndex(slots)
        self.scheduled_count = 0
        for rec in run.library:
            self._index(rec)
        # This rule's picks, for the repeat check's ledger half and for the limit.
        self.picks = [r for r in rows if r.action == RULE_MATCH_SCHEDULED]

    def _index(self, rec):
        if (self.index.add(rec) and rec.rule_id == self.rule_id
                and rec.status == REC_STATUS_SCHEDULED):
            self.scheduled_count += 1

    def add_created(self, rec: _Lib, row: _Row):
        self.run.library.append(rec)
        self.run.lib_by_id[rec.id] = rec
        self._index(rec)
        self.picks.append(row)

    def _when(self, dt):
        return _local(dt, self.run)

    def verdict(self, program):
        """(action, detail) for a skip, or (None, None) when the program should be
        scheduled. Order matters: a repeat is judged before the limit, so the limit's one
        slot goes to the race rather than the replay (§3.5)."""
        for check in (self._existing, self._repeat, self._limit, self._cap):
            found = check(program)
            if found is not None:
                return found
        return None, None

    def _existing(self, program):
        for rec in self.index.same_program(program.identity):
            return (RULE_MATCH_SKIPPED_EXISTING,
                    f'Already {_state_phrase(rec.status)} as "{rec.name}" '
                    f'(recording #{rec.id}).')
        for rec, s in self.index.covering(program):
            return (RULE_MATCH_SKIPPED_EXISTING,
                    f'Recording "{rec.name}" (#{rec.id}) on the same '
                    f'{"group" if rec.group_id == s.group_id and s.group_id else "channel"}'
                    ' already covers its whole air time.')
        return None

    def _repeat(self, program):
        tkey, episode, start = program.identity
        if not self.settings.skip_repeats or not episode:
            return None
        for aired, rec in self.index.by_title_episode.get((tkey, episode), ()):
            if aired >= start or rec.id in self.run.user_modified_recs:
                continue
            return (RULE_MATCH_SKIPPED_REPEAT,
                    f'A repeat: the airing on {self._when(aired)} is already '
                    f'{_state_phrase(rec.status)} as "{rec.name}".')
        for row in self.picks:
            if (row.title_key == tkey and row.episode_key == episode and row.start < start
                    and _pick_is_spoken_for(row, self.run.lib_by_id)):
                return (RULE_MATCH_SKIPPED_REPEAT,
                        f'A repeat: this rule already picked the airing on '
                        f'{self._when(row.start)}.')
        return None

    def _limit(self, program):
        count, window = self.settings.limit_count, self.settings.limit_window
        if not count or window is None:
            return None
        t = program.identity[2]
        picks = [r.start for r in self.picks if _pick_counts_for_limit(r, self.run.lib_by_id)]
        for s in [p for p in picks if t - window < p <= t] + [t]:
            inside = sum(1 for p in picks if s <= p < s + window)
            if inside + 1 > count:
                hours = int(window.total_seconds() // 3600)
                return (RULE_MATCH_SKIPPED_LIMIT,
                        f'Over the rule\'s limit of {count} within {hours} hours: it already '
                        f'has {inside} pick{"s" if inside != 1 else ""} in that window.')
        return None

    def _cap(self, program):
        cap = self.settings.cap
        if cap and self.scheduled_count >= cap:
            return (RULE_MATCH_SKIPPED_CAP,
                    f'The rule already has {self.scheduled_count} scheduled recordings, its '
                    f'cap. This is scheduled once one of them is over.')
        return None


_CHANGED = object()
#: How many of a simulcast's other channels a scheduled row's detail names before it counts.
_CHOSEN_OVER_NAMED = 5


def _schedule(rule_id, revision, program, row, settings, run, judge):
    """Plan and create one program's recording (§4.3 steps 6-7, §4.4). Returns (the new
    ledger row's snapshot or None, a sentence for last_problems or ''), or _CHANGED when the
    rule moved underneath it."""
    from .routes import recordings as rec_routes
    lead = program.lead
    when = judge._when(lead.start)
    plans = rec_routes._plan_bulk_schedule([(lead.entry_id, lead.group_id)],
                                           settings.profile_choice(rule_id), run.cfg,
                                           skip_overlapped=False)
    plan = plans[0]
    args = plan.get('_create')
    if plan['verdict'] == rec_routes.BULK_SKIP or args is None:
        message = f'"{lead.title}" on {when} could not be placed: {plan.get("reason")}'
        log.warning('Auto-record rule #%d: %s - judged again next run', rule_id, message)
        return None, message
    # The planner reads the listing by id, and an EPG import that ran since the search (a sync
    # never yields to a rule check) deletes and re-inserts a source's listings - SQLite can
    # hand a deleted id to a different program. Recording whatever holds the id now while the
    # ledger names the judged program would mark the race done and record something else.
    if not _listing_is(args.get('entry'), lead):
        message = (f'"{lead.title}" on {when} could not be placed: the guide changed while '
                   'it was being checked.')
        log.warning('Auto-record rule #%d: %s - judged again next run', rule_id, message)
        return None, message

    warnings = [w['message'] for w in plan.get('warnings', {}).values()
                if isinstance(w, dict) and w.get('message')]
    chosen_over = list(dict.fromkeys(s.group_name or s.channel_name
                                     for s in program.showings[1:]))
    where = (f'{lead.group_name} (via {plan.get("member_name") or lead.channel_name})'
             if args.get('group_id') else plan.get('channel_name') or lead.channel_name)
    detail = f'Scheduled on {where}.'
    if chosen_over:
        # A simulcast on a big provider can be twenty feeds; name a few and count the rest.
        shown = ', '.join(chosen_over[:_CHOSEN_OVER_NAMED])
        rest = len(chosen_over) - _CHOSEN_OVER_NAMED
        detail += f' Chosen over {shown}{f" and {rest} more" if rest > 0 else ""}.'
    if warnings:
        detail += ' Scheduled with a warning: ' + ' '.join(warnings)
    holder = {}

    def link(rec):
        # The create path rolls back when this answers False.
        if not _claim_rule(rule_id, revision):
            return False
        rule = db.session.get(AutoRule, rule_id)
        rec.rule_id = rule_id
        add_recording_event(
            rec.id, RECORDING_RULE_SCHEDULED,
            detail=(f'Scheduled by auto-record rule "{rule.name}" for '
                    f'"{lead.title}{" - " + lead.sub_title if lead.sub_title else ""}" '
                    f'({when}). {detail}'),
            extra={'rule_id': rule_id, 'rule_name': rule.name,
                   'program_start': lead.start.strftime('%Y-%m-%dT%H:%M:%S'),
                   'chosen_over_channel_ids': [s.channel_id for s in program.showings[1:]],
                   'warnings': sorted(plan.get('warnings', {}))})
        m = db.session.get(RuleMatch, row.id) if row is not None and row.id else None
        if m is None:
            m = RuleMatch(rule_id=rule_id, title_key=program.identity[0],
                          episode_key=program.identity[1],
                          program_start_time=program.identity[2], created_at=datetime.utcnow())
            db.session.add(m)
        m.channel_id = rec.channel_id
        m.group_id = rec.group_id
        m.program_title = lead.title
        m.program_sub_title = lead.sub_title or None
        m.program_stop_time = lead.stop
        m.action_taken = RULE_MATCH_SCHEDULED
        m.recording_id = rec.id
        m.removed_status = None
        m.removed_cancel_reason = None
        m.rec_start_time = rec.start_time
        m.rec_stop_time = rec.stop_time
        m.rec_name = rec.name
        m.detail = detail
        m.updated_at = datetime.utcnow()
        db.session.flush()
        holder.update(recording_id=rec.id, row_id=m.id, start=rec.start_time,
                      stop=rec.stop_time, name=rec.name, channel_id=rec.channel_id,
                      group_id=rec.group_id)
        return True

    try:
        rec = rec_routes._create_scheduled_recording(run.cfg, **args, link=link)
    except Exception as exc:
        db.session.rollback()
        committed = holder.get('recording_id') is not None and db.session.query(
            Recording.id).filter(Recording.id == holder['recording_id'],
                                 Recording.rule_id == rule_id).scalar() is not None
        if not committed:
            log.exception('Auto-record rule #%d: could not create "%s" (%s)', rule_id,
                          lead.title, when)
            return None, f'"{lead.title}" on {when} could not be scheduled: {describe_error(exc)}'
        log.exception('Auto-record rule #%d: recording #%d was created but a step after its '
                      'commit failed; re-arming its start', rule_id, holder['recording_id'])
        return _snapshot_created(holder, program, lead, judge), _rearm(rule_id, holder)
    if rec is None:
        return _CHANGED
    return _snapshot_created(holder, program, lead, judge), ''


def _listing_is(entry, showing) -> bool:
    """Does the listing row read now still hold the program this showing judged? An import
    that ran since the search (a sync never yields to a rule check) deletes and re-inserts a
    source's listings, and SQLite can hand a deleted id to a different program."""
    return (entry is not None and entry.channel_id == showing.channel_id
            and entry.start_time == showing.start
            and title_key(entry.title) == showing.title_key
            and title_key(entry.sub_title) == showing.episode_raw)


def _rearm(rule_id, holder) -> str:
    """After a create committed and a later step raised: arm its start once more (it replaces
    any job already there). '' when that worked; otherwise the sentence the recording's event,
    the row's detail and last_problems all carry (§4.3)."""
    from flask import current_app
    from .routes import recordings as rec_routes
    rid = holder['recording_id']
    try:
        rec_routes.schedule_recording(current_app._get_current_object(), rid,
                                      holder['start'], holder['stop'])
        return ''
    except Exception:
        log.exception('Auto-record rule #%d: recording #%d start could not be armed', rule_id, rid)
    sentence = (f'Recording #{rid} ("{holder["name"]}") was scheduled but its start could not '
                'be armed, so it will not start on its own. Restarting ChannelBin arms it.')

    @retry_on_locked()
    def _note():
        add_recording_event(rid, RECORDING_START_NOT_ARMED, detail=sentence)
        m = db.session.get(RuleMatch, holder['row_id'])
        if m is not None:
            m.detail = f'{m.detail or ""} {sentence}'.strip()
        db.session.commit()

    _note()
    return sentence


def _snapshot_created(holder, program, lead, judge):
    rec = _Lib(id=holder['recording_id'], name=holder['name'], status=REC_STATUS_SCHEDULED,
               cancel_reason=None, title_key=program.identity[0],
               episode_raw=program.identity[1], program_start=lead.start,
               program_stop=lead.stop, channel_id=holder['channel_id'],
               group_id=holder['group_id'], start=holder['start'], stop=holder['stop'],
               rule_id=judge.rule_id)
    row = _Row(id=holder['row_id'], title_key=program.identity[0],
               episode_key=program.identity[1], start=program.identity[2], stop=lead.stop,
               action=RULE_MATCH_SCHEDULED, recording_id=rec.id, removed_status=None,
               removed_cancel_reason=None, channel_id=rec.channel_id, group_id=rec.group_id,
               program_title=lead.title)
    judge.add_created(rec, row)
    return row


# ---------------------------------------------------------------------------
# Retrack (§5) - a program the guide moved
# ---------------------------------------------------------------------------

_FOLLOW_MOVED = 'moved'
_FOLLOW_LET_GO = 'let go'
_FOLLOW_ANCHOR = 'anchor'


def _retrack(rule_id, revision, rows, programs, ledger, slots, run, now, outcome, moved):
    """Follow each of this rule's picks whose program the guide moved (§5). Returns the
    sentences for last_problems - a moved recording whose start could not be armed - or
    _CHANGED when the rule moved underneath a write. The ids of the rows whose recording it
    moved are added to `moved`.

    A pick is followed only when all of this holds, and otherwise nothing is touched:
    - its program has not started, and it has a non-empty episode - under a bare series
      title a moved race cannot be told from next week's race or a replay;
    - its program is listed nowhere at its own time - not among this rule's matches, and not
      anywhere else in the guide either: a rule narrowed by an edit no longer matches a race
      that is still listed, and that is not a move. When in doubt, nothing moves;
    - its recording did not FAIL. A failed pick is left where it is, so the program's new
      time is judged afresh and normally recorded, as a failed race's replay is (R8);
    - an unambiguous target exists: the same title and episode on the same channel or group,
      at a different time, with no row of this rule of its own. The nearest wins;
    - and, for a recording that would actually move, no other recording already records the
      target (_holder) - moving there would make the second recording of one program that
      retrack exists to prevent. A held target is said on the recording and the row and not
      taken; a farther target is never tried instead, since it would only be a replay.

    In-memory state is updated to match each write - the ledger dict, the row snapshot, the
    run's library and its user_modified set - so the judge built after this sees exactly
    what was committed, and the moved program's new start finds its row."""
    listed = {p.identity for p in programs}
    by_program = {}
    for p in programs:
        by_program.setdefault(p.identity[:2], []).append(p)

    gone = []
    for row in sorted(rows, key=lambda r: (r.start, r.id)):
        if (row.action != RULE_MATCH_SCHEDULED or row.start <= now or not row.episode_key
                or (row.title_key, row.episode_key, row.start) in listed):
            continue
        if _fate(row, run.lib_by_id)[0] == REC_STATUS_FAILED:
            continue
        if any(p.identity[2] != row.start and p.identity not in ledger
               for p in by_program.get((row.title_key, row.episode_key), ())):
            gone.append(row)
    if gone:
        still = _still_listed(gone)
        gone = [r for r in gone if r.id not in still]
    if not gone:
        return []

    members = _group_members({r.group_id for r in gone if r.group_id is not None})
    problems, taken, index = [], set(), None
    for row in gone:
        found = _retrack_target(row, by_program.get((row.title_key, row.episode_key), ()),
                                ledger, taken, members)
        if found is None:
            continue
        program, showing = found
        taken.add(program.identity)
        if _fate(row, run.lib_by_id)[0] == REC_STATUS_SCHEDULED:
            if index is None:
                index = _LibIndex(slots, run.library)
            holder = _holder(program, showing, row, index, run)
            if holder is not None:
                if _note_held(rule_id, revision, row, showing, holder, run) is _CHANGED:
                    return _CHANGED
                continue
        done = _follow(rule_id, revision, row, showing, run)
        if done is _CHANGED:
            return _CHANGED
        if done is None:
            continue
        kind = done['kind']
        if kind == _FOLLOW_MOVED:
            _move_row(row, showing, ledger)
            lib = run.lib_by_id.get(done['recording_id'])
            if lib is not None:
                lib.program_start, lib.program_stop = showing.start, showing.stop
                lib.start, lib.stop, lib.name = done['start'], done['stop'], done['name']
            outcome.retracked += 1
            moved.add(row.id)
            index = None   # the library moved; the next row reads it as it now stands
            problem = _rearm_moved(rule_id, done)
            if problem:
                problems.append(problem)
        elif kind == _FOLLOW_LET_GO:
            row.action = RULE_MATCH_USER_MODIFIED
            run.user_modified_recs.add(done['recording_id'])
        elif kind == _FOLLOW_ANCHOR:
            _move_row(row, showing, ledger)
        else:
            log.error('Auto-record rule #%d: retrack reported an unknown outcome %r', rule_id,
                      kind)
    return problems


def _still_listed(rows) -> set:
    """Ids of the rows whose program the guide still lists at its own time, on any channel.
    Read only when a move is about to be made."""
    exact, titled = _guide_keys({r.start for r in rows})
    return {r.id for r in rows
            if _is_listed(exact, titled, r.title_key, r.episode_key, r.start)}


def _guide_keys(starts):
    """({(title key, episode key, start)}, {(title key, start)}) for every listing in the
    guide starting at one of `starts`. One indexed read (ix_epg_entries_start_stop leads
    with start_time)."""
    exact, titled = set(), set()
    for start, t, s in db.session.query(EPGEntry.start_time, EPGEntry.title,
                                        EPGEntry.sub_title).filter(
            EPGEntry.start_time.in_(sorted(starts))):
        exact.add((title_key(t), title_key(s), start))
        titled.add((title_key(t), start))
    return exact, titled


def _is_listed(exact, titled, tkey, episode, start) -> bool:
    """Does the guide list this program at this moment? An episode-less listing of the
    title counts as it - it may be the same program, and doubt never moves a recording."""
    if episode:
        return (tkey, episode, start) in exact or (tkey, '', start) in exact
    return (tkey, start) in titled


def _group_members(group_ids) -> dict:
    """{group id: member channel ids} - one read for every group a moved pick was made on."""
    if not group_ids:
        return {}
    from .database import ChannelGroupMember
    out = {}
    for gid, cid in db.session.query(ChannelGroupMember.group_id,
                                     ChannelGroupMember.channel_id).filter(
            ChannelGroupMember.group_id.in_(sorted(group_ids))):
        out.setdefault(gid, set()).add(cid)
    return out


def _retrack_target(row, programs, ledger, taken, members):
    """(program, the showing of it on the pick's own channel or group) for the nearest
    target, or None (§5). `programs` already share the row's title and episode."""
    best = None
    for p in programs:
        start = p.identity[2]
        if start == row.start or p.identity in ledger or p.identity in taken:
            continue
        showing = next((s for s in p.showings if _on_rows_channel(s, row, members)), None)
        if showing is None:
            continue
        key = (abs(start - row.start), start)
        if best is None or key < best[0]:
            best = (key, p, showing)
    return None if best is None else best[1:]


def _holder(program, showing, row, index, run):
    """The recording that already records the target, or None (§5). What step 4's existing
    check would find there (the same program, or a recording covering one of its showings on
    that showing's channel or group), and also a recording covering it on the channel or
    group this pick's recording would land on - two recordings there hand off, and the one
    cut short would be the other one.

    A rule recording that is itself displaced does not hold anything: still SCHEDULED, not
    edited, with an episode, and its own program no longer listed at its time. It is about to
    follow its program away (this pass or a later rule's), so counting it would leave a whole
    evening that slid an hour, or two episodes that swapped slots, stuck where they were."""
    own = row.recording_id
    lib = run.lib_by_id.get(own) if own is not None else None
    channel_id = lib.channel_id if lib is not None else row.channel_id
    group_id = lib.group_id if lib is not None else row.group_id
    found = {}
    for rec in index.same_program(program.identity):
        found.setdefault(rec.id, rec)
    for rec, _s in index.covering(program):
        found.setdefault(rec.id, rec)
    for rec in index.covering_on(channel_id, group_id, showing.start, showing.stop):
        found.setdefault(rec.id, rec)
    found.pop(own, None)
    if not found:
        return None
    movable = [rec for rec in found.values()
               if rec.rule_id is not None and rec.status == REC_STATUS_SCHEDULED
               and rec.id not in run.user_modified_recs and rec.episode_raw
               and rec.title_key and rec.program_start is not None]
    displaced = set()
    if movable:
        exact, titled = _guide_keys({rec.program_start for rec in movable})
        displaced = {rec.id for rec in movable if not _is_listed(
            exact, titled, rec.title_key, rec.episode_raw, rec.program_start)}
    for rid in sorted(found):
        if rid not in displaced:
            return found[rid]
    return None


def _note_held(rule_id, revision, row, showing, holder, run):
    """A target another recording already records: this pick is not moved, and its row and
    its recording say where the program went and what records it there. Said once - the new
    time then holds a row of this rule (step 4 judges it as covered), so it is no longer a
    target. Returns None or _CHANGED."""
    @retry_on_locked()
    def _write():
        if not _claim_rule(rule_id, revision):
            db.session.rollback()
            return _CHANGED
        m = db.session.get(RuleMatch, row.id, populate_existing=True)
        if (m is None or m.action_taken != RULE_MATCH_SCHEDULED
                or m.program_start_time != row.start):
            db.session.rollback()
            return None
        rec = (db.session.get(Recording, m.recording_id, populate_existing=True)
               if m.recording_id is not None else None)
        rule_name = db.session.get(AutoRule, rule_id).name
        old = _local(m.program_start_time, run)
        new = _local(showing.start, run)
        where = f'recording #{holder.id} ("{holder.name}") already records it there'
        m.detail = _append(m.detail, f'The guide moved the program to {new}, but {where}, so '
                                     'this pick was not moved.')
        m.updated_at = datetime.utcnow()
        if rec is not None:
            extra = _moved_extra(m, rule_name, 'covered', m.program_start_time,
                                 m.program_stop_time, showing)
            extra['covered_by'] = holder.id
            add_recording_event(
                rec.id, RECORDING_RULE_PROGRAM_MOVED,
                detail=(f'The guide moved "{showing.title}" from {old} to {new}, but {where}. '
                        f'Auto-record rule "{rule_name}" left this recording as it is rather '
                        'than record the program twice.'),
                extra=extra)
        db.session.commit()
        return None

    return _write()


def _on_rows_channel(showing, row, members) -> bool:
    """Is this showing on the channel or the group the pick was made on? A group pick's
    channel_id is the member it was planned on, and any member of its group is the group."""
    if row.channel_id is not None and showing.channel_id == row.channel_id:
        return True
    if row.group_id is None:
        return False
    return (showing.group_id == row.group_id
            or showing.channel_id in members.get(row.group_id, ()))


def _move_row(row, showing, ledger):
    ledger.pop((row.title_key, row.episode_key, row.start), None)
    row.start, row.stop, row.program_title = showing.start, showing.stop, showing.title
    ledger[(row.title_key, row.episode_key, row.start)] = row


def _append(detail, sentence) -> str:
    return f'{detail or ""} {sentence}'.strip()


def _follow(rule_id, revision, row, showing, run):
    """One write unit for one pick whose program moved to `showing` (§5). Decides from what
    it reads inside the unit, not from the run's snapshot: a person can edit, cancel or start
    the recording between the two. Returns {'kind': ...} with what it did, None when there
    was nothing to follow after all, or _CHANGED."""
    @retry_on_locked()
    def _write():
        if not _claim_rule(rule_id, revision):
            db.session.rollback()
            return _CHANGED
        m = db.session.get(RuleMatch, row.id, populate_existing=True)
        if (m is None or m.action_taken != RULE_MATCH_SCHEDULED
                or m.program_start_time != row.start):
            db.session.rollback()
            return None
        rec = (db.session.get(Recording, m.recording_id, populate_existing=True)
               if m.recording_id is not None else None)
        status = rec.status if rec is not None else m.removed_status
        if status == REC_STATUS_FAILED:
            db.session.rollback()
            return None
        rule_name = db.session.get(AutoRule, rule_id).name
        now = datetime.utcnow()
        if rec is not None and rec.status == REC_STATUS_SCHEDULED:
            unedited = (m.rec_start_time is not None and m.rec_stop_time is not None
                        and rec.start_time == m.rec_start_time
                        and rec.stop_time == m.rec_stop_time)
            if unedited:
                done = _move_recording(m, rec, showing, rule_name, run, now)
            else:
                done = _let_go(m, rec, showing, rule_name, run)
        else:
            done = _move_anchor(m, rec, showing, rule_name, run)
        m.updated_at = now
        db.session.commit()
        return done

    return _write()


def _move_recording(m, rec, showing, rule_name, run, now) -> dict:
    """The recording still holds the window this rule wrote: it moves with its program. The
    new window is the new air time plus the recording's profile's padding; scheduled_* are
    re-baselined as a user edit does (dev/changelog/471); program_* move - the one exception
    to their immutability - and the event keeps the old values."""
    profile = (db.session.get(RecordingProfile, rec.profile_id)
               if rec.profile_id is not None else None)
    old_air = (m.program_start_time, m.program_stop_time)
    if profile is not None:
        pre = timedelta(minutes=profile.pre_padding_minutes or 0)
        post = timedelta(minutes=profile.post_padding_minutes or 0)
    else:
        # No profile - none was chosen, or it was deleted since, which unlinks it: keep the
        # padding the recording was scheduled with rather than dropping it to none.
        pre = max(timedelta(0), (rec.program_start_time or old_air[0]) - rec.start_time)
        post = max(timedelta(0), rec.stop_time - (rec.program_stop_time or old_air[1]))
    # A start whose padding has already begun is now, as the create path makes it.
    start = max(showing.start - pre, now)
    stop = showing.stop + post
    old_start, old_stop, old_name = rec.start_time, rec.stop_time, rec.name
    rec.start_time = rec.scheduled_start_time = start
    rec.stop_time = rec.scheduled_stop_time = stop
    rec.program_start_time, rec.program_stop_time = showing.start, showing.stop

    renamed = ''
    if m.rec_name is not None and rec.name == m.rec_name:
        name = _rendered_name(showing, profile, run)
        if name and name != rec.name:
            rec.name = m.rec_name = name
            renamed = f' Its name follows the program: "{old_name}" is now "{name}".'

    m.program_start_time, m.program_stop_time = showing.start, showing.stop
    m.program_title, m.program_sub_title = showing.title, showing.sub_title or None
    m.rec_start_time, m.rec_stop_time = start, stop
    m.detail = _append(m.detail, f'Moved with the program from {_local(old_air[0], run)} to '
                                 f'{_local(showing.start, run)}.')
    add_recording_event(
        rec.id, RECORDING_RULE_RETRACKED,
        detail=(f'Auto-record rule "{rule_name}" moved this recording with its program: the '
                f'guide moved "{showing.title}" from {_local(old_air[0], run)} to '
                f'{_local(showing.start, run)}. Window {_local(old_start, run)} to '
                f'{_local(old_stop, run)} is now {_local(start, run)} to '
                f'{_local(stop, run)}.{renamed}'),
        extra={'rule_id': m.rule_id, 'rule_name': rule_name,
               'old_program_start': _iso(old_air[0]), 'old_program_stop': _iso(old_air[1]),
               'new_program_start': _iso(showing.start), 'new_program_stop': _iso(showing.stop),
               'old_start': _iso(old_start), 'old_stop': _iso(old_stop),
               'new_start': _iso(start), 'new_stop': _iso(stop),
               'old_name': old_name if renamed else None,
               'new_name': rec.name if renamed else None})
    return {'kind': _FOLLOW_MOVED, 'recording_id': rec.id, 'row_id': m.id, 'start': start,
            'stop': stop, 'name': rec.name, 'start_moved': start != old_start}


def _let_go(m, rec, showing, rule_name, run) -> dict:
    """The user edited the recording's times: it is theirs now. The row becomes
    user_modified and keeps its anchor, the recording is never touched again, and the new
    time is judged as a showing of its own (§5, R5)."""
    old = _local(m.program_start_time, run)
    new = _local(showing.start, run)
    m.action_taken = RULE_MATCH_USER_MODIFIED
    m.detail = _append(m.detail, f'The guide moved the program to {new}, but recording '
                                 f'#{rec.id} had been edited, so the rule left it as it is '
                                 'and stopped tracking it. The new time is judged as a '
                                 'showing of its own.')
    add_recording_event(
        rec.id, RECORDING_RULE_PROGRAM_MOVED,
        detail=(f'The guide moved "{showing.title}" from {old} to {new}. This recording\'s '
                f'times had been edited, so auto-record rule "{rule_name}" left it as it is '
                'and no longer tracks it; the rule judges the new time as a showing of its '
                'own.'),
        extra=_moved_extra(m, rule_name, 'edited', m.program_start_time, m.program_stop_time,
                           showing))
    return {'kind': _FOLLOW_LET_GO, 'recording_id': rec.id}


def _move_anchor(m, rec, showing, rule_name, run) -> dict:
    """The recording is gone, has started, or has finished, so it is never touched - only
    the row's anchor follows the program, so the decision it records (a cancellation, a
    recording already under way) holds at the new time too rather than the new time reading
    as a program nobody judged (§5)."""
    from .channel_search_rows import REC_DONE, REC_LIVE
    old_air = (m.program_start_time, m.program_stop_time)
    old = _local(old_air[0], run)
    new = _local(showing.start, run)
    status = rec.status if rec is not None else m.removed_status
    reason = rec.cancel_reason if rec is not None else m.removed_cancel_reason
    which = f'Recording #{rec.id}' if rec is not None else 'Its recording'
    if status == REC_STATUS_ABORTED and reason == CANCEL_GROUP_CHANGE:
        kind = 'group_change'
        why = (f'{which} was called off by a channel group change, so the rule judges the '
               'new time afresh.')
    elif rec is None:
        kind = 'removed'
        why = ('Its recording was removed, and the rule keeps that decision at the new time: '
               'nothing is scheduled there either.')
    elif status == REC_STATUS_ABORTED:
        kind = 'cancelled'
        why = (f'{which} was cancelled, and the rule keeps that decision at the new time: '
               'nothing is scheduled there either.')
    elif status in REC_LIVE:
        kind = 'started'
        why = (f'{which} had already started, so it keeps its window, and this rule does '
               'not record the new time.')
    elif status in REC_DONE:
        kind = 'finished'
        why = f'{which} has finished, so it keeps its window.'
    else:
        log.error('Auto-record: retrack found recording #%s in unexpected status %r; only '
                  'its rule row follows the program', getattr(rec, 'id', None), status)
        kind = 'unknown'
        why = f'{which} is {str(status).lower()}, so it keeps its window.'
    m.program_start_time, m.program_stop_time = showing.start, showing.stop
    m.program_title, m.program_sub_title = showing.title, showing.sub_title or None
    m.detail = _append(m.detail, f'The guide moved the program to {new}. {why}')
    if rec is not None:
        add_recording_event(
            rec.id, RECORDING_RULE_PROGRAM_MOVED,
            detail=(f'The guide moved "{showing.title}" from {old} to {new}. {why} '
                    f'(Auto-record rule "{rule_name}".)'),
            extra=_moved_extra(m, rule_name, kind, *old_air, showing))
    return {'kind': _FOLLOW_ANCHOR}


def _moved_extra(m, rule_name, reason, old_start, old_stop, showing) -> dict:
    """RECORDING_RULE_PROGRAM_MOVED's extra: why the recording stayed, and both air times."""
    return {'rule_id': m.rule_id, 'rule_name': rule_name, 'reason': reason,
            'old_program_start': _iso(old_start), 'old_program_stop': _iso(old_stop),
            'new_program_start': _iso(showing.start), 'new_program_stop': _iso(showing.stop)}


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S') if dt else None


def _rendered_name(showing, profile, run):
    """The name the planner would give this showing under `profile` - the same template,
    cleanup, tags and timezone a new recording of it gets - or None when its listing no
    longer reads as the program judged (an import replaced it mid-run), in which case the
    name is left alone."""
    from .accounts import filename_naming_for
    from .channel_search_rows import _suggested_name
    from .database import Tag
    entry = db.session.get(EPGEntry, showing.entry_id)
    if not _listing_is(entry, showing):
        return None
    ch = db.session.get(Channel, entry.channel_id)
    if ch is None:
        return None
    tags = {t.name: t for t in Tag.query.all()}
    return _suggested_name(ch, entry, filename_naming_for(run.cfg, profile), tags, run.tz)


def _rearm_moved(rule_id, done) -> str:
    """After a move committed: the re-plan tail a user edit runs too
    (routes/recordings.py::_rearm_replanned) - the old window's jobs come off, the new ones
    go on, and a capture wait from the old plan ends (dev/changelog/1141). '' when that
    worked, else the sentence for last_problems. The old jobs come off FIRST, so a re-arm that
    fails leaves nothing armed at the slot the program left - which is what the "could not
    be armed" sentence says."""
    from .recorder import end_capture_wait
    from .routes import recordings as rec_routes
    rid = done['recording_id']
    try:
        rec_routes._rearm_replanned(rid, done['start'], done['stop'], done['start_moved'])
        return ''
    except Exception:
        log.exception('Auto-record rule #%d: recording #%d moved, but its jobs could not be '
                      're-armed; trying once more', rule_id, rid)
    problem = _rearm(rule_id, {'recording_id': rid, 'start': done['start'],
                               'stop': done['stop'], 'name': done['name'],
                               'row_id': done['row_id']})
    if done['start_moved']:
        end_capture_wait(rid)
    return problem


def _fill_episodes(rule_id, revision, fills) -> bool:
    @retry_on_locked()
    def _write():
        if not _claim_rule(rule_id, revision):
            db.session.rollback()
            return False
        for row_id, episode in fills:
            m = db.session.get(RuleMatch, row_id)
            if m is not None:
                m.episode_key = episode
                m.updated_at = datetime.utcnow()
        db.session.commit()
        return True
    return _write()


def _write_verdicts(rule_id, revision, verdicts) -> bool:
    """Every skip this rule's run reached, in one commit after its last create (§4.4). Only
    changed verdicts arrive here: a re-judged row that reaches the same verdict is not
    rewritten."""
    @retry_on_locked()
    def _write():
        if not _claim_rule(rule_id, revision):
            db.session.rollback()
            return False
        now = datetime.utcnow()
        for v in verdicts:
            m = db.session.get(RuleMatch, v.row_id) if v.row_id else None
            if m is None:
                m = RuleMatch(rule_id=rule_id, title_key=v.identity[0],
                              episode_key=v.identity[1], program_start_time=v.identity[2],
                              created_at=now)
                db.session.add(m)
            m.channel_id = v.lead.channel_id
            m.group_id = v.lead.group_id
            m.program_title = v.lead.title
            m.program_sub_title = v.lead.sub_title or None
            m.program_stop_time = v.lead.stop
            m.action_taken = v.action
            m.detail = v.detail
            m.updated_at = now
        db.session.commit()
        return True
    return _write()


def _guide_reach_for_rule(rule_id, now, reach):
    by_rule, member_channels, channel_source = _reach_inputs([rule_id])
    value, empty = _reach_for(
        _rule_sources(by_rule.get(rule_id, []), member_channels, channel_source), reach)
    return None if empty else value


def _finish(rule_id, revision, now, reach, problems) -> bool:
    """Step 5's write for a check that ran: it clears last_error - the rule is running - and
    records what it could not place in last_problems, which never means "not running"."""
    @retry_on_locked()
    def _write():
        if not _claim_rule(rule_id, revision):
            db.session.rollback()
            return False
        rule = db.session.get(AutoRule, rule_id)
        rule.last_evaluated_at = now
        rule.last_guide_reach = reach
        rule.last_error = None
        rule.last_problems = problems
        db.session.commit()
        return True
    return _write()


def _rule_alerts_after_run(rule_id, revision, outcome, settings):
    """A rule that ran is not broken; it is capped while this run left a showing at its cap."""
    from .alerts import AUTO_RULE_BROKEN, AUTO_RULE_CAPPED, dismiss_open_alerts
    dismiss_open_alerts(AUTO_RULE_BROKEN, f'auto-rule:{rule_id}:broken')
    source = f'auto-rule:{rule_id}:cap'
    if not outcome.capped:
        dismiss_open_alerts(AUTO_RULE_CAPPED, source)
        return
    n = outcome.capped
    _raise_standing(AUTO_RULE_CAPPED, source,
                    f'Auto-record rule "{settings.name}" is at its cap',
                    f'{n} upcoming showing{"s" if n != 1 else ""} this rule matches '
                    f'{"are" if n != 1 else "is"} not scheduled, because it already has '
                    f'{settings.cap} scheduled recordings. They are scheduled as earlier ones '
                    'finish; if it should hold more, raise its cap with Edit settings on the '
                    'rule\'s page. This clears itself when nothing is held back.')
    _unless_rule_moved(rule_id, revision)


def _unless_rule_moved(rule_id, revision):
    """Take a rule's alerts back down if it was paused, edited or deleted after the write that
    raised them committed. That pause or delete cleared the rule's alerts already - possibly
    before this run raised one - and nothing will ever evaluate a paused or deleted rule to
    clear it again."""
    row = db.session.query(AutoRule.enabled, AutoRule.revision).filter(
        AutoRule.id == rule_id).first()
    if row is None or not row.enabled or row.revision != revision:
        _clear_rule_alerts(rule_id)


# ---------------------------------------------------------------------------
# Triggers (§4.1)
# ---------------------------------------------------------------------------

def queue_evaluation(reason: str = '', delay_seconds: int = 30) -> bool:
    """Ask for one evaluation of every enabled rule soon. Several asks inside the delay
    become one run (a fixed job id, replace_existing). Does nothing when no scheduler is
    running - a test app, a stopped one."""
    from .scheduler import queue_auto_rule_evaluation
    return queue_auto_rule_evaluation(delay_seconds=delay_seconds, reason=reason)


def evaluate_now(rule_id: int) -> dict:
    """The save and resume path: evaluate one rule in the request (§4.1). When admission
    refuses, queue it and say what it waits behind."""
    outcome = run_evaluation(TRIGGER_SAVE, [rule_id])
    if isinstance(outcome, admission.Refusal):
        queue_evaluation(f'rule #{rule_id} saved, {waiting_phrase(outcome)}')
        return {'queued': True, 'waiting_for': outcome.reason}
    result = outcome.get(rule_id)
    return {'queued': False, **(result.payload() if result else {'rule_id': rule_id})}


def prune_history(keep_days: int, now=None) -> int:
    """Delete ledger rows whose program started more than `keep_days` ago (0 = keep)."""
    if not keep_days or keep_days <= 0:
        return 0
    cutoff = (now or datetime.utcnow()) - timedelta(days=keep_days)

    @retry_on_locked()
    def _prune():
        n = RuleMatch.query.filter(RuleMatch.program_start_time < cutoff).delete(
            synchronize_session=False)
        db.session.commit()
        return n
    return _prune()


# ---------------------------------------------------------------------------
# Lifecycle (§8) - what the routes call
# ---------------------------------------------------------------------------

@dataclass
class RuleValues:
    name: str
    search_params: str
    profile_id: int | None
    skip_repeats: bool
    limit_count: int | None
    limit_window_hours: int | None
    max_upcoming: int | None


def read_rule_values(data: dict, existing: AutoRule | None = None) -> RuleValues:
    """Validate a create or edit body into RuleValues, or RuleError with the sentence."""
    def opt_int(key, label, minimum):
        raw = data.get(key, getattr(existing, key, None) if existing else None)
        if raw in (None, ''):
            return None
        if isinstance(raw, bool):
            raise RuleError(f'{label} must be a whole number.')
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise RuleError(f'{label} must be a whole number.') from None
        if value < minimum:
            raise RuleError(f'{label} must be at least {minimum}.')
        return value

    if 'search' in data or existing is None:
        _state, params = clean_search(str(data.get('search') or ''))
    else:
        params = existing.search_params
    name = str(data.get('name', existing.name if existing else '') or '').strip()
    if not name:
        raise RuleError('Give the rule a name.')
    if len(name) > 255:
        raise RuleError('The name is longer than 255 characters.')
    profile_id = opt_int('profile_id', 'Profile', 1)
    if profile_id is not None and db.session.get(RecordingProfile, profile_id) is None:
        raise RuleError('That recording profile no longer exists.')
    skip = data.get('skip_repeats', existing.skip_repeats if existing else True)
    if isinstance(skip, str):
        skip = skip.strip().lower() in ('1', 'true', 'on', 'yes')
    limit_count = opt_int('limit_count', 'The limit', 1)
    limit_window = opt_int('limit_window_hours', 'The limit\'s window', 1)
    if (limit_count is None) != (limit_window is None):
        raise RuleError('Set both halves of "at most X within Y hours", or neither.')
    return RuleValues(name=name, search_params=params, profile_id=profile_id,
                      skip_repeats=bool(skip), limit_count=limit_count,
                      limit_window_hours=limit_window,
                      max_upcoming=opt_int('max_upcoming', 'The cap', 0))


def create_rule(values: RuleValues) -> int:
    @retry_on_locked()
    def _create():
        now = datetime.utcnow()
        rule = AutoRule(name=values.name, search_params=values.search_params,
                        profile_id=values.profile_id, skip_repeats=values.skip_repeats,
                        limit_count=values.limit_count,
                        limit_window_hours=values.limit_window_hours,
                        max_upcoming=values.max_upcoming, enabled=True, revision=1,
                        created_at=now, updated_at=now)
        db.session.add(rule)
        db.session.commit()
        return rule.id
    return _create()


UNMATCHED_CANCEL = 'cancel'
UNMATCHED_KEEP = 'keep'
UNMATCHED_ACTIONS = (UNMATCHED_CANCEL, UNMATCHED_KEEP)


@dataclass
class Unmatched:
    """The rule's upcoming SCHEDULED recordings a new search would leave behind (§8).
    `determined` False means the new search could not be compared at all, `reason` says why
    in a sentence,
    and `recordings` is then every upcoming SCHEDULED recording of the rule - the person is
    asked about all of them rather than none."""
    recordings: list
    determined: bool = True
    reason: str = ''

    @property
    def ids(self) -> set:
        return {r['id'] for r in self.recordings}

    def payload(self):
        return {'unmatched': self.recordings, 'determined': self.determined,
                'reason': self.reason or None}


def unmatched_by_search(rule_id: int, search_params: str) -> Unmatched:
    """Which of the rule's upcoming SCHEDULED picks `search_params` no longer matches (§8).

    Runs the new search once, as the engine would, and keeps a pick only when the search has
    no showing of its program at its time - "the same program" read leniently, as retrack's
    still-present check reads it, so an episode-less listing of the title counts as a match
    and doubt never offers a recording for cancelling. A pick whose program the guide no
    longer lists at its time is left out too: the program moved or vanished, which is
    retrack's business (§5), not the edit's. Recordings the person edited (`user_modified`)
    are theirs and are never offered. Reads only; the caller decides."""
    from .channel_search import SearchContext, SearchStateError
    from .config import load_config
    from .tz_utils import format_local, get_display_tz, is_24h

    now = datetime.utcnow()
    picks = db.session.query(
        Recording.id, Recording.name, Recording.start_time, RuleMatch.title_key,
        RuleMatch.episode_key, RuleMatch.program_start_time,
    ).join(RuleMatch, RuleMatch.recording_id == Recording.id).filter(
        RuleMatch.rule_id == rule_id, RuleMatch.action_taken == RULE_MATCH_SCHEDULED,
        Recording.rule_id == rule_id, Recording.status == REC_STATUS_SCHEDULED,
        RuleMatch.program_start_time > now,
    ).order_by(Recording.start_time, Recording.id).all()
    if not picks:
        return Unmatched([])
    tz, h24 = get_display_tz(), is_24h()

    def described(rows):
        return [{'id': r.id, 'name': r.name,
                 'when': format_local(r.start_time, 'day_datetime', tz=tz, h24=h24)}
                for r in rows]

    def undetermined(reason):
        return Unmatched(described(picks), determined=False, reason=reason)

    try:
        state = parse_search(search_params)
    except SearchStateError as exc:
        return undetermined(f'The new search cannot be read: {exc}.')
    gone = missing_names(state, known_names([state]))
    if gone:
        return undetermined(gone)
    # _search_rule reads only the run's search context.
    run = SimpleNamespace(ctx=SearchContext.build(load_config()))
    try:
        entries, _wins = _search_rule(state, run)
    except SearchStateError as exc:
        return undetermined(f'The new search cannot be run: {exc}.')
    if entries is None:
        return undetermined(f'The new search matches more than {MAX_SHOWINGS:,} upcoming '
                            'showings, which is more than a rule will run.')
    exact, titled = set(), set()
    for e in entries:
        exact.add((title_key(e.title), title_key(e.sub_title), e.start))
        titled.add((title_key(e.title), e.start))
    left = [r for r in picks if not _is_listed(exact, titled, r.title_key,
                                               r.episode_key or '', r.program_start_time)]
    if left:
        exact, titled = _guide_keys({r.program_start_time for r in left})
        left = [r for r in left if _is_listed(exact, titled, r.title_key,
                                              r.episode_key or '', r.program_start_time)]
    return Unmatched(described(left))


def _cancel_scheduled(rec):
    """Call off one SCHEDULED recording inside the caller's write unit: deleted, since nothing
    was captured to keep (dev/changelog/814). The caller unschedules its jobs after the
    commit."""
    detach_recording_references(rec.id)
    db.session.delete(rec)  # hidden-recompute-ok: a Recording, not a group or membership


def _settle_unmatched(rule, action, ids) -> tuple:
    """The person's answer for the picks an edited search left behind (§8), inside the edit's
    own write unit: ([cancelled recording ids], kept count). Only a recording that is still
    SCHEDULED and still this rule's is touched - one that started since the question was asked
    is left alone. Either way its ledger row goes: the rule no longer matches that program, so
    the row must not keep deciding it - a cancelled pick's row would otherwise make the
    program's later airings repeats (§3.4) of a recording the person never turned down."""
    cancelled, kept = [], 0
    if not ids:
        return cancelled, kept
    for rec in Recording.query.filter(Recording.id.in_(sorted(ids))).all():
        if rec.rule_id != rule.id or rec.status != REC_STATUS_SCHEDULED:
            continue
        row_ids = [rid for (rid,) in db.session.query(RuleMatch.id).filter(
            RuleMatch.rule_id == rule.id, RuleMatch.recording_id == rec.id)]
        if action == UNMATCHED_CANCEL:
            _cancel_scheduled(rec)
            cancelled.append(rec.id)
        elif action == UNMATCHED_KEEP:
            rec.rule_id = None
            add_recording_event(
                rec.id, RECORDING_RULE_RELEASED,
                detail=(f'Auto-record rule "{rule.name}" had its search changed and no longer '
                        'matches this program. Kept as an ordinary recording: it still records '
                        'as scheduled, but no longer counts toward the rule\'s cap.'),
                extra={'rule_id': rule.id, 'rule_name': rule.name})
            kept += 1
        else:
            raise ValueError(f'unknown answer for unmatched recordings: {action!r}')
        if row_ids:
            RuleMatch.query.filter(RuleMatch.id.in_(row_ids)).delete(
                synchronize_session=False)
    return cancelled, kept


def update_rule(rule_id: int, values: RuleValues, unmatched=None):
    """Edit a rule (§8): bump its revision and, in the same commit, delete its ledger rows for
    future showings it did not schedule, so the edited rule judges them afresh.

    Recordings it already scheduled are kept unless `unmatched` = (action, recording ids)
    says otherwise: the person's answer, after unmatched_by_search() named the picks a changed
    search no longer matches - nothing is dropped without being named first. Returns None
    when the rule does not exist, else ([cancelled recording ids], kept count)."""
    from .scheduler import unschedule_recording

    @retry_on_locked()
    def _update():
        rule = db.session.get(AutoRule, rule_id)
        if rule is None:
            return None
        rule.name = values.name
        rule.search_params = values.search_params
        rule.profile_id = values.profile_id
        rule.skip_repeats = values.skip_repeats
        rule.limit_count = values.limit_count
        rule.limit_window_hours = values.limit_window_hours
        rule.max_upcoming = values.max_upcoming
        # A SQL expression, not the loaded value plus one: the loaded value was read outside
        # this write, and an engine claim between the two would be overwritten as unchanged.
        rule.revision = AutoRule.revision + 1
        rule.updated_at = datetime.utcnow()
        RuleMatch.query.filter(
            RuleMatch.rule_id == rule_id,
            RuleMatch.action_taken.in_(RULE_MATCH_SKIPS),
            RuleMatch.program_start_time > datetime.utcnow(),
        ).delete(synchronize_session=False)
        # Read after the revision is flushed (the statements here autoflush it), so the write
        # lock is held and no evaluation can commit a change to these recordings meanwhile.
        settled = _settle_unmatched(rule, *unmatched) if unmatched else ([], 0)
        db.session.commit()
        return settled

    result = _update()
    if result is None:
        return None
    for rid in result[0]:
        unschedule_recording(rid)
    _clear_rule_alerts(rule_id)
    return result


def set_rule_enabled(rule_id: int, enabled: bool) -> bool:
    """Pause or resume (§8). Either bumps the revision. A paused rule's scheduled recordings
    are untouched and its own alerts clear; the guide-reach alert is re-checked. The caller
    evaluates a resumed rule at once, as a save does."""
    @retry_on_locked()
    def _set():
        rule = db.session.get(AutoRule, rule_id)
        if rule is None:
            return False
        if rule.enabled != enabled:
            rule.enabled = enabled
            rule.revision = AutoRule.revision + 1   # see update_rule()
            rule.updated_at = datetime.utcnow()
            db.session.commit()
        return True

    if not _set():
        return False
    if not enabled:
        _clear_rule_alerts(rule_id)
        _guide_alert_after_a_rule_left()
    return True


def _guide_alert_after_a_rule_left():
    """Re-check the guide-reach alert once a rule stops counting (paused or deleted), §6.

    With no enabled rule left the alert simply clears - nothing to read. Otherwise reading
    the guide is an evaluation's job and takes its ticket: a sync mid-import has deleted a
    source's listings and not yet written the new ones, and a reach read then would raise
    the alert against a source a minute from full. Refused, the re-check is queued and runs
    under admission when the sync is done."""
    if db.session.query(AutoRule.id).filter(AutoRule.enabled.is_(True)).first() is None:
        refresh_guide_alert()
        return
    ticket = admission.try_start(admission.KIND_RULES, 'guide-reach re-check')
    if not ticket.granted:
        queue_evaluation(waiting_phrase(ticket))
        return
    try:
        refresh_guide_alert()
    finally:
        admission.release(ticket)


def delete_rule(rule_id: int, cancel_scheduled: bool):
    """Delete a rule as one unit (§8), or None when it does not exist.

    With `cancel_scheduled`, every SCHEDULED recording the rule made is cancelled - deleted,
    since a recording that never started has nothing to keep (dev/changelog/814). Under
    either choice every surviving recording of the rule loses its rule_id, and IN_PROGRESS
    or finished ones are never touched. The ledger rows and the rule go in the same commit,
    explicitly, because foreign keys are off. Jobs and alerts are dealt with after the
    commit. Returns (cancelled recording ids, kept recording count)."""
    from .scheduler import unschedule_recording

    @retry_on_locked()
    def _delete():
        # The write lock first, then the list. An evaluation inside its create unit holds
        # the lock with a recording not yet committed; listing the rule's recordings before
        # waiting for it would miss that one and leave it SCHEDULED, uncancelled, naming a
        # rule that no longer exists. Moving the revision also fails every claim the run
        # makes after this commits (_claim_rule).
        moved = db.session.execute(
            update(AutoRule).where(AutoRule.id == rule_id)
            .values(revision=AutoRule.revision + 1)
            .execution_options(synchronize_session=False)).rowcount
        if not moved:
            db.session.rollback()
            return None
        rule = db.session.get(AutoRule, rule_id, populate_existing=True)
        cancelled, kept = [], 0
        for rec in Recording.query.filter_by(rule_id=rule_id).all():
            if cancel_scheduled and rec.status == REC_STATUS_SCHEDULED:
                _cancel_scheduled(rec)
                cancelled.append(rec.id)
            else:
                rec.rule_id = None
                kept += 1
        RuleMatch.query.filter_by(rule_id=rule_id).delete(synchronize_session=False)
        db.session.delete(rule)  # hidden-recompute-ok: an auto-record rule, not a group
        db.session.commit()
        return cancelled, kept

    result = _delete()
    if result is None:
        return None
    for rid in result[0]:
        unschedule_recording(rid)
    _clear_rule_alerts(rule_id)
    _guide_alert_after_a_rule_left()
    return result


def unlink_profile(profile_id: int) -> None:
    """A deleted profile's rules fall back to each showing's default (§8). Mutates without
    committing - the profile delete's own unlink commit owns it. Not an edit of the rule, so
    the revision does not move."""
    AutoRule.query.filter_by(profile_id=profile_id).update(
        {'profile_id': None}, synchronize_session=False)


def scheduled_counts(rule_ids) -> dict:
    """{rule id: SCHEDULED recordings it made} in one grouped query - what "scheduled" means
    on every Rules surface and in the delete dialog, which offers to cancel exactly these."""
    if not rule_ids:
        return {}
    return dict(db.session.query(Recording.rule_id, func.count(Recording.id)).filter(
        Recording.rule_id.in_(sorted(rule_ids)), Recording.status == REC_STATUS_SCHEDULED
    ).group_by(Recording.rule_id))


def rule_list(rule_ids=None) -> list:
    """Every rule (or the ones in `rule_ids`) as the API answers it - a fixed number of
    queries however many rules there are."""
    from .tz_utils import format_local, get_display_tz, is_24h
    tz, h24 = get_display_tz(), is_24h()
    query = AutoRule.query
    if rule_ids is not None:
        query = query.filter(AutoRule.id.in_(sorted(rule_ids)))
    rules = query.order_by(func.lower(AutoRule.name), AutoRule.id).all()
    ids = [r.id for r in rules]
    scheduled = scheduled_counts(ids)
    counts = {}
    if ids:
        for rid, action, n in db.session.query(
                RuleMatch.rule_id, RuleMatch.action_taken, func.count(RuleMatch.id)).filter(
                RuleMatch.rule_id.in_(ids)).group_by(RuleMatch.rule_id, RuleMatch.action_taken):
            counts.setdefault(rid, {})[action] = n
    profiles = dict(db.session.query(RecordingProfile.id, RecordingProfile.name))
    out = []
    for r in rules:
        out.append({
            'id': r.id, 'name': r.name, 'enabled': bool(r.enabled),
            'search_params': r.search_params,
            'profile_id': r.profile_id, 'profile_name': profiles.get(r.profile_id),
            'skip_repeats': bool(r.skip_repeats), 'limit_count': r.limit_count,
            'limit_window_hours': r.limit_window_hours, 'max_upcoming': r.max_upcoming,
            'scheduled_count': scheduled.get(r.id, 0),
            'history': counts.get(r.id, {}),
            'last_evaluated': format_local(r.last_evaluated_at, 'day_datetime', none_value=None,
                                           tz=tz, h24=h24),
            'last_evaluated_at': (r.last_evaluated_at.strftime('%Y-%m-%dT%H:%M:%S')
                                  if r.last_evaluated_at else None),
            'guide_reach': format_local(r.last_guide_reach, 'day_datetime', none_value=None,
                                        tz=tz, h24=h24),
            'last_error': r.last_error,
            'last_problems': r.last_problems,
        })
    return out
