"""The channel search's row payload: one dict per result row, every enrichment batched.

`app/channel_search.py` decides *which* channels a search returns; this decides *what is
said about each of them*. It is a separate module because the two answer different questions
and change for different reasons - a new column here is not a new predicate there - and
because the page route (phase B's first paint) and the JSON endpoint both need exactly this
payload, so it cannot live inside either one.

**The payload is the approved mockup's ten fields, in full, plus the three things the row
carries that are not columns** - the DUP badge, the KEPT badge and the "why" chip. All ten
fields ship batched together, not a minimal payload widened later (2026-07-30). Health, now
airing, status, account, category, stream id, EPG id, URL tail, groups and tags are the ten;
the picker in the UI hides them, it does not stop them being sent, because hiding a column is
a client-side preference and re-fetching a page to unhide one would be a round trip for data
the server already had in hand.

**Every enrichment is one query for the whole page, and that is a hard rule here rather than
a style preference** (CLAUDE.md "no hidden I/O in per-row loops"): a page is up to 500 rows
and the search is re-run on every keystroke, so a per-row lookup is 500 queries per
keystroke. `tests/test_scaling_pages.py::test_channel_search_api` is the guard - it fails if
the endpoint's query count moves at all between a small and a large seed.

**Measured on the production database 2026-07-30 (136,130 channels, warm, best of three) -
do not re-derive.** The whole payload for a 100-row page costs **75-84ms** on top of the
search itself, and it does not move with the page size (a 500-row page measured 367ms
end-to-end against 322ms for 100). Where that time goes:

| enrichment | q='' | q='espn' |
|---|---|---|
| tags | 69.5 ms | 66.8 ms |
| field hits ("why") | - | 5.7 ms |
| lifecycle (2 queries) | 2.1 ms | 2.6 ms |
| now airing | 0.9 ms | 0.9 ms |
| groups | 0.8 ms | 0.8 ms |
| duplicate clusters | 0.0 ms | 0.0 ms |

**The tag matrix is all of it, and it is the one that scales with something the user
controls** - not the row count, but how many tag patterns exist (three today). A tag is a set
of literal patterns matched against the channel name AND against what it is airing, so each
one is an FTS query against both indexes, and a pattern matching thousands of channels costs
tens of ms (13-132ms measured per pattern in the facet, dev/changelog/396). The lever if it
ever bites is the one the engine's own TAG FACET note names - memoize the per-tag channel-id
sets against the search-index watermark - **not** re-implementing pattern matching in Python
over the page's rows. That would be cheaper and would put "what a tag means" in two places,
which is how the badge and the facet end up disagreeing.

What is deliberately NOT here:

* **Formatting.** Numbers, dates and byte counts go out as values, not as strings, except
  where the string *is* the datum (the lifecycle date the badge reads). The page formats;
  `static/js/util.js` already has the formatters.
* **Truncation.** The URL goes out whole (masked) and the page decides how much of the tail
  to draw - the mockup's own column does exactly that, and a server-side ellipsis would put
  the tooltip's full value out of reach.
**The airing grain reuses every one of those enrichments, batched over the page's DISTINCT
channel ids.** A page of 100 showings is routinely 20 channels, so asking the channel
questions once per channel rather than once per row is both cheaper and the only way the two
grains can agree about a channel. What it adds is per-showing: the times, the program text,
and the recording state the Record button draws its five states from.
"""
import logging
from datetime import datetime

from sqlalchemy import case, select

from . import db, health_bands
from .channel_search import (FIELDS, GRAIN_AIRINGS, GRAIN_CHANNELS,
                             HEALTH_UNTESTED, SearchStateError, effective_health,
                             field_hit_predicate, in_any_group_expr, include_terms,
                             matching_programs, tag_hit_predicate)
from .logo_cache import resolve_logo_url
from .database import (Channel, ChannelGroup, ChannelGroupMember, EPGEntry,
                       Recording,
                       REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
                       REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
                       REC_STATUS_COMPLETED,
                       REC_STATUS_ABORTED, REC_STATUS_FAILED)
from .recording_match import build_rec_indexes, candidate_recs, match_recording
from .url_utils import mask_creds

log = logging.getLogger(__name__)

#: How many of a duplicate cluster's other members the DUP tooltip names before it says
#: "+ N more". The mockup's number; listed per channel and never deduped by name, because a
#: cluster whose copies share one name is exactly the finding the badge exists to show.
DUP_TOOLTIP_MEMBERS = 4

#: What a row IS. Sent on every row of the channel grain, because a page can now hold two
#: kinds and a renderer that has to infer the kind from which keys are present is one payload
#: change away from drawing a group as a channel.
CHANNEL_ROW = 'channel'
GROUP_ROW = 'group'

