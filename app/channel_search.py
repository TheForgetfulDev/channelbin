"""The channel search engine: one search state, one set of registries, one query builder.

Every surface that asks "which of these 136,130 channels" goes through here - a group's
`+ Add channels`, the TV Guide's `Manage channels`, and the Channels hub's own Browse
list. They differ only in the state they arrive with, never in how the search behaves,
which is the whole point of putting it in one module.

**Nothing here is a page.** This builds and runs queries and returns plain data; the JSON
endpoint and the templates live elsewhere. Nothing in this module reads `request`.

Four things a future editor needs to know before changing anything:

**There are two result GRAINS and both are built.** `channels` returns one row per *channel*
("which of my 136,130 channels"); `airings` returns one row per *showing*, ordered by start
time, with recording state per row ("when is it on"). They share one state model, one set of
registries, one facet counter and one envelope, and differ only in the predicates and the row
builder - which is what the `grain` parameter was reserved for when the channel half shipped
(dev/changelog/412 built the second half).

Three things about the seam that a future editor gets wrong:

* **The standing options and every dimension except `when` are channel-grain concepts.** On
  the airing grain they apply to the channel a showing is on, not to the showing. "Hide
  duplicates" is about two channels sharing a stream URL; it says nothing about two airings.
* **An EPG search field means something different on each grain, and this is deliberate.** On
  the channel grain `epg-title` matches what the channel is airing RIGHT NOW, because a
  channel row shows one program - the one in its `Now airing` column - and a row that matched
  on a showing at 11pm reads as the search being broken (dev/changelog/861). On the airing
  grain it matches *this showing*, because the question is "when is this on". Same word, same
  box, two honest answers.
* **A registry entry scoped to one grain carries `grain=`; no `grain` means both.** An
  out-of-grain FILTER is ignored rather than rejected, so a chip parked by flipping grain
  survives a reload; an out-of-grain SORT is still a 400, because silently sorting by
  something else is wrong data wearing the right label.

**The registries below are the contract.** FIELDS, DIMENSIONS, STANDING_OPTIONS and SORTS
are read by the URL parser, the query builder, the facet counter and (through the endpoint)
the page itself. Adding a search field or a facet means adding one entry, not editing five
call sites - and anything not in a registry is not addressable, which is what keeps the URL
parameters an API rather than an accident.

**Standing options are not filters.** A filter is "I picked this, for this search"; a
standing option is a preference that survives every search ("show duplicates", off by
default). They gate the facet counts too, because a count that ignored them would promise
rows the list then refuses to show. And per this project's founding principle, everything a
standing option hides is counted and reported back in `standing_hidden` - nothing is hidden
silently.

**A facet count ignores its own dimension.** Picking `Category = Sports` must not drop every
other category's count to zero, or the facet becomes unusable the moment it is used. So each
dimension is counted against the state with *its own* filter removed, which is why the
counts are one query per dimension rather than one pass.

Measurements that shaped this file, all taken on the production database 2026-07-30 and
recorded in dev/changelog/395 - do not re-derive them:

* the unfiltered facet pass (the default page load) was ~295ms, and what fixes it is ordered
  single-column indexes, not a covering index; migration 24 creates them
* the tag facet is the expensive dimension, not the columns - see TAG FACET below
* trigram FTS answers `LIKE`/`GLOB` off the index, but doing so against an external-content
  FTS table raises "database disk image is malformed" once any matching row's channel has
  been deleted; wildcards therefore run against the base tables (`search_index.glob_to_like`)
"""
import logging
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from sqlalchemy import (String, and_, case, cast, func, literal_column, not_, or_, select,
                        union)

from . import db, health_bands
from .channel_groups import guide_scope_channel_ids, guide_scope_group_member_ids
from .database import (Account, AccountSyncLog, Channel, ChannelGroup,
                       ChannelGroupMember, EPGEntry, Tag)
from .search_index import (AIRING_PROBE_MAX_ROWS, SEARCH_INDEX_CHANNELS, SEARCH_INDEX_NAMES,
                           SEARCH_INDEX_PROGRAMS, TRIGRAM_MIN_CHARS,
                           airing_narrowing_channel_ids, airing_probe_count, fts_match_term,
                           fts_rowid_select, glob_to_like, has_wildcards, literal_runs,
                           readiness_map, search_index_readiness, source_watermark)

log = logging.getLogger(__name__)


class SearchStateError(ValueError):
    """A search state that cannot be honoured - an unknown field, dimension, sort or grain.

    Raised rather than silently ignored: a caller that asks to sort by something this engine
    cannot sort by must be told so (the endpoint turns this into a 400), because quietly
    sorting by something else is wrong data wearing the right label.
    """


# ---------------------------------------------------------------------------
# Grains
# ---------------------------------------------------------------------------

GRAIN_CHANNELS = 'channels'
GRAIN_AIRINGS = 'airings'
IMPLEMENTED_GRAINS = (GRAIN_CHANNELS, GRAIN_AIRINGS)


def _in_grain(entry, grain: str) -> bool:
    """Is this registry entry offered on `grain`? An entry with no `grain` is on both.

    One reader for the rule, used by the dimensions, the standing options and the catalog
    alike - a second spelling is how a dimension ends up filterable on a grain that cannot
    express it.
    """
    return not getattr(entry, 'grain', '') or entry.grain == grain


# ---------------------------------------------------------------------------
# Search fields - "Search in"
# ---------------------------------------------------------------------------

SOURCE_CHANNEL = 'channel'
SOURCE_PROGRAM = 'program'

FIELD_GROUP_CHANNEL = 'Channel'
FIELD_GROUP_EPG = 'TV Guide (EPG)'


@dataclass(frozen=True)
class SearchField:
    """One switchable thing a typed term is matched against.

    `fts_column` is the column's name inside its FTS index, or None when the field has no
    index behind it and always takes the base-table path. `column` is the SQLAlchemy column
    the un-indexed path matches on - for a program field that column lives on `chan_prog`,
    reached through `_PROG`.
    """
    key: str
    label: str
    group: str
    source: str
    fts_column: str | None
    column: object
    #: Wrap the column before matching. Only stream_id needs it (an INTEGER that the user
    #: types as text); everything else is already text.
    cast_text: bool = False
    #: An EXAMPLE of what this field holds, for the Search-in pane's right-hand column. Here
    #: rather than in the page for the same reason the label is: a field added later would
    #: otherwise ship with a blank column and nobody would notice.
    hint: str = ''


#: The Search-in pane's headings, in render order. A group is a HEADING and nothing else -
#: it carries no key, so nothing can switch it, and every entry in FIELDS is a leaf. (The
#: parent-control spelling was tried and dropped in mockup 21 round 10: it forced a half-on
#: state onto a switch that has none.) The tuple IS the order; nothing sorts it downstream.
FIELD_GROUPS = (
    (FIELD_GROUP_CHANNEL, ''),
    (FIELD_GROUP_EPG,
     'Switch any of these on and your search also runs against your guide data - what is '
     "scheduled on each channel - not just the channel's own name, ids and category."
     '\n\nOn Channels, a channel matches when the program it is airing RIGHT NOW matches - '
     'the one in its Now airing column. On Guide (EPG), every showing is matched on its own, '
     'so that is where to look for what is on later.'),
)


# chan_prog is the deduped, future-only projection of epg_entries that chan_prog_fts indexes
# (see search_index.py). Addressed as a lightweight table rather than an ORM model because
# it is a derived cache with no identity worth mapping.
_PROG = db.Table('chan_prog', db.MetaData(),
                 db.Column('id', db.Integer, primary_key=True),
                 db.Column('channel_id', db.Integer),
                 db.Column('title', db.Text),
                 db.Column('sub_title', db.Text),
                 db.Column('description', db.Text))

FIELDS = (
    SearchField('name', 'Channel name', FIELD_GROUP_CHANNEL, SOURCE_CHANNEL,
                'name', Channel.name, hint='FS2 (720p)'),
    SearchField('cat', 'Category', FIELD_GROUP_CHANNEL, SOURCE_CHANNEL,
                'category_name', Channel.category_name, hint='WORLD CUP 2026'),
    # No fts_column on purpose: ch_fts indexes name/stream_url/epg_channel_id/category_name,
    # and stream_id is an INTEGER besides. Adding it would mean dropping and rebuilding the
    # channels index; measured 21ms as a plain CAST + LIKE scan, on a field that ships off,
    # so it is not worth a migration. Revisit only if it becomes a default field.
    SearchField('sid', 'Stream id', FIELD_GROUP_CHANNEL, SOURCE_CHANNEL,
                None, Channel.stream_id, cast_text=True, hint='2077000'),
    SearchField('tvg', 'EPG id', FIELD_GROUP_CHANNEL, SOURCE_CHANNEL,
                'epg_channel_id', Channel.epg_channel_id, hint='foxsports2.us'),
    SearchField('url', 'Stream URL', FIELD_GROUP_CHANNEL, SOURCE_CHANNEL,
                'stream_url', Channel.stream_url, hint='45570'),
    SearchField('epg-title', 'Program title', FIELD_GROUP_EPG, SOURCE_PROGRAM,
                'title', _PROG.c.title),
    SearchField('epg-sub', 'Episode / subtitle', FIELD_GROUP_EPG, SOURCE_PROGRAM,
                'sub_title', _PROG.c.sub_title),
    # A normal, working field. It is NOT "unavailable" and needs no slow-mode warning:
    # descriptions were indexed into chan_prog on 2026-07-30 and measure 1-72ms in
    # production (dev/changelog/395). Any UI text still saying otherwise is stale.
    SearchField('epg-desc', 'Description', FIELD_GROUP_EPG, SOURCE_PROGRAM,
                'description', _PROG.c.description),
)
FIELD_BY_KEY = {f.key: f for f in FIELDS}
#: What a caller that names no fields gets, PER GRAIN - because the two grains are asking
#: different questions and one shared default answered neither well. On the channel grain a
#: row IS a channel, so the name is the whole default: adding `epg-title` there made a typed
#: word match channels whose only connection to it was a program on tonight, which reads as
#: the search being wrong rather than as a wider net. On the airing grain a row IS a program,
#: so all three program fields lead and the channel's name is one tick away in the scope pane.
DEFAULT_FIELDS_BY_GRAIN = {
    GRAIN_CHANNELS: ('name',),
    GRAIN_AIRINGS: ('epg-title', 'epg-sub', 'epg-desc'),
}
#: The channel grain's, under the name every existing caller already imports.
DEFAULT_FIELDS = DEFAULT_FIELDS_BY_GRAIN[GRAIN_CHANNELS]


def default_fields_for(grain: str) -> tuple:
    """The scope a search on `grain` runs with when it names no fields of its own.

    `.get`, not `[]`, so an unknown grain in a hand-built state reaches search()'s own
    SearchStateError rather than dying here as a KeyError - the same contract
    `default_standing_for` and DEFAULT_SORT_BY_GRAIN follow.
    """
    return DEFAULT_FIELDS_BY_GRAIN.get(grain, DEFAULT_FIELDS)


# ---------------------------------------------------------------------------
# Health bands
# ---------------------------------------------------------------------------

# The bands themselves are declared once in app/health_bands.py and their cut points are
# configurable (`channel_testing.health_bands`), so this engine holds no numbers of its own -
# it resolves them per request from `ctx.cfg` and hands them down. The band KEYS are fixed,
# which is what lets the URL contract below stay a constant: a saved search naming
# `health=fair` keeps meaning the Fair band even after its floor is moved.
HEALTH_UNTESTED = health_bands.UNTESTED
HEALTH_UNTESTED_LABEL = health_bands.UNTESTED_LABEL
HEALTH_VALUES = health_bands.BAND_KEYS + (HEALTH_UNTESTED,)


def effective_health():
    """The score the badges show: the observed score plus the manual adjustment.

    Deliberately unclamped. `health_score_badge` clamps to 0-100 for display, but clamping
    cannot move a value across the 80 or 50 cut points, so banding on the raw sum is the same
    answer with one less expression in every query.
    """
    return Channel.health_score + Channel.manual_health_adjustment


def _health_band_expr(cfg):
    bands = health_bands.resolve_bands(cfg)
    band = db.case(
        (Channel.health_score.is_(None), HEALTH_UNTESTED),
        *[(effective_health() >= b.floor, b.key) for b in bands],
        else_=bands[-1].key,
    )
    return band


def _health_predicate(value: str, cfg):
    if value == HEALTH_UNTESTED:
        return Channel.health_score.is_(None)
    band = health_bands.band_by_key(health_bands.resolve_bands(cfg), value)
    if band is None:
        raise SearchStateError(f'unknown health band {value!r}')
    clauses = [Channel.health_score.isnot(None), effective_health() >= band.floor]
    if band.ceiling is not None:
        clauses.append(effective_health() < band.ceiling)
    return and_(*clauses)


# ---------------------------------------------------------------------------
# Dimensions - the facet rail
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Dimension:
    """One facet: a set of values a channel can carry, filterable three ways.

    Three-state, per value: absent, included (`values`), or excluded (`ex`). Values inside
    one dimension OR together and dimensions AND together - a split that matters, because
    the shipped recordings list ANDs same-field values, so picking two of anything there
    matches nothing. That is a defect this model fixes before it gets copied.

    `hidden` marks a dimension that has no rail card of its own and is only reachable from
    somewhere else (today: `chan`, from the search box's suggestion menu and from a
    channel's own detail page - "Search all programs" on the What's On card). It still
    chips, negates and serializes like any other - it is hidden, not special.
    """
    key: str
    label: str
    hidden: bool = False
    #: True when one channel can carry several of this dimension's values at once. Such a
    #: dimension cannot be counted with a plain GROUP BY over channels.
    multi: bool = False
    #: The one grain this dimension exists on, or '' for both. Only `when` is scoped today:
    #: a channel has no start time, which is the whole reason the two grains exist.
    grain: str = ''
    #: What this dimension MEANS, for the rail's info tooltip. It lives here for the same
    #: reason the label does: a page that spelled it out itself would be a second vocabulary
    #: to keep in step, and a dimension added later would silently ship with no explanation.
    #: Only the three whose meaning is not self-evident carry one.
    help: str = ''


DIMENSIONS = (
    # Airings only, and first in the rail on the grain that owns it - a dimension that
    # exists only on the grain you just entered is the reason you entered it (mockup
    # 25 P9). See `dimensions_for()`, which is what applies that ordering.
    Dimension('when', 'When', multi=True, grain=GRAIN_AIRINGS,
              help='When a showing STARTS. Only on the Airings grain - a channel does not '
                   'have a start time, which is the whole reason the two grains exist.\n\n'
                   'These overlap on purpose: a showing at 7pm today is Today and, if you '
                   'ask for it, the next 6 hours at once. Values inside one filter mean '
                   '"any of these", so that is not a conflict.\n\n'
                   'There is deliberately no default window. With nothing picked you get '
                   'every future showing, earliest first.'),
    Dimension('duration', 'Program length', multi=True, grain=GRAIN_AIRINGS,
              help='How long a showing runs. Only on the Airings grain - a channel has no '
                   'length of its own, only a showing does.\n\n'
                   'Either bound alone is a real filter: set only "at least" for "longer '
                   'than X", or only "at most" for "shorter than X".'),
    Dimension('tag', 'Tag', multi=True,
              help="A label you put on a channel yourself, not the provider's, so it "
                   "survives a sync. A tag is a set of literal patterns, not a column: a "
                   "channel carries the tag when its name, or something it is airing, "
                   "contains one of that tag's patterns.\n\n"
                   'On Channels "something it is airing" means the program on RIGHT NOW - '
                   'the one in the Now airing column - so the tag beside a row always '
                   'describes what that row is showing. On Guide (EPG) each showing answers '
                   'for itself.'),
    Dimension('acct', 'Account'),
    Dimension('health', 'Health'),
    Dimension('group', 'Channel group', multi=True,
              help='A named set of channels carrying the same feed, so a recording can '
                   'fail over between them. Exclude "Any group" to find every channel '
                   'that is in no group at all.'),
    Dimension('cat', 'Category'),
    Dimension('other', 'Other', multi=True,
              help="Deleted by provider: no longer present in the account's synced feed. "
                   'Excluding it here is the only way this list hides them - nothing hides '
                   'them from the TV Guide, from groups, or from a recording that '
                   'references them.\n\nIn your guide means every channel whose listings '
                   'can reach your guide, which is more than the channels you added one by '
                   'one: a channel group appears as a single guide row, so all of its '
                   'members count too. Has its own guide row and In the guide via a group '
                   'are its two halves, so you can ask which of the two puts a channel on '
                   'screen - pick both and you are back to In your guide.\n\n'
                   'Duplicated stream URL composes with the Show '
                   'duplicates standing option rather than overriding it, so with that one '
                   'off you get one survivor per cluster and the count line says how many '
                   'copies are still hidden.'),
    Dimension('chan', 'Channel', hidden=True),
)
DIMENSION_BY_KEY = {d.key: d for d in DIMENSIONS}
#: Every non-hidden dimension, in registry order, across both grains. `dimensions_for()` is
#: what a grain actually offers - use that for anything that queries or counts.
VISIBLE_DIMENSIONS = tuple(d for d in DIMENSIONS if not d.hidden)


def dimensions_for(grain: str) -> tuple:
    """The dimensions this grain offers, grain-scoped ones first.

    The facet order is otherwise fixed and the registry tuple IS that order - nothing sorts
    it downstream. The one reordering is P9's: a dimension that exists only on the grain you
    just switched into leads, because it is the reason you switched.
    """
    scoped = tuple(d for d in DIMENSIONS if d.grain == grain)
    shared = tuple(d for d in DIMENSIONS if not d.grain)
    return scoped + shared


def visible_dimensions_for(grain: str) -> tuple:
    return tuple(d for d in dimensions_for(grain) if not d.hidden)

#: `group` carries this pseudo-value for "is in any group at all", so that excluding it is
#: how you find every channel that is in none.
GROUP_ANY = '__any__'

OTHER_REMOVED = 'removed'
OTHER_NEW = 'new'
#: Guide SCOPE - see `channel_groups.guide_scope_channel_ids()`, which is the definition.
#: Deliberately not `Channel.in_guide`, even now that the column is honest (dev/changelog/751):
#: scope is the wider question, because a member with its own flag off still has its listings
#: on screen through its group's row. The value keeps its `guide` spelling because it is a URL
#: parameter callers link to; what changed is the answer (dev/changelog/734).
OTHER_IN_GUIDE = 'guide'
#: The two halves of `OTHER_IN_GUIDE`, addressable separately because "does this channel hold
#: its own guide row" and "is it only on screen through a group's row" are different questions
#: and the union could not answer either (dev/changelog/791). They OR back to `guide` exactly -
#: `dimension_predicates` ORs the values within one dimension - which is what lets the union
#: keep its wide meaning rather than becoming a third spelling of one of these.
#:
#: `OTHER_GUIDE_OWN_ROW` is the one place a bare `Channel.in_guide` read is the RIGHT answer:
#: the value exists precisely to expose the column's own meaning, not to re-derive scope from
#: it. Everything asking the wider question still goes through guide_scope_channel_ids().
OTHER_GUIDE_OWN_ROW = 'guiderow'
OTHER_GUIDE_VIA_GROUP = 'guidegroup'
OTHER_DUP_URL = 'dupurl'
OTHER_VALUES = (OTHER_REMOVED, OTHER_NEW, OTHER_IN_GUIDE, OTHER_GUIDE_OWN_ROW,
                OTHER_GUIDE_VIA_GROUP, OTHER_DUP_URL)
#: The rail's wording for those four, from the approved mockup. Here rather than in the
#: template because the values are a registry and their labels are part of it - a page that
#: spelled them itself would be a second vocabulary to keep in step.
OTHER_LABELS = {
    OTHER_REMOVED: 'Deleted by provider',
    OTHER_NEW: 'Newly added by provider',
    OTHER_IN_GUIDE: 'In your guide',
    OTHER_GUIDE_OWN_ROW: 'Has its own guide row',
    OTHER_GUIDE_VIA_GROUP: 'In the guide via a group',
    OTHER_DUP_URL: 'Duplicated stream URL',
}


# ---------------------------------------------------------------------------
# `when` - the one dimension whose values carry their own state
# ---------------------------------------------------------------------------
#
# Every other dimension's values are names or ids. Two of these are WINDOWS the user fills
# in, so the window has to live inside the value: that is what keeps them chippable,
# negatable, savable inside a saved search and readable back out of a URL, instead of the two
# loose `datetime-local` inputs the old Extended Search modal had (mockup 25, P5).
#
# The separator inside `custom` is `..`, NOT a third colon: a local wall-clock time contains
# one (`2026-08-01T19:00`), so `custom:<from>:<to>` cannot be split back apart. `next` has no
# such problem and keeps colons.

WHEN_NOW = 'now'
WHEN_TODAY = 'today'
WHEN_TOMORROW = 'tomorrow'
#: `next:<n>:<minutes|hours|days>`
WHEN_NEXT_PREFIX = 'next:'
#: `custom:<from>..<to>`, either side optionally empty (one bound on its own is a real window)
WHEN_CUSTOM_PREFIX = 'custom:'
WHEN_RANGE_SEP = '..'

#: The values with a fixed spelling. The two parametrized prefixes are not in here - there
#: are infinitely many of them - which is why `when` has no closed vocabulary and its facet
#: counts the static three plus whatever windows the current state names.
WHEN_STATIC_VALUES = (WHEN_NOW, WHEN_TODAY, WHEN_TOMORROW)
WHEN_STATIC_LABELS = {
    WHEN_NOW: 'On now',
    WHEN_TODAY: 'Today',
    WHEN_TOMORROW: 'Tomorrow',
}

#: The units `next:` accepts, and what one of them is worth. Minutes/hours/days rather than a
#: fixed list of windows: "next 3 hours" / "tonight" / "next 7 days" were cut in round 3
#: because a fixed list is arbitrary and "tonight" needs a rule about when evening starts,
#: and any such rule is somebody's wrong.
WHEN_UNITS = {'minutes': 'minutes', 'hours': 'hours', 'days': 'days'}

#: A `when` window WIDER than this takes its `start_time` comparisons out of the planner's
#: reach; a narrower one keeps them indexable. See `_start_window()` for the measurements.
#: An hour is where the two costs crossed on 2026-08-01, and the threshold is deliberately at
#: the low end of the crossing because the two mistakes are not symmetric: keeping the index on
#: a window that is too wide costs up to 40s, defeating it on one that is too narrow costs 0.6s.
WHEN_INDEX_DEFEAT_WIDTH = timedelta(hours=1)


