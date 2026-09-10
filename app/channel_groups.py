"""Manual channel grouping: member ranking + the suggest-duplicates helper.

A ChannelGroup (app/database.py) is a user-defined set of duplicate feeds of one
logical channel. The group appears in the TV Guide as a single row; recordings
created from it resolve to the highest-scored member at record start
(app/recorder.py) and fail over to the next-best member when the active feed dies
(app/watchdog.py). Routes live in app/routes/channel_groups.py.
"""
import collections
import json
import math
import re
import statistics

# Ranking-only neutral score for members that have never been observed. Display
# code deliberately shows "untested" instead (e.g. routes/channels.py channel
# detail) - do not reuse this constant for display.
UNSCORED_NEUTRAL = 50

# Ranking-only fallback for the consecutive-FAILED-test streak that demotes a member
# to last-resort (dev/changelog/478) - mirrors channel_testing.failing_streak_threshold's
# own default (app/config.py _DEFAULTS). Callers that already have cfg loaded pass the
# live configured value explicitly; this is the fallback for the few that don't.
DEFAULT_FAILING_STREAK_THRESHOLD = 3

# Suggest-candidates ranking order, shared with app/routes/channel_groups.py::suggest -
# lower is better/stronger. A candidate matched by both epg_id and name outranks an
# epg_id-only or name-only match; a format-confirmed candidate outranks unverified,
# which outranks a known-different (incompatible) one.
MATCH_REASON_STRENGTH = {'epg_id+name': 0, 'epg_id': 1, 'name': 2}
FORMAT_STATUS_STRENGTH = {'confirmed': 0, 'unverified': 1, 'different': 2}


def effective_score(channel) -> int:
    """0-100 ranking score: lifetime health score (neutral 50 if never observed)
    plus the manual adjustment, clamped."""
    base = channel.health_score if channel.health_score is not None else UNSCORED_NEUTRAL
    return int(round(max(0, min(100, base + (channel.manual_health_adjustment or 0)))))


def is_streaking(channel, streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD) -> bool:
    """True when `channel` has failed its last `streak_threshold`-or-more health
    checks in a row (Channel.consecutive_test_failures, maintained by
    app/health_score.py::apply_test_health_observation). `streak_threshold <= 0`
    disables the concept entirely - a distinct signal from effective_score, kept
    separate per CLAUDE.md 'one flag, one meaning' (see channel_failing_reason)."""
    return streak_threshold > 0 and (channel.consecutive_test_failures or 0) >= streak_threshold


def resolution_pixels(resolution) -> int:
    """width * height off a "1920x1080" string, 0 when it cannot be parsed. One spelling
    of the parse, shared by the member ranking and the bucket ranking so the two cannot
    disagree about which of two formats is the larger picture."""
    try:
        w_str, h_str = resolution.split('x', 1)
        return int(w_str) * int(h_str)
    except (ValueError, AttributeError):
        return 0


def rank_members(members, latest_by_channel=None, exclude_ids=frozenset(),
                  streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD):
    """Members sorted best-first: non-streaking members by effective score, then by the
    quality signals in _format_sort_key's own order - measured bitrate, then picture
    size, then frame rate - and only then id ascending; then streaking members the same
    way. A streaking member ranks last-resort, never excluded outright, so a group where
    every member is streaking still hands back its best-scoring one rather than nothing
    (CLAUDE.md Product Principle 2: complete the recording at almost all costs).

    **Health score strictly dominates; the quality signals only break its exact ties.**
    That is the difference between this and the blended quality score CLAUDE.md's "Format
    lock filters, health score ranks" rule forbids - ranking on bitrate would pick a dead
    8 Mb/s feed over a live 3 Mb/s one, which is the failure this app exists to prevent.
    Two members that score identically have already passed that test equally, and there
    the goal is the higher-quality recording, so every signal that indicates quality is
    consulted before falling back to insertion order (dev/changelog/890).

    Unmeasured sorts last in each of the three, matching _format_sort_key's -1
    convention: an untested member must never sort as if it had the highest bitrate.

    `latest_by_channel` is an optional channel_id -> ChannelTest map (e.g. from
    _latest_tests_by_channel) supplying those tie-breaks; omit it (or pass None) to fall
    straight through to the id tie-break, as before this param existed."""
    latest_by_channel = latest_by_channel or {}

    def _quality(ch):
        test = latest_by_channel.get(ch.id)
        if test is None:
            return (-1, 0, -1)
        bitrate = getattr(test, 'bitrate_kbps', None)
        fps = getattr(test, 'fps', None)
        return (-1 if bitrate is None else bitrate,
                resolution_pixels(getattr(test, 'resolution', None)),
                -1 if fps is None else fps)

    candidates = [ch for ch in members if ch.id not in exclude_ids]

    def _key(ch):
        bitrate, pixels, fps = _quality(ch)
        return (is_streaking(ch, streak_threshold), -effective_score(ch),
                -bitrate, -pixels, -fps, ch.id)

    return sorted(candidates, key=_key)


def pick_best_member(members, latest_by_channel=None, exclude_ids=frozenset(),
                      streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD):
    """Highest-ranked member not excluded, or None - see rank_members for ordering."""
    ranked = rank_members(members, latest_by_channel, exclude_ids, streak_threshold)
    return ranked[0] if ranked else None


# Decoration tokens providers append to duplicate feeds of the same channel
# (quality/codec/region variants). Removed before name comparison so
# "US: FS1 FHD" and "FS1 HD [4K]" normalize to the same key. EAST/WEST are
# included deliberately: they're strong duplicate *candidates* worth suggesting
# even though time-shifted feeds may not belong in one group - the user decides.
_DECORATION_TOKENS = {
    'hd', 'fhd', 'uhd', 'sd', '4k', '8k', 'hevc', 'h264', 'h265',
    '50fps', '60fps', 'raw', 'vip', 'backup',
    'us', 'usa', 'uk', 'ca', 'east', 'west',
}

_PREFIX_RE = re.compile(r'^[a-z]{2,3}\s*[:|\-]\s*', re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r'[^a-z0-9 ]+')


def normalize_channel_name(name: str) -> str:
    """Collapse a channel name to a comparison key: lowercase, leading country
    prefix ("US:", "USA |") dropped, punctuation dropped, decoration tokens
    dropped, whitespace collapsed. Returns '' if nothing meaningful remains."""
    s = _PREFIX_RE.sub('', (name or '').lower())
    s = _NON_ALNUM_RE.sub(' ', s)
    words = [w for w in s.split() if w not in _DECORATION_TOKENS]
    return ' '.join(words)


def format_key(test):
    """The (resolution, fps-rounded) identity used to decide whether two feeds are
    the *same video format* for grouping. Returns None when the format is unknown
    (no test, or the test is missing resolution or fps).

    - Resolution: the exact ffprobe string ("1920x1080") - both sides come from
      ffprobe so exact-string equality is correct.
    - FPS: rounded to the nearest integer, so broadcast fractional rates compare
      equal to their nominal (29.97≈30, 59.94≈60, 23.976≈24). Never compare raw floats.
    - Bitrate is deliberately NOT part of the key: same-format feeds routinely differ
      in bitrate and that must never block grouping.
    """
    if test is None:
        return None
    return format_key_from(getattr(test, 'resolution', None), getattr(test, 'fps', None))


def format_key_from(resolution, fps):
    """format_key() over a loose (resolution, fps) pair, for a source that spells the
    two fields differently. One rounding rule, in one place: a second copy of
    `round(fps)` elsewhere would let two callers disagree about whether 59.94 and 60
    are the same format, which is the whole question the key exists to answer."""
    res = (resolution or '').strip()
    if not res or not fps:
        return None
    return (res, round(fps))


def segment_format_key(seg):
    """The format_key of a RecordingSegment, from the capture-time ffprobe the watchdog
    ran against the segment file (`probe_resolution`/`probe_fps`).

    This is what was actually captured, not what a health check once measured off the
    same channel - which is why the recording's format pin is sourced from it
    (DESIGN-channel-groups-model.md 5.1, dev/changelog/754). None when the segment was
    never probed, which reads as "unknown", never as "different"."""
    if seg is None:
        return None
    return format_key_from(getattr(seg, 'probe_resolution', None),
                           getattr(seg, 'probe_fps', None))


def format_label(key) -> str:
    """Human-readable label for a format_key tuple, e.g. "1920x1080 @ 60"."""
    if not key:
        return 'unknown'
    res, fps = key
    return f'{res} @ {fps}'


def classify_group_formats(channels, latest_by_channel):
    """Partition would-be group members by video format for the grouping guard.

    `latest_by_channel` maps channel_id -> latest ChannelTest (or omits untested
    channels). Returns a dict:
      {
        'buckets': {format_key: [channel, ...], ...},   # only KNOWN formats
        'untested': [channel, ...],                     # unknown format (no usable test)
        'distinct_known': int,                          # len(buckets)
      }
    A hard mismatch exists when distinct_known >= 2. Pure/DB-free so it stays
    unit-testable - the caller supplies the test map.
    """
    buckets = {}
    untested = []
    for ch in channels:
        key = format_key(latest_by_channel.get(ch.id))
        if key is None:
            untested.append(ch)
        else:
            buckets.setdefault(key, []).append(ch)
    return {'buckets': buckets, 'untested': untested, 'distinct_known': len(buckets)}


# The four named auto-select-format strategies (dev/changelog/494, planned
# 2026-08-06). Deliberately not a single blended "quality score" - CLAUDE.md Product
# Principle 1, a number the user cannot explain is worse than no number.
FORMAT_STRATEGIES = ('highest_bitrate', 'highest_resolution', 'most_channels', 'balanced')
# duplicated from static/js/format-plan.js FORMAT_STRATEGY_LABELS - the dropdown's labels
# are built client-side and a server-side prose string (the GROUP_FORMAT_STRATEGY_APPLIED
# event) has to name the same strategy the user picked with the same words. Parity with
# FORMAT_STRATEGIES and with the JS list is asserted in tests.
FORMAT_STRATEGY_LABELS = {
    'highest_bitrate': 'Highest bitrate',
    'highest_resolution': 'Highest resolution',
    'most_channels': 'Most members',
    'balanced': 'Balanced',
}
BALANCED_COVERAGE_FLOOR = 0.60
# A WARN feed played, it just didn't play cleanly - it counts as healthy for format
# selection. FAIL, CANCELLED and never-tested channels are excluded from both the tally
# and the kept set. Mirrors app/routes/channel_tests.py::_test_status_label's own values.
HEALTHY_TEST_LABELS = frozenset({'PASS', 'WARN'})


def healthy_channels(channels, latest_by_channel) -> list:
    """`channels` whose latest test is healthy enough to count toward a format decision -
    see HEALTHY_TEST_LABELS. Pure/DB-free; late-imports _test_status_label (the single
    PASS/WARN/FAIL/CANCELLED/WAITING derivation) to keep this module free of an
    import-time dependency on a routes module, matching this file's existing late-import
    convention (see evaluate_and_reconcile_group)."""
    from .routes.channel_tests import _test_status_label
    return [ch for ch in channels
            if _test_status_label(latest_by_channel.get(ch.id)) in HEALTHY_TEST_LABELS]