#: Which rung of the keep-rule cascade decided a KEPT row. Says which one actually decided
#: it, not all five - "kept because it is in your guide" is actionable, a recital of the
#: rule is not.
KEEP_REASON_NOT_HIDDEN = 'the others are hidden'
KEEP_REASON_IN_GUIDE = 'already in your TV Guide'
KEEP_REASON_IN_GROUP = 'in a channel group'
KEEP_REASON_HEALTH = 'the best health score'
KEEP_REASON_ID = 'the lowest channel id'


def build_rows(result, state, ctx) -> list:
    """The result's rows as JSON-ready dicts. One entry point, dispatched on the grain."""
    if state.grain == GRAIN_CHANNELS:
        return _channel_rows(result, state, ctx)
    if state.grain == GRAIN_AIRINGS:
        return _airing_rows(result, state, ctx)
    raise SearchStateError(f'no row builder for result grain {state.grain!r}')


def _channel_rows(result, state, ctx) -> list:
    rows = list(result.rows)
    if not rows:
        return []
    # A page can hold two row kinds since dev/changelog/811. They are enriched separately -
    # a group has no account, no stream and no duplicate cluster, and asking the channel
    # questions about one would answer them with `--` at best - and re-interleaved at the
    # end in the order the engine merged them.
    groups = [r for r in rows if isinstance(r, ChannelGroup)]
    if groups:
        group_payloads = build_group_rows(groups, ctx)
        channel_payloads = _plain_channel_rows(
            [r for r in rows if not isinstance(r, ChannelGroup)], result, state, ctx)
        by_id = {(GROUP_ROW, p['id']): p for p in group_payloads}
        by_id.update({(CHANNEL_ROW, p['id']): p for p in channel_payloads})
        return [by_id[(GROUP_ROW if isinstance(r, ChannelGroup) else CHANNEL_ROW, r.id)]
                for r in rows]
    return _plain_channel_rows(rows, result, state, ctx)


def _plain_channel_rows(rows, result, state, ctx) -> list:
    if not rows:
        return []
    ids = [r.id for r in rows]

    hits = _field_hits(ids, state, ctx)
    programs = matching_programs(ids, state, ctx) if hits else {}
    airing = _now_airing(ids)
    groups, guide_via = _groups_by_channel(ids)
    tags = _tags_by_channel(ids, ctx)
    lifecycle = _lifecycle(rows, ctx)
    clusters = _duplicate_clusters(rows)
    # Hoisted: banding a score is per-row work, resolving the bands is per-request.
    bands = health_bands.resolve_bands(ctx.cfg)

    out = []
    for ch in rows:
        account = ctx.accounts_by_id.get(ch.account_id)
        state_name, since = lifecycle.get(ch.id, (None, None))
        why = _why(ch.id, hits, programs, state)
        out.append({
            'kind': CHANNEL_ROW,
            'id': ch.id,
            'name': ch.name,
            'notes': ch.notes or '',
            'in_guide': bool(ch.in_guide),
            # Only ever true on a search that ticked "Show hidden channels", so the row
            # is here because the user asked to see it - and it has to say why it is unusual
            # rather than looking like every other row. `hidden_deferred` is the other half:
            # something wants this channel hidden and its guide row or group membership is
            # what is still keeping it here.
            'hidden': bool(ch.hidden),
            'hidden_deferred': bool(ch.hidden_deferred),
            'category': ch.category_name or '',
            'stream_id': ch.stream_id,
            'epg_channel_id': ch.epg_channel_id or '',
            # Masked, always. A stream URL is an unknown-provenance URL that routinely
            # carries the account's credentials in its path (DESIGN-secrets.md §4.2), and
            # this one is bound for a JSON response that any browser tab can read.
            'stream_url': mask_creds(ch.stream_url or ''),
            'health': _health(ch),
            'health_band': _health_band(ch, bands),
            'account': _account(account),
            'airing': airing.get(ch.id),
            'lifecycle': state_name,
            # Pre-formatted, unlike every other date here, because it is a badge label
            # ("Missing 2026-07-14") rather than a value the page does arithmetic on.
            'lifecycle_date': since.strftime('%Y-%m-%d') if since else '',
            'not_normalized': (not ch.url_normalizable
                               and ch.account_id in ctx.normalizing_account_ids),
            'groups': groups.get(ch.id, []),
            # The in-guide subset of `groups`, not a different question asked twice: the
            # badge names WHICH group puts this channel's listings in the guide, which
            # `in_guide` (its own row, and nothing else since dev/changelog/751) cannot say.
            'guide_via': guide_via.get(ch.id, []),
            'tags': tags.get(ch.id, []),
            'dup': clusters.get(ch.id),
            'kept': ch.id in result.kept_ids,
            'why': why,
        })
    return out


# ---------------------------------------------------------------------------
# The group row
# ---------------------------------------------------------------------------