def parse_when_next(value: str):
    """`next:<n>:<unit>` -> a timedelta, or None when the value is not a usable window.

    None rather than an error: this is a number the user typed into a box, not a registry
    key, so a half-finished one narrows to nothing and the page says so - it does not 400 the
    search that produced it.
    """
    if not value.startswith(WHEN_NEXT_PREFIX):
        return None
    parts = value[len(WHEN_NEXT_PREFIX):].split(':')
    if len(parts) != 2:
        return None
    raw_n, unit = parts
    if unit not in WHEN_UNITS:
        return None
    try:
        n = int(raw_n)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return timedelta(**{WHEN_UNITS[unit]: n})


def parse_when_custom(value: str, tz=None):
    """`custom:<from>..<to>` -> (from, to) as naive UTC, either side possibly None.

    The two halves are LOCAL wall-clock text, because that is what an `<input
    type="datetime-local">` sends and what the display timezone means to the person reading
    the page (CLAUDE.md Timezone Rules). Returns None when neither side parses - a custom
    range with no bounds is not a window.

    `tz` is passed in by every caller inside a request (it is on SearchContext), because
    resolving it reads config and this is reached once per facet dimension.
    """
    if not value.startswith(WHEN_CUSTOM_PREFIX):
        return None
    body = value[len(WHEN_CUSTOM_PREFIX):]
    if WHEN_RANGE_SEP not in body:
        return None
    raw_from, _, raw_to = body.partition(WHEN_RANGE_SEP)
    start = _parse_local(raw_from, tz)
    stop = _parse_local(raw_to, tz)
    if start is None and stop is None:
        return None
    return start, stop


def _parse_local(raw: str, tz=None):
    from .tz_utils import to_naive_utc, get_display_tz
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz or get_display_tz())
    return to_naive_utc(parsed)


# ---------------------------------------------------------------------------
# `duration` - a program-length bound, the second (and only other) parametrized dimension
# ---------------------------------------------------------------------------
#
# Same shape as `when`'s `custom:` range - a value the page builds from its own controls, not
# a closed vocabulary - but simpler: a duration bound is just a number, not something relative
# to "now", so there is no `next:`-equivalent half to build.

#: `dur:<min>..<max>`, minutes on both sides, either side optionally empty (one bound alone is
#: a real filter: "longer than 30 minutes" is `dur:30..`). Minutes because that is the unit
#: EPGEntry.duration_minutes (migration 36) is expressed in - no unit conversion at the SQL
#: boundary.
DUR_PREFIX = 'dur:'


def parse_duration(value: str):
    """`dur:<min>..<max>` -> (min_minutes, max_minutes) as ints, either possibly None.

    None (whole tuple) rather than an error when neither side parses - same reasoning as
    parse_when_custom: a bound still being typed is not a registry key and must not 400 the
    search that produced it.
    """
    if not value.startswith(DUR_PREFIX):
        return None
    body = value[len(DUR_PREFIX):]
    if WHEN_RANGE_SEP not in body:
        return None
    raw_min, _, raw_max = body.partition(WHEN_RANGE_SEP)

    def _parse_bound(raw):
        raw = (raw or '').strip()
        if not raw:
            return None
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
        return n if n >= 0 else None

    lo, hi = _parse_bound(raw_min), _parse_bound(raw_max)
    if lo is None and hi is None:
        return None
    return lo, hi


# ---------------------------------------------------------------------------
# Standing options
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StandingOption:
    """A preference that survives every search, not a filter picked for one.

    `cluster` marks the one option whose answer depends on the whole set of rows sharing a
    stream URL rather than on the row in front of it, so it is resolved as its own subquery
    rather than as a row predicate.
    """
    key: str
    label: str
    default: bool
    cluster: bool = False
    #: The one grain this option exists on, or '' for both. A control that is a no-op on the
    #: grain you are looking at is worse than not offering it: a channel does not end, and
    #: "one row per channel" is what the channel grain already is.
    grain: str = ''
    #: What the noun in "849 duplicates" is - what this option TOOK OUT, named. The
    #: disclosure line under the search box used to render every option as the same word
    #: ("849 hidden - 752 hidden - 101,916 hidden"), which says something vanished but not
    #: what, and gets worse with every option added (dev/changelog/778).
    noun: str = ''
    #: Does having this key in the standing set mean rows get REMOVED? False for the six
    #: options phrased as "Show ...", where the hide applies when the key is ABSENT.
    #:
    #: `firstonly` and `grpdedup` keep True: they are modes rather than hide switches
    #: ("One row per channel" is a shape you asked for, not an exclusion you tolerated), so
    #: their label already says what ticking them does. Everything reads this through
    #: `standing_applied()` - never test membership directly, or the two families diverge.
    hides_when_on: bool = True


STANDING_OPTIONS = (
    # Phrased as SHOW, not HIDE, and the defaults are inverted to match, so a ticked box
    # always means "put these in my results" (dev/changelog/778). The keys carry the same
    # `show` prefix deliberately: `standing=dup` meant duplicates were HIDDEN, so reusing it
    # here would silently reverse every existing bookmark. A renamed key 400s instead.
    #
    # The first is the one surface that can expose a hidden channel in a list. Everything
    # else built on this engine - the pickers, "+ Add channels", Manage channels - inherits
    # the default and is not given a control to turn it on, which is what "hidden means not
    # offered to you" amounts to in practice (dev/changelog/775).
    StandingOption('showhidden', 'Show hidden channels', False,
                   noun='hidden', hides_when_on=False),
    StandingOption('showdup', 'Show duplicates', False, cluster=True,
                   noun='duplicates', hides_when_on=False),
    StandingOption('shownotnorm', 'Show "not normalized" URLs', False,
                   noun='not normalized', hides_when_on=False),
    StandingOption('shownoepg', 'Show channels with no EPG data', True,
                   noun='no EPG', hides_when_on=False),
    StandingOption('showuntested', 'Show never-tested channels', True,
                   noun='never tested', hides_when_on=False),
    # The fold. Channel grain only, because a group is a row kind on that grain and the
    # airing grain expresses the same idea through `grpdedup` instead
    # (DESIGN-group-search-rows.md §5.1). Default ON, which means the fold is OFF: a channel
    # in a group keeps a row of its own, beside its group's row rather than behind it.
    #
    # It shipped the other way round, mirroring the TV Guide, and the guide was the wrong
    # model to borrow from: this page is where you go to FIND a channel, and a search that
    # silently answers "no such channel" because it is in a group is the hidden behavior
    # this project exists to refuse (dev/changelog/860). The group's own row is unaffected -
    # both are on screen, which is what `guide_via` on the member row explains.
    #
    # Its noun is what came out, named, exactly as every other option's is - "39 folded into
    # groups" when it is turned off, clickable to put them back.
    StandingOption('showmembers', 'Show group members as their own rows', True,
                   grain=GRAIN_CHANNELS, noun='folded into groups', hides_when_on=False),
    # Three that exist only on the airing grain.
    #
    # `showpast` REPLACES the old sync.epg_search_past config key rather than shadowing it
    # (mockup 25 P8): a setting you can toggle from the page it affects has no reason
    # to also live in a file, and two of them would eventually disagree. Two real limits ride
    # with it and belong in the UI tooltip rather than being discovered - history only reaches
    # back sync.epg_keep_days, and chan_prog holds future showings only, so including the past
    # forces the unindexed path.
    StandingOption('showpast', 'Show airings that have ended', False, grain=GRAIN_AIRINGS,
                   noun='ended', hides_when_on=False),
    # The old modal's "Unique channels only". NOT the same as flipping to the channel grain,
    # which is what it looks like: the channel grain can say WHICH channels air something but
    # not WHEN, because chan_prog holds no times by construction.
    StandingOption('firstonly', 'One row per channel', False, cluster=True,
                   grain=GRAIN_AIRINGS, noun='repeat showings'),
    # What routes/guide.py::guide_search did unconditionally. Carried over as a switchable,
    # disclosed option instead: group members carry near-identical EPG, so without it one
    # program becomes one row per member.
    StandingOption('grpdedup', 'Collapse channel groups', True, cluster=True,
                   grain=GRAIN_AIRINGS, noun='group duplicates'),
)
STANDING_BY_KEY = {s.key: s for s in STANDING_OPTIONS}
DEFAULT_STANDING = frozenset(
    s.key for s in STANDING_OPTIONS if s.default and _in_grain(s, GRAIN_CHANNELS))


def standing_options_for(grain: str) -> tuple:
    return tuple(s for s in STANDING_OPTIONS if _in_grain(s, grain))


def default_standing_for(grain: str) -> frozenset:
    return frozenset(s.key for s in standing_options_for(grain) if s.default)


def standing_applied(standing, key: str) -> bool:
    """Is this option actually REMOVING rows from a search whose standing set is `standing`?

    The one reader of the rule, because two families share one set: a `show*` key removes
    rows when it is ABSENT, and `firstonly`/`grpdedup` when they are PRESENT. Testing
    membership directly gets one of the two backwards, and gets it backwards silently -
    the query still runs, it just answers a different question (dev/changelog/778).

    Takes the set rather than a `SearchState` so the four narrowing-decision sites can ask
    it without building one.
    """
    opt = STANDING_BY_KEY.get(key)
    if opt is None:
        raise SearchStateError(f'unknown standing option {key!r}')
    return (key in standing) == opt.hides_when_on


# ---------------------------------------------------------------------------
# Sorts
# ---------------------------------------------------------------------------

# Only what SQL can order the whole 136,130-row result set by. Paging is server-side, so a
# sort that cannot be expressed here cannot be honoured at all - sorting the current page
# would silently reorder 100 rows out of thousands and call it a sort.
#
# Four columns the approved mockup makes sortable are deliberately NOT here, and phase B has
# to render their headers unsorted rather than guess:
#   airing  - "now airing" is a per-channel correlated lookup into epg_entries; ordering
#             136k rows by it is a scan per row
#   status  - the lifecycle state is derived from per-account sync aggregates
#             (accounts.channel_lifecycle_state), not a column
#   groups / tags - multi-valued; "first group name" is not a well-defined order
# Adding any of them is real work with a measurement attached, not a registry entry.
SORTS = {
    'name': lambda: [func.lower(Channel.name)],
    'category': lambda: [Channel.category_name, func.lower(Channel.name)],
    'health': lambda: [effective_health()],
    'account': lambda: [Channel.account_id, func.lower(Channel.name)],
    'sid': lambda: [Channel.stream_id],
    'tvg': lambda: [Channel.epg_channel_id],
    'url': lambda: [Channel.stream_url],
}
# Channel name, because that is what a person browsing their channels is looking for - and
# it is the pinned first column, so the default order is the one the eye is already reading
# down. Backed by ix_channels_lower_name, whose whole reason to exist is that this sort is
# now the landing page: unindexed it cost 105.5ms per first paint against 9.0ms with it
# (dev/changelog/699). Changing this key means checking that the new one has an index that
# matches its expression, or the default page goes back to sorting 138k rows per request.
DEFAULT_SORT = 'name'

# The airing grain's sorts. The overlap is large and the gap is small, which is what makes
# flipping grain mostly lossless: six keys mean the same thing on both sides - `health`,
# `account`, `category`, `sid`, `tvg`, `url` - because they all describe THE CHANNEL, and an
# airing is on a channel. Only three do not cross: `name` orders channels and has no meaning
# for a row that is a program, while `title` and `when` order programs and have none for a row
# that is a channel. The airing query joins Channel, so a channel column is orderable here
# without a correlated lookup.
#
# `airing`, `status`, `groups` and `tags` are absent from BOTH registries for the reasons
# above, and `desc` is absent here because a paragraph is not an order.
SORTS_AIRINGS = {
    'title': lambda: [func.lower(EPGEntry.title)],
    'when': lambda: [EPGEntry.start_time],
    'channel': lambda: [func.lower(Channel.name), EPGEntry.start_time],
    'health': lambda: [effective_health(), EPGEntry.start_time],
    'account': lambda: [Channel.account_id, EPGEntry.start_time],
    'category': lambda: [Channel.category_name, EPGEntry.start_time],
    'sid': lambda: [Channel.stream_id, EPGEntry.start_time],
    'tvg': lambda: [Channel.epg_channel_id, EPGEntry.start_time],
    'url': lambda: [Channel.stream_url, EPGEntry.start_time],
}
SORTS_BY_GRAIN = {GRAIN_CHANNELS: SORTS, GRAIN_AIRINGS: SORTS_AIRINGS}
DEFAULT_SORT_BY_GRAIN = {GRAIN_CHANNELS: DEFAULT_SORT, GRAIN_AIRINGS: 'when'}
MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# The state
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DimensionFilter:
    """One dimension's three-state selection. `values` is the "is" side, `ex` the "is not"."""
    key: str
    values: tuple = ()
    ex: tuple = ()