def format_buckets(channels, latest_by_channel, rank_ids=None) -> list:
    """Healthy channels grouped by video format (format_key) - one bucket per distinct
    (resolution, fps). A channel with an unknown format is dropped; a bucket needs a
    known format to be selectable. Each bucket:
      {'key', 'label', 'resolution', 'fps', 'pixels', 'channel_ids', 'count',
       'pass_count', 'warn_count', 'median_bitrate_kbps', 'median_bpp',
       'rank_count', 'rank_median_bitrate_kbps', 'rank_median_bpp'}
    'pixels' = width * height parsed off the resolution string (so each strategy's sort
    key doesn't re-parse it). Medians are statistics.median over non-None values, None
    when the bucket has none - never silently substitute a number. Pure/DB-free.

    **The bucket answers two different questions and keeps them apart.** `count`,
    `channel_ids` and `median_*` describe every channel handed in: that is the MEMBERSHIP
    question ("who matches this format"), and apply_format_plan can delete the members a
    winning bucket leaves out, so narrowing it would delete members nobody asked about.
    `rank_count` and `rank_median_*` describe only `rank_ids` - the members the lock will
    actually filter, i.e. the recording-enabled ones - and are what the strategies rank
    on. `rank_ids=None` ranks over everyone, which is the right answer for a group with
    nothing recording-enabled yet (a clone starts that way) and reproduces the behavior
    that predates the split.

    `pass_count`/`warn_count` split the healthy set by test label. A WARN still counts as
    healthy - see HEALTHY_TEST_LABELS - so a bucket can win on channels that are every
    one of them warning, and the surfaces that show a bucket owe the user that number
    rather than the word "healthy" on its own (dev/changelog/890)."""
    from .routes.channel_tests import _test_status_label
    by_key = {}
    for ch in channels:
        test = latest_by_channel.get(ch.id)
        label = _test_status_label(test)
        if label not in HEALTHY_TEST_LABELS:
            continue
        key = format_key(test)
        if key is None:
            continue
        by_key.setdefault(key, []).append((ch, test, label))

    def _median(values):
        return statistics.median(values) if values else None

    buckets = []
    for key, rows in by_key.items():
        res, fps = key
        ranked = rows if rank_ids is None else [r for r in rows if r[0].id in rank_ids]
        buckets.append({
            'key': key,
            'label': format_label(key),
            'resolution': res,
            'fps': fps,
            'pixels': resolution_pixels(res),
            'channel_ids': [ch.id for ch, _t, _l in rows],
            'count': len(rows),
            'pass_count': sum(1 for _ch, _t, lbl in rows if lbl == 'PASS'),
            'warn_count': sum(1 for _ch, _t, lbl in rows if lbl != 'PASS'),
            'median_bitrate_kbps': _median(
                [t.bitrate_kbps for _ch, t, _l in rows if t.bitrate_kbps is not None]),
            'median_bpp': _median(
                [t.bits_per_pixel_frame for _ch, t, _l in rows
                 if t.bits_per_pixel_frame is not None]),
            'rank_count': len(ranked),
            'rank_median_bitrate_kbps': _median(
                [t.bitrate_kbps for _ch, t, _l in ranked if t.bitrate_kbps is not None]),
            'rank_median_bpp': _median(
                [t.bits_per_pixel_frame for _ch, t, _l in ranked
                 if t.bits_per_pixel_frame is not None]),
        })
    return buckets


def _format_sort_key(strategy, bucket):
    """Ascending sort key tuple for `strategy` - the winner is min(buckets, key=this).
    A None median substitutes -1 so an unmeasured bucket can never silently sort as if
    it had the highest bitrate; never leave a tie resolving to insertion order.

    Reads the `rank_*` statistics, never `count`/`median_bitrate_kbps`: a lock exists to
    filter recording candidates, so it is ranked over the members it will actually filter
    (format_buckets' `rank_ids`). The membership figures stay on the bucket for the
    surfaces that report who matches."""
    bitrate = bucket['rank_median_bitrate_kbps']
    bitrate = -1 if bitrate is None else bitrate
    if strategy == 'highest_bitrate':
        return (-bitrate, -bucket['pixels'], -bucket['fps'], bucket['resolution'])
    if strategy == 'highest_resolution':
        return (-bucket['pixels'], -bucket['fps'], -bitrate, bucket['resolution'])
    if strategy == 'most_channels':
        return (-bucket['rank_count'], -bitrate, -bucket['pixels'], -bucket['fps'],
                bucket['resolution'])
    raise ValueError(f'unknown format strategy: {strategy}')


def _rankable_buckets(buckets):
    """The buckets a strategy may pick from: those holding at least one of the members
    the lock will filter (`rank_count`).

    A bucket with none of them cannot be a correct answer to the question a lock exists
    to answer - it would filter out every member the group could record from, so every
    recording would fall through the zero-survivor override from the moment the lock was
    written rather than as the stale-lock exception that override was built for
    (dev/changelog/890). With `rank_ids=None` every bucket has rank_count == count and
    nothing is excluded."""
    return [b for b in buckets if b['rank_count'] > 0]


def _pick_format_bucket(strategy, buckets):
    """The winning bucket for `strategy`, or None when no bucket qualifies (no healthy
    formats at all, none holding a member the lock would filter, or - balanced only - no
    bucket clears the coverage floor)."""
    buckets = _rankable_buckets(buckets)
    if not buckets:
        return None
    if strategy == 'balanced':
        max_count = max(b['rank_count'] for b in buckets)
        floor = math.ceil(BALANCED_COVERAGE_FLOOR * max_count)
        eligible = [b for b in buckets if b['rank_count'] >= floor]
        if not eligible:
            return None
        return min(eligible, key=lambda b: _format_sort_key('highest_bitrate', b))
    return min(buckets, key=lambda b: _format_sort_key(strategy, b))


def _no_winner_rationale(strategy, buckets):
    """Why `strategy` picked nothing, naming the specific reason rather than one sentence
    covering three different situations - the three are fixed by three different actions,
    so a user told only "no eligible format" cannot act on it (CLAUDE.md: failure paths
    must be observable)."""
    if not buckets:
        return 'No format has enough healthy channels to build a group on.'
    if not _rankable_buckets(buckets):
        return ('No measured format holds a member this group is set to record from. '
                'Turn Recording on for a member, or pin a format by hand.')
    return (f'No format holds at least {int(BALANCED_COVERAGE_FLOOR * 100)}% as many '
            f'recordable channels as the largest one, so Balanced has nothing broad '
            f'enough to settle on.')


def _format_strategy_entry(strategy, buckets):
    """One FORMAT_STRATEGIES entry: the winning bucket restated as
    {'key', 'label', 'resolution', 'fps', 'channel_ids', 'count', 'rank_count',
    'pass_count', 'warn_count', 'rationale'}, or the no-winner shape with a plain-English
    reason.

    `count`/`channel_ids` are the MEMBERSHIP figures (every member measuring this format);
    `rank_count` is how many of those the group would actually record from, and is what
    the strategy ranked on. Both are reported because they answer different questions and
    one standing in for the other is how a bucket holding nothing recordable came to win
    a lock in the first place."""
    winner = _pick_format_bucket(strategy, buckets)
    if winner is None:
        return {'key': None, 'label': None, 'resolution': None, 'fps': None,
                'channel_ids': [], 'count': 0, 'rank_count': 0,
                'pass_count': 0, 'warn_count': 0,
                'rationale': _no_winner_rationale(strategy, buckets)}
    candidates = len(_rankable_buckets(buckets))
    bitrate = winner['rank_median_bitrate_kbps']
    bitrate_txt = (f'a median {bitrate / 1000:.2f} Mb/s' if bitrate is not None
                   else 'an unmeasured bitrate')
    # "channels" here is always the ranked population - the number that decided it. Saying
    # "healthy" and stopping there is what let a bucket whose every channel is warning read
    # as a clean win (dev/changelog/890).
    n = winner['rank_count']
    channels_txt = f'{n} recordable channel{"" if n == 1 else "s"}'
    if winner['pass_count'] == 0:
        channels_txt += ' (every one of them warning)'
    if strategy == 'highest_bitrate':
        rationale = (f'{channels_txt} at {bitrate_txt}, the highest of '
                     f'{candidates} candidate formats')
    elif strategy == 'highest_resolution':
        rationale = (f"{channels_txt} at {winner['label']}, the largest picture of "
                     f'{candidates} candidate formats')
    elif strategy == 'most_channels':
        rationale = (f'{channels_txt}, the most of {candidates} candidate formats - '
                     f'a tie is broken by median bitrate, then picture size, then frame rate')
    else:
        rationale = (f'{channels_txt} at {bitrate_txt}, the highest bitrate among formats '
                     f'holding at least {int(BALANCED_COVERAGE_FLOOR * 100)}% as many '
                     f'recordable channels as the largest')
    return {'key': winner['key'], 'label': winner['label'], 'resolution': winner['resolution'],
            'fps': winner['fps'], 'channel_ids': winner['channel_ids'],
            'count': winner['count'], 'rank_count': winner['rank_count'],
            'pass_count': winner['pass_count'], 'warn_count': winner['warn_count'],
            'rationale': rationale}


def plan_format_selection(channels, latest_by_channel, rank_ids=None) -> dict:
    """The auto-select-format engine: for each of FORMAT_STRATEGIES, which format wins
    and why. Returns {'buckets', 'eligible_count', 'excluded_count', 'total',
    'rank_total', 'strategies': {strategy: entry, ...}}. Pure/DB-free - the caller
    supplies the test map (channel_id -> latest ChannelTest). See format_buckets for what
    counts as healthy, how buckets are built and what `rank_ids` narrows, and
    _format_sort_key for the deterministic tie-break per strategy.

    `total` counts every channel handed in; `rank_total` counts the ones the lock would
    actually filter. The two differ on a group whose members are not all set to record,
    and a surface that shows one as if it were the other is describing a group the user
    does not have."""
    total = len(channels)
    buckets = format_buckets(channels, latest_by_channel, rank_ids=rank_ids)
    eligible_count = sum(b['count'] for b in buckets)
    return {
        'buckets': buckets,
        'eligible_count': eligible_count,
        'excluded_count': total - eligible_count,
        'total': total,
        'rank_total': total if rank_ids is None else sum(1 for ch in channels
                                                         if ch.id in rank_ids),
        # How many of the ranking population have a measured format at all. The consequence
        # line needs it to tell "measures a different format, so it is skipped" apart from
        # "never tested, so it is still eligible" - the two behave differently and one
        # number covering both is wrong in the direction that matters. Counted here rather
        # than client-side because the page's own rows are scoped to one health check and
        # would answer a different question (dev/changelog/890).
        'rank_measured': sum(1 for ch in channels
                             if (rank_ids is None or ch.id in rank_ids)
                             and format_key(latest_by_channel.get(ch.id)) is not None),
        'strategies': {s: _format_strategy_entry(s, buckets) for s in FORMAT_STRATEGIES},
    }


_UNSET = object()