#: Why each channel-only column reads `--` on a group row. The reason ships with the row
#: rather than being worded in the page, because it is a fact about the data model and the
#: two widths word everything else differently (DESIGN-group-search-rows.md §5.2: the action
#: set and the empty cells fall out of the row by subtraction, and each one says why).
GROUP_NO_VALUE = {
    'account': 'A group has no account of its own - its members can come from different '
               'providers.',
    'category': "A group has no category of its own - its members come from different "
                "provider categories.",
    'sid': 'A group has no stream of its own. Each member has one, and which of them serves '
           'a recording is decided at record start.',
    'tvg': 'A group has no EPG id of its own - its listings are its members\'.',
    'url': 'A group has no stream URL of its own. Each member has one, and which of them '
           'serves a recording is decided at record start.',
    'groups': 'A group is not a member of a group.',
    # No `status` entry: the Status COLUMN is gone from both grains (dev/changelog/860), and
    # its badges are drawn from the row itself rather than from a track a group row would
    # have to fill. A group carries none of them, which is now simply an empty badge line.
    'tags': 'Tags are matched against a channel name and what it is airing. A group carries '
            "its members'.",
}


def build_group_rows(groups, ctx) -> list:
    """One dict per group row, every enrichment batched over the whole page.

    §5.2's fourth rule, and the reason `tests/test_scaling_pages.py` gets a case for this:
    **a group row must not resolve its serving member per row.** Member counts, the
    recording-enabled counts, the serving member and its now-airing program are all
    batch-fetchable, and fetched per row they reintroduce exactly the defect class that file
    guards. Three statements for the whole page, however many groups are on it:

    * the memberships and their channels, eager-loaded by `matching_groups()` itself;
    * the latest health check per member channel, which is what the format lock reads;
    * what the serving members are airing right now.

    The serving member is picked by the SAME rule the TV Guide row and the recorder use -
    format lock filters, health score ranks - through the one helper that spells it,
    `channel_groups.serving_member()` (dev/changelog/753, dev/changelog/904). A row that
    named a different member than a recording would open is a row that lies about what
    clicking Record does.
    """
    from .channel_groups import format_label, serving_member
    from .routes.channel_tests import _latest_tests_by_channel
    if not groups:
        return []

    member_ids = [m.channel_id for g in groups for m in g.memberships]
    latest = _latest_tests_by_channel(member_ids)
    bands = health_bands.resolve_bands(ctx.cfg)

    serving_by_group = {}
    for group in groups:
        serving_by_group[group.id] = serving_member(group, latest).member

    airing = _now_airing([s.id for s in serving_by_group.values() if s is not None])

    out = []
    for group in groups:
        serving = serving_by_group.get(group.id)
        members = list(group.memberships)
        recording_count = sum(1 for m in members if m.recording_enabled)
        locked = group.locked_format_key
        out.append({
            'kind': GROUP_ROW,
            'id': group.id,
            'name': group.name,
            # A group holds a guide row of its own; that is the whole point of the column,
            # and it is NOT `Channel.in_guide` wearing the same name (dev/changelog/751).
            'in_guide': bool(group.in_guide),
            'member_count': len(members),
            'recording_member_count': recording_count,
            'health': None if group.health_score is None else round(group.health_score, 1),
            'health_band': (HEALTH_UNTESTED if group.health_score is None
                            else health_bands.band_for(group.health_score, bands)),
            'format_strategy': group.format_strategy,
            # Named only when the lock is actually pinned. A strategy that follows the data
            # has no lock to show, and showing the last one it derived would read as a
            # setting the user made (dev/changelog/762).
            'format_label': format_label(locked) if locked else '',
            # Not a recording source at all, so the row offers no Record and says why rather
            # than drawing a dead button (DESIGN-channel-groups-model.md DECIDED 2). The one
            # definition of "records": a member has Recording on - never the strategy
            # (channel_groups.participation_is_recording, dev/changelog/1077).
            'check_only': recording_count == 0,
            'serving': None if serving is None else {'id': serving.id, 'name': serving.name},
            # Read from the serving member, because that is the feed this row would open.
            'airing': None if serving is None else airing.get(serving.id),
            'no_value': GROUP_NO_VALUE,
        })
    return out


# ---------------------------------------------------------------------------
# The airing grain
# ---------------------------------------------------------------------------

#: The five states the Record action can be in, per SHOWING and never per channel: the same
#: channel can have one airing recording, one scheduled and one neither, so a button reading a
#: channel-level flag would say the same wrong thing on all three rows. Sent as a value rather
#: than as a label - the page owns the wording, and both mockups word it differently by width.
REC_STATE_NONE = 'none'
REC_STATE_RECORDING = 'recording'
REC_STATE_SCHEDULED = 'scheduled'
REC_STATE_RECORDED = 'recorded'
REC_STATE_PAST = 'past'

