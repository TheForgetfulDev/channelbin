"""Moving a single-channel recording to another channel with the same program when its
account has no connection free (dev/changelog/1611).

Two places move one: an auto-record rule or a league page's Record when it schedules
(`auto_rules.seat_choice()`), and record start, when the account turns out to be full or
blocked (`recorder.start_recording()`). Both ask this module whether the move is allowed and
in what order to try the channels, so the two cannot disagree.

Whether a move is allowed is a three-level setting, the narrowest answer winning: the
recording's own `Recording.move_when_account_full`, its profile's
`RecordingProfile.move_when_account_full`, then `recording.move_when_account_full` in
config.yaml (on by default). NULL on either column means "use the next level up". A group
recording is not governed by it at start: the group already chooses among the channels its owner
put in it. A rule's schedule-time move is, whatever its first choice is.

The order is the health tiers below, then health score. A channel nobody has tested is tried
after one whose latest check passed and before one whose latest check failed: a recording on
an unknown feed beats no recording at all. A channel on a failing streak is never taken.
"""
import logging
import re

from . import db
from .database import TEST_STATUS_CANCELLED, TEST_STATUS_COMPLETED

log = logging.getLogger(__name__)

LEVEL_RECORDING = 'recording'
LEVEL_PROFILE = 'profile'
LEVEL_GLOBAL = 'global'

#: The health tiers a move tries in order. Not a ranking of their own: health score ranks
#: inside each one.
TIER_PASSED = 0
TIER_UNTESTED = 1
TIER_FAILED = 2

TIER_WORDS = {
    TIER_PASSED: 'its latest health check passed',
    TIER_UNTESTED: 'it has not been health checked',
    TIER_FAILED: 'its latest health check failed, and no channel with a better one was free',
}


def globally_enabled(cfg: dict) -> bool:
    return bool(cfg.get('recording', {}).get('move_when_account_full', True))


def move_source(cfg: dict, profile=None, recording=None) -> tuple:
    """(level, allowed): which setting decides whether this recording may move, and its
    answer. Tri-state columns are tested with `is not None` - False is an answer."""
    if recording is not None and recording.move_when_account_full is not None:
        return LEVEL_RECORDING, bool(recording.move_when_account_full)
    if profile is not None and profile.move_when_account_full is not None:
        return LEVEL_PROFILE, bool(profile.move_when_account_full)
    return LEVEL_GLOBAL, globally_enabled(cfg)


def move_allowed(cfg: dict, profile=None, recording=None) -> bool:
    return move_source(cfg, profile, recording)[1]


LEVEL_WORDS = {LEVEL_RECORDING: 'set on this recording', LEVEL_PROFILE: 'from its profile',
               LEVEL_GLOBAL: 'from the global setting'}

#: Why a waiting start did not move, said once in its deferral event.
NOTE_NO_LISTING = ('It cannot move to another channel: it has no guide listing to find the same '
                   'program by.')
NOTE_NONE_FREE = ('No other channel showing this program is on an account with a connection '
                  'free, so it waits here.')


def note_turned_off(level: str) -> str:
    return f'Moving to another channel when the account is full is off ({LEVEL_WORDS[level]}).'


def has_program(rec) -> bool:
    """Whether a recording carries the program snapshot a move matches other listings by."""
    return bool(rec.program_title) and rec.program_start_time is not None


def describe(level: str, allowed: bool, program: bool = True) -> str:
    """The setting in words, for the recording page. `program` is has_program()."""
    if allowed and not program:
        return ('Waits for a connection on this channel (it has no guide listing to find the '
                'same program on another channel by)')
    what = ('Moves to another channel with the same program' if allowed
            else 'Waits for a connection on this channel')
    return f'{what} ({LEVEL_WORDS[level]})'


def parse_choice(raw):
    """A form's tri-state value: '' = None (use the next level up), '1' = True, '0' = False.
    Raises ValueError for anything else."""
    raw = (raw or '').strip()
    if raw == '':
        return None
    if raw in ('1', '0'):
        return raw == '1'
    raise ValueError('The "if the account is full" choice must be blank, 1 or 0.')