def group_name_conflict(name, exclude_group_id=None) -> bool:
    """Case-insensitive name collision check, including the pinned system group - its
    name is taken like any other group's. `exclude_group_id` lets a rename keep its own
    current name (any case)."""
    from . import db
    from .database import ChannelGroup
    q = ChannelGroup.query.filter(db.func.lower(ChannelGroup.name) == name.lower())
    if exclude_group_id is not None:
        q = q.filter(ChannelGroup.id != exclude_group_id)
    return q.first() is not None


def build_group_with_members(name, channel_ids, strategy=None):
    """Create a ChannelGroup holding `channel_ids`, WITHOUT committing. Returns the
    flushed group.

    The one way a group is brought into existence with members, so every path that makes
    one produces the same thing. The health-check create route used to assemble its own
    group out of bare ChannelGroup/ChannelGroupMember rows and got a second-class one -
    no CHANNEL_GROUPED events, so the members' own timelines never recorded that they had
    been grouped, and no hiding recompute either (dev/changelog/831).

    Mutates and adds without committing - the same convention as set_participation() and
    database.py::add_recording_event() - so the caller owns the commit and can keep the
    whole read-modify-write inside one retry_on_locked unit. Callers that also need a
    name-conflict check ask group_name_conflict() first; this does not, because it has no
    way to return an error the user can act on.

    `channel_ids` order becomes membership position order. The two participation columns
    take their model defaults - Recording off, Health check on - which is the whole of
    DESIGN-channel-groups-model.md 14's "created as a health check"; the group is never
    put in the guide, however many of its members already are (dev/changelog/751)."""
    from . import channel_hiding, db
    from .database import (Channel, ChannelEvent, ChannelGroup, ChannelGroupMember,
                           CHANNEL_GROUPED, GROUP_FORMAT_HEALTH_CHECK_ONLY)
    group = ChannelGroup(name=name,
                         format_strategy=strategy or GROUP_FORMAT_HEALTH_CHECK_ONLY)
    db.session.add(group)
    db.session.flush()  # need group.id/name for the member FKs and the events
    ids = list(channel_ids)
    for idx, cid in enumerate(ids):
        db.session.add(ChannelGroupMember(group_id=group.id, channel_id=cid, position=idx))
    if ids:
        by_id = {ch.id: ch for ch in Channel.query.filter(Channel.id.in_(ids)).all()}
        for cid in ids:
            if cid in by_id:
                db.session.add(ChannelEvent(
                    channel_id=cid, event_type=CHANNEL_GROUPED,
                    detail=f'Added to channel group "{group.name}"'))
        # A group membership keeps a channel visible even when a source says hide it, so
        # every membership gained rewrites the answer for the channels involved. After the
        # membership rows are in the session, never before: the recompute reads protection
        # in SQL, so it has to run once the rows it is judging are settled.
        channel_hiding.recompute(ids)
    return group


def member_channels(memberships):
    """The group's member Channels, in membership position order."""
    return [m.channel for m in memberships]


def recording_members(memberships):
    """Channels of the recording-enabled memberships - the set record resolution and
    failover choose from, before the format lock filters it further.

    Membership in this set is user intent and nothing else: a member whose format does
    not match the group's lock is filtered out where members are CHOSEN, not here, and
    is never unticked in the database (DESIGN-channel-groups-model.md 4.1)."""
    return [m.channel for m in memberships if m.recording_enabled]


def lock_ranking_ids(memberships):
    """The channel ids a format lock is DECIDED over: the recording-enabled members when
    the group has any, else every member.

    A lock's only job is to filter recording candidates (format_eligible_members), so a
    format holding none of them cannot be a correct answer to it - group 5 was locked to a
    720p30 bucket of 40 healthy channels holding zero recording-enabled ones, which
    filtered out all 29 members the group could record from and sent every recording
    through the zero-survivor override from the moment it was written (dev/changelog/890).

    The fallback is not a nicety: a clone's members are all Recording-off by model default
    (DESIGN-channel-groups-model.md 14), so a group with nothing enabled has no recording
    population to rank over yet and ranking over everyone is the only answer that
    describes it. Read-only - deciding a lock never writes a participation switch (4.1).

    Handed to format_buckets/plan_format_selection as `rank_ids`; the membership figures
    those return still cover every member, which is what apply_format_plan's remove
    option must keep reading."""
    return {m.channel_id for m in memberships if m.recording_enabled} or {
        m.channel_id for m in memberships}


def test_member_ids(memberships):
    """Channel ids of the memberships included in this group's health check runs.

    Membership-scoped only. A run also honors Channel.test_enabled, the channel-wide off
    switch, which wins - see check_run_channels()."""
    return {m.channel_id for m in memberships if m.test_enabled}


def participation_is_recording(group) -> bool:
    """Which of the two participation switches answers "is this member taking part in
    what this group is FOR" - True for Recording, False for Testing.

    The gate is the group's format_strategy, the same one
    DESIGN-channel-groups-model.md 16 uses for its warnings: a group still on
    health_check_only is not a recording source, so reading it through a Recording
    switch nobody has turned on yet reports every member of every new group as sitting
    out. Once it is promoted, Recording is the switch that matters.

    Every display surface that dims, counts or labels a member as "disabled" asks this
    - there is exactly one definition of it (dev/changelog/743)."""
    from .database import GROUP_FORMAT_HEALTH_CHECK_ONLY
    return group.format_strategy != GROUP_FORMAT_HEALTH_CHECK_ONLY


def participating_member_ids(group, memberships):
    """Channel ids of the members currently taking part in what `group` is for, read
    through whichever switch participation_is_recording() names."""
    if participation_is_recording(group):
        return {m.channel_id for m in memberships if m.recording_enabled}
    return test_member_ids(memberships)


# The two participation switches, by column name, with the label their event log entry
# uses. A field outside this map is refused server-side - enforcement never lives in
# whichever control happened to post it (DESIGN-channel-groups-model.md 4.2).
PARTICIPATION_FIELDS = {
    'recording_enabled': 'Recording',
    'test_enabled': 'Health check',
}

# Where the human was standing when they moved the switch. Recorded because two
# different pages write these columns and "who or what changed it, and why" is half of
# what 4.5 asks the event to carry.
PARTICIPATION_SURFACES = {
    'group_page': None,
    'check_channel_list': "from the health check's channel list",
}


def set_participation(membership, field, enabled, surface='group_page') -> bool:
    """Move one membership's Recording or Health check switch and log the move.

    THE only writer of either column, and reached only from a human action: no engine
    may set them (DESIGN-channel-groups-model.md 4.1). A member whose format does not
    match the group lock is filtered where members are chosen, not unticked here, which
    is what lets it become eligible again on its own.

    Mutates and adds the ChannelGroupEvent without committing - same convention as
    database.py::add_recording_event() - so the caller owns the commit and can keep the
    whole read-modify-write inside one retry_on_locked unit. Returns True when the
    switch moved, False for a no-op: a switch that did not move is not something that
    happened, and logging it would fill the Activity Timeline with lines saying nothing
    changed.

    A second writer that skipped this path let a member's health-check participation
    flip with no trace anywhere (dev/changelog/748); tests/test_static_invariants.py
    ::ParticipationWriteBypassTests is what keeps there being one."""
    from . import db
    from .database import ChannelGroupEvent, GROUP_MEMBER_PARTICIPATION
    if field not in PARTICIPATION_FIELDS:
        raise ValueError(f'Unknown participation field "{field}"')
    enabled = bool(enabled)
    if getattr(membership, field) == enabled:
        return False
    setattr(membership, field, enabled)  # participation-write-ok: the canonical writer
    where = PARTICIPATION_SURFACES[surface]
    detail = (f"{PARTICIPATION_FIELDS[field]} turned {'on' if enabled else 'off'} by hand"
              + (f' {where}' if where else ''))
    db.session.add(ChannelGroupEvent(
        group_id=membership.group_id, channel_id=membership.channel_id,
        event_type=GROUP_MEMBER_PARTICIPATION, detail=detail,
        extra_data=json.dumps({'field': field, 'enabled': enabled,
                               'source': 'user', 'surface': surface})))
    return True


def group_live_recordings(group):
    """Recordings capturing, concatenating or converting on this group right now.

    RESTART_BLOCKING_STATUSES is reused rather than re-typed: it is already this app's
    answer to "is a capture under way", it is what ./restart.sh refuses on, and a second
    hand-written tuple would drift the first time a status is added."""
    from .database import Recording, RESTART_BLOCKING_STATUSES
    return (Recording.query
            .filter(Recording.group_id == group.id,
                    Recording.status.in_(RESTART_BLOCKING_STATUSES))
            .order_by(Recording.stop_time.asc()).all())


def group_scheduled_recordings(group):
    """Recordings scheduled on this group that have not started yet.

    Separate from group_live_recordings() on purpose - the two are not the same thing
    (DESIGN-channel-groups-model.md 15.1). Nothing has been captured for these, so
    cancelling one costs the user nothing but a future file; a live one is a capture in
    progress and aborting it is its own deliberate verb."""
    from .database import Recording, REC_STATUS_SCHEDULED
    return (Recording.query
            .filter(Recording.group_id == group.id,
                    Recording.status == REC_STATUS_SCHEDULED)
            .order_by(Recording.start_time.asc()).all())


def guide_invariant_check(group, losing_channel_ids=frozenset()):
    """Would this action leave `group` with nothing switched on for recording, and what
    does the user need to be told before it goes ahead?

    THE one answer to DESIGN-channel-groups-model.md 15's question, asked by every path
    that can breach the invariant: the single participation switch, the bulk switch, the
    member removal and the guide button. A guard a bulk action can walk around is not a
    guard, so all four ask this rather than each counting members its own way.

    `losing_channel_ids` is what the action is about to take away - the memberships whose
    Recording switch is going off, or the ones being removed. Pass an empty set to ask
    about the group as it stands, which is what the guide button does.

    Returns a dict, never a bool: the caller needs the reason AND the facts to name in
    its confirm, and re-deriving them at each call site is how two surfaces end up
    describing the same action differently. Keys:

      breaches      - True when the action empties the recording-enabled set
      losing        - the member Channels actually being taken away (already-off members
                      the caller included are not something that happens)
      remaining     - how many recording-enabled members would survive
      in_guide      - whether the group currently holds a guide row
      live          - Recordings under way right now; non-empty means 15.1 REFUSES the
                      action outright rather than confirming it
      scheduled     - Recordings that would be cancelled if this goes ahead
    """
    losing_ids = set(losing_channel_ids or ())
    enabled = [m for m in group.memberships if m.recording_enabled]
    losing = [m for m in enabled if m.channel_id in losing_ids]
    remaining = len(enabled) - len(losing)
    breaches = bool(enabled) and remaining <= 0
    live = group_live_recordings(group) if breaches else []
    return {
        'breaches': breaches,
        'losing': [m.channel for m in losing],
        'remaining': remaining,
        'in_guide': bool(group.in_guide),
        'live': live,
        'scheduled': group_scheduled_recordings(group) if breaches and group.in_guide else [],
    }