#: Recording statuses that mean "this showing is spoken for".
_REC_LIVE = (REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
             REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING)
_REC_DONE = (REC_STATUS_COMPLETED,)
_REC_DEAD = (REC_STATUS_ABORTED, REC_STATUS_FAILED)


def _airing_rows(result, state, ctx) -> list:
    """One dict per showing: the program, the channel it is on, and what is recording it.

    Every channel-side value is the channel grain's own, batched over the page's DISTINCT
    channel ids - a page of 100 showings is routinely 20 channels, and asking the channel
    questions once per channel is both cheaper and the only way the two grains can agree
    about a channel.
    """
    entries = list(result.rows)
    if not entries:
        return []
    channel_ids = list({e.channel_id for e in entries})
    channels = Channel.query.filter(Channel.id.in_(channel_ids)).all()
    channels_by_id = {c.id: c for c in channels}

    hits = _airing_field_hits(channel_ids, state, ctx)
    groups, guide_via = _groups_by_channel(channel_ids)
    # "Ever airs" here, unlike the channel grain: this badge is about the row's CHANNEL, and
    # the row itself is a showing that is usually not the one on now - see tag_hit_predicate.
    tags = _tags_by_channel(channel_ids, ctx, now_scoped=False)
    lifecycle = _lifecycle(channels, ctx)
    clusters = _duplicate_clusters(channels)
    recordings = _recordings_for(entries, channels_by_id)
    stands_for = _airing_group_labels(result, ctx)
    templates = _filename_templates(channels, ctx)
    tag_cleanup = _tag_cleanup(ctx)
    all_tags = list(ctx.tags)
    # Hoisted out of the row loop, not looked up inside it: the context already holds every
    # Tag, so the renderer below never needs to ask the database for one.
    tags_by_name = {t.name: t for t in all_tags}
    # Hoisted for the same reason as on the channel grain: per-request, not per-row.
    bands = health_bands.resolve_bands(ctx.cfg)

    out = []
    for entry in entries:
        ch = channels_by_id.get(entry.channel_id)
        if ch is None:
            # The join that produced this row guarantees the channel exists, so this is
            # unreachable rather than tolerated - but skipping beats an AttributeError in a
            # response, and the log line says a row went missing.
            log.warning('Airing %s has no channel row %s; skipped', entry.id, entry.channel_id)
            continue
        account = ctx.accounts_by_id.get(ch.account_id)
        state_name, since = lifecycle.get(ch.id, (None, None))
        rec = recordings.get(entry.id)
        out.append({
            'id': entry.id,
            'title': entry.title or '',
            'sub_title': entry.sub_title or '',
            'description': entry.description or '',
            # The PROGRAM's category, not the channel's - they are different facts and the
            # airing row draws the channel's in its own column.
            'program_category': entry.category or '',
            'rating': entry.rating or '',
            # Naive-UTC ISO, the shape every other JSON surface in this app sends; the page
            # converts to the display timezone.
            'start_time': _iso(entry.start_time),
            'stop_time': _iso(entry.stop_time),
            'on_now': bool(entry.start_time and entry.stop_time
                           and entry.start_time <= ctx.now < entry.stop_time),
            'ended': bool(entry.stop_time and entry.stop_time <= ctx.now),
            'record_state': _record_state(entry, rec, ctx),
            'recording': None if rec is None else {
                'id': rec.id,
                'status': rec.status,
                'start_time': _iso(rec.start_time),
                'stop_time': _iso(rec.stop_time),
            },
            # Rendered here rather than on the page: the template can come from the channel's
            # default profile and the {tag:...} tokens resolve against Tag rows, neither of
            # which the browser has. It is what the Schedule Recording modal opens prefilled
            # with, so leaving it out would cost a round trip per Record click.
            'suggested_name': _suggested_name(ch, entry, templates, tag_cleanup,
                                              tags_by_name, ctx.display_tz),
            'matched_tags': _matched_tags(all_tags, entry),
            # The group this showing STANDS FOR, or None. Present only while "Collapse
            # channel groups" is on: that option is what makes this one row the group's row
            # rather than one member's among several identical ones, so labelling it as the
            # group without it would name a group the sibling rows are equally part of. Same
            # payload shape the channel grain's own group rows carry, so one renderer draws
            # the Group pill, the stacked tile and the accent edge in both places.
            'group': stands_for.get(entry.id),
            'channel': {
                'id': ch.id,
                'name': ch.name,
                'in_guide': bool(ch.in_guide),
                'hidden': bool(ch.hidden),
                'hidden_deferred': bool(ch.hidden_deferred),
                'category': ch.category_name or '',
                'stream_id': ch.stream_id,
                'epg_channel_id': ch.epg_channel_id or '',
                # Masked for the same reason it is on the channel grain: a stream URL
                # routinely carries the account's credentials in its path.
                'stream_url': mask_creds(ch.stream_url or ''),
                'health': _health(ch),
                'health_band': _health_band(ch, bands),
                'account': _account(account),
                'lifecycle': state_name,
                'lifecycle_date': since.strftime('%Y-%m-%d') if since else '',
                'not_normalized': (not ch.url_normalizable
                                   and ch.account_id in ctx.normalizing_account_ids),
                'groups': groups.get(ch.id, []),
                'guide_via': guide_via.get(ch.id, []),
                'tags': tags.get(ch.id, []),
                'dup': clusters.get(ch.id),
                'default_profile_id': ch.default_profile_id,
                'logo_url': resolve_logo_url(ch),
                'notes': ch.notes or '',
            },
            'why': _airing_why(ch.id, hits),
        })
    return out