@dataclass(frozen=True)
class SearchState:
    """Everything a search is, in one serializable value.

    This is the API. Once other pages link into this search, `to_params()` is what they
    build their links out of and `from_params()` is what the endpoint parses - so a change
    to the parameter names is a breaking change to every entry point, not an internal
    rename. The full contract is written up in dev/docs/DESIGN-channel-search.md.

    Frozen because the facet counter needs to ask "what would this state be without its own
    filter" for six dimensions in a row; `replace()` on a frozen value cannot leak a mutation
    back into the caller's state the way editing a dict in place would.
    """
    q: str = ''
    match_all: bool = True
    filters: tuple = ()
    #: All three default PER GRAIN, so none can be a plain dataclass default - `None`,
    #: `None` and `''` mean "whatever this grain's default is" and are resolved in
    #: __post_init__. Spelling them as the channel grain's values here would hand a
    #: hand-built airing state a sort that grain does not have, and a scope of the channel's
    #: own name on a list whose rows are programs.
    #:
    #: `fields` draws the same absent-vs-empty distinction `standing` does, and for a
    #: sharper reason: `None` is "this grain's default scope", `()` is "search NOTHING",
    #: which matches nothing at all. Collapsing the two would turn every-field-switched-off
    #: into a search that quietly matched on the name instead - the search ignoring what was
    #: typed, which is the one outcome the empty scope exists to make visible.
    fields: tuple | None = None
    standing: frozenset | None = None
    sort: str = ''
    sort_desc: bool = False
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    grain: str = GRAIN_CHANNELS
    #: Which dimensions to count. None means "every visible one"; see TAG FACET in
    #: compute_facets() for why a caller would ever ask for fewer.
    facets: tuple | None = None
    #: The action context ("you came here to add channels to group fox"). Carried through
    #: because it is the one piece of arriving state that stays fixed rather than rendering
    #: as a removable chip - the user arrived specifically to do it. Everything else that
    #: arrives pre-applied is an ordinary, removable chip.
    add_to_group: int | None = None
    #: The second action context: "you came here to replace SCHEDULED recording <id>". Same
    #: kind of value as `add_to_group` and held to the same rules - it narrows NOTHING, it
    #: changes what a row offers to do, and it is dismissable. Set by the recording detail
    #: page's "Find another airing" on a SCHEDULED recording; recording a showing while it is
    #: set also deletes the recording named here (dev/changelog/416).
    replace_rec: int | None = None

    def __post_init__(self):
        # object.__setattr__ because the value is frozen - the alternative is every caller
        # remembering to pass the right per-grain default, which is the kind of thing one
        # caller always forgets.
        if self.fields is None:
            object.__setattr__(self, 'fields', default_fields_for(self.grain))
        if self.standing is None:
            object.__setattr__(self, 'standing', default_standing_for(self.grain))
        if not self.sort:
            # .get, not [], so an unknown grain in a hand-built state reaches search()'s
            # SearchStateError rather than dying here as a KeyError.
            object.__setattr__(
                self, 'sort', DEFAULT_SORT_BY_GRAIN.get(self.grain, DEFAULT_SORT))

    def filter_for(self, key: str) -> DimensionFilter | None:
        for f in self.filters:
            if f.key == key:
                return f
        return None

    def without_dimension(self, key: str) -> 'SearchState':
        return replace(self, filters=tuple(f for f in self.filters if f.key != key))

    # -- serialization ----------------------------------------------------

    @classmethod
    def from_params(cls, params) -> 'SearchState':
        """Parse a request's query parameters. `params` is a werkzeug MultiDict or any
        mapping exposing `getlist`.

        Multi-valued parameters are repeated (`f.cat=News&f.cat=Sports`), never
        comma-separated: category, tag and group names are user/provider text and a comma in
        one of them would silently split a value into two that match nothing.

        Unknown keys raise. That is the point of a registry - a typo in a link is a 400 the
        author sees, not an empty result they debug for an hour.
        """
        getlist = getattr(params, 'getlist', None)
        if getlist is None:
            def getlist(key):
                value = params.get(key)
                if value is None:
                    return []
                return list(value) if isinstance(value, (list, tuple)) else [value]

        def one(key, default=None):
            values = getlist(key)
            return values[-1] if values else default

        grain = one('grain', GRAIN_CHANNELS)
        if grain not in IMPLEMENTED_GRAINS:
            raise SearchStateError(
                f'unknown result grain {grain!r} - this search returns '
                f'{", ".join(IMPLEMENTED_GRAINS)}')

        # Absent (or empty) means this grain's own default scope, resolved in __post_init__
        # rather than here - `in=` cannot mean "search nothing", so the two cases the
        # `standing` parameter has to keep apart do not arise for this one.
        fields = tuple(getlist('in')) or default_fields_for(grain)
        for key in fields:
            if key not in FIELD_BY_KEY:
                raise SearchStateError(f'unknown search field {key!r}')

        filters = []
        for dim in DIMENSIONS:
            values = tuple(getlist(f'f.{dim.key}'))
            ex = tuple(getlist(f'x.{dim.key}'))
            if values or ex:
                filters.append(DimensionFilter(dim.key, values, ex))
        for key in params.keys():
            if (key.startswith('f.') or key.startswith('x.')) \
                    and key[2:] not in DIMENSION_BY_KEY:
                raise SearchStateError(f'unknown filter dimension {key[2:]!r}')

        # Absent means "the defaults"; present-but-empty (`standing=`) means "none of them".
        # A default-on option cannot be turned off any other way, so the two cases have to
        # be distinguishable.
        raw_standing = getlist('standing')
        if not raw_standing:
            standing = default_standing_for(grain)
        else:
            standing = frozenset(k for k in raw_standing if k)
            for key in standing:
                if key not in STANDING_BY_KEY:
                    raise SearchStateError(f'unknown standing option {key!r}')

        # Strict, unlike an out-of-grain FILTER. A filter the target grain cannot express is
        # parked and comes back when you flip back; a sort it cannot express has no such
        # harmless resting state - honouring it would mean ordering by something else and
        # calling it the requested sort. The page remaps to the target's default and says so;
        # it never generates one of these.
        sorts = SORTS_BY_GRAIN[grain]
        default_sort = DEFAULT_SORT_BY_GRAIN[grain]
        sort = one('sort', default_sort) or default_sort
        sort_desc = sort.startswith('-')
        sort = sort.lstrip('-')
        if sort not in sorts:
            raise SearchStateError(
                f'cannot sort {grain} by {sort!r} - sortable: {", ".join(sorted(sorts))}')

        # Absent means "count every visible dimension"; present-but-empty (`facets=`) means
        # "count none of them" - the rows-only fetch the page uses while the user is typing
        # (46ms against 667ms with the rail attached, measured 2026-07-30). Same
        # absent-vs-empty distinction as `standing` above, and for the same reason: "none"
        # is not expressible any other way.
        raw_facets = getlist('facets')
        facets = tuple(k for k in raw_facets if k) if raw_facets else None
        if facets is not None:
            for key in facets:
                if key not in DIMENSION_BY_KEY:
                    raise SearchStateError(f'unknown facet dimension {key!r}')

        return cls(
            q=(one('q', '') or '').strip(),
            fields=fields,
            match_all=(one('match', 'all') or 'all') != 'any',
            filters=tuple(filters),
            standing=standing,
            sort=sort,
            sort_desc=sort_desc,
            page=_positive_int(one('page'), 1),
            page_size=min(_positive_int(one('per_page'), DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE),
            grain=grain,
            facets=facets,
            add_to_group=_optional_int(one('add_to_group')),
            replace_rec=_optional_int(one('replace_rec')),
        )

    def to_params(self) -> list:
        """(key, value) pairs, repeated for multi-valued keys - what a link into this search
        is built from. Round-trips through from_params(); anything at its default is left
        out, so a plain link stays short."""
        out = []
        if self.q:
            out.append(('q', self.q))
        if tuple(self.fields) != default_fields_for(self.grain):
            out.extend(('in', k) for k in self.fields)
        if not self.match_all:
            out.append(('match', 'any'))
        for f in self.filters:
            out.extend((f'f.{f.key}', v) for v in f.values)
            out.extend((f'x.{f.key}', v) for v in f.ex)
        if self.standing != default_standing_for(self.grain):
            # An empty standing set still has to be written, or from_params reads its absence
            # as "use the defaults" and turns two options back on. One empty value says
            # "present, and none of them".
            out.extend(('standing', k) for k in sorted(self.standing))
            if not self.standing:
                out.append(('standing', ''))
        if self.sort != DEFAULT_SORT_BY_GRAIN[self.grain] or self.sort_desc:
            out.append(('sort', ('-' if self.sort_desc else '') + self.sort))
        if self.page != 1:
            out.append(('page', str(self.page)))
        if self.page_size != DEFAULT_PAGE_SIZE:
            out.append(('per_page', str(self.page_size)))
        if self.grain != GRAIN_CHANNELS:
            out.append(('grain', self.grain))
        if self.facets is not None:
            # An empty facet tuple still has to be written, or from_params reads its absence
            # as "count every dimension" - the same trap the empty standing set has above.
            out.extend(('facets', k) for k in self.facets)
            if not self.facets:
                out.append(('facets', ''))
        if self.add_to_group is not None:
            out.append(('add_to_group', str(self.add_to_group)))
        if self.replace_rec is not None:
            out.append(('replace_rec', str(self.replace_rec)))
        return out


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _optional_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The typed query
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Term:
    """One typed word or "quoted phrase". `exclude` is the `-word` form."""
    text: str
    exclude: bool = False

    @property
    def wildcard(self) -> bool:
        return has_wildcards(self.text)


def parse_terms(raw: str) -> tuple:
    """Split a typed query into terms: bare words, "quoted phrases", and -exclusions.

    A leading `-` on a non-empty body excludes; quotes group a phrase containing spaces and
    are stripped from the term itself. Everything else - including FTS5's own operators and
    a stray `:` - is literal text, because the user is describing a substring to find, not
    writing a boolean expression.
    """
    words, current, quoted = [], [], False
    for char in raw:
        if char == '"':
            quoted = not quoted
        elif char.isspace() and not quoted:
            if current:
                words.append(''.join(current))
                current = []
        else:
            current.append(char)
    if current:
        words.append(''.join(current))

    terms = []
    for word in words:
        exclude = word.startswith('-') and len(word) > 1
        body = word[1:] if exclude else word
        if body:
            terms.append(Term(body, exclude))
    return tuple(terms)


# ---------------------------------------------------------------------------
# Matching one term
# ---------------------------------------------------------------------------

#: Moved into search_index.py (dev/changelog/412) so the airing planner and this share one
#: spelling of "MATCH, safely, with a bind name that cannot collide". Local alias only.
_fts_rowid_select = fts_rowid_select


def _column_like(field: SearchField, term: Term):
    """The base-table predicate for one field and one term - the un-indexed path.

    **The `IS NOT NULL` guard is what makes an EXCLUDED term correct**, and it is not
    cosmetic. `NULL LIKE x` is NULL, so on a nullable column (`sub_title`, `description`) the
    OR across fields evaluates to NULL rather than FALSE, `not_()` of NULL is NULL, and SQLite
    drops the row - meaning `-word` silently hides every showing that simply has no subtitle.
    Forcing the miss to FALSE answers both directions with one expression. Same defect the old
    /api/guide/search route was fixed for in its own way; see BUGS.md 2026-07-31 08:44 PM.
    """
    col = cast(field.column, String) if field.cast_text else field.column
    if term.wildcard:
        pattern, needs_escape = glob_to_like(term.text)
        like = col.like(pattern, escape='\\') if needs_escape else col.like(pattern)
    else:
        like = col.ilike(f'%{term.text}%')
    return and_(col.isnot(None), like)


def _prefilter_ids(fts_table: str, columns: tuple, term: Term):
    """Rowids the trigram index says could match a wildcard term, or None for "no help".

    Every string a glob matches contains each of the glob's literal runs, so MATCHing all of
    them is a proven superset of the glob's own answer. That makes it a safe narrowing pass:
    the caller still applies the real pattern to the base table, and this only decides how
    many rows it has to apply it to. Without it a wildcard against program descriptions is
    a 341,731-row scan (measured ~500ms); with it, a single-digit number of rows.
    """
    runs = literal_runs(term.text)
    if not runs:
        return None
    scope = '{' + ' '.join(columns) + '} : '
    expr = ' AND '.join(scope + fts_match_term(run) for run in runs)
    return _fts_rowid_select(fts_table, expr)


def _channel_side(term: Term, fields: tuple, indexed: bool):
    """Predicate on Channel for the channel-side fields, or None when none are active."""
    if not fields:
        return None
    clauses = []
    fts_fields = [f for f in fields if f.fts_column] if indexed else []
    plain_fields = [f for f in fields if f not in fts_fields]

    if fts_fields and not term.wildcard and len(term.text) >= TRIGRAM_MIN_CHARS:
        scope = '{' + ' '.join(f.fts_column for f in fts_fields) + '} : '
        clauses.append(Channel.id.in_(
            _fts_rowid_select('ch_fts', scope + fts_match_term(term.text))))
    else:
        plain_fields = list(fields)

    if plain_fields:
        like = or_(*[_column_like(f, term) for f in plain_fields])
        prefilter = None
        if indexed and term.wildcard:
            indexable = tuple(f.fts_column for f in plain_fields if f.fts_column)
            if indexable and len(indexable) == len(plain_fields):
                prefilter = _prefilter_ids('ch_fts', indexable, term)
        if prefilter is not None:
            clauses.append(and_(Channel.id.in_(prefilter), like))
        else:
            clauses.append(like)
    return or_(*clauses) if len(clauses) > 1 else clauses[0]


def _program_side(term: Term, fields: tuple, indexed: bool):
    """Predicate on Channel for the EPG-side fields ("airs something matching"), or None.

    "Ever airs", over chan_prog's future-only window. Since dev/changelog/861 this is NOT what
    a typed query means on the channel grain (`_now_program_side`), and since dev/changelog/862
    it is not what a tag means there either. What is left are the two places where "ever" is
    still the question being asked: the airing grain's tag filter, whose shared prefilter is
    built from this (`_cached_tag_channel_ids`), and the tag badge on an airing row, which
    describes that row's channel rather than the instant (`tag_hit_predicate(now_scoped=False)`).
    """
    if not fields:
        return None
    columns = tuple(f.fts_column for f in fields)

    if indexed and not term.wildcard and len(term.text) >= TRIGRAM_MIN_CHARS:
        scope = '{' + ' '.join(columns) + '} : '
        airing = (select(_PROG.c.channel_id)
                  .where(_PROG.c.id.in_(
                      _fts_rowid_select('chan_prog_fts', scope + fts_match_term(term.text)))))
        return Channel.id.in_(airing)

    if indexed:
        # Wildcard or too-short term, but the index is still trustworthy, so chan_prog is a
        # sound place to scan - 341,731 deduped rows instead of 999,914 raw airings.
        like = or_(*[_column_like(f, term) for f in fields])
        prefilter = _prefilter_ids('chan_prog_fts', columns, term) if term.wildcard else None
        where = and_(_PROG.c.id.in_(prefilter), like) if prefilter is not None else like
        return Channel.id.in_(select(_PROG.c.channel_id).where(where))

    # No usable index: chan_prog may be empty or half-built, so go straight to the source.
    # Same future-only window chan_prog is built with, so the two agree on results.
    by_key = {'title': EPGEntry.title, 'sub_title': EPGEntry.sub_title,
              'description': EPGEntry.description}
    entry_fields = tuple(
        replace(f, column=by_key[f.fts_column], cast_text=False) for f in fields)
    like = or_(*[_column_like(f, term) for f in entry_fields])
    return Channel.id.in_(
        select(EPGEntry.channel_id).where(EPGEntry.stop_time >= datetime.utcnow(), like))


def _now_program_side(term: Term, fields: tuple, ctx: 'SearchContext'):
    """The EPG-side predicate on the CHANNEL grain: does what is on RIGHT NOW match?

    A channel row carries exactly one program - the `Now airing` column - so matching it on
    any of the ~15 showings in its next three days puts rows on screen that visibly do not
    contain the word that was typed, and the "why" chip then has to name a program the row is
    not showing - *"it matching on future airings that don't match the Now airing field is
    confusing"*. So the channel grain asks about the current showing and
    the airing grain asks about every showing, which is what the grain switch is FOR.

    **chan_prog cannot answer this and is therefore not used at all here.** It is deduped
    across showings, so it holds no per-airing times to compare against a clock - the same
    reason `channel_search_rows._now_airing()` reads epg_entries. That makes this the one
    program-side predicate whose correctness does not depend on a search index, and it is
    also why it is not slower for losing one: the now-window is 35,201 rows out of 2,078,375,
    which is far more selective than any trigram prefilter over the 341,731 deduped ones.

    **A resolved id list, not a subquery** - `SearchContext.now_program_channel_ids()` runs it
    once per request and every caller here shares that answer. The whole reason is in that
    method's docstring; the short version is that one request asks this question about ten
    times and the overlap scan costs the same each time.

    The context is also what supplies the clock, so the row query, the facet counts and the
    "why" chip resolve "now" to the same instant - a row that arrived because of the 8pm
    showing must not lose its chip to the 9pm one starting mid-request.
    """
    if not fields:
        return None
    return Channel.id.in_(ctx.now_program_channel_ids(term, fields))


def _airing_program_side(term: Term, fields: tuple, narrow: bool):
    """The EPG-side predicate on the AIRING grain: does THIS showing match?

    The semantic difference between the two grains, in one function. `_program_side` asks
    "does this channel air anything matching" and answers it off the deduped chan_prog index;
    this asks about the row in front of it, so the predicate is always a LIKE on the
    `epg_entries` row itself and the index can only ever NARROW which rows that LIKE is
    applied to.

    That asymmetry is what makes the planner safe: turn `narrow` off and the answer is
    identical, just slower.
    """
    if not fields:
        return None
    by_key = {'title': EPGEntry.title, 'sub_title': EPGEntry.sub_title,
              'description': EPGEntry.description}
    entry_fields = tuple(
        replace(f, column=by_key[f.fts_column], cast_text=False) for f in fields)
    like = or_(*[_column_like(f, term) for f in entry_fields])
    if not narrow:
        return like
    columns = tuple(f.fts_column for f in fields)
    return and_(EPGEntry.channel_id.in_(airing_narrowing_channel_ids(term.text, columns)),
                like)


def _term_predicate(term: Term, fields: tuple, indexed: bool,
                    grain: str = GRAIN_CHANNELS, narrow: bool = False,
                    ctx: 'SearchContext' = None):
    """"Does this row match this term, on any active field" - the OR across fields.

    The channel side is identical on both grains: a showing's channel is still the thing that
    has a name, a category and a stream id. The program side is where the grains part company:
    this showing on one, what is on right now on the other.

    `ctx` is the request's context and carries both the clock and the per-request memo the
    channel grain's program side is resolved through. It defaults to a fresh one rather than
    raising so a caller that forgot it gets an answer that is correct but unshared, not a 500
    on a search page.
    """
    if grain == GRAIN_AIRINGS:
        program = _airing_program_side(
            term, tuple(f for f in fields if f.source == SOURCE_PROGRAM), narrow)
    else:
        program = _now_program_side(
            term, tuple(f for f in fields if f.source == SOURCE_PROGRAM),
            ctx if ctx is not None else SearchContext())
    sides = [
        _channel_side(term, tuple(f for f in fields if f.source == SOURCE_CHANNEL), indexed),
        program,
    ]
    sides = [s for s in sides if s is not None]
    if not sides:
        # Every field switched off. Matching nothing is the honest answer; matching
        # everything would look like the search silently ignored what was typed.
        return db.false()
    return or_(*sides) if len(sides) > 1 else sides[0]


def _airing_narrowing_conjuncts(include: list, fields: tuple, indexed: bool,
                                match_all: bool) -> list:
    """The drivable half of the airing narrowing: `channel_id IN (...)` as a top-level AND.

    `_term_predicate` can only put the narrowing *inside* the OR with the channel side, and
    an OR spanning two tables is undrivable by any index - so a term matching 36 of 1.48M
    rows planned as a walk of every future `epg_entries` row and cost the same as a
    stopword. ANDing in the union of the OR's two arms restores the index: `A OR (N AND L)`
    implies `channel_id IN (A's channels UNION N)`, so this conjunct removes no row the OR
    keeps, and it hands the planner `ix_epg_entries_channel_stop` to drive. Measured
    2026-08-16 on 1,476,074 rows: `wembley` 5,699ms -> 71ms, `liverpool` 3,488 -> 143.

    Redundant by construction, which is the whole safety argument - the OR above stays the
    row-level truth and this only decides how many rows it is applied to. The channel arm is
    built through `_channel_side` rather than reaching for `ch_fts` directly because that
    function can legitimately emit a plain LIKE (the `sid` field has no `fts_column`), and
    the arm has to stay an exact superset whichever shape it took.

    Only ever called when `airing_narrowing_decision()` said yes, so all five of its
    correctness refusals - and the cost gate - govern this too.
    """
    chan_fields = tuple(f for f in fields if f.source == SOURCE_CHANNEL)
    prog_cols = tuple(f.fts_column for f in fields if f.source == SOURCE_PROGRAM)
    if not chan_fields:
        # No channel-side arm means `_term_predicate` emitted no OR at all, so its own
        # `channel_id IN (...)` is already a top-level conjunct and already drivable.
        # Restating it here would just make the planner run the subquery twice - measured
        # as a real regression (`football` over program fields alone, 348ms -> 476ms).
        return []

    def ids_for(term):
        out = []
        side = _channel_side(term, chan_fields, indexed)
        if side is not None:
            out.append(select(Channel.id).where(side))
        if prog_cols:
            out.append(airing_narrowing_channel_ids(term.text, prog_cols))
        return out

    def conjunct(selects):
        if not selects:
            return None
        return EPGEntry.channel_id.in_(
            selects[0] if len(selects) == 1 else union(*selects))

    if match_all:
        # Every term has to hold, so each term's own union is a valid narrowing by itself
        # and ANDing all of them leaves the planner free to drive the most selective one.
        return [c for c in (conjunct(ids_for(t)) for t in include) if c is not None]
    # Match-any keeps a row satisfying ANY term, so only the union across every term is
    # implied. A per-term conjunct here would drop rows the OR keeps.
    one = conjunct([s for t in include for s in ids_for(t)])
    return [one] if one is not None else []


def airing_narrowing_decision(state: 'SearchState', ctx: 'SearchContext') -> tuple:
    """(narrow?, why not) - the airing planner, decided once per request.

    **Five things force the unindexed path, and all five are correctness rather than tuning.**
    The sixth is cost - "this term is too common for narrowing to pay" - and it is real: past
    `AIRING_PROBE_MAX_ROWS` matching `chan_prog` rows the narrowing narrows to most of the
    table and only adds a sort. See the long comment above that constant for the numbers.

    * **The `past` option is off.** `chan_prog` holds FUTURE showings only, so narrowing by it
      would silently drop every past match - the exact case the user turned the option off to
      see.
    * **A wildcard term**, which the trigram index cannot answer directly (it narrows by the
      term's literal runs on the channel grain; here the LIKE already runs on the base table,
      so there is nothing left for it to buy).
    * **A term under TRIGRAM_MIN_CHARS**, where an FTS MATCH returns zero rows rather than
      erroring - trusting it would return nothing at all.
    * **No positive terms**, e.g. a query that is only exclusions: there is nothing to narrow
      *to*, and narrowing on an excluded term would invert the result.
    * **The index is not ready**, per the same `search_index_readiness()` gate every other
      read rides. A stale index must never silently serve wrong results.

    Anything else narrows.
    """
    if not standing_applied(state.standing, 'showpast'):
        return False, 'the past is included and chan_prog holds future showings only'
    fields = tuple(FIELD_BY_KEY[k] for k in state.fields if k in FIELD_BY_KEY)
    prog_fields = tuple(f for f in fields if f.source == SOURCE_PROGRAM)
    if not prog_fields:
        return False, ''
    ready, reason = ctx.readiness_for(fields)
    if not ready:
        return False, reason
    include = [t for t in parse_terms(state.q) if not t.exclude]
    if not include:
        return False, ''
    for term in include:
        if term.wildcard:
            return False, 'a wildcard term runs against the base table'
        if len(term.text) < TRIGRAM_MIN_CHARS:
            return False, (f'a term under {TRIGRAM_MIN_CHARS} characters is invisible to the '
                           'trigram index')
    if AIRING_PROBE_MAX_ROWS is not None:
        # The cheapest term decides. Under match-all every term must hold, so narrowing on
        # the rarest one already cuts the scan to its size; under match-any the union is at
        # most the sum, so the commonest one bounds it.
        columns = tuple(f.fts_column for f in prog_fields)
        counts = [ctx.probe(t.text, columns) for t in include]
        decisive = min(counts) if state.match_all else max(counts)
        if decisive >= AIRING_PROBE_MAX_ROWS:
            return False, ''
    return True, ''


def text_predicates(state: SearchState, ctx: 'SearchContext') -> list:
    """The predicates for what the user typed. Empty list for an empty query.

    Terms AND together under "match all" and OR together under "match any"; an excluded term
    is always an AND NOT, whichever mode is on, because "not this" is not a thing you want
    ORed into a wider result.

    Readiness comes off the context, which evaluated it once for the request. That matters
    beyond tidiness: this is called once for the row query and once per facet dimension, and
    each of those would otherwise be two more index lookups for a fact that cannot change
    mid-request. The airing planner's probe is memoized on the context for the same reason.
    """
    terms = parse_terms(state.q)
    if not terms:
        return []
    fields = tuple(FIELD_BY_KEY[k] for k in state.fields if k in FIELD_BY_KEY)
    indexed, reason = ctx.readiness_for(fields)
    if not indexed:
        log.info('Channel search %r using unindexed scan (slower): %s',
                 state.q, reason)

    narrow = False
    if state.grain == GRAIN_AIRINGS:
        narrow, why_not = airing_narrowing_decision(state, ctx)
        if not narrow and why_not:
            log.info('Airing search %r using the unindexed scan (slower): %s',
                     state.q, why_not)

    include = [t for t in terms if not t.exclude]
    exclude = [t for t in terms if t.exclude]

    def one(term):
        return _term_predicate(term, fields, indexed, state.grain, narrow, ctx)

    out = []
    if include:
        parts = [one(t) for t in include]
        if state.match_all:
            out.extend(parts)
        else:
            out.append(or_(*parts))
        if narrow:
            # The narrowing inside `one()` cannot drive an index from inside an OR; this is
            # the same narrowing restated as a top-level conjunct, which can.
            out.extend(_airing_narrowing_conjuncts(
                include, fields, indexed, state.match_all))
    # An EXCLUDED term is never narrowed: `AND NOT (narrowing AND like)` is not the same
    # question as `AND NOT like`, and getting it wrong keeps rows the user asked to remove.
    out.extend(not_(_term_predicate(t, fields, indexed, state.grain, False, ctx))
               for t in exclude)
    return out


def _index_names(fields: tuple) -> tuple:
    if any(f.source == SOURCE_PROGRAM for f in fields):
        return SEARCH_INDEX_NAMES
    return (SEARCH_INDEX_CHANNELS,)


# ---------------------------------------------------------------------------
# Dimensions -> SQL
# ---------------------------------------------------------------------------

#: The fields a tag is matched against: the channel's own name, plus every program column.
#: Matching what routes/guide.py::_matched_tags matches (title, sub-title, description), so
#: a tag means the same thing here as it does in the guide.
_TAG_FIELDS = tuple(FIELD_BY_KEY[k] for k in ('name', 'epg-title', 'epg-sub', 'epg-desc'))


#: Per-tag memoized "which channels carry this tag via something they air" - the "airs
#: something matching" half of _tag_predicate, and the one whose live cost scales with how
#: common the tag's patterns are rather than with the rest of the search (a bare-word pattern
#: like `live`/`new` can make the query planner pick it as the driving predicate over far more
#: selective filters - dev/changelog/597, BUGS.md 2026-08-07). Keyed by tag id; each entry
#: also carries the exact pattern tuple and the programs index's own watermark it was built
#: against, so an edited tag or a landed EPG sync is a cache MISS rather than a stale hit - no
#: explicit invalidation call needed anywhere a tag's patterns can change. Reset between tests
#: via tests/support/app.py::reset_module_globals.
_tag_channel_ids_cache: dict = {}
_tag_channel_ids_lock = threading.Lock()


#: The now-scoped half of a channel-grain tag: `{vocabulary: (expiry, {tag id: channel ids})}`,
#: where one entry answers the WHOLE tag vocabulary it is keyed on. Deliberately a different
#: cache from `_tag_channel_ids_cache` above rather than a second entry in it, because the two
#: are invalidated by different things: "ever airs" stops being true when a sync lands, which
#: `source_watermark` records, while "airing now" stops being true when a program ends, which
#: no column records at all. There is no watermark to key this on, so it expires on time and
#: the staleness it admits is bounded and named rather than unbounded and silent.
#:
#: Keyed rather than a single slot because a caller holding a partial vocabulary (a bare
#: `SearchContext`, which knows of no tags) would otherwise evict the full answer on every
#: request and turn the cache into a guaranteed miss. Entries are few - one per distinct
#: vocabulary, which changes only when a tag is edited - and expired ones are pruned on write.
_now_tag_channel_ids_cache: dict = {}
_now_tag_lock = threading.Lock()

#: How long a now-scoped tag set is served before it is recomputed. A tag's membership only
#: moves when a program starts or ends, so a minute of lag is not visible beside a `Now airing`
#: column that is itself a snapshot taken when the page was requested. The alternative -
#: recomputing per request - costs ~0.45s on `channels no-q rows`, which is 171ms today
#: (measured, dev/changelog/862), and that is the most common request on the page.
NOW_TAG_TTL_SECONDS = 60


def clear_tag_channel_ids_cache():
    """Drop every cached tag/channel-id set, both the "ever airs" sets and the now-scoped ones.

    Called when `search.tag_id_cache_enabled` is turned off, so the RAM is actually freed
    rather than just frozen going forward.
    """
    with _tag_channel_ids_lock:
        _tag_channel_ids_cache.clear()
    with _now_tag_lock:
        _now_tag_channel_ids_cache.clear()


def evict_tag_channel_ids(tag_id):
    """Drop one tag's cached channel-id set - a dead tag id would otherwise sit in the
    dict forever after the tag is deleted (harmless, but pointless to keep)."""
    with _tag_channel_ids_lock:
        _tag_channel_ids_cache.pop(tag_id, None)


def _cached_tag_channel_ids(tag: Tag, patterns: list, cfg: dict) -> frozenset | None:
    """The channel ids carrying this tag via something they air, memoized.

    Only called once the caller already knows the search index is ready - None here means
    the cache itself is switched off (`search.tag_id_cache_enabled: false`), and the caller
    falls back to computing this live, exactly as it did before this cache existed.

    `cfg` is the caller's own `SearchContext.cfg` - read once per request there, same as
    every other config-driven predicate in this file - rather than a fresh `load_config()`
    call here, which would be exactly the per-call config read CLAUDE.md's "no hidden I/O in
    per-row loops" rule exists to catch (this runs once per tag per request, not per row, but
    the discipline is the same: read it where the request-scoped context already reads it).

    Cheap to build: measured 7-64ms per pattern against the production database, because it
    reuses the same indexed chan_prog_fts lookup `_program_side` already does for its fast
    path rather than re-deriving the logic - this just executes and remembers the result
    instead of leaving it as a live subquery embedded in the final query every time.
    """
    if not (cfg or {}).get('search', {}).get('tag_id_cache_enabled', True):
        return None

    key = tuple(patterns)
    watermark = source_watermark(SEARCH_INDEX_PROGRAMS)
    with _tag_channel_ids_lock:
        cached = _tag_channel_ids_cache.get(tag.id)
        if cached is not None and cached[0] == key and cached[1] == watermark:
            return cached[2]

    program_fields = tuple(f for f in _TAG_FIELDS if f.source == SOURCE_PROGRAM)
    ids = set()
    for p in patterns:
        pred = _program_side(Term(p), program_fields, True)
        ids.update(db.session.execute(select(Channel.id).where(pred)).scalars())
    result = frozenset(ids)

    with _tag_channel_ids_lock:
        _tag_channel_ids_cache[tag.id] = (key, watermark, result)
    return result


def _scan_now_tag_channel_ids(vocabulary: tuple, now: datetime) -> dict:
    """`{tag id: frozenset(channel ids)}` - who is airing something carrying each tag at `now`,
    in ONE pass over the now-window.

    The flags-per-tag shape is `SearchContext.now_program_channel_ids`'s, for the same reason
    and on the same measurement: the window is ~35,000 rows out of 2,078,375 and the scan costs
    what it costs whether it answers one tag or every tag, so asking per tag would multiply the
    only expensive part by the size of the tag vocabulary - the one number in this query that
    the user controls, through the rail's own `+ Create tag` button.

    `chan_prog` cannot serve this and is not consulted: it is deduped across showings and holds
    no times, which is the same reason `_now_program_side` reads `epg_entries` too. So a tag on
    the channel grain, like a typed query there, keeps answering while an index rebuilds.
    """
    by_key = {'title': EPGEntry.title, 'sub_title': EPGEntry.sub_title,
              'description': EPGEntry.description}
    entry_fields = [replace(f, column=by_key[f.fts_column], cast_text=False)
                    for f in _TAG_FIELDS if f.source == SOURCE_PROGRAM]
    out = {tag_id: set() for tag_id, _patterns in vocabulary}
    # A tag with no patterns matches nothing rather than everything, exactly as in
    # _tag_predicate - and it must also not contribute an empty or_() to the WHERE.
    live = [(tag_id, or_(*[_column_like(f, Term(p)) for p in patterns for f in entry_fields]))
            for tag_id, patterns in vocabulary if patterns]
    if live:
        flags = [case((clause, 1), else_=0) for _tag_id, clause in live]
        rows = db.session.execute(
            select(EPGEntry.channel_id, *flags).where(
                EPGEntry.start_time <= now,
                _unindexed(EPGEntry.stop_time) > now,
                or_(*[clause for _tag_id, clause in live])).distinct()).all()
        for row in rows:
            for (tag_id, _clause), flag in zip(live, row[1:]):
                if flag:
                    out[tag_id].add(row[0])
    return {tag_id: frozenset(ids) for tag_id, ids in out.items()}


def _now_tag_sets(tags: list, now: datetime, cfg: dict) -> dict:
    """`{tag id: frozenset(channel ids)}` for `tags`, from the TTL cache when it can be.

    A miss recomputes the whole vocabulary, so the cache is keyed on the vocabulary itself -
    every tag id with its exact patterns. An added, deleted or edited tag therefore changes the
    key and is a MISS rather than a stale hit, the same property `_cached_tag_channel_ids` gets
    from its watermark, and for the same payoff: no route has to remember to evict anything.

    `search.tag_id_cache_enabled: false` turns this cross-request layer off too. It is the
    switch for "answer tags live", and having it govern one of the two tag caches but not the
    other would be a trap. The per-request memo on `SearchContext` is NOT part of that switch:
    a tag predicate has to be a resolved id list either way, or the scan above lands inside
    every facet aggregate instead of running once.
    """
    # Patterns are sorted into the key because the relationship is unordered: two requests
    # reading the same tag could otherwise build two different keys for one vocabulary and
    # miss every time.
    vocabulary = tuple((t.id, tuple(sorted(p.pattern for p in t.patterns if p.pattern)))
                       for t in tags)
    cacheable = (cfg or {}).get('search', {}).get('tag_id_cache_enabled', True)
    if cacheable:
        with _now_tag_lock:
            cached = _now_tag_channel_ids_cache.get(vocabulary)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
    result = _scan_now_tag_channel_ids(vocabulary, now)
    if cacheable:
        now_mono = time.monotonic()
        with _now_tag_lock:
            for key in [k for k, v in _now_tag_channel_ids_cache.items() if v[0] <= now_mono]:
                del _now_tag_channel_ids_cache[key]
            _now_tag_channel_ids_cache[vocabulary] = (
                now_mono + NOW_TAG_TTL_SECONDS, result)
    return result


def _tag_predicate(tag: Tag, indexed: bool, grain: str = GRAIN_CHANNELS, narrow: bool = False,
                   cfg: dict | None = None, hides_past: bool = True,
                   ctx: 'SearchContext' = None, now_scoped: bool = True):
    """"Carries this tag" - one of the tag's patterns found in the channel's name or in
    something it is airing.

    A tag is a set of literal patterns (Tag + TagPattern), not a column or a membership
    table, so this is an ordinary text search and goes through the same term machinery -
    which is what keeps "what a tag means" in one place as the search fields evolve.

    A tag with no patterns matches nothing rather than everything: an empty pattern set is
    an unfinished tag, and the alternative reads as "every channel is tagged Live".

    **On the channel grain "something it is airing" means the program on RIGHT NOW**
    (`now_scoped`, dev/changelog/862). A channel row shows one program, so a tag badge drawn
    from tonight's listing had nothing on the row to point at - the identical complaint, and
    the identical fix, as `_now_program_side` for a typed query, and the two now agree with
    each other and with the `Now airing` column. The channel-NAME half is untouched by this
    and still asks nothing about a clock, exactly as the channel-side text fields do.

    `now_scoped=False` asks the older "ever airs, over chan_prog's future-only window"
    question, and has one caller: the tag badge on an AIRING row, which is a fact about that
    row's channel rather than about the showing (`tag_hit_predicate`). Both spellings are
    channel-grain expressions; the flag chooses which question, `grain` chooses which shape.

    `tag` is the second exception to "everything except `when` is a channel-grain concept"
    (DESIGN-channel-search.md §1.1, alongside `when`): on the airings grain this answers "does
    THIS showing carry the tag" against the same title/sub_title/description columns
    `app/accounts.py::tags_matching()` checks for the row's own `matched_tags` badge - not
    "does this channel ever air anything matching, at any time," which is what the channel-name
    field and the channel-grain scan below give. Without this branch a common pattern (an
    ordinary word, not a rare glyph) makes the airings-grain filter pass almost every row
    regardless of what that row itself is - hit concretely in practice (BUGS.md 2026-08-07) -
    because nearly any channel airs *something* matching a common word at some point.

    `narrow` is the caller's `airing_narrowing_decision()` result - the QUERY'S OWN typed
    terms' narrowing decision - and only matters to the uncached fallback below (see
    `hides_past` for the cached path's own, different, condition).

    **The "airs something matching" half is memoized** (`_cached_tag_channel_ids`) rather than
    computed as a live per-request subquery: on both grains that half is what the query
    planner was picking as the driving predicate over far more selective filters, and handing
    it a precomputed list sidesteps that choice entirely instead of trying to out-guess the
    planner (measured 4.5s -> 0.12s and 14.4s -> 0.20s on the two reproductions in
    dev/changelog/597). One cached set serves both grains - it is built per-pattern the exact
    same way `_program_side`'s fast path already does, so a cache miss falls back to computing
    that same answer live rather than to a different, unverified code path. On the airings
    grain the cached set is used as a shared prefilter across every pattern's own LIKE
    (`_airing_program_side(..., narrow=False)` supplies the LIKE half); that is a superset for
    any one pattern (it is a union built by processing every pattern, including this one), so
    it can never wrongly exclude a real match - unlike per-pattern narrowing, it also covers a
    pattern under `TRIGRAM_MIN_CHARS`, since that pattern's own contribution to the cached set
    used a trigram-safe fallback rather than an untrustworthy short MATCH.

    `hides_past` - not `narrow` - gates whether the cached set is used as that prefilter.
    `narrow`'s other conditions (wildcard/too-short/no-positive-terms) are about the query's
    OWN typed terms and have nothing to do with this tag's patterns; reusing `narrow` wholesale
    for the cached path would fail closed on "no positive terms" for a bare tag-only filter
    with no text query - exactly the case the cached path must not fail closed on. The one
    condition that genuinely carries over is `chan_prog` being future-only: the cached set
    only ever covers future entries, so it is only a safe prefilter when the search is ALSO
    future-only (`standing_applied(state.standing, 'showpast')`, mirroring
    `airing_narrowing_decision`'s own first check for the identical reason).
    """
    patterns = [p.pattern for p in tag.patterns if p.pattern]
    if not patterns:
        return db.false()

    program_fields = tuple(f for f in _TAG_FIELDS if f.source == SOURCE_PROGRAM)
    channel_fields = tuple(f for f in _TAG_FIELDS if f.source == SOURCE_CHANNEL)

    if grain == GRAIN_CHANNELS and now_scoped:
        # No `_cached_tag_channel_ids` on this path, and no readiness gate on this half: the
        # question is answered from epg_entries, which is never half-built the way an index is.
        channel_side = or_(*[_channel_side(Term(p), channel_fields, indexed) for p in patterns])
        ctx = ctx if ctx is not None else SearchContext()
        return or_(channel_side, Channel.id.in_(ctx.now_tag_channel_ids(tag)))

    channel_ids = _cached_tag_channel_ids(tag, patterns, cfg) if indexed else None

    if grain == GRAIN_AIRINGS:
        if channel_ids is not None:
            likes = or_(*[_airing_program_side(Term(p), program_fields, False)
                          for p in patterns])
            # Gated on `hides_past`, NOT `narrow` - `narrow` is airing_narrowing_decision()'s
            # answer for the QUERY'S OWN typed terms (wildcard/too-short/no-positive-terms are
            # all about what the user typed, not about this tag's patterns) and a bare tag
            # filter with no text query always fails that decision on "no positive terms",
            # which would silently defeat the cache on exactly the first reproduction this
            # item exists to fix (`f.tag=live`/`new`, no other filter). The cached set's own
            # correctness condition is only ever "does chan_prog's future-only coverage match
            # what this search is showing" - the same single condition
            # airing_narrowing_decision's first check answers.
            return and_(EPGEntry.channel_id.in_(channel_ids), likes) if hides_past else likes
        # Cache unavailable (the setting is off; index-not-ready already short-circuited
        # above) - the original per-pattern live narrowing, unchanged.
        return or_(*[_airing_program_side(
            Term(p), program_fields, narrow and len(p) >= TRIGRAM_MIN_CHARS) for p in patterns])

    channel_side = or_(*[_channel_side(Term(p), channel_fields, indexed) for p in patterns])
    if channel_ids is not None:
        program_side = Channel.id.in_(channel_ids)
    else:
        program_side = or_(*[_program_side(Term(p), program_fields, indexed) for p in patterns])
    return or_(channel_side, program_side)


def _missing_predicate(ctx: 'SearchContext'):
    """The 'deleted by provider' half of accounts.channel_lifecycle_state(), as SQL.

    Kept deliberately parallel to accounts.missing_channels_query() - if that condition
    changes, this changes with it.

    Spelled as an EXISTS rather than a join so it composes into a WHERE clause without
    changing the shape of the query it lands in. De-correlating it into one OR branch per
    account looks like it should be faster - there are only four accounts - and was measured
    twice as slow (62.9ms vs 32.2ms), because the OR chain costs the account index. Measured
    2026-07-30; do not "optimize" it back.
    """
    missing_days = (ctx.cfg or {}).get('sync', {}).get('channel_missing_after_days', 7)
    if missing_days <= 0:
        return db.false()
    cutoff = datetime.utcnow() - timedelta(days=missing_days)
    return and_(
        Channel.last_seen_at.isnot(None),
        Channel.last_seen_at < cutoff,
        select(Account.id).where(
            Account.id == Channel.account_id,
            Account.last_sync_at.isnot(None),
            Account.last_sync_at > Channel.last_seen_at,
        ).exists(),
    )


def _new_predicate(ctx: 'SearchContext'):
    """The 'newly added' half of accounts.channel_lifecycle_state(), as SQL.

    Kept deliberately parallel to that function's 'new' branch - if that condition changes,
    this changes with it. Unlike _missing_predicate, "is this account past its first-sync
    era" does not depend on the channel row at all, only on the account, so it is precomputed
    once per account into ctx.new_eligible_account_ids (SearchContext.build()) rather than
    expressed as a correlated subquery - a plain IN-list is both simpler and cheaper.
    """
    new_days = (ctx.cfg or {}).get('sync', {}).get('channel_new_within_days', 3)
    if new_days <= 0 or not ctx.new_eligible_account_ids:
        return db.false()
    cutoff = datetime.utcnow() - timedelta(days=new_days)
    return and_(
        Channel.first_seen_at.isnot(None),
        Channel.first_seen_at > cutoff,
        Channel.account_id.in_(ctx.new_eligible_account_ids),
    )


def _unindexed(col):
    """The same column, spelled so SQLite's planner cannot use an index on it.

    `+` is SQLite's documented no-op unary prefix: it changes nothing about the value and
    everything about the plan. The column's own type comes along or the bind parameter is a
    bare NullType and the datetime reaches the driver unformatted, relying on sqlite3's
    default adapter - deprecated in Python 3.12 and removed in 3.14.

    Do not reach for `with_hint(EPGEntry, 'NOT INDEXED')` instead: SQLAlchemy's SQLite dialect
    silently drops it, and it would also ban the covering `ix_epg_entries_channel_stop` scan
    the fast plan lands on. See dev/changelog/420.
    """
    return literal_column(f'+epg_entries.{col.key}', col.type)


def _start_window(lo, hi):
    """`start_time` between two bounds, indexable only when the window is narrow enough.

    **The width test is the whole point, and a blanket answer either way is wrong.** A
    `start_time` range is indexable, so SQLite drives the outer loop off
    `ix_epg_entries_start_stop` with a random rowid lookup per matched row - and
    base_predicates() re-attaches a `when` filter to the row query, the breakdown and every
    facet aggregate alike, so that plan is paid on each of the rail's 8-9 statements. That is
    a win while the window matches few rows and a rout once it matches many, because the
    defeated plan is a sequential scan whose cost barely moves with the match count.

    Measured 2026-08-01, whole rail request, in process on a local-disk copy of the live
    1.47M-row database (dev/changelog/421):

        window        matched    indexed    defeated
        next:15:min    11,342      3.37s       4.61s
        next:1:hours   24,337      4.12s       4.76s      <- they cross here
        next:2:hours   60,884      8.27s       4.92s
        today         366,592     35.0s        6.55s
        next:7:days   625,882     48.0s        7.67s

    An open-ended window (a `custom:` with one side left blank) counts as the widest there
    is, not the narrowest.
    """
    wide = lo is None or hi is None or (hi - lo) > WHEN_INDEX_DEFEAT_WIDTH
    col = _unindexed(EPGEntry.start_time) if wide else EPGEntry.start_time
    clauses = []
    if lo is not None:
        clauses.append(col >= lo)
    if hi is not None:
        clauses.append(col < hi)
    return clauses


def _when_predicate(value: str, ctx: 'SearchContext'):
    """"Does this SHOWING fall in this window" - the only dimension that reads epg_entries.

    Every other dimension describes the channel and is therefore identical on both grains;
    this one describes the row itself, which is why it exists on one grain only.

    An unusable window - a `next:` with nothing typed into it yet, a malformed `custom:` -
    matches nothing rather than raising. It is a number the user is still typing, not a
    registry key, and 400ing the search someone is mid-way through filling in is not an
    error message, it is a broken page.
    """
    if value == WHEN_NOW:
        # "On now" is an interval OVERLAP, and neither side of it is selective on its own:
        # `start_time <= now` is every showing that has ever begun (806,484 of 1,474,199) and
        # `stop_time > now` is every one that has not yet ended (701,668). Only the
        # conjunction is small (33,953), and no B-tree range can express that. So the planner
        # picks one of them, walks half the table and throws almost all of it away.
        # Measured 2026-08-01, whole rail request: both sides left bare is 10.28s; defeating
        # start_time INSTEAD is 25.5s (the planner falls onto ix_epg_entries_stop_time, the
        # wider of the two); defeating both is 4.77s; defeating stop_time alone - this
        # spelling - is 4.64s and is the floor. Which side is defeated is not cosmetic.
        # A bounded lower edge (`start_time > now - <max duration>`) would beat all of these
        # by making the range genuinely narrow, and it is deliberately NOT used: the longest
        # showing in this database is 115 hours, so any constant is a silent row-dropper the
        # day a longer one arrives.
        return and_(EPGEntry.start_time <= ctx.now, _unindexed(EPGEntry.stop_time) > ctx.now)
    bounds = ctx.day_bounds.get(value)
    if bounds is not None:
        return and_(*_start_window(bounds[0], bounds[1]))
    if value.startswith(WHEN_NEXT_PREFIX):
        delta = parse_when_next(value)
        if delta is None:
            return db.false()
        return and_(*_start_window(ctx.now, ctx.now + delta))
    if value.startswith(WHEN_CUSTOM_PREFIX):
        parsed = parse_when_custom(value, ctx.display_tz)
        if parsed is None:
            return db.false()
        start, stop = parsed
        # `custom`'s upper bound is inclusive where every other window's is exclusive, so it
        # cannot go through _start_window()'s `<`. The width test is the same one.
        wide = start is None or stop is None or (stop - start) > WHEN_INDEX_DEFEAT_WIDTH
        col = _unindexed(EPGEntry.start_time) if wide else EPGEntry.start_time
        clauses = []
        if start is not None:
            clauses.append(col >= start)
        if stop is not None:
            clauses.append(col <= stop)
        return and_(*clauses)
    raise SearchStateError(f'unknown When value {value!r}')


def _duration_predicate(value: str):
    """"Does this SHOWING's length fall in this bound" - the second row-level dimension.

    Reads EPGEntry.duration_minutes, the VIRTUAL generated column (migration 36) rather than
    computing julianday(stop_time)-julianday(start_time) inline: SQLite only recognizes a
    generated-column index for a predicate spelled as a plain column reference, not one that
    repeats the generating expression, so this is not a style choice - it is what keeps the
    predicate indexable (dev/changelog/594).

    An unusable value - a `dur:` with neither bound typed yet, or a malformed one - matches
    nothing rather than raising, same reasoning as `_when_predicate`.
    """
    parsed = parse_duration(value)
    if parsed is None:
        return db.false()
    lo, hi = parsed
    clauses = []
    if lo is not None:
        clauses.append(EPGEntry.duration_minutes >= lo)
    if hi is not None:
        clauses.append(EPGEntry.duration_minutes <= hi)
    return and_(*clauses)


def _value_predicate(dim_key: str, value: str, ctx: 'SearchContext', grain: str = GRAIN_CHANNELS,
                     narrow: bool = False, hides_past: bool = True):
    """"Does a channel carry this value of this dimension" - one entry per dimension.

    Every dimension here except `when`, `duration` and `tag` is about the CHANNEL, on both
    grains alike. That is the §1 caveat of DESIGN-channel-search.md, and it is why the airing
    query can reuse those predicates verbatim: it joins Channel, so a Channel expression
    composes into it directly. `tag` is the other exception - see `_tag_predicate()`'s
    docstring. `narrow` and `hides_past` only matter to that branch; every other dimension
    ignores both.
    """
    if dim_key == 'when':
        return _when_predicate(value, ctx)
    if dim_key == 'duration':
        return _duration_predicate(value)
    if dim_key == 'tag':
        tag = ctx.tags_by_name.get(value)
        # Tags read both indexes, so they need both to be trustworthy.
        return db.false() if tag is None else _tag_predicate(tag, ctx.readiness_for(
            _TAG_FIELDS)[0], grain, narrow, ctx.cfg, hides_past, ctx)
    if dim_key == 'acct':
        parsed = _optional_int(value)
        return db.false() if parsed is None else Channel.account_id == parsed
    if dim_key == 'health':
        return _health_predicate(value, ctx.cfg)
    if dim_key == 'group':
        # .correlate(Channel) is load-bearing, and for the reason `noepg` documents
        # (dev/docs/BUGS.md 2026-08-11): unrestricted auto-correlation strips every table the
        # ENCLOSING query already has, not just the one intended. This predicate ends up
        # nested inside `grpdedup`'s ranking subquery, which selects from epg_entries JOIN
        # channels JOIN channel_group_members - so channel_group_members gets correlated away
        # too. "Any group" is the value that breaks, because that table is its only FROM and
        # removing it leaves the subquery with none; SQLAlchemy raises rather than counting.
        # A NAMED group survived by luck - its extra join to channel_groups left one FROM
        # standing - which is why this was a 500 on one facet value only.
        members = select(ChannelGroupMember.channel_id).where(
            ChannelGroupMember.channel_id == Channel.id).correlate(Channel)
        if value != GROUP_ANY:
            members = members.join(
                ChannelGroup, ChannelGroup.id == ChannelGroupMember.group_id
            ).where(ChannelGroup.name == value)
        return members.exists()
    if dim_key == 'cat':
        return Channel.category_name == value
    if dim_key == 'other':
        if value == OTHER_REMOVED:
            return _missing_predicate(ctx)
        if value == OTHER_NEW:
            return _new_predicate(ctx)
        if value == OTHER_IN_GUIDE:
            # Guide SCOPE, not the raw flag - the difference, and why an id set rather than a
            # column predicate, are on guide_scope_channel_ids(). Channel-side on BOTH grains:
            # the airing query joins Channel, so this composes in unchanged and keeps
            # `other` honestly channel-side for `_DIMENSION_SIDE`.
            return Channel.id.in_(guide_scope_channel_ids())
        if value == OTHER_GUIDE_OWN_ROW:
            # The column, deliberately and for the only reason that licenses reading it bare:
            # this value's whole job is to expose "this channel is its own guide row", which
            # is what the column means and all it means (dev/changelog/751).
            return Channel.in_guide.is_(True)
        if value == OTHER_GUIDE_VIA_GROUP:
            # Membership of an in-guide group, NOT "in scope but without its own row" - a
            # channel that holds a row AND sits in an in-guide group is honestly both, and
            # subtracting one from the other would stop the two values ORing back to `guide`.
            return Channel.id.in_(guide_scope_group_member_ids())
        if value == OTHER_DUP_URL:
            return Channel.is_duplicate_stream_url.is_(True)
        raise SearchStateError(f'unknown Other value {value!r}')
    if dim_key == 'chan':
        parsed = _optional_int(value)
        return db.false() if parsed is None else Channel.id == parsed
    raise SearchStateError(f'unknown dimension {dim_key!r}')


def dimension_predicates(state: SearchState, ctx: 'SearchContext', skip: str = '',
                         by_key: bool = False) -> list:
    """The filter predicates, optionally omitting one dimension's own filter.

    `skip` is what makes facet counting work: a dimension is counted against a state that
    does not filter on itself, or every count in it reads zero as soon as one value is
    picked.

    `by_key=True` returns `[(dimension key, predicate), ...]` instead of a bare list, which
    is what `_split_predicates()` needs to decide which table each predicate narrows. The
    keyed form exists so that splitter does not have to re-derive this loop and drift from
    it - one builder, two shapes.

    **A filter on a dimension this grain does not have is IGNORED, not rejected** (mockup 25,
    P2, settled round 3). Flipping grain parks such a chip rather than dropping it, and
    parking has to survive a reload - which it only can if `f.when=` is a legal thing to send
    while looking at channels. This is the one place the registry is deliberately lenient,
    and it is lenient in the direction that cannot show wrong data: an ignored filter widens
    the result set, and the page draws the chip struck through so the widening is visible.
    """
    # Computed once, only if a tag filter is actually active. `tag_narrow` feeds the uncached
    # fallback only (see _tag_predicate's docstring for why it must not gate the cached path):
    # an un-narrowed airings-grain tag predicate there is a plain-text scan of the whole
    # epg_entries table, so a common pattern (an ordinary word rather than a rare glyph) needs
    # the same trigram narrowing the typed query terms get, or it costs the same as re-running
    # the row query per pattern. `tag_hides_past` is the cached path's own, simpler condition -
    # chan_prog (what the cache is built from) is future-only, so the cache is only a safe
    # prefilter when this search is too.
    tag_narrow = False
    tag_hides_past = standing_applied(state.standing, 'showpast')
    if state.grain == GRAIN_AIRINGS and any(f.key == 'tag' for f in state.filters):
        tag_narrow, _ = airing_narrowing_decision(state, ctx)

    out = []
    for f in state.filters:
        if f.key == skip:
            continue
        if f.key not in DIMENSION_BY_KEY:
            raise SearchStateError(f'unknown filter dimension {f.key!r}')
        if not _in_grain(DIMENSION_BY_KEY[f.key], state.grain):
            continue
        if f.ex:
            out.append((f.key, not_(or_(*[_value_predicate(f.key, v, ctx, state.grain,
                                                           tag_narrow, tag_hides_past)
                                          for v in f.ex]))))
        if f.values:
            out.append((f.key, or_(*[_value_predicate(f.key, v, ctx, state.grain, tag_narrow,
                                                      tag_hides_past)
                                     for v in f.values])))
    return out if by_key else [pred for _key, pred in out]


# ---------------------------------------------------------------------------
# Standing options -> SQL
# ---------------------------------------------------------------------------

def in_any_group_expr():
    """"Is this channel in any channel group at all" as a correlated EXISTS, for use as a
    keep-rule rung. ANY group, deliberately - not only one that is in the guide.

    A channel sitting in a health-check-only group is one the user deliberately curated and
    is monitoring, so a rule that only protected in-guide members would hide it in favour of
    an untouched copy of the same stream - deliberate, see dev/changelog/759. It is also the
    cheaper of the two spellings and the one that reads as a single sentence in the KEPT
    tooltip, which the guide-scope variant did not.

    Correlated rather than an id set: it runs inside a window ORDER BY over only the ~1,600
    rows flagged is_duplicate_stream_url, and channel_group_members is indexed on channel_id,
    so each row costs one index seek. Measured at +1.2ms over the whole cascade on the live
    database - the guide-scope variant measured +1.3ms, i.e. neither cost decided this.
    """
    return select(ChannelGroupMember.id).where(
        ChannelGroupMember.channel_id == Channel.id).correlate(Channel).exists()


def _duplicate_losers():
    """Channels hidden while "Show duplicates" is off: every member of a shared-stream_url
    cluster except the one kept.

    The keep rule is a CASCADE, not five exclusive rules - each rung only gets a say when
    the one above it ties: not hidden, then already in your guide, then in a channel group,
    then the best health score, then the lowest channel id (mockup 21 round 6; the group rung
    added by dev/changelog/759, the hidden rung by dev/changelog/775). SQLite sorts NULL
    lowest, so DESC puts a never-scored channel behind a scored one, which is what the rule
    intends.

    **The hidden rung is at the top and is not optional.** This ranks over the whole channels
    table, so without it a cluster whose KEPT copy the user hid makes every remaining copy a
    loser - and with `hidden` and `dup` both defaulting on, the stream then disappears from
    the search entirely rather than falling back to a visible copy. A hidden channel must
    never win a cluster, whatever the search says.

    **`channel_search_rows._keep_rank()` spells this same cascade in Python** so the KEPT
    badge can name the rung that decided a row. The two are one rule and must move together:
    a badge explaining a rung this query does not apply is a badge that lies.

    Computed over the whole channels table rather than the filtered set, on purpose: which
    copy is KEPT must not change because the user typed something. Only the ~1,600 rows
    already flagged is_duplicate_stream_url take part.
    """
    ranked = (
        select(Channel.id.label('id'),
               func.row_number().over(
                   partition_by=Channel.stream_url,
                   order_by=[Channel.hidden.asc(),
                             Channel.in_guide.desc(),
                             in_any_group_expr().desc(),
                             effective_health().desc(),
                             Channel.id.asc()]).label('rn'))
        .where(Channel.is_duplicate_stream_url.is_(True))
        .subquery()
    )
    return (select(ranked.c.id)
            .group_by(ranked.c.id)
            .having(func.min(ranked.c.rn) > 1))


def _standing_reject(key: str, ctx: 'SearchContext', row_preds=(), state=None):
    """"What this standing option takes out" - the predicate, un-negated, so the same
    expression can both filter rows out and count what it took out.

    Un-negated is the point, and it did not change when the options were rephrased as
    "Show ..." (dev/changelog/778): this answers what would be REMOVED, which is the same
    question whichever way the label reads. Whether it is asked at all is
    `standing_applied()`'s job, not this function's.

    **Every spelling here was measured against its obvious alternative on 2026-07-30, and
    the obvious alternative lost every time.** Standing options land in the row query, in the
    breakdown and in every facet aggregate, so a bad spelling is paid many times over - but
    the fix is not to rewrite them as subqueries. Rewriting `notnorm` as `id IN (SELECT id
    ...)` took the account facet from 90ms to 138ms, and rewriting `noepg` as
    `NOT (id IN (SELECT channel_id FROM epg_entries ...))` took it from 82ms to 883ms. What
    actually pays is resolving the per-request facts once, in `SearchContext`, so no predicate
    here reaches for config or a query of its own - not restating any individual option.

    `state` is read by `showmembers` alone, which needs the group-shaped filters to know
    which groups have rows on this page. It defaults to None so a caller that cannot have one
    still gets every other option's answer; with no state the fold is defined over every
    non-system group, which is what an unfiltered search sees anyway.
    """
    if key == 'showhidden':
        # Straight off the materialized column, which is the whole point of materializing it:
        # the four sources, the human's override and the guide/group protection are all
        # already resolved into this one indexed boolean by channel_hiding.recompute(), so
        # neither this predicate nor the six facet aggregates that also carry it ever has to
        # re-derive any of them. Measured at +2ms on a 100ms channel-grain scan.
        return Channel.hidden.is_(True)
    if key == 'showdup':
        return Channel.id.in_(_duplicate_losers())
    if key == 'shownotnorm':
        # Only meaningful for an account that actually normalizes: with the mode disabled
        # the provider's URL is what it is, and "normalization left it alone" describes
        # every channel on that account rather than a property worth hiding.
        if not ctx.normalizing_account_ids:
            return db.false()
        return and_(Channel.url_normalizable.is_(False),
                    Channel.account_id.in_(sorted(ctx.normalizing_account_ids)))
    if key == 'shownoepg':
        # Straight off epg_entries, not chan_prog: chan_prog is a derived cache that a
        # rebuild empties, and this option HIDES rows, so trusting a half-built cache here
        # would hide the whole list. The correlated EXISTS is deliberate (see above).
        # .correlate(Channel) is load-bearing: on the airings grain the enclosing query's
        # FROM already joins EPGEntry and Channel together, so unrestricted auto-correlation
        # strips BOTH tables from this subquery (leaving it with no FROM clause of its own)
        # instead of just the intended Channel correlation - SQLAlchemy raises
        # InvalidRequestError. dev/docs/BUGS.md 2026-08-11.
        return not_(select(EPGEntry.id).where(
            EPGEntry.channel_id == Channel.id,
            EPGEntry.stop_time >= datetime.utcnow()).correlate(Channel).exists())
    if key == 'showuntested':
        return Channel.health_score.is_(None)
    if key == 'showmembers':
        # THE FOLD, and it is deliberately not spelled as "is this channel's group on the
        # page". It does not have to be: a channel is only ever a candidate row when it
        # already satisfies this search, so every group it belongs to necessarily has a
        # matching member - itself - and is therefore on the page already. What is left to
        # ask is only whether that group is allowed a row at all, which is
        # `_group_row_filters`. Spelling it the long way would make this predicate depend on
        # the group query and the group query on this predicate.
        #
        # A channel holding its OWN guide row is never folded: the TV Guide paints it beside
        # its group's row, so folding it would stop mirroring the guide at the one channel
        # that proves both can hold a row (DESIGN-channel-groups-model.md DECIDED 5). This is
        # the column read for exactly what it means and nothing else (dev/changelog/751).
        #
        # .correlate(Channel) for the reason `noepg` documents (dev/docs/BUGS.md 2026-08-11):
        # unrestricted auto-correlation strips every table the enclosing query already has.
        #
        # THE COMMON CASE IS THE CHEAP ONE, and deliberately so: this predicate lands in
        # the row query, the standing breakdown and every facet aggregate, so it is paid
        # many times per request. With no group-shaped filter active the only thing
        # `_group_row_filters` says is "not the system group", and the system group stores
        # NO membership rows at all - its membership is computed - so nothing reachable
        # through `channel_group_members` can be one. The question is then just "is this
        # channel in any group", and `ctx` already holds the answer as a bounded id list
        # (see `group_member_channel_ids`), which is both the cheapest spelling and the
        # only one allowed here - a predicate may not run a query of its own.
        if not ctx.group_member_channel_ids:
            return db.false()
        gates = _group_row_filters(state, ctx)
        if len(gates) == 1:
            return and_(Channel.in_guide.is_(False),
                        Channel.id.in_(ctx.group_member_channel_ids))
        member_of = (select(ChannelGroupMember.channel_id)
                     .select_from(ChannelGroupMember)
                     .join(ChannelGroup, ChannelGroup.id == ChannelGroupMember.group_id)
                     .where(ChannelGroupMember.channel_id == Channel.id, *gates)
                     .correlate(Channel))
        return and_(Channel.in_guide.is_(False),
                    Channel.id.in_(ctx.group_member_channel_ids), member_of.exists())
    if key == 'showpast':
        # The `+` is a SQLite no-op prefix, and it is load-bearing. Written as
        # `EPGEntry.stop_time <= ctx.now` this negates to an indexable range that matches
        # HALF the 1.89M-row table, so the planner drives every statement off
        # ix_epg_entries_stop_time - ~941k index entries, each with a random rowid lookup
        # into a 1.66GB table and another into channels. `past` is on by default and
        # base_predicates() re-attaches it to the row query, the breakdown and every facet
        # aggregate alike, so that plan was paid 8-9 times per rail request at ~4s each.
        # Measured 2026-07-31 on a local-disk copy of the live DB, swapping only this
        # spelling: facet cat 4265->1261ms, ROW PAGE 3999->844ms, facet when 4212->2491ms,
        # facet acct 2803->619ms, combined scan 4059->1867ms. The plan flips to a sequential
        # SCAN of channels or of ix_epg_entries_channel_stop as a covering index.
        # Do NOT "clean this up" - see _unindexed() for why, and for what not to use instead.
        return _unindexed(EPGEntry.stop_time) <= ctx.now
    if key == 'firstonly':
        return EPGEntry.id.in_(_later_showings(row_preds))
    if key == 'grpdedup':
        return EPGEntry.id.in_(_group_collapse_losers(row_preds))
    raise SearchStateError(f'unknown standing option {key!r}')


# ---------------------------------------------------------------------------
# The two airing CLUSTER options, and why they are scoped differently to `dup`
# ---------------------------------------------------------------------------
#
# `dup` ranks over the WHOLE channels table on purpose: which copy of a duplicated feed is
# "the one kept" is an identity question, and it must not change because the user typed
# something.
#
# These two are the opposite, and copying `dup`'s spelling here is a real defect rather than a
# style difference. "Keep each channel's earliest MATCHING showing" and "keep the group's best
# member for this program" are both statements about the result set, so ranking them over the
# whole table drops rows silently: a channel whose earliest showing ever has already ended
# would contribute nothing at all under `firstonly` + `past` (its survivor is a row `past`
# already removed), and a group whose best member is excluded by a filter would vanish
# entirely rather than falling back to the next member. Both were caught in the smoke pass
# before this shipped.
#
# So they rank over `row_preds` - the typed query, the filters, and every non-cluster standing
# option - which is exactly "the rows this search would otherwise show".


def _later_showings(row_preds=()):
    """Entry ids hidden by "One row per channel": every matching showing but the earliest."""
    ranked = (
        select(EPGEntry.id.label('id'),
               func.row_number().over(
                   partition_by=EPGEntry.channel_id,
                   order_by=[EPGEntry.start_time.asc(), EPGEntry.id.asc()]).label('rn'))
        .select_from(EPGEntry)
        .join(Channel, Channel.id == EPGEntry.channel_id)
        .where(*row_preds)
        .subquery()
    )
    return (select(ranked.c.id)
            .group_by(ranked.c.id)
            .having(func.min(ranked.c.rn) > 1))


def _group_ranked_entries(row_preds=()):
    """One row per (entry, group it is in), carrying that group's rank `rn`.

    Split out from `_group_collapse_losers` so the equivalence test can run a reference
    spelling over the SAME window instead of a copy of it - a copied window spec is
    duplication that goes stale the first time the ranking rule changes. The ranking rule
    itself, and why each part of it is the way it is, is documented on `_group_collapse_losers`
    below.
    """
    return (
        select(EPGEntry.id.label('id'),
               ChannelGroupMember.group_id.label('group_id'),
               func.row_number().over(
                   partition_by=[ChannelGroupMember.group_id, EPGEntry.title,
                                 EPGEntry.start_time, EPGEntry.stop_time],
                   order_by=[
                       db.case((ChannelGroupMember.recording_enabled.is_(True), 0), else_=1),
                       func.min(100, func.max(
                           0, func.coalesce(Channel.health_score, 50)
                           + func.coalesce(Channel.manual_health_adjustment, 0))).desc(),
                       Channel.id.asc(),
                   ]).label('rn'))
        .select_from(EPGEntry)
        .join(Channel, Channel.id == EPGEntry.channel_id)
        .join(ChannelGroupMember, ChannelGroupMember.channel_id == Channel.id)
        .join(ChannelGroup, ChannelGroup.id == ChannelGroupMember.group_id)
        .where(*row_preds)
        .subquery()
    )


def _group_collapse_losers(row_preds=()):
    """Entry ids hidden by "Collapse channel groups" - what guide_search did unconditionally.

    Members of a group carry near-identical EPG, so without this one program
    becomes one row per member. The surviving row is the one on the group's best member, by
    the SAME rule record resolution and failover use (`channel_groups.effective_score`:
    a never-tested channel ranks as 50 and the sum is clamped to 0-100) - deliberately NOT
    `effective_health()`, which is unclamped and treats never-tested as NULL. A collapse that
    kept a different member than the recorder would pick is a row that records the wrong feed.

    Three things this spelling gets right that a simpler one does not:

    * **Recording-disabled members rank last rather than being excluded.**
      `recording_members()` drops them, but dropping them here would hide the entire
      schedule of a group whose every member is recording-disabled.
    * **A channel may be in several groups.** An airing is one row and cannot be
      emitted once per group, so it is hidden only when it loses in EVERY group its channel is
      in - hence `min(rn) > 1` over all of an entry's partitions, never a bare `rn > 1`, which
      would hide a row that wins one group and places second in another.
    * **The partition is the program, not the group.** Ranking by (group, title, start, stop)
      keeps a showing whose best member has no listing for it, which ranking members alone
      would have silently dropped.

    The aggregate is also why this is one pass. Spelled as `grouped EXCEPT winners` - which is
    what it was until dev/changelog/657 - the four-table join and its window sort are
    materialized TWICE per statement and reconciled through a temp B-tree; measured in
    isolation against the live database that is a straight 2x (1.89-2.02x warm).

    Do not expect that 2x to show up in a request, and do not read the doubled materialization
    as the reason an airings page is slow. This join is driven off `channel_group_members`, so
    it spans only entries whose channel is in a group - 636 of 2.2M rows across 8
    memberships when 657 measured it - which is single-digit milliseconds either way. What the
    option actually costs a statement is the outer `NOT IN` over the whole surviving set
    (0.341s -> 0.478s on a count stand-in), and that is unchanged by the spelling. The saving
    here scales with how many channels are in groups, not with the size of `epg_entries`.
    """
    ranked = _group_ranked_entries(row_preds)
    return (select(ranked.c.id)
            .group_by(ranked.c.id)
            .having(func.min(ranked.c.rn) > 1))


def group_wins_by_entry(entry_ids, row_preds=()) -> dict:
    """{entry id: (group id, ...)} - which groups each of these showings WON.

    The other side of `_group_collapse_losers`: the same window, the same ranking rule, asked
    for the winners instead of the losers, and restricted to one page's rows. Reusing
    `_group_ranked_entries` rather than re-deriving "who won" is the whole reason that window
    was split out - two spellings of one ranking is a row labelled with a group the recorder
    would not have picked.

    ONE query for the page. The window itself is still computed over the whole scope, which is
    what makes the answer the same one the collapse gave: restricting the ranking to the page
    would let a showing win a partition its real competitors were paged out of.
    """
    if not entry_ids:
        return {}
    ranked = _group_ranked_entries(row_preds)
    rows = db.session.execute(
        select(ranked.c.id, ranked.c.group_id)
        .where(ranked.c.id.in_(list(entry_ids)), ranked.c.rn == 1))
    out = {}
    for entry_id, group_id in rows:
        out.setdefault(entry_id, []).append(group_id)
    return {k: tuple(v) for k, v in out.items()}


# ---------------------------------------------------------------------------
# Channel groups as rows
# ---------------------------------------------------------------------------
#
# A channel group is a row kind of its own on the CHANNEL grain
# (DESIGN-group-search-rows.md §5.1). The airing grain expresses the same idea differently -
# `grpdedup` already collapses a group's near-identical listings to its best member's
# showing, and that survivor is relabelled as the group it stands for rather than a second
# row kind being introduced there.
#
# §5.2's first rule is what this section exists to honour: **groups are fetched by their own
# query and spliced into the page; they are NEVER unioned into `_page_rows()`'s id
# subquery.** That subquery has to stay one statement over one primary key - it is what took
# the default sort from 400ms to 135ms at offset 100,000 (dev/changelog/697) - and there are
# tens of groups against 137,260 channels, so a union would trade a measured win for a
# feature that never needed it.

#: The two sorts a group can answer honestly: `name` is its own name and `health` its own
#: blended score, so both cross and group rows interleave with channels under them.
#: `category`, `account`, `sid`, `tvg` and `url` describe a stream or a provider, and a group
#: has none of them - under those five, group rows land FIRST, in name order, and the count
#: line says so (§5.2's second rule: state it in the UI rather than leaving it to be
#: inferred). First rather than last because there are tens of them against six figures of
#: channels, and a row nobody will ever page to is the same as no row.
GROUP_SORTS = frozenset({'name', 'health'})


def _group_row_filters(state: SearchState, ctx: 'SearchContext') -> list:
    """What a `ChannelGroup` must satisfy to be a row at all, ignoring what was typed.

    Only the dimensions a GROUP can answer are here. Account, category, health, tag, when and
    duration describe a stream or a showing, a group has none of them, and the count line says
    so when one of them is active rather than a group row quietly vanishing.

    `chan` is the one channel-shaped dimension that must still be answered, with `false`: it
    names one channel by primary key (the duplicate drill-in builds it), so leaving it
    unanswered would put every group in the app into a three-row cluster view.

    The system group is excluded everywhere and permanently. Its membership is computed from
    `in_guide` + `test_enabled` rather than stored, so it has no `channel_group_members` rows
    to be found through, it is not a recording target, and as a row it would fold away every
    channel in the guide.
    """
    preds = [ChannelGroup.is_system.is_(False)]
    if state is None:
        return preds
    for f in state.filters:
        if f.key == 'group':
            if f.values:
                preds.append(db.true() if GROUP_ANY in f.values
                             else ChannelGroup.name.in_(list(f.values)))
            if f.ex:
                preds.append(db.false() if GROUP_ANY in f.ex
                             else not_(ChannelGroup.name.in_(list(f.ex))))
        elif f.key == 'other':
            if f.values:
                preds.append(or_(*[_group_other_predicate(v) for v in f.values]))
            if f.ex:
                preds.append(not_(or_(*[_group_other_predicate(v) for v in f.ex])))
        elif f.key == 'chan':
            preds.append(db.false())
    return preds


def _group_other_predicate(value: str):
    """One `Other` value, answered for a GROUP rather than for a channel.

    A group in the TV Guide answers both `guide` (it is in the guide) and `guidegroup` (a
    group is how those listings reach the guide). It never answers `guiderow`, which is
    `Channel.in_guide` and is about a CHANNEL holding a row of its own - keeping those two
    apart here is the same distinction dev/changelog/751 exists to protect. The three
    provider-shaped values describe a stream and a group has none.
    """
    if value in (OTHER_IN_GUIDE, OTHER_GUIDE_VIA_GROUP):
        return ChannelGroup.in_guide.is_(True)
    if value in (OTHER_GUIDE_OWN_ROW, OTHER_REMOVED, OTHER_NEW, OTHER_DUP_URL):
        return db.false()
    raise SearchStateError(f'unknown Other value {value!r}')


def _group_name_predicate(state: SearchState):
    """The typed query against a group's OWN name, or None when nothing was typed.

    Gated on the `name` search field being on, because that is the field this IS: a search
    scoped to EPG titles alone is asking what is airing, and a group name is not an answer to
    it. No FTS here and deliberately so - there are tens of groups, so a `LIKE` over them is
    free, and putting group names in the search index would be a second index to keep in step
    with the channel one for no measurable gain.
    """
    if 'name' not in state.fields:
        return None
    terms = parse_terms(state.q)
    include = [t for t in terms if not t.exclude]
    exclude = [t for t in terms if t.exclude]
    if not include and not exclude:
        return None

    def one(term):
        if term.wildcard:
            pattern, needs_escape = glob_to_like(term.text)
            return (ChannelGroup.name.like(pattern, escape='\\') if needs_escape
                    else ChannelGroup.name.like(pattern))
        return ChannelGroup.name.ilike(f'%{term.text}%')

    preds = []
    if include:
        preds.append(and_(*[one(t) for t in include]) if state.match_all
                     else or_(*[one(t) for t in include]))
    if exclude:
        preds.append(not_(or_(*[one(t) for t in exclude])))
    return and_(*preds)


def matching_groups(state: SearchState, ctx: 'SearchContext', member_preds) -> list:
    """The groups this search returns, unordered. ONE query, always.

    A group is in the result set when its own name matches, or when any of its members is a
    channel this search returns - the same question its members answer, which is what makes
    "why is this group here" explainable rather than a guess.

    `member_preds` is the channel-side narrowing WITHOUT the fold (`showmembers`), for the
    obvious reason: the fold's whole definition is "this member is represented by its group",
    so feeding it back in here would remove the very members that put the group on screen.
    """
    if state.grain != GRAIN_CHANNELS:
        return []
    # The member ids as a bound list, and the reason is the planner rather than tidiness:
    # with them present it drives the member side from a handful of rowid seeks and
    # evaluates the search's own predicates over those rows, instead of evaluating them
    # across the table and joining the survivors back. Measured on the live database, a
    # `q=news` row request went 285ms -> 245ms with this one conjunct added. Memberships
    # are bounded (50 on this install) - the same fact §5.2's first rule rests on. Read off
    # `ctx`, which resolved it once for the whole request alongside the fold predicate.
    member_ids = ctx.group_member_channel_ids
    if not member_ids:
        member_side = db.false()
    else:
        member_side = ChannelGroup.id.in_(
            select(ChannelGroupMember.group_id)
            .select_from(ChannelGroupMember)
            .join(Channel, Channel.id == ChannelGroupMember.channel_id)
            .where(Channel.id.in_(list(member_ids)), *member_preds))
    name_side = _group_name_predicate(state)
    reach = member_side if name_side is None else or_(member_side, name_side)
    # The eager options are what keep the row builder's walk of memberships and their
    # channels two constant statements instead of two per group - the same reason the Groups
    # page uses them, and the same test file caught it.
    from .channel_groups import member_eager_options
    return (ChannelGroup.query
            .options(*member_eager_options())
            .filter(*_group_row_filters(state, ctx))
            .filter(reach).all())


def group_member_preds(state: SearchState, ctx: 'SearchContext', narrowing: list) -> list:
    """The predicates `matching_groups()` reaches a group's members through.

    TWO OPTIONS ARE DELIBERATELY LEFT OUT, and both for a reason before a cost.

    `showmembers` is the fold itself, whose whole definition is "this member is represented
    by its group" - feeding it back in here would remove the very members that put the group
    on screen.

    **A cluster option decides which COPY of a duplicated feed to show. It says nothing
    about whether a group matches what you typed**, which is the only question this asks -
    the row it puts on screen is the GROUP's, not the member's, so "show me one of these
    three identical channels" has no bearing on it. It is also by far the most expensive
    predicate the engine has: `showdup` ranks a window over every duplicate row in the
    table, and including it here paid that a second time per keystroke. Measured on the live
    database (112,968 channels): a `q=news` row request went 345ms -> 240ms with it dropped,
    and `q=football` 144ms -> 116ms.
    """
    return list(narrowing) + [
        pred for key, pred in standing_predicates(state, ctx, narrowing, by_key=True)
        if key != 'showmembers' and not STANDING_BY_KEY[key].cluster]


# The sort keys, in Python, for the merge below. They exist because the merge has to decide
# where a group falls among channels SQLite ordered, and the only honest way to do that is to
# spell SQLite's own answer rather than an approximation of it.


def _sqlite_lower(text: str) -> str:
    """SQLite's `lower()`, which is ASCII-only - `Ä` comes back unchanged from it and
    lowercased from Python's `str.lower()`. Ordering a merged page by two different
    definitions of one expression is how a row appears on two pages and on neither."""
    return ''.join(c.lower() if 'A' <= c <= 'Z' else c for c in text)


#: Sorts ascending ahead of every real value, the way SQLite orders NULL.
_NULL_FIRST = (0,)
_NOT_NULL = (1,)


def _null_key(value):
    return _NULL_FIRST if value is None else _NOT_NULL + (value,)


#: Which kind wins a dead-heat. A group sorting ahead of a channel it ties with is arbitrary,
#: but it has to be SOMETHING and it has to be the same something every time, or the row on
#: the seam between two pages is served twice or not at all.
_TIE_GROUP = 0
_TIE_CHANNEL = 1


def group_sort_key(group, sort: str):
    """Where a group falls under `sort`, as a tuple comparable with `channel_sort_key`'s.

    The leading element is the placement rung: 0 for a group under a sort it cannot answer
    (they land first, ordered by name), 1 for everything carrying a real value. A channel is
    always 1, so the two orders compose without either knowing about the other's rows.
    """
    if sort not in GROUP_SORTS:
        return (0, _sqlite_lower(group.name or ''), _TIE_GROUP, group.id)
    if sort == 'health':
        return (1, _null_key(group.health_score), _TIE_GROUP, group.id)
    return (1, _sqlite_lower(group.name or ''), _TIE_GROUP, group.id)


def channel_sort_key(channel, sort: str):
    """The same order `SORTS[sort]` gives SQLite, spelled in Python.

    **It must agree with the SQL exactly, tiebreak included** - `_with_tiebreak` appends
    `Channel.id` to every channel sort, so this does too, and nothing else may be inserted
    ahead of it. Adding a name tiebreak to the health key here (which reads more naturally)
    would reorder equal-health channels against the order the database just returned them in.

    Under the five sorts a group cannot answer, every group sorts ahead of every channel and
    the channel's own value is never consulted - the window arrives in SQL order and a stable
    sort keeps it.
    """
    if sort not in GROUP_SORTS:
        return (1,)
    if sort == 'health':
        score = None
        if channel.health_score is not None:
            score = channel.health_score + (channel.manual_health_adjustment or 0)
        return (1, _null_key(score), _TIE_CHANNEL, channel.id)
    return (1, _sqlite_lower(channel.name or ''), _TIE_CHANNEL, channel.id)


def active_standing(state: SearchState) -> list:
    """The standing options this search actually applies, in registry order.

    "Applies" means "removes rows", which for a `show*` key means it is ABSENT - see
    `standing_applied()`. So this list is what `standing_hidden` reports counts for, and a
    ticked "Show duplicates" correctly contributes nothing to it.

    Out-of-grain options are skipped for the same reason out-of-grain filters are (see
    `dimension_predicates`): flipping grain must not silently discard the set.
    """
    return [s for s in STANDING_OPTIONS
            if standing_applied(state.standing, s.key) and _in_grain(s, state.grain)]


def _cluster_scope(state: SearchState, ctx: 'SearchContext', narrowing: list) -> list:
    """The rows an airing cluster option ranks over: everything a row-level predicate keeps.

    Cluster options are excluded from their own scope - `firstonly` must not be defined in
    terms of what `grpdedup` left, or the answer depends on which of the two is evaluated
    first. On the channel grain this is unused: `dup` is deliberately whole-table.
    """
    if state.grain != GRAIN_AIRINGS:
        return []
    return list(narrowing) + [not_(_standing_reject(s.key, ctx, state=state))
                              for s in active_standing(state) if not s.cluster]


def standing_predicates(state: SearchState, ctx: 'SearchContext', narrowing=(),
                        by_key: bool = False) -> list:
    """One NOT-reject predicate per active standing option, in registry order.

    `by_key=True` pairs each with its option key, for the same reason `dimension_predicates`
    offers it - see `_split_predicates()`.
    """
    scope = _cluster_scope(state, ctx, list(narrowing))
    out = [(s.key, not_(_standing_reject(s.key, ctx, scope, state)))
           for s in active_standing(state)]
    return out if by_key else [pred for _key, pred in out]


# ---------------------------------------------------------------------------
# Per-request context
# ---------------------------------------------------------------------------

@dataclass
class SearchContext:
    """The per-request facts every predicate above needs, resolved exactly once.

    This type exists to make CLAUDE.md's "no hidden I/O in per-row loops" structurally hard
    to violate: config, the tag rows, the readiness tuple and the set of normalizing
    accounts are all read here, before any query is built, and the builders take them as
    arguments rather than reaching for them. Build it once per request and pass it down.
    """
    cfg: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)
    normalizing_account_ids: frozenset = frozenset()
    #: {account id: last_sync_at}. Read once so the "deleted by provider" predicate can be
    #: a handful of constants instead of a correlated subquery - see _missing_predicate.
    account_last_sync: dict = field(default_factory=dict)
    #: Account ids past their first-sync era (>= 2 completed syncs, and the earliest one not
    #: itself inside the configured "new" window) - the set _new_predicate() is allowed to
    #: match against. Precomputed once per account, same reasoning as account_last_sync.
    new_eligible_account_ids: frozenset = frozenset()
    #: {index name tuple: (ready, reason)} - both scopes, evaluated once. Two entries rather
    #: than one because a search over channel names alone must not be pushed onto the slow
    #: path just because the program index happens to be mid-rebuild.
    readiness: dict = field(default_factory=dict)
    tags_by_name: dict = field(default_factory=dict)
    #: {account id: Account}. The rows a result page carries name their account, and reading
    #: it off `channel.account` per row would be the N+1 this type exists to prevent. Every
    #: account is loaded here anyway for normalizing_account_ids, so keeping them costs
    #: nothing and gives the row builder its own batched source.
    accounts_by_id: dict = field(default_factory=dict)
    #: ONE clock for the whole request. Every `when` value, the `past` option and the Record
    #: button's five states are answered against this, so a search cannot report a showing as
    #: "on now" in one predicate and "ended" in the next because the second ran a second later.
    now: datetime = field(default_factory=datetime.utcnow)
    #: The display timezone, resolved once. `today` and `tomorrow` are DAY boundaries and a
    #: day starts where the person reading the page is, not at 00:00 UTC.
    display_tz: object = None
    #: {'today': (start, stop), 'tomorrow': (start, stop)} as naive UTC.
    day_bounds: dict = field(default_factory=dict)
    #: {(term, columns): matching chan_prog rows}. The airing planner's probe runs a real
    #: query and text_predicates() is called once for the rows and once per facet dimension,
    #: so without this the planner would cost seven probes to answer one question.
    probe_cache: dict = field(default_factory=dict)
    #: {(term, ONE field key): channel ids airing matching text RIGHT NOW}. Same shape and
    #: same reason as probe_cache above, and the same reason `group_member_channel_ids` is a
    #: bounded id list rather than a subquery - see now_program_channel_ids().
    now_program_cache: dict = field(default_factory=dict)
    #: {tag id: channel ids airing something carrying that tag RIGHT NOW}. The channel grain's
    #: half of a tag, resolved once per request for the same reason as now_program_cache - the
    #: tag facet, the row badge and the filter itself all ask it, and the scan behind it costs
    #: the same every time. Behind this sits a short-lived cross-request cache (_now_tag_sets).
    now_tag_cache: dict = field(default_factory=dict)
    #: Every channel id that is in any channel group, read once. It is here for exactly the
    #: reason this type exists: the fold predicate (`showmembers`) and the group query both
    #: need it, they run in the row query, the standing breakdown and every facet aggregate,
    #: and `_standing_reject` is not allowed a query of its own. It is also the CHEAPEST
    #: spelling - a bounded id list lets SQLite test 112,968 channels against an ephemeral
    #: index of 50 instead of seeking `ix_channel_group_members_channel_id` per row.
    #: Empty is a real answer (no memberships exist), and the fold then removes nothing.
    group_member_channel_ids: tuple = ()

    @classmethod
    def build(cls, cfg=None) -> 'SearchContext':
        from .accounts import NORM_DISABLED, resolve_normalization_mode
        from .config import load_config
        from .tz_utils import UTC, to_naive_utc, get_display_tz
        cfg = cfg if cfg is not None else load_config()
        tags = Tag.query.order_by(Tag.name).all()
        accounts = Account.query.all()
        normalizing = frozenset(
            a.id for a in accounts if resolve_normalization_mode(a, cfg) != NORM_DISABLED)
        now = datetime.utcnow()
        tz = get_display_tz()
        local_midnight = (now.replace(tzinfo=UTC).astimezone(tz)
                          .replace(hour=0, minute=0, second=0, microsecond=0))

        def _utc(local):
            return to_naive_utc(local)

        # Built by adding a day to a LOCAL midnight and re-converting, not by adding 24h to
        # the UTC value: across a DST boundary the local day is 23 or 25 hours long, and the
        # 25-hour one would otherwise drop an hour of showings out of both buckets.
        days = [_utc(local_midnight + timedelta(days=n)) for n in range(3)]

        # First-sync-era eligibility for the 'new' filter, mirrored from
        # accounts.channel_lifecycle_state()'s 'new' branch - same two aggregates per
        # account (completed sync count, earliest sync started_at), computed once here
        # rather than per channel row.
        new_days = cfg.get('sync', {}).get('channel_new_within_days', 3)
        new_eligible = frozenset()
        if new_days > 0 and accounts:
            account_ids = [a.id for a in accounts]
            completed_counts = dict(
                db.session.query(AccountSyncLog.account_id, func.count(AccountSyncLog.id))
                .filter(AccountSyncLog.account_id.in_(account_ids),
                        AccountSyncLog.status.in_(['SUCCESS', 'PARTIAL']))
                .group_by(AccountSyncLog.account_id).all())
            earliest_by_account = dict(
                db.session.query(AccountSyncLog.account_id, func.min(AccountSyncLog.started_at))
                .filter(AccountSyncLog.account_id.in_(account_ids))
                .group_by(AccountSyncLog.account_id).all())
            new_cutoff = now - timedelta(days=new_days)
            eligible = set()
            for a in accounts:
                first_sync_marker = earliest_by_account.get(a.id) or a.created_at
                in_first_sync_era = (
                    completed_counts.get(a.id, 0) < 2
                    or (first_sync_marker is not None and first_sync_marker > new_cutoff))
                if not in_first_sync_era:
                    eligible.add(a.id)
            new_eligible = frozenset(eligible)

        member_ids = tuple(
            row[0] for row in
            db.session.execute(select(ChannelGroupMember.channel_id).distinct()))

        return cls(cfg=cfg, tags=tags, normalizing_account_ids=normalizing,
                   group_member_channel_ids=member_ids,
                   account_last_sync={a.id: a.last_sync_at for a in accounts},
                   new_eligible_account_ids=new_eligible,
                   readiness=readiness_map(),
                   tags_by_name={t.name: t for t in tags},
                   accounts_by_id={a.id: a for a in accounts},
                   now=now, display_tz=tz,
                   day_bounds={WHEN_TODAY: (days[0], days[1]),
                               WHEN_TOMORROW: (days[1], days[2])})

    def probe(self, term_text: str, columns: tuple) -> int:
        """The airing planner's probe, memoized for the request. See airing_probe_count."""
        key = (term_text, columns)
        if key not in self.probe_cache:
            self.probe_cache[key] = airing_probe_count(term_text, columns)
        return self.probe_cache[key]

    def now_program_channel_ids(self, term: 'Term', fields: tuple) -> tuple:
        """Channel ids whose CURRENT showing matches `term` on any of `fields`, memoized.

        Resolved to a bounded id list once per request rather than left as a subquery, and
        that is a performance decision with a measured reason. An interval overlap has no
        selective side (`_when_predicate`'s WHEN_NOW comment has the row counts), so this
        costs ~0.38s of `ix_epg_entries_start_stop` scan however it is spelled - and
        `text_predicates()` is called once for the rows, once for the standing breakdown and
        once per facet dimension, so a subquery pays that ~10 times in one request. Measured
        over HTTP on the live 2.08M-row database: `news` over `epg-title` was 4.6s as a
        subquery and 0.6s memoized, against 0.6s for the old chan_prog predicate this
        replaced (dev/changelog/861).

        Same technique `_cached_tag_channel_ids` uses on the tag side, for the same reason -
        handing the planner a list sidesteps its choice of driving predicate instead of
        trying to out-guess it - and the same bound applies: at most one showing per channel
        is on at any instant, so this list can never exceed the channel count, and on this
        database its ceiling is 31,495 (measured, against 35,187 showings on now).

        Not gated on a search index and deliberately so: chan_prog is deduped across showings
        and holds no times, so it cannot answer "now" at all. That makes this the one program
        predicate whose results do not change when an index is mid-rebuild.

        **Cached per FIELD and filled for every missing field in ONE scan**, because the two
        callers ask overlapping questions: `text_predicates` asks about all three program
        fields at once and `field_hit_predicate` then asks about each one alone to draw the
        "why" chip. Caching whole field sets made those four different keys and so four scans
        of the same 35,187 rows - measured at 5.0s for a three-field search, against 1.4s for
        one field. Per-field keys make it one scan for any combination.
        """
        wanted = [f for f in fields if (term.text, f.key) not in self.now_program_cache]
        if wanted:
            by_key = {'title': EPGEntry.title, 'sub_title': EPGEntry.sub_title,
                      'description': EPGEntry.description}
            entry_fields = [
                replace(f, column=by_key[f.fts_column], cast_text=False) for f in wanted]
            likes = [_column_like(f, term) for f in entry_fields]
            # The WHERE keeps only rows matching SOMETHING; the flags say which field it was,
            # so one pass answers every field the request will ask about separately.
            flags = [case((clause, 1), else_=0) for clause in likes]
            rows = db.session.execute(
                select(EPGEntry.channel_id, *flags).where(
                    EPGEntry.start_time <= self.now,
                    _unindexed(EPGEntry.stop_time) > self.now,
                    or_(*likes)).distinct()).all()
            found = {f.key: set() for f in wanted}
            for row in rows:
                for f, flag in zip(wanted, row[1:]):
                    if flag:
                        found[f.key].add(row[0])
            for f in wanted:
                self.now_program_cache[(term.text, f.key)] = frozenset(found[f.key])
        out = set()
        for f in fields:
            out |= self.now_program_cache[(term.text, f.key)]
        return tuple(sorted(out))

    def now_tag_channel_ids(self, tag: 'Tag') -> tuple:
        """Channel ids whose CURRENT showing carries `tag`, memoized for the request.

        The channel grain's half of "does this channel carry this tag" since
        dev/changelog/862 - see `_tag_predicate`. A bounded id list rather than a subquery for
        the reason `now_program_channel_ids` spells out at length, and here the bound is the
        same: at most one showing per channel is on at any instant.

        Filled for the whole tag vocabulary at once, because one scan answers every tag (see
        `_scan_now_tag_channel_ids`). A tag that is not in `self.tags` - a caller holding a
        bare context - joins that one scan rather than being given a second one.
        """
        if tag.id not in self.now_tag_cache:
            vocabulary = list(self.tags)
            if not any(t.id == tag.id for t in vocabulary):
                vocabulary.append(tag)
            self.now_tag_cache.update(_now_tag_sets(vocabulary, self.now, self.cfg))
            self.now_tag_cache.setdefault(tag.id, frozenset())
        return tuple(sorted(self.now_tag_cache[tag.id]))

    def readiness_for(self, fields) -> tuple:
        """(is the index this set of fields needs usable, why not). False means "scan
        instead", never "return nothing" - the unindexed path returns the same results."""
        names = _index_names(tuple(fields))
        if names in self.readiness:
            return self.readiness[names]
        return search_index_readiness(*names)