def cancel_scheduled_recordings(scheduled, detail):
    """Abort `scheduled` recordings, naming `detail` as the reason on each one.

    Mutates and writes one RECORDING_ABORTED event per recording WITHOUT committing and
    WITHOUT touching the scheduler - same convention as set_participation() above.
    Returns the recording ids the caller must deregister after its commit lands.

    Two paths cancel a group's schedule and they must say it the same way: the guide
    demotion below, and dissolving the group outright (dev/changelog/763). A second
    hand-written copy of this loop is how one of them ends up leaving a SCHEDULED row
    that fires against a group that no longer exists."""
    from datetime import datetime
    from .database import REC_STATUS_ABORTED, RECORDING_ABORTED, add_recording_event
    ids = []
    for rec in scheduled:
        add_recording_event(rec.id, RECORDING_ABORTED, detail=detail)
        rec.status = REC_STATUS_ABORTED
        rec.completed_at = datetime.utcnow()
        ids.append(rec.id)
    return ids


def deregister_cancelled_recordings(recording_ids):
    """Drop the APScheduler jobs for recordings cancel_scheduled_recordings() just aborted.

    The pair of that function, and deliberately a separate call: unscheduling is a
    non-idempotent side effect and must run AFTER the caller's commit, never inside the
    retried closure (CLAUDE.md Agent Behavior). The surviving race is the harmless
    direction - a job that fires before this runs finds an ABORTED row and does nothing,
    where deregistering first and then failing to commit would leave a live SCHEDULED
    recording whose job no longer exists: a recording that silently never runs.

    One home rather than one per blueprint. Every path that cancels a group's schedule
    owes this call, and the ordering above is the whole content of it - a second copy is
    a second chance to put the two in the wrong order (dev/changelog/869)."""
    if not recording_ids:
        return
    from .scheduler import unschedule_recording
    for rid in recording_ids:
        unschedule_recording(rid)


def log_guide_change(group, in_guide, detail, extra=None):
    """Record that the group's TV Guide row appeared or vanished, and why.

    The one writer of GROUP_GUIDE_ADDED / GROUP_GUIDE_REMOVED, for the same reason
    set_participation() is the one writer of a participation switch: a guide row is the
    group's most visible state, and every path that moves it - the hand toggle, the
    promotion walkthrough's last step, the invariant's confirmed demotion - has to leave
    the same trace on the Activity Timeline (DESIGN-channel-groups-model.md 4.5,
    dev/changelog/764). `detail` names the cause; the type names the direction.

    Adds the event WITHOUT committing and WITHOUT writing `in_guide` itself - the caller
    owns both, so the flag and its event land in one retry_on_locked unit and a row cannot
    move with nothing said about it."""
    from . import db
    from .database import ChannelGroupEvent, GROUP_GUIDE_ADDED, GROUP_GUIDE_REMOVED
    db.session.add(ChannelGroupEvent(
        group_id=group.id,
        event_type=GROUP_GUIDE_ADDED if in_guide else GROUP_GUIDE_REMOVED,
        detail=detail,
        extra_data=json.dumps(extra) if extra else None))


def demote_group_from_guide(group, scheduled, detail):
    """Take `group` out of the TV Guide and cancel the scheduled recordings that were
    counting on it, because its last recording-enabled member is going away.

    Mutates and adds events WITHOUT committing, and WITHOUT touching the scheduler -
    same convention as set_participation() above, and for the same reason: the caller
    owns the commit, so the participation write and this demotion stay inside one
    retry_on_locked unit and cannot half-happen. Deregistering the APScheduler jobs is a
    non-idempotent side effect and is the caller's job AFTER that commit has durably
    succeeded (CLAUDE.md Agent Behavior); until then the job firing on an ABORTED row is
    the harmless half of the race, where a deregistered job with a live SCHEDULED row
    would be a recording that silently never runs.

    Returns the recording ids the caller must now deregister."""
    # hidden-recompute-ok: ChannelGroup.in_guide, not a channel's. A group's own guide row
    # is not what protects its members from being hidden - the membership rows are, and
    # those are untouched here.
    group.in_guide = False
    ids = cancel_scheduled_recordings(scheduled, detail)
    log_guide_change(group, False, detail, extra={'cancelled_recording_ids': ids})
    return ids


def report_broken_guide_row(group, detail):
    """The no-human-present half of DESIGN-channel-groups-model.md 15's breach path 3.

    The group STAYS in the guide - quietly pulling a row out overnight is exactly the
    silent behavior 15 refuses, and the user may well want to fix it by re-enabling a
    member rather than by losing the row. What happens instead is that the state gets
    said out loud, three ways: this event on the group's Activity Timeline, an ERROR
    alert, and the non-mutable banner the group page renders while the state is true.

    Adds the event without committing, like every other writer in this module. The alert
    is raised here rather than by the caller because create_alert() opens its own app
    context and commits on its own - it is not part of the caller's unit and must not be
    retried with it."""
    from . import db
    from .alerts import create_alert
    from .database import ChannelGroupEvent, GROUP_GUIDE_BROKEN
    db.session.add(ChannelGroupEvent(
        group_id=group.id, event_type=GROUP_GUIDE_BROKEN, detail=detail))
    create_alert(
        'GROUP_GUIDE_NO_RECORDING_MEMBER',
        f'"{group.name}" is in the TV Guide with nothing to record from',
        body=(f'{detail} The group is still in the TV Guide, so its row and its listings '
              'are unchanged - but a recording started from it now has no member to use. '
              'Turn Recording on for at least one member, or take the group out of the '
              'guide.'),
        source='channel_groups')


def report_orphaned_guide_groups(group_ids, cause='A channel it could record from was removed.'):
    """Say so about any of `group_ids` now sitting in the TV Guide with nothing switched
    on for recording, and commit those reports.

    The sweep half of 15's breach path 3, for the paths that destroy memberships in bulk
    across groups the user is not looking at: the dedup transfer, and the delete of
    channels the provider dropped. Neither can prompt about a group that is not on the
    screen, so neither is allowed to act on one - it reports and leaves the row alone.

    Deliberately silent about a group that is already broken and already said so: the
    check is for a GROUP_GUIDE_BROKEN newer than the group's last event of its own, so a
    repeated bulk delete does not re-alert for a state nobody has fixed yet. Commits its
    own writes, because its callers have already committed theirs and this is a report
    about what happened rather than part of it."""
    from . import db
    from .database import ChannelGroup, ChannelGroupEvent, GROUP_GUIDE_BROKEN
    from .db_utils import retry_on_locked
    ids = [gid for gid in set(group_ids or ()) if gid]
    if not ids:
        return []
    reported = []
    for gid in ids:
        group = db.session.get(ChannelGroup, gid)
        if group is None or not group.in_guide or group.is_system:
            continue
        if any(m.recording_enabled for m in group.memberships):
            continue
        already = (ChannelGroupEvent.query
                   .filter_by(group_id=gid, event_type=GROUP_GUIDE_BROKEN)
                   .order_by(ChannelGroupEvent.timestamp.desc()).first())
        newer = (ChannelGroupEvent.query
                 .filter(ChannelGroupEvent.group_id == gid,
                         ChannelGroupEvent.event_type != GROUP_GUIDE_BROKEN)
                 .order_by(ChannelGroupEvent.timestamp.desc()).first())
        if already is not None and (newer is None or newer.timestamp <= already.timestamp):
            continue
        report_broken_guide_row(group, cause)
        reported.append(gid)

    if reported:
        @retry_on_locked()
        def _commit():
            db.session.commit()
        _commit()
    return reported


def warning_labels():
    """The warning banners a user may hide, by kind, with the label their event log entry
    uses. Keyed on database.GROUP_WARNING_KINDS - a kind outside that tuple is refused in
    the route, for the same reason a participation field is.

    A function rather than a module constant only because this module imports from
    .database lazily throughout, to keep the import graph acyclic."""
    from .database import GROUP_WARNING_EPG, GROUP_WARNING_FORMAT, GROUP_WARNING_OVERRIDE
    return {
        GROUP_WARNING_FORMAT: 'mixed video formats',
        GROUP_WARNING_EPG: 'mismatched EPG data',
        GROUP_WARNING_OVERRIDE: 'no member matching the group format',
    }


def set_warning_muted(group, kind, muted) -> bool:
    """Hide or re-arm one of a group's warning banners, and log the move.

    THE only writer of ChannelGroup.muted_warnings, and reached only from a human action:
    the banners describe a setup the user chose, so only the user decides whether to keep
    being told about it. Nothing derives a mute from measured state - a banner that
    silences itself is one the user can never learn the reason for
    (DESIGN-channel-groups-model.md 16.2).

    Same convention as set_participation() above: mutates and adds the ChannelGroupEvent
    without committing, so the caller owns the commit and the whole read-modify-write
    stays inside one retry_on_locked unit. Returns True when the mute actually moved.

    Hiding a warning changes nothing about what the app DOES - the alerts still fire, the
    events are still written, and a recording made under an override still says so on its
    own detail page. It hides one banner on one page, which is the only thing it should be
    able to do."""
    from . import db
    from .database import ChannelGroupEvent, GROUP_WARNING_MUTED
    labels = warning_labels()
    if kind not in labels:
        raise ValueError(f'Unknown warning kind "{kind}"')
    muted = bool(muted)
    current = group.muted_warning_set()
    if (kind in current) == muted:
        return False
    group.set_muted_warnings(current | {kind} if muted else current - {kind})
    label = labels[kind]
    detail = (f'Warning about {label} {"hidden" if muted else "turned back on"} for this '
              'group by hand')
    db.session.add(ChannelGroupEvent(
        group_id=group.id, event_type=GROUP_WARNING_MUTED, detail=detail,
        extra_data=json.dumps({'kind': kind, 'muted': muted, 'source': 'user'})))
    return True


def guide_scope_channel_ids():
    """Subquery of the channel ids whose listings can reach the TV Guide: every in-guide
    channel, plus every member of an in-guide group.

    **This, not `Channel.in_guide`, is still the answer to "is it in the guide"**, but for
    one reason only: a group member with its own flag off has its listings on screen through
    the group's row. The column itself is now honest - it means "this channel is its own
    guide row" and nothing else, because group membership no longer suppresses anything
    (dev/changelog/751, DESIGN-channel-groups-model.md DECIDED 5). Before that it also meant
    "restore this as a guide row if its group dissolves", and reading it got the question
    wrong in both directions at once: measured on the live database, the flag was true on 6
    channels while 19 actually fed the guide's 8 rows.

    Spelled as a set of channel ids rather than a predicate over joined `Channel` columns, so
    a caller can narrow `EPGEntry.channel_id.in_(...)` directly. An id set is what lets SQLite
    drive an EPG query off ix_epg_entries_channel_stop instead of joining every row to its
    channel and discarding it - measured at 2048ms versus 26ms when the retired guide search
    hit it (dev/changelog/366). Both sides of that join plan identically today, because SQLite
    propagates the join equality either way, but the id-set shape is the half that was load
    bearing and it costs nothing to keep.

    Two things this deliberately does NOT do, both because they need per-member health state
    that is not a SQL predicate: it does not narrow a group to the single member whose
    listings currently fill the row (which member wins is decided per program at render time,
    and `grpdedup` collapses them with the same rule the recorder uses), and it does not drop
    a group whose every member is recording-disabled. Those members are kept for the reason
    `channel_search._group_collapse_losers` documents - excluding them hides the whole
    schedule of a group that has nothing else.
    """
    from . import db
    from .database import Channel
    individual = db.session.query(Channel.id).filter(Channel.in_guide.is_(True))
    return individual.union(_guide_scope_group_members_q()).scalar_subquery()