def _airing_group_labels(result, ctx) -> dict:
    """{entry id: group payload} - which group each surviving showing is labelled as.

    The engine already decided which groups each row WON, using the collapse's own ranking
    rule (`channel_search.group_wins_by_entry`). What is left is presentation, and it is
    here for that reason: **which of several groups a row is named after.**

    A row can win in more than one group, and it gets exactly one name. The group whose row
    is in the TV Guide wins, then the larger group, then the lower id - "the guide is what
    this list mirrors" first, and a deterministic answer after that, because a row that
    changed its label between two identical searches would be worse than either choice.
    """
    wins = getattr(result, 'airing_group_ids', None)
    if not wins:
        return {}
    ids = sorted({gid for gids in wins.values() for gid in gids})
    from .channel_groups import member_eager_options
    groups = (ChannelGroup.query
              .options(*member_eager_options())
              .filter(ChannelGroup.id.in_(ids)).all())
    payloads = {p['id']: p for p in build_group_rows(groups, ctx)}
    by_id = {g.id: g for g in groups}

    def rank(group_id):
        group = by_id.get(group_id)
        if group is None:
            return (1, 0, group_id)
        return (0 if group.in_guide else 1, -len(group.memberships), group.id)

    out = {}
    for entry_id, group_ids in wins.items():
        known = [gid for gid in group_ids if gid in payloads]
        if known:
            out[entry_id] = payloads[min(known, key=rank)]
    return out


def _iso(value):
    return value.strftime('%Y-%m-%dT%H:%M:%S') if value else None


def _record_state(entry, rec, ctx) -> str:
    """Which of the five Record states this showing is in.

    A live or scheduled recording outranks "ended": a showing that is being recorded right
    now has necessarily started, and the useful action there is Manage, not a dead button.
    """
    if rec is not None:
        if rec.status in _REC_LIVE:
            return REC_STATE_RECORDING
        if rec.status in _REC_DONE:
            return REC_STATE_RECORDED
        if rec.status not in _REC_DEAD:
            return REC_STATE_SCHEDULED
    if entry.stop_time and entry.stop_time <= ctx.now:
        return REC_STATE_PAST
    return REC_STATE_NONE


def _airing_field_hits(channel_ids, state, ctx) -> dict:
    """{channel id: (field key, ...)} for the CHANNEL-side search fields only.

    The why chip is quieter on this grain by design: the program is the row, so a title or
    sub-title hit needs no explanation and only a channel-side hit is a surprise worth
    labelling. Restricting the query to channel-side fields is also what keeps it a single
    `SELECT ... FROM channels` - an EPG predicate on this grain is an expression over
    `epg_entries` and could not be evaluated there at all.
    """
    from .channel_search import SOURCE_CHANNEL
    if not include_terms(state):
        return {}
    keys, exprs = [], []
    for field in FIELDS:
        if field.key not in state.fields or field.source != SOURCE_CHANNEL:
            continue
        predicate = field_hit_predicate(field.key, state, ctx)
        if predicate is None:
            continue
        keys.append(field.key)
        exprs.append(case((predicate, 1), else_=0))
    if not keys:
        return {}

    out = {}
    for row in db.session.execute(select(Channel.id, *exprs).where(Channel.id.in_(channel_ids))):
        out[row[0]] = tuple(key for key, flag in zip(keys, row[1:]) if flag)
    return out


def _airing_why(channel_id, hits):
    matched = hits.get(channel_id)
    if not matched:
        return None
    field = next((f for f in FIELDS if f.key in matched), None)
    if field is None:
        return None
    return {'field': field.key, 'label': field.label, 'source': field.source,
            'program': None}