# ---------------------------------------------------------------------------
# Running the search
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    """What the endpoint serializes. `total` counts what the list would show; `standing_hidden`
    counts, per option, what it took out of that same set - so the two add up and every number
    the user is shown is one they can act on.

    `total`/`pages`/`standing_hidden` are `None` rather than a number when `search()` was
    called with `want_counts=False` - a pending count, not a zero one (dev/changelog/598)."""
    rows: list = field(default_factory=list)
    total: int | None = 0
    #: The two numbers behind `total`, never one blended one (DESIGN-group-search-rows.md
    #: §5.2's third rule). `total` is their sum because that is what the PAGER has to page,
    #: but the heading names both kinds and the facet rail counts only the channels - a
    #: single number over two tables is the one number here the user could not explain.
    channel_total: int | None = 0
    group_total: int = 0
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    pages: int | None = 0
    standing_hidden: dict | None = field(default_factory=dict)
    facets: dict = field(default_factory=dict)
    kept_ids: frozenset = frozenset()
    #: {EPGEntry id: (group id, ...)} - the groups each surviving airing row WON, and so
    #: stands for, under "Collapse channel groups". Empty when that option is off, because
    #: then every member has a row of its own and naming one of them as the group would be a
    #: lie. Ids rather than rows: which of several a row is LABELLED with is presentation,
    #: and `app/channel_search_rows.py` owns it.
    airing_group_ids: dict = field(default_factory=dict)
    degraded: str = ''