def _guide_scope_group_members_q():
    """The grouped half of guide scope, as a query rather than a subquery.

    Split out so `guide_scope_channel_ids()` and `guide_scope_group_member_ids()` are one
    definition read two ways instead of two spellings that can disagree - the channel
    search offers the halves as separate filters that must OR back to the whole
    (`channel_search.OTHER_GUIDE_VIA_GROUP`, dev/changelog/791).
    """
    from . import db
    from .database import ChannelGroup, ChannelGroupMember
    return (db.session.query(ChannelGroupMember.channel_id)
            .join(ChannelGroup, ChannelGroupMember.group_id == ChannelGroup.id)
            .filter(ChannelGroup.in_guide.is_(True)))


def guide_scope_group_member_ids():
    """Subquery of the channel ids that reach the guide through a group's row.

    The grouped half of `guide_scope_channel_ids()`, which is the union of this and the
    channels flagged `in_guide`. Deliberately NOT "in scope but holding no row of its own":
    a channel can honestly be both, and subtracting would break the union.
    """
    return _guide_scope_group_members_q().scalar_subquery()


def member_eager_options():
    """Eager-load options for a query that will rank a whole set of groups' members.

    Both helpers below walk every group and read `g.memberships` and each membership's
    `.channel`, and both relationships are lazy - so without these the walk is two queries
    per group and the Groups page's cost scales with how many groups exist
    (tests/test_scaling_pages.py is what caught it). selectinload makes it two constant
    statements however many groups there are."""
    from sqlalchemy.orm import selectinload
    from .database import ChannelGroup, ChannelGroupMember
    return (selectinload(ChannelGroup.memberships).selectinload(ChannelGroupMember.channel),)


# Who a group would record from right now, as one value. `member` is the channel a
# recording started this instant would capture from (None when nothing is eligible);
# `selection` is the FormatSelection behind it, so a caller that has to disclose the
# lock's zero-survivors override already holds it rather than re-filtering to find out.
ServingChoice = collections.namedtuple('ServingChoice', 'member selection')


def serving_member(group, latest_by_channel=None,
                   streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD) -> ServingChoice:
    """The member `group` would record from right now - format lock filters, health score
    ranks (DESIGN-channel-groups-model.md 5, dev/changelog/753).

    The ONE spelling of that two-step for display surfaces. The TV Guide row, the channel
    search page's group row and the scheduling modal's group disclosure
    (dev/changelog/904) all ask it, so none of them can name a member the others would not
    have picked - a row that says one feed while Record starts another is a disagreement
    the user sees directly.

    `app/recorder.py::start_recording` deliberately does NOT call this. It layers
    busy-channel and busy-account exclusions between the filter and the ranking, which no
    display surface has and which can only be resolved against a live recording set.

    `latest_by_channel` is load bearing rather than a tie-break: a member's format is read
    from its latest health check, so omitting the map leaves every format unknown and the
    lock filters nothing. Callers batch it over the whole page - asking per row is the
    per-row-I/O defect class. Must be called inside an app context."""
    members = recording_members(group.memberships) if group is not None else []
    selection = format_eligible_members(group, members, latest_by_channel or {})
    return ServingChoice(
        pick_best_member(selection.members, latest_by_channel,
                         streak_threshold=streak_threshold),
        selection)


def guide_row_targets(streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD,
                      latest_by_channel=None):
    """Ordered guide rows: every in_guide channel, interleaved with every in_guide group
    by their shared guide_sort_order space. Returns
    [('channel', Channel, None) | ('group', ChannelGroup, serving_member)]; a group with
    no recording-enabled member is skipped, because it paints no row.

    A channel that belongs to a group is NOT excluded. It gets a row when its own flag
    says so, alongside its group's row if the group has one - being in a group stopped
    suppressing anything (dev/changelog/751).

    The ONE definition of "what the guide paints, and which member serves each row". The
    guide renders from it and the automatic health check probes it (dev/changelog/752), so
    the two cannot disagree about which feed a row is actually showing - the same reason
    guide_scope_channel_ids() is never re-derived inline.

    `latest_by_channel` is **load bearing, not just a tie-break**, since the format lock
    began filtering who serves (dev/changelog/753): a member's format is read from its
    latest test, so omitting the map makes every format unknown and filters nothing.
    Both callers batch it; a new one that does not would paint a row naming a member the
    recorder would not have picked. Must be called inside an app context."""
    from .database import Channel, ChannelGroup
    entries = [((ch.guide_sort_order or 0, ch.name.lower()), ('channel', ch, None))
               for ch in Channel.query.filter(Channel.in_guide.is_(True)).all()]
    for g in ChannelGroup.query.filter_by(in_guide=True).options(*member_eager_options()).all():
        # The row must name the member a recording would actually start from, or clicking
        # Record on it records a feed the guide never showed (5, dev/changelog/753). The
        # override case still yields a member: the filter hands back the unfiltered list
        # rather than emptying it.
        serving = serving_member(g, latest_by_channel,
                                 streak_threshold=streak_threshold).member
        if serving is None:
            continue
        entries.append(((g.guide_sort_order or 0, g.name.lower()), ('group', g, serving)))
    entries.sort(key=lambda e: e[0])
    return [e[1] for e in entries]


def active_recurring_jobs(include_system=True):
    """The health-check jobs that constitute ongoing monitoring, newest-id last.

    "Active recurring" is `recurring AND status=='SCHEDULED' AND NOT recur_paused`: a
    one-shot job is not ongoing monitoring, and a paused recurring one has no live
    APScheduler trigger behind it. The ONE definition - channel_tester's
    monitored_channel_ids() and groups_with_own_schedule_ids() below both read it, and
    two answers to "is this monitored on a schedule" is a disagreement the user sees.

    Must be called inside an app context."""
    from .database import OnDemandTestJob
    q = OnDemandTestJob.query.filter_by(status='SCHEDULED', recurring=True,
                                        recur_paused=False)
    if not include_system:
        q = q.filter(OnDemandTestJob.is_system.is_(False))
    return q.order_by(OnDemandTestJob.id).all()


def groups_with_own_schedule_ids():
    """Ids of the groups that carry an active recurring health-check schedule of their own.

    The pinned system job is excluded: it IS the fallback, so counting it would make every
    group look scheduled and the fallback would then cover nothing.

    Must be called inside an app context."""
    return {job.group_id for job in active_recurring_jobs(include_system=False)
            if job.group_id is not None}


def _fallback_serving_member(group, streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD,
                             latest_by_channel=None):
    """The one member the automatic check probes on behalf of a group that has no
    schedule of its own, or None when the group has nobody taking part.

    Read through participation_is_recording() rather than recording_enabled directly: a
    group still on health_check_only has nothing recording-enabled by construction
    (DESIGN-channel-groups-model.md 14), so asking for its recording members would hand
    back nothing and the fallback would cover exactly the groups it exists to cover."""
    if participation_is_recording(group):
        candidates = recording_members(group.memberships)
    else:
        tested = test_member_ids(group.memberships)
        candidates = [m.channel for m in group.memberships if m.channel_id in tested]
    return pick_best_member(candidates, latest_by_channel, streak_threshold=streak_threshold)


def system_check_targets(streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD):
    """The ordered channel list the automatic "TV Guide Channels" check probes.

    **One probe per guide row, plus one per scheduleless group** - deliberate, and the
    reason health checks exist at all (DESIGN-channel-groups-model.md 6):

      "I think it should only check the standalone channels + the current active channel
      of a group. It should not check every channel in every group."

    Three sources, deduplicated, guide rows first:

    1. Every standalone in-guide channel.
    2. The serving member of every in-guide group - the member guide_row_targets() says is
       filling that row right now, so the probe measures the feed the user would actually
       record.
    3. The serving member of every other group with no active recurring schedule of its
       own. Without this a user with 40 groups and no schedules gets zero monitoring, which
       fails the founding thesis; bounding it at one member per group is what keeps a sweep
       group - legal at 50,000 members (6 7) - from turning a nightly job into 83 hours.

    What it deliberately does NOT cover: a group's non-serving members. They go stale
    unless that group carries a schedule of its own - a deliberate division of labor
    between the automatic check and a group's own.

    Must be called inside an app context."""
    from .database import ChannelGroup
    from .routes.channel_tests import _latest_tests_by_channel

    groups = (ChannelGroup.query
              .filter(ChannelGroup.is_system.is_(False))
              .options(*member_eager_options())
              .order_by(ChannelGroup.name).all())
    # One batched fetch spanning every candidate member, never a query per group in the
    # loops below. It is also what makes this agree with the guide: rank_members' bitrate
    # tie-break is only applied when it is handed a map, so a check that ranked without
    # one could probe a different member than the guide is painting.
    latest = _latest_tests_by_channel([m.channel_id for g in groups for m in g.memberships])

    channels, seen = [], set()

    def _add(ch):
        if ch is not None and ch.id not in seen:
            seen.add(ch.id)
            channels.append(ch)

    covered_group_ids = set()
    for kind, obj, serving in guide_row_targets(streak_threshold=streak_threshold,
                                                latest_by_channel=latest):
        if kind == 'channel':
            _add(obj)
        else:
            covered_group_ids.add(obj.id)
            _add(serving)

    scheduled_ids = groups_with_own_schedule_ids()
    for g in groups:
        if g.id in covered_group_ids or g.id in scheduled_ids:
            continue
        _add(_fallback_serving_member(g, streak_threshold=streak_threshold,
                                      latest_by_channel=latest))
    return channels


def check_target_channels(group):
    """(ordered channel list, excluded-id set) for a health check over `group` - the ONE
    place that resolves the system group's computed membership
    (DESIGN-groups-unification.md: is_system membership is never stored).

    - is_system ("TV Guide Channels"): system_check_targets() - one probe per guide row
      plus one per scheduleless group, NOT every in-guide channel and never every member
      of every group (dev/changelog/752). Excluded means Channel.test_enabled=False (all
      are listed so excluded ones stay visible/toggleable, mirroring how stored groups
      list theirs).
    - stored groups: memberships in position order; excluded means the membership's
      test_enabled is off, OR the channel-wide Channel.test_enabled is - the latter is an
      off switch and wins (DESIGN-channel-groups-model.md 4.2).

    Must be called inside an app context."""
    if group.is_system:
        channels = system_check_targets()
        return channels, {ch.id for ch in channels if not ch.test_enabled}
    memberships = group.memberships
    tested = test_member_ids(memberships)
    channels = member_channels(memberships)
    return channels, {ch.id for ch in channels
                      if ch.id not in tested or not ch.test_enabled}