def _recordings_for(entries, channels_by_id) -> dict:
    """{entry id: Recording} - one query for the page, matched exactly as the guide does.

    The window is the page's own span rather than "everything in the future", because this
    grain can show showings that have already ended and a finished recording of one of them is
    a real state the Record action draws (Re-record, not Record).

    Group-backed recordings are matched through the group a channel belongs to, which is why
    the membership map is read here: a recording created from a group carries `group_id` and
    no `channel_id`, so a channel-keyed lookup alone would miss every one of them and the row
    would offer to schedule a second recording of something already being recorded.
    """
    spans = [(e.start_time, e.stop_time) for e in entries if e.start_time and e.stop_time]
    if not spans:
        return {}
    window_start = min(s for s, _ in spans)
    window_stop = max(s for _, s in spans)

    candidates = Recording.query.filter(
        Recording.stop_time >= window_start,
        Recording.start_time <= window_stop,
        Recording.status.notin_(_REC_DEAD),
    ).all()
    if not candidates:
        return {}
    indexes = build_rec_indexes(candidates)

    # setdefault(...).append(...), not {channel: group}: a channel may belong to several
    # groups, and a dict would silently keep one of them.
    groups_by_channel = {}
    rows = (db.session.query(ChannelGroupMember.channel_id, ChannelGroup)
            .join(ChannelGroup, ChannelGroup.id == ChannelGroupMember.group_id)
            .filter(ChannelGroupMember.channel_id.in_(list(channels_by_id))).all())
    for channel_id, group in rows:
        groups_by_channel.setdefault(channel_id, []).append(group)

    out = {}
    for entry in entries:
        ch = channels_by_id.get(entry.channel_id)
        if ch is None or not entry.start_time or not entry.stop_time:
            continue
        recs = []
        for group in groups_by_channel.get(ch.id, []):
            recs += candidate_recs(indexes, ch, ch.stream_url or '', group)
        recs += candidate_recs(indexes, ch, ch.stream_url or '')
        match = match_recording(recs, entry.start_time, entry.stop_time)
        if match is not None:
            out[entry.id] = match
    return out


def _filename_templates(channels, ctx) -> dict:
    """{channel id: template} - resolved once per channel, never inside the row loop.

    No batching query is needed: `Channel.default_profile` is declared `lazy='joined'`
    (app/database.py), so the profile arrives with the channel. Resolved here anyway rather
    than in the loop, because "which template" is a per-channel fact and a page has far more
    rows than channels.
    """
    from .accounts import effective_filename_template
    return {c.id: effective_filename_template(ctx.cfg, c) for c in channels}


def _tag_cleanup(ctx) -> list:
    from .accounts import filename_tag_cleanup
    return filename_tag_cleanup(ctx.cfg)


def _suggested_name(ch, entry, templates, tag_cleanup, tags_by_name, tz) -> str:
    from .accounts import render_filename_template
    # tags_by_name is what keeps this out of the N+1 class: this runs once per showing, and
    # without a prefetched map the renderer would issue up to two Tag queries per row.
    # tests/test_scaling_pages.py is the guard (dev/changelog/441). tz is the same fix,
    # one call site over: without it the renderer calls get_display_tz() -> load_config()
    # per showing.
    return render_filename_template(templates.get(ch.id, ''), {
        'start_time': entry.start_time,
        'stop_time': entry.stop_time,
        'title': entry.title or ch.name,
        'sub_title': entry.sub_title or '',
        'description': entry.description or '',
        'channel_name': ch.name,
        'category': entry.category or '',
    }, tag_cleanup=tag_cleanup, tags_by_name=tags_by_name, tz=tz)


def _matched_tags(tags, entry) -> list:
    """The tags this SHOWING's own text carries.

    Deliberately over the showing rather than over the channel: the channel's tags are in the
    row's `channel.tags`, and the two are different answers - a channel tagged Live because
    something on it is live is not the same as this program being the live one. Same helper
    the TV Guide's program cells use, so one tag means one thing everywhere.
    """
    from .accounts import tags_matching
    return tags_matching(tags, entry.title, entry.sub_title, entry.description)


# ---------------------------------------------------------------------------
# The enrichments - one query each, for the whole page
# ---------------------------------------------------------------------------

def _field_hits(ids, state, ctx) -> dict:
    """{channel id: (field key, ...)} - which of the active search fields each row matched.

    One query holding one boolean expression per active field, rather than the obvious
    per-field query: the expressions are the same ones the result set was built from, so
    asking them all at once about 100 known ids costs one statement and keeps the answer
    consistent with the search itself.
    """
    if not include_terms(state):
        return {}
    keys, exprs = [], []
    for field in FIELDS:
        if field.key not in state.fields:
            continue
        predicate = field_hit_predicate(field.key, state, ctx)
        if predicate is None:
            continue
        keys.append(field.key)
        exprs.append(case((predicate, 1), else_=0))
    if not keys:
        return {}

    query = select(Channel.id, *exprs).where(Channel.id.in_(ids))
    out = {}
    for row in db.session.execute(query):
        out[row[0]] = tuple(key for key, flag in zip(keys, row[1:]) if flag)
    return out