def base_predicates(state: SearchState, ctx: SearchContext, skip_dimension: str = '',
                    text_preds: list | None = None) -> list:
    """Everything narrowing the result set: the typed query, the filters, the standing options.

    Applied as a flat list rather than a prebuilt query so each caller (the row query, the
    total, six facet aggregates) can attach them to a query shaped for its own job and leave
    the planner free to use the facet indexes migration 24 creates.

    `text_preds` is the one part that is identical for every dimension, so the facet counter
    builds it once and hands it in rather than re-deriving it six times.
    """
    if text_preds is None:
        text_preds = text_predicates(state, ctx)
    narrowing = list(text_preds) + dimension_predicates(state, ctx, skip=skip_dimension)
    return narrowing + standing_predicates(state, ctx, narrowing)


#: Which table a predicate narrows, for the split `_combined_facet_scan`'s tally shape needs.
#: `channel` reads only `channels` columns at its top level, `epg` only `epg_entries` ones -
#: what a self-contained (uncorrelated) subquery reads inside itself does not count, because
#: it is evaluated on its own. A key absent from either map is unsplittable and forces the
#: fallback, which is why `tag` is deliberately absent from the second: a tag predicate reads
#: channel names AND the programs on them, so it belongs to neither side.
_SIDE_CHANNEL = 'channel'
_SIDE_EPG = 'epg'
_STANDING_SIDE = {
    'showhidden': _SIDE_CHANNEL,
    'showdup': _SIDE_CHANNEL, 'shownotnorm': _SIDE_CHANNEL, 'shownoepg': _SIDE_CHANNEL,
    'showuntested': _SIDE_CHANNEL, 'showmembers': _SIDE_CHANNEL,
    'showpast': _SIDE_EPG, 'firstonly': _SIDE_EPG, 'grpdedup': _SIDE_EPG,
}
#: The channel-side standing options cheap enough to defer past the tally - the two that
#: hide by default and reject a fixed handful of rows (~1,600 duplicate losers, and whatever
#: normalization left alone) rather than narrowing the set. `shownoepg` and `showuntested`
#: are channel-side too but genuinely selective, and deferring one of those costs more than
#: the split saves: measured on the production database, `noepg`+`untested` together are
#: 717ms joined against 909ms split. An option not named here forces the joined shape.
#:
#: `showhidden` is here because that is what it is TODAY - with no rule engine yet, the only
#: hidden channels are ones somebody hid by hand, which is the same "fixed handful" shape as
#: the other two. It is the one entry whose membership depends on user data rather than on
#: the predicate: once blanket rules exist a realistic rule set hides about half the table,
#: which is `shownoepg` territory, and the entry has to be re-measured rather than assumed
#: (dev/changelog/775). Leaving it out instead would have cost the landing-page split - a
#: measured 1033ms -> 2125ms on the airings rail - to pre-pay for a state that does not
#: exist yet.
_SPLIT_SAFE_STANDING = frozenset({'showhidden', 'showdup', 'shownotnorm'})
_DIMENSION_SIDE = {
    'acct': _SIDE_CHANNEL, 'health': _SIDE_CHANNEL, 'cat': _SIDE_CHANNEL,
    'other': _SIDE_CHANNEL, 'group': _SIDE_CHANNEL, 'chan': _SIDE_CHANNEL,
    'when': _SIDE_EPG, 'duration': _SIDE_EPG,
}