def group_channel_ids(group):
    """This group's target channel ids. Stored membership, except for is_system, whose
    membership is computed rather than stored (check_target_channels()). Used to scope
    the missing-channel delete preview/action to one group's own channels."""
    if group.is_system:
        channels, _excluded_ids = check_target_channels(group)
        return [ch.id for ch in channels]
    return [ch.id for ch in member_channels(list(group.memberships))]


def teardown_test_job(job):
    """Release what creating an OnDemandTestJob acquired: its APScheduler entry and its
    ChannelTest rows (lazy='dynamic' means the relationship needs an explicit bulk
    delete, not just deleting the job row). Caller still deletes the job row itself
    (and the group, where that's appropriate) afterward. Shared by the single-job
    delete route and the group-cascade delete - CLAUDE.md's teardown-releases-
    everything rule applies to both call sites identically.

    Returns the screenshot_path values the deleted ChannelTest rows carried. Both
    callers run this from inside their own retry_on_locked() closure, so it must not
    unlink anything itself - the caller unlinks the returned paths with
    app.recorder.delete_files() only after that closure's commit has durably
    succeeded (tests/test_static_invariants.py::RetryOnLockedSideEffectTests)."""
    from .channel_tester import delete_tests_collecting_screenshots
    from .database import ChannelTest
    from .scheduler import cancel_on_demand_job_schedule
    cancel_on_demand_job_schedule(job)
    return delete_tests_collecting_screenshots(ChannelTest.query.filter_by(job_id=job.id))


def check_run_channels(group):
    """The channels a check run over `group` actually tests, in order.

    A member is tested when BOTH switches are on - the membership's test_enabled and the
    channel-wide Channel.test_enabled, which is an off switch and wins
    (DESIGN-channel-groups-model.md 4.2). check_target_channels() already resolves both
    into its excluded-id set. Must be called inside an app context."""
    channels, excluded_ids = check_target_channels(group)
    return [ch for ch in channels if ch.id not in excluded_ids]


def group_manages_format(group) -> bool:
    """Whether `group`'s format lock is allowed to filter who serves and records.

    False for health_check_only (not a recording source, and DESIGN-channel-groups-model.md
    16 gates every format warning on the same condition) and for unmanaged, whose whole
    meaning is that mixed formats are permitted - 16.2's own copy promises the group
    "records from whichever member ranks best, whatever its format". highest_score writes
    no lock, so it never filters by construction rather than by this gate."""
    from .database import GROUP_FORMAT_HEALTH_CHECK_ONLY, GROUP_FORMAT_UNMANAGED
    return group is not None and group.format_strategy not in (
        GROUP_FORMAT_HEALTH_CHECK_ONLY, GROUP_FORMAT_UNMANAGED)


# What the format lock did to a candidate list, as one value so every selection site
# reports the same thing. `members` is what to rank (already the unfiltered list when the
# override applies), `filtered` the channels the lock excluded, `override` True when the
# lock would have left nothing, `reference` the locked key the filter used (None = no
# filtering happened at all).
FormatSelection = collections.namedtuple('FormatSelection',
                                         'members filtered override reference')


def format_eligible_members(group, members, latest_by_channel) -> FormatSelection:
    """Filter `members` (already recording-enabled) down to the group's locked format -
    layer 2 of the three-layer story: **the format lock filters, health score ranks**
    (DESIGN-channel-groups-model.md 5, dev/changelog/753).

    This is a READ. It never writes a participation column: a member off the lock is
    skipped where members are chosen and stays exactly as the user left it, so one that
    starts matching again is eligible again on its own with no re-enable machinery (4.1).

    Three rules, each of which changes the answer:

    - **No lock, or a group that manages no format, filters nothing** - see
      group_manages_format(). The result is `members` unchanged with reference None.
    - **An untested member is never filtered out.** Its format is unknown, not
      proven-different - the same call group_format_outliers() already makes, and the
      opposite call would make a never-tested member permanently unselectable in a locked
      group that has no health check to prove it either way.
    - **Zero survivors is an override, never a skip** (15.2), specified as: "if there are
      no channels available because of filtering and not because the user chose to disable
      all channels, then that should force the recording to happen but be loud about it."
      The caller gets the unfiltered list back with override=True and owes the user the
      three voices 15.2 names - it must never turn this into an empty list and give up.

    Pure/DB-free apart from the constant import; the caller supplies the test map."""
    reference = group.locked_format_key if group is not None else None
    if reference is None or not group_manages_format(group):
        return FormatSelection(list(members), [], False, None)
    keep, dropped = [], []
    for ch in members:
        key = format_key(latest_by_channel.get(ch.id))
        (dropped if (key is not None and key != reference) else keep).append(ch)
    if not keep and members:
        return FormatSelection(list(members), [], True, reference)
    return FormatSelection(keep, dropped, False, reference)


def group_reference_key(group, memberships, latest_by_channel,
                        streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD):
    """The group's effective format reference key.

    - If the group's format is locked (both format_resolution and format_fps set),
      returns that locked (resolution, fps) - a member's provider-side drift can no
      longer move the reference.
    - Otherwise auto-derived: the format_key of the highest-ranked RECORDING-ENABLED
      member (rank_members order - non-streaking preferred, then effective_score,
      id-ascending tie-break) that has a known format. Returns None when no such
      candidate has a known format.

    Candidates are the recording-enabled members and only those, because the reference
    exists to describe what this group would record as. A group with nothing enabled has
    no reference, which is the right answer rather than a gap: it is not a recording
    source, and the format warnings that consume this are gated off for it anyway
    (DESIGN-channel-groups-model.md 16).

    Lock-aware - the one place callers should source "the group format" from. A caller
    that needs the format the data alone points at, with any lock ignored, wants
    derived_reference_key() instead: this function answering both questions is how the
    settings picker came to label "Healthiest member's format" with the group's existing
    lock (dev/changelog/890)."""
    locked = group.locked_format_key if group is not None else None
    if locked is not None:
        return locked
    return derived_reference_key(memberships, latest_by_channel, streak_threshold)


def derived_reference_key(memberships, latest_by_channel,
                          streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD):
    """The format the group's own data points at, with any lock ignored: the format_key
    of the highest-ranked RECORDING-ENABLED member (rank_members order) that has a known
    format, or None when no such candidate has one.

    This is what the `highest_score` strategy actually follows - it pins nothing and lets
    whichever member is healthiest serve the group - so it is what a surface offering that
    strategy must name. group_reference_key() returns the LOCK when one is set, which is
    correct for its own callers and wrong for this question: on a group locked to
    3840x2160 @ 50 the picker offered "Healthiest member's format - 3840x2160 @ 50" while
    its healthiest member measured 1920x1080 @ 50, and no member of the named format
    scored above 92 (dev/changelog/890). One value cannot answer both questions - CLAUDE.md
    'one flag, one meaning'."""
    for ch in rank_members(recording_members(memberships), latest_by_channel,
                           streak_threshold=streak_threshold):
        key = format_key(latest_by_channel.get(ch.id))
        if key is not None:
            return key
    return None


def group_format_outliers(members, latest_by_channel, reference_key=_UNSET,
                          streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD):
    """Which already-committed members clash with the group's established format.

    `reference_key`:
      - `_UNSET` (default): derive the reference the legacy way - the known format of
        the best-ranked member (rank_members order) among all members. Kept for callers
        that predate the lock/disable model.
      - an explicit key (or None): use it directly (None ⇒ no reference ⇒ no outliers),
        so lock-aware callers pass `group_reference_key(...)`.

    Any *tested* member whose format differs from the reference is an outlier - untested
    members are never outliers (unknown, not proven-different). Returns
    (reference_key, [outlier_channel, ...]); reference_key is None (list empty) when
    there is no reference.

    Drives the group-detail mismatch banner and the post-health-test re-check -
    same definition in both places."""
    if reference_key is _UNSET:
        ref = None
        for ch in rank_members(members, latest_by_channel, streak_threshold=streak_threshold):
            key = format_key(latest_by_channel.get(ch.id))
            if key is not None:
                ref = key
                break
    else:
        ref = reference_key
    if ref is None:
        return None, []
    outliers = [ch for ch in members
                if (k := format_key(latest_by_channel.get(ch.id))) is not None and k != ref]
    return ref, outliers


def plan_reconcile(group, memberships, latest_by_channel,
                   streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD):
    """Detect which members' formats disagree with the group's reference, without
    touching the DB. Unit-testable with supplied membership rows + test map.

    **Detection only - nothing acts on this by changing a member.** The engine used to
    auto-disable outliers and auto-re-enable them on recovery; that write half is gone
    (DESIGN-channel-groups-model.md 4.1). A participation checkbox is written by a human
    and by nothing else, and the format lock instead filters members where they are
    chosen, so a member that starts matching again is eligible again immediately with no
    re-enable machinery. What survives is this diff, which _log_and_alert_reconcile()
    turns into the CHANNEL_GROUP_FORMAT_* event log and its alert - the surfaces that make
    a mismatch visible.
    """
    members = member_channels(memberships)
    reference = group_reference_key(group, memberships, latest_by_channel, streak_threshold)
    locked = (group.locked_format_key is not None) if group is not None else False
    _, outlier_channels = group_format_outliers(members, latest_by_channel,
                                                reference_key=reference,
                                                streak_threshold=streak_threshold)
    outlier_ids = {ch.id for ch in outlier_channels}

    # Would the lock leave anything for selection to choose from? False is the loud "no
    # eligible member matches the group format" state - a locked format no live member
    # conforms to. It never suppresses a recording; zero eligible members at record time
    # is an override, not a skip (15.2), and this flag is what fires the first of that
    # section's three voices.
    #
    # Asked through format_eligible_members() rather than re-derived here, because
    # selection asks the same question at record start and two answers to it is a
    # disagreement the user sees: an untested member survives the filter (unknown, not
    # proven-different), so a group holding one is NOT in the zero-eligible state.
    selection = format_eligible_members(group, recording_members(memberships),
                                        latest_by_channel)
    eligible_member_matches_reference = bool(selection.members) and not selection.override

    return {
        'reference': reference,
        'locked': locked,
        'outliers': sorted(outlier_ids),
        'eligible_member_matches_reference': eligible_member_matches_reference,
        # True only for 15.2's case: members ARE enabled for recording and the lock
        # excluded every one of them. Distinct from "the user disabled them all", which
        # is a choice rather than a state to alert about, and which the flag above cannot
        # tell apart on its own.
        'format_override': selection.override,
    }


def reconcile_group(group, memberships, latest_by_channel,
                    streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD):
    """The group's format reconciliation diff. Detection only - see plan_reconcile.

    Kept as a named entry point because callers pair it with _log_and_alert_reconcile();
    it writes nothing itself."""
    return plan_reconcile(group, memberships, latest_by_channel, streak_threshold)