def _why(channel_id, hits, programs, state):
    """Which field earned this row, or None when the name did.

    The name winning silently is the point: the chip is there for a surprise ("this matched
    something it is AIRING"), so a row whose name matched needs no explanation. Fields are
    consulted in registry order so the answer does not depend on the order the caller
    happened to list them in.
    """
    matched = hits.get(channel_id)
    if not matched:
        return None
    if 'name' in state.fields and 'name' in matched:
        return None
    field = next((f for f in FIELDS if f.key in matched and f.key != 'name'), None)
    if field is None:
        return None

    program = programs.get(channel_id)
    return {
        'field': field.key,
        'label': field.label,
        'source': field.source,
        'program': None if program is None else {
            'title': program[0] or '',
            'sub_title': program[1] or '',
            'description': program[2] or '',
        },
    }


def _now_airing(ids) -> dict:
    """{channel id: {title, sub_title, start_time, stop_time}} for what is on right now.

    Straight off epg_entries rather than chan_prog: chan_prog is deduped, so it has no
    per-airing times to compare against the clock, and "now" is a time question.
    """
    now = datetime.utcnow()
    rows = (db.session.query(EPGEntry.channel_id, EPGEntry.title, EPGEntry.sub_title,
                             EPGEntry.start_time, EPGEntry.stop_time)
            .filter(EPGEntry.channel_id.in_(ids),
                    EPGEntry.start_time <= now, EPGEntry.stop_time > now)
            .order_by(EPGEntry.channel_id, EPGEntry.start_time).all())
    out = {}
    for channel_id, title, sub_title, start, stop in rows:
        out.setdefault(channel_id, {
            'title': title or '',
            'sub_title': sub_title or '',
            # Naive UTC ISO, the shape every other JSON surface in this app sends
            # (routes/guide.py::_program_dict) - the page converts for display.
            'start_time': start.strftime('%Y-%m-%dT%H:%M:%S') if start else None,
            'stop_time': stop.strftime('%Y-%m-%dT%H:%M:%S') if stop else None,
        })
    return out


def _groups_by_channel(ids) -> tuple:
    """({channel id: [{id, name}, ...]}, {channel id: [{id, name} for in-guide groups]}).

    Ordered by lowered name, matching the Channels hub's own membership badges, so the
    "first group" the column shows is the same group on both pages.

    The id rides along with the name because both of these render as CONTROLS - the Groups
    column's pill and the "In guide via CW" badge each open the group they name - and a
    name is not an address. Same shape as `tags` for the same reason (dev/changelog/860).

    The second map is the "In guide via FS1" badge's source, and it is built here rather
    than by a query of its own because `ChannelGroup.in_guide` is one more column on a join
    this already runs - a second query would be the per-page I/O the row builder exists to
    avoid. It answers the WIDER guide question the `f.other=guide` filter asks
    (`channel_groups.guide_scope_channel_ids()`): a member of an in-guide group has its
    listings on screen through that group's row even with its own flag off. Reading only
    `Channel.in_guide` is what made the filter and the badges disagree (dev/changelog/759).
    """
    rows = (db.session.query(ChannelGroupMember.channel_id, ChannelGroup.id,
                             ChannelGroup.name, ChannelGroup.in_guide)
            .join(ChannelGroup, ChannelGroup.id == ChannelGroupMember.group_id)
            .filter(ChannelGroupMember.channel_id.in_(ids))
            .order_by(db.func.lower(ChannelGroup.name)).all())
    out, via = {}, {}
    for channel_id, group_id, name, group_in_guide in rows:
        entry = {'id': group_id, 'name': name}
        out.setdefault(channel_id, []).append(entry)
        if group_in_guide:
            via.setdefault(channel_id, []).append(entry)
    return out, via


def _tags_by_channel(ids, ctx, now_scoped=True) -> dict:
    """{channel id: [{id, name, color}, ...]} - the tags each row carries.

    A tag is a set of literal patterns matched against the channel's name and against what it
    is airing, not a membership table, so this is one boolean expression per tag - the same
    expression the tag facet counts with. One query for the page regardless of how many tags
    exist: the cost scales with the tag vocabulary, which is a per-request constant, never
    with the number of rows.

    `now_scoped` must match what the FILTER on this grain means, or the page contradicts
    itself: a badge the user can see and a facet count they can click have to agree with the
    rows they produce. `tag_hit_predicate` carries the argument for why the two grains answer
    differently (dev/changelog/862).

    This is the expensive enrichment - 67-70ms of the payload's 75-84ms, measured. See the
    module docstring for why the answer is not to match the patterns in Python here.
    """
    if not ctx.tags or not ids:
        return {}
    exprs = [case((tag_hit_predicate(tag, ctx, now_scoped), 1), else_=0) for tag in ctx.tags]
    query = select(Channel.id, *exprs).where(Channel.id.in_(ids))
    out = {}
    for row in db.session.execute(query):
        carried = [{'id': tag.id, 'name': tag.name, 'color': tag.color}
                   for tag, flag in zip(ctx.tags, row[1:]) if flag]
        if carried:
            out[row[0]] = carried
    return out