def split_predicates(state: SearchState, ctx: SearchContext, text_preds: list):
    """`base_predicates()` sorted into (channel-side, epg-side), or None if it cannot be.

    The same predicate set as `base_predicates(state, ctx, text_preds=text_preds)`, split by
    which table each half reads so a counting query can narrow `epg_entries` **before** it
    joins `channels` rather than after. That reordering is the whole saving - see
    `_combined_facet_scan`.

    Returns None rather than guessing whenever the state contains anything this cannot place:
    a typed query (its predicates span both tables and the FTS indexes), or a filter on a
    dimension with no entry in `_DIMENSION_SIDE`. The caller then keeps the joined shape,
    which is always correct. **None must stay the safe answer** - a predicate placed on the
    wrong side silently changes the counts rather than failing, so a new dimension or
    standing option is unsplittable until someone adds it to a map above deliberately.

    **A channel-side FILTER also returns None, and that one is about cost, not correctness.**
    Deferring the join pays only while the channel side is standing options, which reject a
    fixed ~1,600 rows and narrow nothing: the tally then reads the same showings the joined
    shape would. A filter the user picked is usually far more selective than that, and the
    joined shape can apply it before touching `epg_entries` while the tally cannot - measured
    on the production database, one category filter is 91ms joined against 1004ms split.
    Splitting there would make the rail eleven times slower for anyone who clicks a facet.
    `_SPLIT_SAFE_STANDING` applies the same test to the standing options.
    """
    if text_preds:
        return None
    keyed_dims = dimension_predicates(state, ctx, by_key=True)
    narrowing = [pred for _key, pred in keyed_dims]
    dim_sides = [_DIMENSION_SIDE.get(key) for key, _pred in keyed_dims]
    if any(side is None or side == _SIDE_CHANNEL for side in dim_sides):
        return None
    keyed_standing = standing_predicates(state, ctx, narrowing, by_key=True)
    if any(_STANDING_SIDE.get(key) == _SIDE_CHANNEL and key not in _SPLIT_SAFE_STANDING
           for key, _pred in keyed_standing):
        return None
    keyed = (list(zip(dim_sides, narrowing)) +
             [(_STANDING_SIDE.get(key), pred) for key, pred in keyed_standing])
    if any(side is None for side, _pred in keyed):
        return None
    return ([pred for side, pred in keyed if side == _SIDE_CHANNEL],
            [pred for side, pred in keyed if side == _SIDE_EPG])