def _dismiss_format_mismatch_alerts(group, channel_ids=None):
    """Dismiss this group's open GROUP_FORMAT_MISMATCH alerts - the ones for
    `channel_ids`, or every one the group holds when that is None.

    The source key is `group:<gid>:ch:<cid>`, so the whole-group case matches on that
    prefix rather than on current membership: an alert raised for a member that has since
    been removed is exactly the one nothing else would ever clear. The `%` cannot
    over-match a longer group id - `group:2:ch:%` requires the literal `group:2:ch:`
    prefix, which `group:20:ch:5` does not have."""
    from . import db
    from .database import Alert
    from .db_utils import retry_on_locked
    from datetime import datetime

    @retry_on_locked()
    def _dismiss():
        q = Alert.query.filter(Alert.alert_type == 'GROUP_FORMAT_MISMATCH',
                               Alert.dismissed_at.is_(None))
        if channel_ids is None:
            q = q.filter(Alert.source.like(f'group:{group.id}:ch:%'))
        else:
            q = q.filter(Alert.source.in_(
                [f'group:{group.id}:ch:{cid}' for cid in channel_ids]))
        now = datetime.utcnow()
        for a in q.all():
            a.dismissed_at = now
        db.session.commit()
    _dismiss()


def _log_and_alert_reconcile(group, members, latest_by_channel, diff):
    """Part E: turn a reconcile diff into a group Activity-Timeline log entry AND a
    system notification, in both directions and idempotently.

    The durable state is the CHANNEL_GROUP_FORMAT_* **ChannelGroupEvent** log for THIS
    group: a member is *newly* mismatched when it's currently a format outlier but the
    last format event this group logged for it was not a mismatch, and *newly* resolved
    when it's no longer an outlier but the last one was a mismatch. That state-transition
    test is what fires log+alert, and it naturally covers the reference itself shifting (a
    member that was conforming becomes an outlier, or vice-versa, when the best member's
    format changes or the lock moves).

    **The state is per-membership, so it cannot live on ChannelEvent** - that table is
    keyed on channel_id with no group_id, and a channel here belongs to several groups. A
    member that is an outlier in one group and conforming in another shared one state
    slot, so the two groups took turns overwriting it: every reconcile pass logged
    MISMATCH from one and RESOLVED from the other, ~110ms apart, forever, and the alert
    dismissal below never matched because its source is correctly group-scoped while the
    state driving it was not (dev/changelog/789).

    Gated on group_manages_format(): a group that is not a recording source has no format
    to be wrong about, which is DESIGN-channel-groups-model.md 16's condition for every
    other format warning. Its open mismatch alerts are dismissed on the way out, so a
    group switched to health_check_only clears rather than stranding them.

    Best-effort: never raises into the caller (a health-test run, the startup sweep) - a
    logging failure must not break reconciliation."""
    from . import db
    from .database import (Channel, ChannelGroupEvent,
                           CHANNEL_GROUP_FORMAT_MISMATCH, CHANNEL_GROUP_FORMAT_RESOLVED)
    from .db_utils import retry_on_locked
    import logging
    log = logging.getLogger(__name__)

    try:
        if group is None:
            return
        if not group_manages_format(group):
            _dismiss_format_mismatch_alerts(group)
            return

        reference = diff.get('reference')
        outlier_ids = set(diff.get('outliers') or [])
        member_ids = [ch.id for ch in members]
        if not member_ids:
            return

        # Last format-event state per member *of this group* (one query): 'mismatch' if
        # the newest CHANNEL_GROUP_FORMAT_* event is a MISMATCH, else 'resolved'/absent.
        # id breaks a timestamp tie - a pass writes its rows within one microsecond-
        # resolution instant, and an arbitrary winner there is an arbitrary state.
        last_state = {}
        for e in (ChannelGroupEvent.query
                  .filter(ChannelGroupEvent.group_id == group.id)
                  .filter(ChannelGroupEvent.channel_id.in_(member_ids))
                  .filter(ChannelGroupEvent.event_type.in_(
                      [CHANNEL_GROUP_FORMAT_MISMATCH, CHANNEL_GROUP_FORMAT_RESOLVED]))
                  .order_by(ChannelGroupEvent.timestamp.asc(),
                            ChannelGroupEvent.id.asc()).all()):
            last_state[e.channel_id] = (
                'mismatch' if e.event_type == CHANNEL_GROUP_FORMAT_MISMATCH else 'resolved')

        by_id = {ch.id: ch for ch in members}
        newly_mismatched, newly_resolved = [], []
        for cid in member_ids:
            is_outlier = cid in outlier_ids
            prev = last_state.get(cid)
            if is_outlier and prev != 'mismatch':
                newly_mismatched.append(cid)
            elif not is_outlier and prev == 'mismatch':
                newly_resolved.append(cid)
        if not newly_mismatched and not newly_resolved:
            return

        ref_label = format_label(reference)
        lock_note = ' (locked)' if diff.get('locked') else ''

        # Write the ChannelGroupEvents in one retry_on_locked closure (re-fetch inside).
        group_id = group.id
        @retry_on_locked()
        def _write_events():
            for cid in newly_mismatched:
                ch = db.session.get(Channel, cid)
                if ch is None:
                    continue
                fmt = format_label(format_key(latest_by_channel.get(cid)))
                db.session.add(ChannelGroupEvent(
                    group_id=group_id, channel_id=cid,
                    event_type=CHANNEL_GROUP_FORMAT_MISMATCH,
                    detail=f'{fmt} differs from group format {ref_label}{lock_note}'))
            for cid in newly_resolved:
                ch = db.session.get(Channel, cid)
                if ch is None:
                    continue
                fmt = format_label(format_key(latest_by_channel.get(cid)))
                db.session.add(ChannelGroupEvent(
                    group_id=group_id, channel_id=cid,
                    event_type=CHANNEL_GROUP_FORMAT_RESOLVED,
                    detail=f'{fmt} now matches group format {ref_label}{lock_note}'))
            db.session.commit()
        _write_events()

        # Auto-dismiss the standing mismatch alert for each newly-resolved member.
        if newly_resolved:
            _dismiss_format_mismatch_alerts(group, newly_resolved)

        # Raise a WARN alert for each newly-mismatched member (create_alert manages its
        # own app context + retry). Source keyed per group+channel so the resolve above
        # can find and dismiss it.
        if newly_mismatched:
            from .alerts import create_alert
            for cid in newly_mismatched:
                ch = by_id.get(cid)
                if ch is None:
                    continue
                fmt = format_label(format_key(latest_by_channel.get(cid)))
                create_alert(
                    'GROUP_FORMAT_MISMATCH',
                    f'Format mismatch in group "{group.name}"',
                    body=(f'"{ch.name}" reports {fmt}, which differs from the group\'s '
                          f'format {ref_label}{lock_note}. It stays in the group and stays '
                          f'switched on, and is skipped whenever a member is chosen - so '
                          f'nothing records from it until it matches again.'),
                    source=f'group:{group.id}:ch:{cid}')
    except Exception:
        log.exception('group format log/alert failed for group %s',
                      getattr(group, 'id', '?'))


def _alert_no_eligible_member(group, diff):
    """The first of the three voices DESIGN-channel-groups-model.md 15.2 asks for: an
    alert the moment a group's format lock leaves no eligible member. The other two fire
    at record time (app/recorder.py).

    Fires once and clears itself, using the open alert as the durable state - the same
    idempotence _log_and_alert_reconcile() gets from its event log, without a second
    bookkeeping column. It says nothing when the user has simply disabled every member:
    that is a choice, and 15.2 is explicitly about the other case.

    Best-effort - never raises into a health test run or the startup sweep."""
    import logging
    log = logging.getLogger(__name__)
    try:
        from .alerts import create_alert, dismiss_open_alerts, has_open_alert
        source = f'group:{group.id}:no-eligible'
        if not diff.get('format_override'):
            if has_open_alert('GROUP_NO_ELIGIBLE_MEMBER', source):
                dismiss_open_alerts('GROUP_NO_ELIGIBLE_MEMBER', source)
            return
        if has_open_alert('GROUP_NO_ELIGIBLE_MEMBER', source):
            return
        ref_label = format_label(diff.get('reference'))
        create_alert(
            'GROUP_NO_ELIGIBLE_MEMBER',
            f'No eligible member in group "{group.name}"',
            body=(f'Every member enabled for recording differs from the group format '
                  f'{ref_label}. A recording will still run - it will use the '
                  f'best-ranked member whatever its format, and will say so on the '
                  f'recording. Change the format strategy, or check the members whose '
                  f'formats drifted.'),
            source=source)
    except Exception:
        log.exception('no-eligible-member alert failed for group %s',
                      getattr(group, 'id', '?'))


def apply_lock_and_log(group, strategy, entry, non_matching, removed_count):
    """Move `group`'s format lock to the winning bucket of `entry` and record it as a
    GROUP_FORMAT_STRATEGY_APPLIED ChannelGroupEvent (DESIGN-channel-groups-model.md 4.5):
    the old format, the new one, the strategy that chose it and the numbers behind it.

    The explanation is `_format_strategy_entry()`'s own `rationale` - the same sentence
    the picker showed the user before they applied it, never a second wording of the same
    decision. channel_id stays NULL: the lock is a fact about the whole group.

    Adds the event and mutates the group; **the caller commits**, so the lock and its
    explanation land in one transaction and neither can exist without the other. An apply
    that lands on the format already locked is still logged - a user pressed a button and
    it may have removed members, and a press that leaves no trace is exactly what
    principle 1 refuses. The standing strategy is the opposite case and checks for itself
    that the lock actually moved before calling this, because a nightly re-evaluation that
    reaffirms the same format forever is noise, not disclosure (dev/changelog/753)."""
    from . import db
    from .database import ChannelGroupEvent, GROUP_FORMAT_STRATEGY_APPLIED
    old_key = group.locked_format_key
    resolution, fps = entry['resolution'], entry['fps']
    group.set_locked_format(resolution, fps)
    new_key = group.locked_format_key
    label = FORMAT_STRATEGY_LABELS.get(strategy, strategy)
    if old_key == new_key:
        moved = f'Format lock reaffirmed at {format_label(new_key)}'
    elif old_key:
        moved = f'Format lock moved from {format_label(old_key)} to {format_label(new_key)}'
    else:
        moved = f'Format locked to {format_label(new_key)}'
    removed_txt = ''
    if removed_count:
        removed_txt = (f", {removed_count} non-matching member"
                       f"{'s' if removed_count != 1 else ''} removed")
    db.session.add(ChannelGroupEvent(
        group_id=group.id, channel_id=None,
        event_type=GROUP_FORMAT_STRATEGY_APPLIED,
        detail=f'{moved} by the {label} strategy - {entry["rationale"]}{removed_txt}',
        extra_data=json.dumps({
            'strategy': strategy,
            'from': list(old_key) if old_key else None,
            'to': list(new_key) if new_key else None,
            'matched': entry['count'],
            'non_matching': non_matching,
            'removed': removed_count,
        })))