def _lifecycle(rows, ctx) -> dict:
    from .accounts import lifecycle_states_for_channels
    return lifecycle_states_for_channels(rows, ctx.cfg, accounts_by_id=ctx.accounts_by_id)


def _duplicate_clusters(rows) -> dict:
    """{channel id: {count, others, kept_id, kept_reason}} for every duplicate row on the page.

    The cluster is read over the whole channels table, not over the result set, for the same
    reason the engine computes the keep-rule that way: which copy is KEPT must not change
    because of what the user typed. One query for every cluster the page touches.
    """
    urls = {r.stream_url for r in rows if r.is_duplicate_stream_url and r.stream_url}
    if not urls:
        return {}

    members = (db.session.query(Channel.id, Channel.name, Channel.category_name,
                                Channel.stream_url, Channel.in_guide, Channel.hidden,
                                in_any_group_expr().label('in_group'),
                                effective_health().label('health'))
               .filter(Channel.stream_url.in_(urls),
                       Channel.is_duplicate_stream_url.is_(True)).all())
    by_url = {}
    for member in members:
        by_url.setdefault(member.stream_url, []).append(member)

    out = {}
    for url, cluster in by_url.items():
        ranked = sorted(cluster, key=_keep_rank)
        keep = ranked[0]
        reason = _keep_reason(keep, cluster)
        for member in cluster:
            others = [m for m in ranked if m.id != member.id]
            out[member.id] = {
                'count': len(cluster),
                # Every member, so the badge's drill-in can ask for exactly this cluster by
                # id (`f.chan=`). It cannot search the URL the way the mockup did: the row
                # payload masks stream URLs, so the text on screen is not the text in the
                # database. `others` is the TOOLTIP's list and is truncated; this one is not.
                'ids': [m.id for m in ranked],
                'others': [{'id': o.id, 'name': o.name, 'category': o.category_name or ''}
                           for o in others[:DUP_TOOLTIP_MEMBERS]],
                'others_hidden': max(0, len(others) - DUP_TOOLTIP_MEMBERS),
                'kept_id': keep.id,
                'kept_reason': reason,
            }
    return out


def _keep_rank(member):
    """The keep-rule cascade as a sort key, spelled to match `_duplicate_losers()`'s SQL
    exactly: not hidden first, then in your guide, then in a channel group, then the best
    health, then the lowest id. SQLite sorts NULL lowest, so a DESC on health puts a
    never-scored channel behind a scored one - hence the explicit "health is None" term
    rather than treating None as any particular number.

    Changing a rung here means changing it in `_duplicate_losers()` in the same edit: that
    query decides which rows are excluded and this one only explains it, so a disagreement
    shows up as a badge describing a rule the list did not follow."""
    health = member.health
    return (1 if member.hidden else 0, 0 if member.in_guide else 1,
            0 if member.in_group else 1,
            0 if health is not None else 1, -(health or 0), member.id)


def _keep_reason(keep, cluster) -> str:
    """The one rung that actually decided it - the first on which the winner differs from
    anything else in the cluster. A rung the whole cluster ties on explains nothing."""
    if any(m.id != keep.id and bool(m.hidden) != bool(keep.hidden) for m in cluster):
        return KEEP_REASON_NOT_HIDDEN
    if any(m.id != keep.id and bool(m.in_guide) != bool(keep.in_guide) for m in cluster):
        return KEEP_REASON_IN_GUIDE
    if any(m.id != keep.id and bool(m.in_group) != bool(keep.in_group) for m in cluster):
        return KEEP_REASON_IN_GROUP
    if any(m.id != keep.id and m.health != keep.health for m in cluster):
        return KEEP_REASON_HEALTH
    return KEEP_REASON_ID


# ---------------------------------------------------------------------------
# Per-row scalars
# ---------------------------------------------------------------------------

def _health(ch):
    """The score the badges show - observed plus the manual adjustment, as the engine's
    banding uses it. None means never tested, which is not the same as zero."""
    if ch.health_score is None:
        return None
    return ch.health_score + (ch.manual_health_adjustment or 0)


def _health_band(ch, bands) -> str:
    """`bands` is resolved once per request by the caller (health_bands.resolve_bands) -
    never re-resolved here, because this runs once per row."""
    if ch.health_score is None:
        return HEALTH_UNTESTED
    return health_bands.band_for(_health(ch), bands)


def _account(account) -> dict:
    if account is None:
        return {'id': None, 'name': '', 'color': ''}
    return {'id': account.id, 'name': account.name, 'color': account.color}