def search(state: SearchState, ctx: SearchContext | None = None,
          want_counts: bool = True, want_facets: bool = True) -> SearchResult:
    """Run a search. The one entry point; everything else in this module supports it.

    `want_counts=False` skips `_standing_breakdown()` - on the airing grain that query alone
    is ~1.6s of a ~2.1s unfiltered request (dev/changelog/598), and the row query never
    depended on its output (only on `standing_predicates()`'s `kept` filters, computed either
    way). `total`/`pages`/`standing_hidden` come back `None` rather than a real (possibly
    stale-looking) number - the row page renders them as pending, and `search_counts()` below
    answers them separately once the caller wants them.

    `want_facets=False` is the same seam for the rail: `facets` comes back empty, so
    `facets_counted` is empty too and the rail renders "not counted" rather than zero. It is
    a separate lever from `state.facets` on purpose - that one is the CALLER naming which
    dimensions it wants, this one is the endpoint overriding the answer to none because the
    box cannot afford them right now (dev/changelog/676).
    """
    if state.grain not in IMPLEMENTED_GRAINS:
        raise SearchStateError(f'unknown result grain {state.grain!r}')
    if state.sort not in SORTS_BY_GRAIN[state.grain]:
        raise SearchStateError(
            f'cannot sort {state.grain} by {state.sort!r} - sortable: '
            f'{", ".join(sorted(SORTS_BY_GRAIN[state.grain]))}')
    ctx = ctx or SearchContext.build()
    if state.grain == GRAIN_CHANNELS:
        return _search_channels(state, ctx, want_counts, want_facets)
    return _search_airings(state, ctx, want_counts, want_facets)


def search_counts(state: SearchState, ctx: SearchContext) -> tuple:
    """(standing_hidden, total, pages, group_total) alone - what a counts-only request needs,
    without
    paying for the row query or the facet rail `search()` also builds. Same narrowing
    `_search_channels`/`_search_airings` build; this is the other half of the split
    `want_counts=False` created in `search()` (dev/changelog/598)."""
    if state.grain not in IMPLEMENTED_GRAINS:
        raise SearchStateError(f'unknown result grain {state.grain!r}')
    text_preds = text_predicates(state, ctx)
    narrowing = text_preds + dimension_predicates(state, ctx)
    hidden, total, pages = _breakdown_and_pages(state, ctx, narrowing)
    # The group rows are part of what the pager pages, so a counts-only request has to
    # include them or the page's own count line disagrees with the pager beside it. Adding
    # the group query here rather than teaching the caller to add it is what keeps the two
    # entry points answering the same question (dev/changelog/598 split them; nothing since
    # has been allowed to make them diverge).
    groups = len(matching_groups(state, ctx, group_member_preds(state, ctx, narrowing)))
    if groups:
        total += groups
        pages = (total + state.page_size - 1) // state.page_size
    return hidden, total, pages, groups


def _order_key(element):
    """A column identity for an ORDER BY term, or None for anything not a plain column.

    `.desc()` wraps the column in a UnaryExpression, and direction is irrelevant here: a
    term repeating a column already ordered on can only break ties the first term already
    broke, so it cannot change a single row's position whichever way it points.
    """
    inner = getattr(element, 'element', element)
    table = getattr(inner, 'table', None)
    name = getattr(inner, 'name', None)
    return (table.name, name) if table is not None and name else None


def _with_tiebreak(order: list, *tiebreak) -> list:
    """`order` plus each tiebreak column it does not already order on.

    A repeated ORDER BY term is a no-op in result terms and was never intended - it fell out
    of the airing sorts, seven of which already end in `start_time`, having `start_time`
    appended again. It is NOT a no-op to the query planner: the duplicate is what used to
    make SQLite pick ix_epg_entries_start_stop over the standalone start_time index, and
    deleting it as obvious dead weight cost 13x on the default airings page until migration
    39 removed the decoy index itself. Dedupe here rather than at each sort's registry entry,
    so a new sort cannot reintroduce it (dev/changelog/692).
    """
    seen = {k for k in (_order_key(o) for o in order) if k}
    return list(order) + [t for t in tiebreak if _order_key(t) not in seen]


def _page_rows(query, base, pk, order: list, state: SearchState,
               offset: int | None = None, limit: int | None = None) -> list:
    """One page of `query`, fetched by selecting its primary keys first.

    The obvious spelling - `query.order_by(...).limit(n).offset(k)` - makes SQLite build
    every one of the k skipped rows in full before discarding them, and a channel row is
    30 columns plus the LEFT JOIN that `Channel.default_profile`'s lazy='joined' drags
    along. Selecting `pk` for the skipped rows instead and joining the surviving 100 back
    by rowid is the same answer for a fraction of the work: on the live database the
    channel grain's default sort goes 400ms -> 135ms at offset 100,000 and 64ms -> 22ms at
    offset 10,000, while page 1 is unchanged (dev/changelog/697).

    THIS MUST STAY ONE STATEMENT. The two-round-trip version of the same idea measures
    identically and is wrong: pysqlite runs SELECTs in autocommit, so no read transaction
    spans two statements, and a delete landing between the id fetch and the row fetch
    silently returns a short page. As a subquery both halves read one snapshot.

    `base` is the query the outer half starts from, not `query` itself - the narrowing and
    standing filters have already done their work inside the subquery, but the airing
    grain's Channel join has to survive, because four of its sorts order by Channel
    columns.

    `offset`/`limit` override the page arithmetic for the one caller that needs a window
    rather than a page - the group merge below, which reads a few rows either side of the
    page so it can place group rows among them. Everything about the statement is otherwise
    unchanged, including that it stays ONE statement.
    """
    if offset is None:
        offset = (state.page - 1) * state.page_size
    if limit is None:
        limit = state.page_size
    ids = (query.with_entities(pk)
           .order_by(*order)
           .limit(limit)
           .offset(offset)
           .scalar_subquery())
    return base.filter(pk.in_(ids)).order_by(*order).all()


def search_facets(state: SearchState, ctx: SearchContext) -> dict:
    """The facet rail alone - the other half of the split `search_counts()` above started.

    Before this existed the page fetched its rail from `GET /api/channels/search` with
    `facets=<dims>&counts=0`, which ran the whole LIMIT-100 row query and threw the rows
    away: every keystroke paid the page query TWICE. On this database that is ~3.4s of
    duplicated work per degraded keystroke, and it was never free on a healthy one either
    (dev/changelog/676).
    """
    if state.grain not in IMPLEMENTED_GRAINS:
        raise SearchStateError(f'unknown result grain {state.grain!r}')
    return compute_facets(state, ctx)


def _breakdown_and_pages(state: SearchState, ctx: SearchContext, narrowing: list) -> tuple:
    """(standing_hidden, total, pages) - the one place all three callers (both grains' full
    search, and the counts-only endpoint) turn a breakdown into the page count the UI needs."""
    hidden, total = _standing_breakdown(state, ctx, narrowing)
    pages = (total + state.page_size - 1) // state.page_size
    return hidden, total, pages


def _merge_group_page(page, state: SearchState, groups: list) -> list:
    """One page of the merged channel + group list, in `state.sort` order.

    THE WHOLE POINT IS THAT THE CHANNEL QUERY IS UNCHANGED. Groups arrive as a separate,
    already-fetched list of at most tens of rows (§5.2's first rule); this places them among
    the channels the paging subquery returned and never widens that subquery. `page(offset,
    limit)` fetches a window of channels in SQL order.

    Under a sort a group cannot answer, groups land first and the merge is arithmetic. Under
    `name` or `health` they interleave, and the window is the page shifted back by the number
    of groups and widened by the same amount - exactly enough, because a merged position `p`
    can only be held by a channel of rank between `p - G` and `p`. It costs one extra channel
    row per group, not one query per group.
    """
    size = state.page_size
    offset = (state.page - 1) * size
    count = len(groups)
    items = []

    if state.sort not in GROUP_SORTS:
        # Groups first, in NAME order, whichever direction the sort runs: the direction
        # applies to a value a group does not have, so reversing it would move rows for a
        # reason the UI cannot explain. The channels keep their own direction.
        ranked = sorted(groups, key=lambda g: (_sqlite_lower(g.name or ''), g.id))
        items = [(index, g) for index, g in enumerate(ranked)
                 if offset <= index < offset + size]
        lo = max(0, offset - count)
        hi = offset + size - count
        if hi > lo:
            for i, channel in enumerate(page(lo, hi - lo)):
                items.append((lo + i + count, channel))
        items.sort(key=lambda pair: pair[0])
        return [row for _position, row in items]

    from bisect import bisect_left

    ranked = sorted(groups, key=lambda g: group_sort_key(g, state.sort),
                    reverse=state.sort_desc)
    keys = [group_sort_key(g, state.sort) for g in ranked]
    # bisect needs an ascending list. A descending order is the same order read backwards, so
    # the reversed keys are it, and the index mirrors back. No key can tie a channel's - the
    # kind rung inside both keys sees to that - so bisect_left and bisect_right agree here.
    ascending = keys[::-1] if state.sort_desc else keys

    def groups_before(key):
        if state.sort_desc:
            return count - bisect_left(ascending, key)
        return bisect_left(ascending, key)

    lo = max(0, offset - count)
    window = page(lo, offset + size - lo)
    channel_keys = [channel_sort_key(c, state.sort) for c in window]
    for i, channel in enumerate(window):
        items.append((lo + i + groups_before(channel_keys[i]), channel))
    # A group's position is its own index among the groups plus the number of channels before
    # it. Inside the window that is exact. Outside it the count is wrong by however many
    # channels the window skipped - and the result still lands off the page in the same
    # direction, so the row is dropped either way and no page can gain or lose one.
    for index, group in enumerate(ranked):
        before = sum(1 for key in channel_keys
                     if (key > keys[index] if state.sort_desc else key < keys[index]))
        items.append((index + lo + before, group))

    items.sort(key=lambda pair: pair[0])
    return [row for position, row in items if offset <= position < offset + size]


def _search_channels(state: SearchState, ctx: SearchContext,
                     want_counts: bool = True,
                     want_facets: bool = True) -> SearchResult:
    text_preds = text_predicates(state, ctx)
    narrowing = text_preds + dimension_predicates(state, ctx)
    kept = standing_predicates(state, ctx, narrowing)

    # ONE query for every group this search returns, whatever the page (§5.2 rule 1). It runs
    # before the counts because the total the pager needs is the merged one.
    groups = matching_groups(state, ctx, group_member_preds(state, ctx, narrowing))

    if want_counts:
        hidden, channel_total, _pages = _breakdown_and_pages(state, ctx, narrowing)
        total = channel_total + len(groups)
        pages = (total + state.page_size - 1) // state.page_size
    else:
        hidden, channel_total, total, pages = None, None, None, None

    query = Channel.query.filter(*narrowing, *kept)
    order = list(SORTS[state.sort]())
    if state.sort_desc:
        order = [o.desc() for o in order]
    # A stable tiebreak, always. Without it SQLite is free to return page 2 of a
    # category-sorted list in a different order than it returned page 1's tail, and rows
    # appear to duplicate or vanish as the user pages.
    tiebroken = _with_tiebreak(order, Channel.id)

    def page(offset, limit):
        return _page_rows(query, Channel.query, Channel.id, tiebroken, state,
                          offset=offset, limit=limit)

    rows = _merge_group_page(page, state, groups) if groups else page(None, None)

    return SearchResult(
        rows=rows,
        total=total,
        channel_total=channel_total,
        group_total=len(groups),
        page=state.page,
        page_size=state.page_size,
        pages=pages,
        standing_hidden=hidden,
        facets=compute_facets(state, ctx, text_preds=text_preds) if want_facets else {},
        kept_ids=_kept_ids(state, rows),
        degraded=degraded_reason(state, ctx),
    )


def _airing_query():
    """The base every airing query is built on: one row per showing, its channel joined.

    The join is what lets every channel-grain predicate compose in unchanged - the standing
    options, the tag/account/health/group/category/other dimensions and the channel-side
    search fields are all `Channel` expressions, and under the airing grain they describe the
    channel a showing is on. That is DESIGN-channel-search.md §1's caveat, honoured
    structurally rather than by remembering it at each call site.
    """
    return EPGEntry.query.join(Channel, Channel.id == EPGEntry.channel_id)


def _search_airings(state: SearchState, ctx: SearchContext,
                    want_counts: bool = True,
                    want_facets: bool = True) -> SearchResult:
    text_preds = text_predicates(state, ctx)
    narrowing = text_preds + dimension_predicates(state, ctx)
    kept = standing_predicates(state, ctx, narrowing)

    if want_counts:
        hidden, total, pages = _breakdown_and_pages(state, ctx, narrowing)
    else:
        hidden, total, pages = None, None, None

    query = _airing_query().filter(*narrowing, *kept)
    order = list(SORTS_AIRINGS[state.sort]())
    if state.sort_desc:
        order = [o.desc() for o in order]
    # Same stable tiebreak the channel grain has, and it matters more here: hundreds of
    # showings share one start time, so without it page 2 can repeat page 1's tail.
    rows = _page_rows(query, _airing_query(), EPGEntry.id,
                      _with_tiebreak(order, EPGEntry.start_time, EPGEntry.id), state)

    # Only while the collapse is actually collapsing. With "Collapse channel groups" off,
    # every member carries its own row for the same program, and labelling one of them as
    # the group would name a row the other rows are equally part of.
    airing_group_ids = {}
    if standing_applied(state.standing, 'grpdedup'):
        airing_group_ids = group_wins_by_entry(
            [r.id for r in rows], _cluster_scope(state, ctx, narrowing))

    return SearchResult(
        rows=rows,
        total=total,
        channel_total=total,
        airing_group_ids=airing_group_ids,
        page=state.page,
        page_size=state.page_size,
        pages=pages,
        standing_hidden=hidden,
        facets=compute_facets(state, ctx, text_preds=text_preds) if want_facets else {},
        # The KEPT badge is about a channel being the survivor of a duplicate cluster, and a
        # showing is not. The airing row's Channel cell carries the DUP badge off the row
        # payload instead.
        kept_ids=frozenset(),
        degraded=degraded_reason(state, ctx),
    )


def degraded_reason(state: SearchState, ctx: SearchContext) -> str:
    """Why this search ran unindexed, or '' when it did not.

    Surfaced rather than only logged: a search that is correct but ten times slower is
    exactly the kind of hidden behavior this project's founding principle says to show.

    Public because the endpoint needs the same sentence for a request that blew its time
    budget (dev/changelog/418), and a search that runs long during a stale window is
    running long *for this reason*. Re-deriving that wording there would be a second
    speller of a string this module owns.
    """
    if not state.q:
        return ''
    fields = tuple(FIELD_BY_KEY[k] for k in state.fields if k in FIELD_BY_KEY)
    ready, reason = ctx.readiness_for(fields)
    return '' if ready else reason


def probe_degraded_reason(state: SearchState) -> str:
    """degraded_reason() for a caller that must know BEFORE it holds a DB connection.

    An unbuilt SearchContext carries an empty `readiness` map, so `readiness_for()` falls
    through to a live `search_index_readiness()` - the same gate, the same wording, no
    second speller. What it buys is that the answer costs one index read and leaves no ORM
    identity map behind, so the caller can hand its connection back and then queue for a
    concurrency slot without holding one (app/routes/channel_search.py, dev/changelog/422).

    Free for an untyped search: degraded_reason() returns '' before touching the database.
    """
    return degraded_reason(state, SearchContext())


def full_scan_reason(state: SearchState) -> str:
    """Why every AGGREGATE over this state has to read the whole table, or '' when it does not.

    A different fact from `degraded_reason()` above, and the difference decides what the user
    is told. Degraded means the index is temporarily unusable: it repairs itself, and waiting
    is the remedy. This means the index cannot answer the question that was asked, however
    healthy it is - so it never repairs, and the remedy is a control the user can reach.

    One condition today, and it is `airing_narrowing_decision()`'s own first gate for the
    identical reason: `chan_prog` (what both program indexes are built over) holds FUTURE
    showings only, so ticking "Show airings that have ended" puts the whole of
    `epg_entries` in scope with nothing able to narrow it. Measured over HTTP on the live
    database (1.9M rows, index healthy): the facet rail goes 7.6s -> 24.4s and blows the 20s
    budget, dominated by the tag facet at 14.2s, which loses its cached channel-id prefilter
    for that same future-only reason (`_tag_predicate`'s `hides_past`).

    Pure state, no context and no database, so a route can ask before it opens a connection -
    same property `probe_degraded_reason()` has and for the same reason.

    ROWS ARE NOT AFFECTED and must not be: this describes what a COUNT over the result set
    costs, and the row page is bounded by LIMIT either way (0.1s untyped, ~8.6s worst case on
    a term with almost no matches). What callers do with this is decline the optional numbers,
    never the rows - dev/changelog/681.
    """
    if state.grain == GRAIN_AIRINGS and not standing_applied(state.standing, 'showpast'):
        return ('showings that have already ended are not in the search index, and '
                '"Show airings that have ended" is on')
    return ''


#: Memoized (standing_hidden, total) for the airing grain's genuinely UNFILTERED case - no
#: text query, no dimension filters (dev/changelog/598). That is the one case these numbers
#: describe: the ~1.6s `_standing_breakdown_compute()` query is a pure function of
#: (which toggles are on, the data) whenever nothing else is narrowing the result, so it is
#: memoized the same shape as #15's per-tag cache (`_tag_channel_ids_cache`,
#: dev/changelog/597) - watermark-invalidated, keyed by the toggle combination plus the one
#: config-driven fact (`normalizing_account_ids`) the channels/programs watermark cannot see.
#:
#: A TTL backstops the watermark rather than replacing it: the watermark
#: (MAX(id)/MAX(last_seen_at) on channels, MAX(id) on epg_entries) does NOT move when a health
#: check updates Channel.health_score (that bumps health_score_updated_at instead - see
#: app/health_score.py) or when a channel-group membership edit happens (ChannelGroupMember/
#: ChannelGroup are not part of either watermark) - both feed the default-on `dup` (health
#: tie-break) and `grpdedup` (group membership) toggles. `past` is wall-clock-driven and has no
#: data watermark at all. The TTL (`search.standing_breakdown_cache_ttl_seconds`) bounds
#: staleness from all three regardless of cause. Reset between tests via
#: tests/support/app.py::reset_module_globals.
_standing_breakdown_cache: dict = {}
_standing_breakdown_lock = threading.Lock()


def clear_standing_breakdown_cache():
    """Drop every cached unfiltered standing-breakdown entry.

    Called when the TTL setting changes, so a shorter TTL takes effect immediately rather than
    only once the last-built entry's original (longer) TTL happens to expire.
    """
    with _standing_breakdown_lock:
        _standing_breakdown_cache.clear()


def _cached_standing_breakdown(state: SearchState, ctx: SearchContext, narrowing: list):
    """The airing grain's unfiltered breakdown, memoized - or None, meaning "compute it live",
    the same fallback shape #15's `_cached_tag_channel_ids` uses.

    Only attempts the cache for the exact case it exists for: the airing grain with no text
    query and no dimension filters. Anything else computes live, unchanged - this is a fix for
    the default first paint, not a general query cache.
    """
    if narrowing or state.grain != GRAIN_AIRINGS:
        return None

    key = (tuple(s.key for s in active_standing(state)),
          tuple(sorted(ctx.normalizing_account_ids)))
    watermark = (source_watermark(SEARCH_INDEX_CHANNELS),
                source_watermark(SEARCH_INDEX_PROGRAMS))
    ttl = ctx.cfg.get('search', {}).get('standing_breakdown_cache_ttl_seconds', 300)
    now = time.monotonic()

    with _standing_breakdown_lock:
        cached = _standing_breakdown_cache.get(key)
        if cached is not None:
            cached_watermark, built_at, value = cached
            if cached_watermark == watermark and now - built_at < ttl:
                return value

    value = _standing_breakdown_compute(state, ctx, narrowing)
    with _standing_breakdown_lock:
        _standing_breakdown_cache[key] = (watermark, now, value)
    return value