def strategy_lock_plan(group, members, latest_by_channel, rank_ids=None) -> dict:
    """What `group`'s standing format strategy would do to its lock right now - layer 1
    of the three-layer story (DESIGN-channel-groups-model.md 4.4, 5).

    Returns {'strategy', 'manages_lock', 'entry'}:
      - `manages_lock` False for the four values that never write a lock
        (health_check_only, highest_score, manual, unmanaged), and `entry` is then None.
        Those are not special cases in the engine - only the four bucket-ranking
        strategies live in FORMAT_STRATEGIES, and the rest have always sat outside it.
      - otherwise `entry` is _format_strategy_entry()'s shape, whose 'key' is None when
        no bucket won. That is the case that matters right after a database wipe, when no
        group has any test history: it carries the plain-English rationale rather than
        leaving the caller with a silent nothing.

    `rank_ids` narrows which members the ranking is decided over - see lock_ranking_ids();
    every caller that is describing a real group's lock passes it, so the answer here and
    the answer apply_format_strategy() writes cannot differ.

    Pure/DB-free - the caller supplies the test map, as plan_format_selection() does."""
    strategy = group.format_strategy
    if strategy not in FORMAT_STRATEGIES:
        return {'strategy': strategy, 'manages_lock': False, 'entry': None}
    buckets = format_buckets(members, latest_by_channel, rank_ids=rank_ids)
    return {'strategy': strategy, 'manages_lock': True,
            'entry': _format_strategy_entry(strategy, buckets)}


def apply_format_strategy(group, streak_threshold=None):
    """Re-evaluate `group`'s standing format strategy and move its format lock to match -
    the standing half of DECIDED 9, which is what makes "today's recording might be 720p @
    30fps, and tomorrow's might be 1080p @ 60fps, not because I changed anything" true
    (dev/changelog/753).

    Called after a health check run over the group's members and when the user changes the
    setting. Returns the strategy_lock_plan() dict with an added 'moved' bool, or None for
    a missing/system group.

    **Silent when nothing moved.** An event is written only when the lock actually
    changes, or on the transition INTO the no-winner state - a nightly re-evaluation that
    logged its own agreement with yesterday would bury the times it disagreed.

    The lock and its explanation are written in one retry_on_locked closure, so neither
    can exist without the other, and the reconcile pass runs afterward so a moved lock's
    new mismatches are logged and alerted the same way a hand-set one's are."""
    if group is None or group.is_system:
        return None
    from . import db
    from .database import (ChannelGroup, ChannelGroupEvent, GROUP_FORMAT_MANUAL,
                           GROUP_FORMAT_STRATEGY_APPLIED, GROUP_FORMAT_STRATEGY_BLOCKED)
    from .db_utils import retry_on_locked
    from .routes.channel_tests import _latest_tests_by_channel

    group_id = group.id
    memberships = list(group.memberships)
    members = member_channels(memberships)
    latest_by_channel = _latest_tests_by_channel([m.channel_id for m in memberships])
    plan = strategy_lock_plan(group, members, latest_by_channel,
                              rank_ids=lock_ranking_ids(memberships))
    plan['moved'] = False
    if not plan['manages_lock']:
        # A pin found under a strategy that owns none is cleared here, which is the
        # recovery half of 16.2's rule (dev/changelog/762). The write paths refuse to
        # create the state now, but a group already carrying one would otherwise keep it
        # forever: nothing else re-evaluates a lock for a strategy that does not manage
        # one, so under highest_score a stale pin went on filtering members while the
        # settings card named a rule that follows the data. `manual` is the exception the
        # rule is built around - its pin is the user's and is never touched.
        if (plan['strategy'] != GROUP_FORMAT_MANUAL
                and group.locked_format_key is not None):
            stale = group.locked_format_key
            label = FORMAT_STRATEGY_LABELS.get(plan['strategy'], plan['strategy'])

            @retry_on_locked()
            def _clear_disowned_lock():
                g = db.session.get(ChannelGroup, group_id)
                if g is None:
                    return None
                g.set_locked_format(None, None)
                db.session.add(ChannelGroupEvent(
                    group_id=group_id, channel_id=None,
                    event_type=GROUP_FORMAT_STRATEGY_APPLIED,
                    detail=(f'Format lock cleared from {format_label(stale)} - the {label} '
                            f'strategy does not pin a format, so the group had been '
                            f'filtering members on one nothing had chosen'),
                    extra_data=json.dumps({'strategy': plan['strategy'],
                                           'from': list(stale), 'to': None,
                                           'cleared_as_disowned': True})))
                db.session.commit()
                return g
            cleared_group = _clear_disowned_lock()
            if cleared_group is not None:
                plan['moved'] = True
                evaluate_and_reconcile_group(cleared_group, streak_threshold)
        return plan

    entry = plan['entry']
    if entry['key'] is None:
        # No bucket won. Logged once, on the way into the state - the last strategy event
        # is the durable record of which state we were already in, the same
        # state-transition test _log_and_alert_reconcile() uses rather than re-firing
        # every night.
        last = (ChannelGroupEvent.query
                .filter(ChannelGroupEvent.group_id == group_id)
                .filter(ChannelGroupEvent.event_type.in_(
                    [GROUP_FORMAT_STRATEGY_APPLIED, GROUP_FORMAT_STRATEGY_BLOCKED]))
                .order_by(ChannelGroupEvent.timestamp.desc(),
                          ChannelGroupEvent.id.desc()).first())
        if last is not None and last.event_type == GROUP_FORMAT_STRATEGY_BLOCKED:
            return plan
        label = FORMAT_STRATEGY_LABELS.get(plan['strategy'], plan['strategy'])

        @retry_on_locked()
        def _log_blocked():
            db.session.add(ChannelGroupEvent(
                group_id=group_id, channel_id=None,
                event_type=GROUP_FORMAT_STRATEGY_BLOCKED,
                detail=f'The {label} strategy left the format lock unchanged - '
                       f'{entry["rationale"]}',
                extra_data=json.dumps({'strategy': plan['strategy']})))
            db.session.commit()
        _log_blocked()
        return plan

    if group.locked_format_key == (entry['resolution'], int(entry['fps'])):
        return plan

    @retry_on_locked()
    def _move_lock():
        g = db.session.get(ChannelGroup, group_id)
        if g is None:
            return None
        apply_lock_and_log(g, plan['strategy'], entry, 'keep', 0)
        db.session.commit()
        return g
    moved_group = _move_lock()
    if moved_group is None:
        return plan
    plan['moved'] = True

    evaluate_and_reconcile_group(moved_group, streak_threshold)
    return plan


def evaluate_and_reconcile_group(group, streak_threshold=None):
    """The single entry point for format mismatch detection plus its log+notify layer.

    Every trigger - create_group / add_members, a grouped channel's health test, the
    startup sweep, and the format-lock saves - calls this. It loads the group's
    memberships + their latest tests, runs reconcile_group (detection only - it writes
    nothing), then hangs ChannelGroupEvent log entries + GROUP_FORMAT_MISMATCH alerts off
    the diff in both directions, and returns the structured diff. Returns None for a
    missing group.

    `streak_threshold`: pass the live `channel_testing.failing_streak_threshold`
    explicitly when calling this in a loop over multiple groups (e.g. sweep_all_groups)
    to avoid a load_config() per group; every single-group caller (routes) can leave
    this None and it resolves the config value itself - one call, not a per-row one."""
    if group is None:
        return None
    if streak_threshold is None:
        from .config import load_config
        streak_threshold = load_config().get('channel_testing', {}).get(
            'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
    from .routes.channel_tests import _latest_tests_by_channel
    memberships = list(group.memberships)
    latest_by_channel = _latest_tests_by_channel([m.channel_id for m in memberships])
    diff = reconcile_group(group, memberships, latest_by_channel, streak_threshold)
    _log_and_alert_reconcile(group, member_channels(memberships), latest_by_channel, diff)
    _alert_no_eligible_member(group, diff)
    return diff


def sweep_all_groups(app):
    """Re-evaluate every group's format state once, at startup, so a mismatch that
    developed while the app was down is logged and alerted rather than waiting for the
    group's next trigger. Best-effort: a failure on one group must not abort startup or
    block the others."""
    import logging
    log = logging.getLogger(__name__)
    with app.app_context():
        from .config import load_config
        from .database import ChannelGroup
        # Hoisted once for the whole sweep (CLAUDE.md no-hidden-I/O-in-per-row-loops) -
        # never call load_config() per group below.
        streak_threshold = load_config().get('channel_testing', {}).get(
            'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
        for group in ChannelGroup.query.all():
            try:
                evaluate_and_reconcile_group(group, streak_threshold)
            except Exception:
                log.exception('group format sweep failed for group %s',
                              getattr(group, 'id', '?'))


def suggest_candidates(seed_channel, all_channels, latest_by_channel, reference_key=_UNSET):
    """The suggest-duplicates helper: channels likely to be the same logical
    channel as seed_channel, ranked strongest-match first.

    Match signals (common name + EPG ID match; dev/changelog/157):
      - epg_id: same non-empty epg_channel_id - strong
      - name:   same normalized name - weaker

    Each match is also classified by video-format confidence relative to a reference
    format (using the shared format_key over `latest_by_channel`, a channel_id -> latest
    ChannelTest map that omits untested channels):
      - 'confirmed'  : the candidate is tested and its resolution+FPS match the reference -
                       the recommended picks, usable by the group without any further move.
      - 'unverified' : the reference OR the candidate has no known format, so it can't be
                       confirmed either way - still suggested, but with a caution.
      - 'different'  : the candidate is tested and its format differs from the reference.
                       Suggested with a warning, never withheld: adding it is allowed and
                       the format lock skips it where members are chosen
                       (dev/changelog/762).

    `reference_key` selects what the candidate format is compared against:
      - `_UNSET` (default): the seed channel's own format (legacy single-seed behavior;
        keeps the Browse-tab single-channel caller working).
      - an explicit key (or None): compare against it directly, so group callers can pass
        the group's lock-aware reference (group_reference_key). None ⇒ nothing to compare
        against ⇒ every candidate is 'unverified'.

    Returns [(channel, reason, format_status)] with reason in
    ('epg_id+name', 'epg_id', 'name'). Pure/DB-free - the caller supplies the test map.
    Grouped channels are NOT excluded - a channel may belong to any number of groups
    (the route filters out the seed group's own members and surfaces current
    memberships so the UI can show "in N groups")."""
    seed_epg = (seed_channel.epg_channel_id or '').strip()
    seed_name = normalize_channel_name(seed_channel.name)
    ref_fmt = (format_key(latest_by_channel.get(seed_channel.id))
               if reference_key is _UNSET else reference_key)

    matches = []
    for ch in all_channels:
        if ch.id == seed_channel.id:
            continue
        epg_match = bool(seed_epg) and (ch.epg_channel_id or '').strip() == seed_epg
        name_match = bool(seed_name) and normalize_channel_name(ch.name) == seed_name
        if epg_match and name_match:
            reason = 'epg_id+name'
        elif epg_match:
            reason = 'epg_id'
        elif name_match:
            reason = 'name'
        else:
            continue

        cand_fmt = format_key(latest_by_channel.get(ch.id))
        if ref_fmt is None or cand_fmt is None:
            status = 'unverified'
        elif cand_fmt == ref_fmt:
            status = 'confirmed'
        else:
            status = 'different'
        matches.append((ch, reason, status))

    matches.sort(key=lambda m: (FORMAT_STATUS_STRENGTH[m[2]], MATCH_REASON_STRENGTH[m[1]],
                                -effective_score(m[0]), m[0].id))
    return matches