def health_tier(latest_test) -> int:
    """`latest_test` is the channel's latest ChannelTest (routes.channel_tests.
    _latest_tests_by_channel) or None. A canceled test, and one still running (written FAILED
    until it ends, with no test_ended_at), say nothing about the feed yet, so they read as
    untested."""
    if (latest_test is None or latest_test.status == TEST_STATUS_CANCELLED
            or latest_test.test_ended_at is None):
        return TIER_UNTESTED
    if latest_test.status == TEST_STATUS_COMPLETED:
        return TIER_PASSED
    return TIER_FAILED


def health_tiers(channel_ids) -> dict:
    """{channel_id: tier} in one query."""
    from .routes.channel_tests import _latest_tests_by_channel
    ids = [c for c in channel_ids if c is not None]
    latest = _latest_tests_by_channel(ids)
    return {c: health_tier(latest.get(c)) for c in ids}


def _same_episode(a: str, b: str) -> bool:
    # One provider leaving the episode out is not a different program.
    return not a or not b or a == b


_WORD = re.compile(r'\w+', re.UNICODE)


def same_program_channel_ids(rec) -> set:
    """The channels, other than its own, whose guide lists this recording's program: the same
    title and episode (program_titles.title_key) starting at the same moment. Empty for a
    recording with no program snapshot. One query, narrowed in SQL by the title's longest
    word (start_time alone matches ~18,500 rows at 8 pm on a prod-sized guide)."""
    from .database import EPGEntry
    from .program_titles import strip_marks, title_key
    if not has_program(rec):
        return set()
    tkey = title_key(rec.program_title)
    episode = title_key(rec.program_sub_title)
    q = db.session.query(EPGEntry.channel_id, EPGEntry.title, EPGEntry.sub_title).filter(
        EPGEntry.start_time == rec.program_start_time)
    words = _WORD.findall(strip_marks(rec.program_title))
    if words:
        longest = max(words, key=len)
        escaped = longest.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        q = q.filter(EPGEntry.title.ilike(f'%{escaped}%', escape='\\'))
    return {cid for cid, title, sub_title in q.all()
            if cid != rec.channel_id and title_key(title) == tkey
            and _same_episode(episode, title_key(sub_title))}


def start_candidates(rec, full_account_id: int, cfg: dict) -> list:
    """[(Channel, tier)] a SCHEDULED single-channel recording whose account `full_account_id`
    has no connection right now may move to at start, best first. Every one is on an account
    with a recording seat free now, shares no login with the full account, carries no other
    live recording, is not hidden and is not on a failing streak. Advisory, like every seat
    read outside connection_limits' lock: the caller still has to take the seat."""
    from . import connection_limits as connlim
    from .account_links import login_siblings
    from .channel_groups import DEFAULT_FAILING_STREAK_THRESHOLD, effective_score, is_streaking
    from .database import Channel, Recording, REC_STATUS_IN_PROGRESS
    ids = same_program_channel_ids(rec)
    if not ids:
        return []
    near = {full_account_id} | set(login_siblings([full_account_id]).get(full_account_id, {}))
    channels = [ch for ch in Channel.query.filter(Channel.id.in_(ids)).all()
                if ch.account_id not in near and not ch.hidden]
    streak = cfg.get('channel_testing', {}).get('failing_streak_threshold',
                                                DEFAULT_FAILING_STREAK_THRESHOLD)
    channels = [ch for ch in channels if not is_streaking(ch, streak)]
    if not channels:
        return []
    live = {cid for (cid,) in db.session.query(Recording.channel_id).filter(
        Recording.status == REC_STATUS_IN_PROGRESS, Recording.id != rec.id,
        Recording.channel_id.in_([ch.id for ch in channels]))}
    channels = [ch for ch in channels if ch.id not in live]
    full = connlim.accounts_without_free_recording_slot({ch.account_id for ch in channels})
    channels = [ch for ch in channels if ch.account_id not in full]
    tiers = health_tiers([ch.id for ch in channels])
    channels.sort(key=lambda ch: (tiers[ch.id], -effective_score(ch), ch.id))
    return [(ch, tiers[ch.id]) for ch in channels]