def _standing_breakdown(state: SearchState, ctx: SearchContext, narrowing: list):
    """({option key: rows it hid}, rows still visible) - one query for both, or a cache hit.

    See `_cached_standing_breakdown` for when a cache is even attempted; every other case
    (any active text query or filter) always calls `_standing_breakdown_compute` directly.
    """
    cached = _cached_standing_breakdown(state, ctx, narrowing)
    if cached is not None:
        return cached
    return _standing_breakdown_compute(state, ctx, narrowing)


def _standing_breakdown_compute(state: SearchState, ctx: SearchContext, narrowing: list):
    """({option key: rows it hid}, rows still visible) - one query for both.

    The CASE is ordered, so a row hidden by two options is attributed to the FIRST one in
    registry order. First, not all: the user is shown one number per option, and a row
    counted twice makes the numbers not add up to the total, which is worse than a slightly
    arbitrary attribution.

    Counted over rows that pass the text and the filters, so "412 duplicates hidden" means
    412 rows this search would give back by turning the option off - not 412 rows somewhere
    in the database. A number the user cannot act on is worse than no number.
    """
    airings = state.grain == GRAIN_AIRINGS
    counted = EPGEntry.id if airings else Channel.id
    active = active_standing(state)
    scope = _cluster_scope(state, ctx, narrowing)
    if not active:
        query = db.session.query(func.count(counted))
        if airings:
            query = query.select_from(EPGEntry).join(
                Channel, Channel.id == EPGEntry.channel_id)
        return {}, (query.filter(*narrowing).scalar() or 0)

    bucket = db.case(*[(_standing_reject(s.key, ctx, scope, state), s.key) for s in active],
                     else_='')
    query = db.session.query(bucket.label('bucket'), func.count(counted))
    if airings:
        query = query.select_from(EPGEntry).join(Channel, Channel.id == EPGEntry.channel_id)
    else:
        query = query.select_from(Channel)
    rows = query.filter(*narrowing).group_by('bucket').all()
    counts = {key: n for key, n in rows}
    total = counts.pop('', 0)
    return {k: n for k, n in counts.items() if n}, total


def _kept_ids(state: SearchState, rows: list) -> frozenset:
    """Which of the rows on this page are the surviving copy of a duplicate cluster - what
    the KEPT badge is drawn from. Only meaningful while duplicates are being hidden: with
    "Show duplicates" ticked every copy is shown and none of them is "the one kept"."""
    if not standing_applied(state.standing, 'showdup'):
        return frozenset()
    # A page can now hold group rows too, and a group has no stream URL to duplicate. Asked
    # by attribute rather than by isinstance so this keeps working for any row kind added
    # later that is equally not a channel.
    return frozenset(r.id for r in rows
                     if getattr(r, 'is_duplicate_stream_url', False))


# ---------------------------------------------------------------------------
# Row support - what a result PAGE needs, never the whole result set
# ---------------------------------------------------------------------------
#
# These three answer "what is true of these particular rows", so every one of them takes an
# explicit id list and is called once for the page, not once per row. They live here rather
# than in the row builder so that "what a tag means" and "what counts as a match on this
# field" have exactly one definition - the one the result set was built with.

def include_terms(state: SearchState) -> tuple:
    """The typed terms that can earn a row. An excluded term (`-word`) never does."""
    return tuple(t for t in parse_terms(state.q) if not t.exclude)


def field_hit_predicate(field_key: str, state: SearchState, ctx: SearchContext):
    """"Did this row match the typed query on THIS field alone", or None for no query.

    What the "why" chip is drawn from: the row is already in the result, and this says which
    field put it there. One field at a time and the same term machinery the result set used,
    so a wildcard or a phrase means the same thing in the chip as it did in the search.
    """
    field = FIELD_BY_KEY.get(field_key)
    if field is None:
        raise SearchStateError(f'unknown search field {field_key!r}')
    terms = include_terms(state)
    if not terms:
        return None
    indexed = ctx.readiness_for((field,))[0]
    # Never narrowed: this is asked about rows that are already in the result, so the index
    # has nothing left to cut and the un-narrowed predicate is the one the result was built
    # with. Grain-aware because an EPG field means the showing here and what is on right now
    # there - and the request's own context rather than a fresh one, so the chip shares both
    # the clock and the resolved id list the row query was built from.
    parts = [_term_predicate(t, (field,), indexed, state.grain, False, ctx)
             for t in terms]
    if len(parts) == 1:
        return parts[0]
    # Match-all asks whether this ONE field carries every term - a row can be in the result
    # because two fields each matched a different term, and neither of them is then "the
    # field that earned it".
    return and_(*parts) if state.match_all else or_(*parts)


def tag_hit_predicate(tag: Tag, ctx: SearchContext, now_scoped: bool = True):
    """"Does this channel carry this tag" - the row-badge form of the tag facet.

    `now_scoped` is the caller's call, not a fact about the tag, and the two answers are both
    right for their own surface (dev/changelog/862). On the CHANNEL grain the badge sits beside
    a `Now airing` column and next to the filter that put the row there, so all three have to
    agree. On the AIRING grain the same badge describes the row's CHANNEL - a showing at 11pm
    on a channel that is running a tagged program at 3pm - so scoping it to this instant would
    describe neither the row nor anything else on screen; the showing's own tags are a separate
    value there (`channel_search_rows._matched_tags`).
    """
    return _tag_predicate(tag, ctx.readiness_for(_TAG_FIELDS)[0], cfg=ctx.cfg, ctx=ctx,
                          now_scoped=now_scoped)


def matching_programs(channel_ids, state: SearchState, ctx: SearchContext) -> dict:
    """{channel id: (title, sub_title, description)} - one program per channel that the
    typed query matched, so the "why" chip can name the show instead of just the field.

    Display only. The rows are already decided, so this is a plain LIKE over the page's own
    channel ids: at most `page_size` of them, where the FTS index has nothing left to narrow.

    **Restricted to the showing that is on right now, because that is what put the row here**
    (`_now_program_side`, dev/changelog/861). It used to read chan_prog when the program index
    was healthy and epg_entries when it was not, mirroring `_program_side`'s two paths - both
    are gone, since chan_prog is deduped and so cannot express "now" at all, and naming
    tonight's showing beside a row whose `Now airing` column says something else is the
    unexplainable number this project's first principle is about. One consequence worth
    keeping: the chip no longer goes blank while the program index rebuilds, because it no
    longer depends on it.
    """
    ids = list(channel_ids)
    fields = tuple(FIELD_BY_KEY[k] for k in state.fields
                   if k in FIELD_BY_KEY and FIELD_BY_KEY[k].source == SOURCE_PROGRAM)
    terms = include_terms(state)
    if not ids or not fields or not terms:
        return {}

    by_key = {'title': EPGEntry.title, 'sub_title': EPGEntry.sub_title,
              'description': EPGEntry.description}
    entry_fields = tuple(
        replace(f, column=by_key[f.fts_column], cast_text=False) for f in fields)
    like = or_(*[_column_like(f, t) for f in entry_fields for t in terms])
    # Bare `stop_time`, not `_unindexed()`: the id list is the driving filter here, so this
    # rides ix_epg_entries_channel_stop rather than the overlap scan _now_program_side needs.
    query = (select(EPGEntry.channel_id, EPGEntry.title, EPGEntry.sub_title,
                    EPGEntry.description)
             .where(EPGEntry.channel_id.in_(ids),
                    EPGEntry.start_time <= ctx.now, EPGEntry.stop_time > ctx.now, like)
             .order_by(EPGEntry.start_time, EPGEntry.id))

    out = {}
    for channel_id, title, sub_title, description in db.session.execute(query):
        out.setdefault(channel_id, (title, sub_title, description))
    return out


# ---------------------------------------------------------------------------
# Facet counts
# ---------------------------------------------------------------------------

def compute_facets(state: SearchState, ctx: SearchContext,
                   text_preds: list | None = None) -> dict:
    """{dimension key: {value: count}} for the rail.

    One aggregate per dimension, each against the state with its own filter removed (see
    dimension_predicates). Values with a zero count still belong in the answer where the
    dimension has a fixed vocabulary - the rail's three-state control has to render a value
    the user can still exclude - so health and Other are seeded at zero rather than left out.

    **TAG FACET.** A tag is a set of literal patterns, so counting it is one FTS query per
    pattern against both indexes, and a pattern matching thousands of channels costs tens of
    ms on the program side (13-132ms measured; three patterns exist today, so ~180ms worst
    case). Unlike every other dimension that cost scales with how many patterns the user
    creates, and the rail has a `+ Create tag` button in it. Hence `state.facets`: the caller
    names which dimensions it wants counted, and a page whose tag facet is collapsed simply
    does not ask. Absent from the result means "not requested", which the UI must render as
    such - never as zero. If this becomes the bottleneck the next move is memoizing the
    per-tag channel-id sets against the search-index watermark, not dropping the airing half:
    that half is what makes a tag mean anything on a generically-named channel.
    """
    wanted = [d for d in visible_dimensions_for(state.grain)
              if state.facets is None or d.key in state.facets]
    if text_preds is None:
        text_preds = text_predicates(state, ctx)

    # A dimension the user has NOT filtered on is counted against the same base as every
    # other such dimension, so every dimension answerable from a row the scan already reads
    # shares one table pass. Measured on the production database: four separate aggregates
    # cost 92 + 100 + 112 + 106 = 410ms, and the single grouped scan that answers all four
    # costs 261ms. A dimension the user HAS filtered on needs its own filter removed, so it
    # cannot join that pass and falls back to its own aggregate.
    #
    # **Both grains take it.** Airings were excluded until 2026-08-01 on the argument that the
    # grouping key is a channel column while the thing counted is a showing, so the "1,949
    # groups against 136,130 rows" trade would not hold. Measured, it holds by more here than
    # on channels: four separate passes cost 677 + 4609 + 4327 + 3908 = 13,521ms against
    # 2117ms for the one grouped scan (710 groups), which took ~11.4s off the rail request.
    filtered = {f.key for f in state.filters if f.values or f.ex}
    shared = tuple(d.key for d in wanted
                   if d.key in _SCAN_DIMENSIONS and d.key not in filtered)

    out = {}
    if shared:
        out.update(_combined_facet_scan(
            shared, state, ctx, base_predicates(state, ctx, text_preds=text_preds),
            split=split_predicates(state, ctx, text_preds)))
    for d in wanted:
        if d.key not in out:
            out[d.key] = _facet_counts(d, state, ctx, text_preds)
    # Registry order, not the order they were computed in - the rail renders this as given.
    return {d.key: out[d.key] for d in wanted}


#: The dimensions answerable from a row the scan is already reading - a plain expression over
#: the joined `channels` row, or a FILTER count needing no grouping key of its own - and which
#: can therefore share a single pass. `group` needs a join from the membership table's small
#: end and `tag` needs the FTS indexes, so neither can join that pass.
_SCAN_DIMENSIONS = ('acct', 'cat', 'health', 'other', 'when')


def _combined_facet_scan(dim_keys: tuple, state: SearchState, ctx: SearchContext,
                         preds: list, split: tuple | None = None) -> dict:
    """Count several single-scan dimensions in one grouped pass over `channels`.

    Grouped by the cross product of whichever of account / category / health band are
    wanted - 1,949 groups on the production database, which is a rounding error next to the
    136,130 rows the scan reads either way - and summed back down per dimension in Python.
    The Other flags and the When windows need no grouping key at all: they are FILTER counts
    that ride along on the same scan (`riders` below).

    The grouping keys are channel columns on either grain; the grain only decides whether a
    channel is counted once or once per showing on it, which is what makes these counts add
    up to the list's own total (DESIGN-channel-search.md §1). Same rule as _facet_counts.

    **On the airing grain the join is deferred, not removed** (`split`, from
    `split_predicates()`). Written the obvious way this scan joins `channels` to all 704,067
    surviving showings and then aggregates, and the join - not the counting - is the cost: an
    identical aggregate reading `epg_entries` alone runs off `ix_epg_entries_channel_stop` as
    a covering index in 314ms against 1256ms joined. So the epg-side predicates are applied
    first to tally showings per channel (26,589 rows, one per channel that has any), and only
    that tally is joined to `channels` for the channel-side predicates and the grouping. Same
    rows, same counts, one join of 26,589 rows instead of 704,067. Measured on the production
    database with the full aggregate set: 2125ms -> 1033ms (dev/changelog/729).

    `split` is None whenever the state cannot be divided that way - a typed query, or a
    filter on a dimension that reads both tables - and then this falls back to the joined
    shape, which is always correct. The no-query landing page this was built for always
    splits; a search that cannot is already paying for its own text predicates.
    """
    airings = state.grain == GRAIN_AIRINGS
    counted = EPGEntry.id if airings else Channel.id
    group_exprs, group_keys = [], []
    for key, expr in (('acct', Channel.account_id),
                      ('cat', Channel.category_name),
                      ('health', _health_band_expr(ctx.cfg))):
        if key in dim_keys:
            group_exprs.append(expr.label(f'facet_{key}'))
            group_keys.append(key)

    # Dimensions with a closed vocabulary and no grouping key: one FILTER aggregate per
    # value, over rows this scan is reading anyway. `when` is here rather than in its own
    # pass because that pass was a second full read of the same 704k joined rows for three
    # counts - 1.2s of a 4.1s rail request on the airings landing page (dev/changelog/729).
    #
    # Only `when`'s three static windows can appear here. A `when` the user has actually
    # filtered on needs its own filter removed, so compute_facets() keeps it out of this scan
    # and _facet_counts() answers it - which is also the only path that can see a user-typed
    # window, of which there are infinitely many. Never reached on the channel grain, where
    # `when` is grain-scoped out of the rail entirely.
    riders = []
    if 'other' in dim_keys:
        riders.append(('other', OTHER_VALUES,
                       lambda v: _value_predicate('other', v, ctx)))
    if 'when' in dim_keys:
        riders.append(('when', WHEN_STATIC_VALUES, lambda v: _when_predicate(v, ctx)))

    # A rider whose predicate reads `epg_entries` has to be counted in the tally, before the
    # rows are collapsed to one per channel; a channel-side one is a property of the channel
    # and is summed after. `when` is the only epg-side rider today.
    epg_riders = [r for r in riders if r[0] == 'when']
    channel_riders = [r for r in riders if r[0] != 'when']

    if airings and split is not None:
        channel_preds, epg_preds = split
        tally = (db.session.query(
            EPGEntry.channel_id.label('cid'),
            func.count(EPGEntry.id).label('n'),
            *[func.count(EPGEntry.id).filter(predicate(v)).label(f'w{i}')
              for _key, values, predicate in epg_riders
              for i, v in enumerate(values)])
            .filter(*epg_preds)
            .group_by(EPGEntry.channel_id)
            .subquery())
        # SUM over the tally, not COUNT over rows: one tally row already stands for however
        # many showings that channel has. A channel-side rider weights its channel's whole
        # tally, which is the same number the FILTER count produced before the split.
        aggregates = [func.sum(tally.c.n)]
        for _key, values, predicate in channel_riders:
            aggregates += [func.sum(case((predicate(v), tally.c.n), else_=0))
                           for v in values]
        aggregates += [func.sum(getattr(tally.c, f'w{i}'))
                       for _key, values, _predicate in epg_riders
                       for i in range(len(values))]
        query = (db.session.query(*group_exprs, *aggregates)
                 .select_from(tally)
                 .join(Channel, Channel.id == tally.c.cid)
                 .filter(*channel_preds))
        # Registry order, so the unpacking below reads the columns where it expects them.
        riders = channel_riders + epg_riders
    else:
        aggregates = [func.count(counted)]
        for _key, values, predicate in riders:
            aggregates += [func.count(counted).filter(predicate(v)) for v in values]
        query = db.session.query(*group_exprs, *aggregates)
        if airings:
            query = query.select_from(EPGEntry).join(
                Channel, Channel.id == EPGEntry.channel_id)
        else:
            query = query.select_from(Channel)
        query = query.filter(*preds)
    if group_exprs:
        query = query.group_by(*[e.name for e in group_exprs])
    rows = query.all()

    tallies = {key: {} for key in group_keys}
    rider_totals = {key: [0] * len(values) for key, values, _ in riders}
    width = len(group_exprs)
    for row in rows:
        count = row[width] or 0
        for key, value in zip(group_keys, row[:width]):
            tallies[key][value] = tallies[key].get(value, 0) + count
        offset = width + 1
        for key, values, _ in riders:
            totals = rider_totals[key]
            for i in range(len(values)):
                totals[i] += row[offset + i] or 0
            offset += len(values)

    out = {}
    if 'acct' in group_keys:
        out['acct'] = {str(k): n for k, n in tallies['acct'].items() if k is not None}
    if 'cat' in group_keys:
        out['cat'] = {k: n for k, n in tallies['cat'].items() if k}
    if 'health' in group_keys:
        # Seeded at zero: the rail's three-state control has to render a band the user can
        # still exclude, so a band nothing currently falls in is a real answer, not an
        # omission.
        counts = dict.fromkeys(HEALTH_VALUES, 0)
        counts.update(tallies['health'])
        out['health'] = counts
    for key, values, _ in riders:
        out[key] = dict(zip(values, rider_totals[key]))
    return out


def _facet_counts(dim: Dimension, state: SearchState, ctx: SearchContext,
                  text_preds: list) -> dict:
    preds = base_predicates(state, ctx, skip_dimension=dim.key, text_preds=text_preds)
    airings = state.grain == GRAIN_AIRINGS
    # What one facet counts, and where the count is taken from. Every value below describes
    # the CHANNEL either way (§1); the grain only changes whether a channel is counted once
    # or once per showing on it, which is what makes the counts add up to the list's total.
    counted = EPGEntry.id if airings else Channel.id

    def over(*cols):
        query = db.session.query(*cols)
        if airings:
            return query.select_from(EPGEntry).join(
                Channel, Channel.id == EPGEntry.channel_id)
        return query.select_from(Channel)

    if dim.key == 'when':
        # Never reached on the channel grain: `when` is grain-scoped out of `wanted`.
        # The three static values always, plus whichever windows the current state names -
        # a value the user has typed a window into has to carry a count, and there are
        # infinitely many possible windows so they cannot be enumerated.
        values = list(WHEN_STATIC_VALUES)
        current = state.filter_for('when')
        if current is not None:
            for value in tuple(current.values) + tuple(current.ex):
                if value not in values:
                    values.append(value)
        row = over(*[func.count(counted).filter(_when_predicate(v, ctx))
                     for v in values]).filter(*preds).one()
        return dict(zip(values, [n or 0 for n in row]))

    if dim.key == 'duration':
        # Never reached on the channel grain: `duration` is grain-scoped out of `wanted`, same
        # as `when`. No static values - a bound is a number the user typed, not a closed
        # vocabulary - so only the current filter's own value(s) get counted.
        current = state.filter_for('duration')
        values = list(dict.fromkeys(
            (tuple(current.values) + tuple(current.ex)) if current is not None else ()))
        if not values:
            return {}
        row = over(*[func.count(counted).filter(_duration_predicate(v))
                     for v in values]).filter(*preds).one()
        return dict(zip(values, [n or 0 for n in row]))

    if dim.key == 'cat':
        rows = (over(Channel.category_name, func.count(counted))
                .filter(*preds).group_by(Channel.category_name).all())
        return {name: n for name, n in rows if name}

    if dim.key == 'acct':
        rows = (over(Channel.account_id, func.count(counted))
                .filter(*preds).group_by(Channel.account_id).all())
        return {str(account_id): n for account_id, n in rows}

    if dim.key == 'health':
        band = _health_band_expr(ctx.cfg)
        rows = (over(band.label('band'), func.count(counted))
                .filter(*preds).group_by('band').all())
        counts = dict.fromkeys(HEALTH_VALUES, 0)
        counts.update({value: n for value, n in rows})
        return counts

    if dim.key == 'other':
        row = over(*[func.count(counted).filter(_value_predicate('other', v, ctx))
                     for v in OTHER_VALUES]).filter(*preds).one()
        return dict(zip(OTHER_VALUES, [n or 0 for n in row]))

    if dim.key == 'group':
        # Driven from the membership table, not from channels. There are 97 membership rows
        # against 136,130 channels, so starting at the small end is the whole game: written
        # the other way round - `select_from(Channel).join(members)` - the planner scans
        # every channel and this facet costs 88ms instead of 1.4ms. The airing grain hangs
        # epg_entries off the same small end rather than reversing it.
        def grouped(*cols):
            query = (db.session.query(*cols)
                     .select_from(ChannelGroupMember)
                     .join(Channel, Channel.id == ChannelGroupMember.channel_id))
            if airings:
                query = query.join(EPGEntry, EPGEntry.channel_id == Channel.id)
            return query

        tally = counted if airings else func.distinct(Channel.id)
        rows = (grouped(ChannelGroup.name, func.count(tally))
                .join(ChannelGroup, ChannelGroup.id == ChannelGroupMember.group_id)
                .filter(*preds).group_by(ChannelGroup.name).all())
        counts = {name: n for name, n in rows}
        # DISTINCT here and not above: "in any group at all" spans every membership a row
        # has, so a channel in two groups would otherwise count its showings twice. Within
        # one group name the (group_id, channel_id) uniqueness constraint already guarantees
        # one membership row, so the per-group counts need no distinct pass.
        counts[GROUP_ANY] = (grouped(func.count(func.distinct(counted)))
                             .filter(*preds).scalar() or 0)
        return counts

    if dim.key == 'tag':
        indexed = ctx.readiness_for(_TAG_FIELDS)[0]
        narrow = False
        if airings:
            narrow, _ = airing_narrowing_decision(state, ctx)
        hides_past = standing_applied(state.standing, 'showpast')
        return {
            # `ctx` is not optional here even though the parameter is: without it the channel
            # grain's now-scoped half falls back to a context holding no tags, which is one
            # scan per tag instead of one per request (measured 2.2s against 0.5s).
            tag.name: (over(func.count(counted))
                       .filter(*preds, _tag_predicate(tag, indexed, state.grain, narrow,
                                                      ctx.cfg, hides_past, ctx))
                       .scalar() or 0)
            for tag in ctx.tags
        }

    raise SearchStateError(f'no facet count defined for dimension {dim.key!r}')
