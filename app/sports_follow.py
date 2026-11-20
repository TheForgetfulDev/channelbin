"""Automated recordings of a game follow the time the game goes by (dev/docs/DESIGN-sports-data.md
§16.1, dev/changelog/1631).

A recording an auto-record rule made, or one a league page's Record made, is kept at the window the
league's answer gives the listing it was made from (app/sports_times.py): moved to TheSportsDB's time,
widened to cover both, or put back to the listing's when the two agree again. Under Cover both with the
two starts too far apart for one recording, a second recording is kept at TheSportsDB's time, in case it
is the right one, and removed when it is no longer needed.

What is never moved:
- a recording made from the TV Guide or by hand, even when it is linked to the game;
- a recording whose times a person set. "ChannelBin set this window" is a stored fact: a rule's
  RuleMatch.rec_start_time/rec_stop_time, a Record recording's or a second recording's
  sports_window_start/stop. A recording whose window is not that one is left as it is, and its page
  says the game moved;
- a recording made from no guide listing of the game on the time its event channel's name gives:
  that clock is the channel's, and TheSportsDB's time says nothing about it;
- the program times, which the game's link (sports_data.recording_matches) and retrack key on.

A recording made from no guide listing on TheSportsDB's own time (an event channel whose name gives
no time zone) has nothing but TheSportsDB to go by, so while it is SCHEDULED and its window is still
ChannelBin's it moves with TheSportsDB's start, program times and all (dev/changelog/1660). Where its
start came from is a stored fact, RuleMatch.sports_start_from or Recording.sports_start_from, never
read off the recording's shape. A rule's one already under way ends later when TheSportsDB moves the
game later after its start (sports_times.name_late, dev/changelog/1643).

A recording already under way keeps its start, whatever TheSportsDB does: its end only ever moves
later, to the end the league's answer now gives its listing (a game TheSportsDB moves later after it
started ends later by the move, sports_times.running_late), and an earlier time changes nothing but
an event saying so (dev/changelog/1632).

Runs after every matcher run, after every rule evaluation, and after a Record save. Each recording is
one write unit; the start, or a running recording's stop, is re-armed after its commit.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace

from . import db, sports_words
from .database import (
    REC_STATUS_SCHEDULED, RECORDING_EDITED, RECORDING_SPORTS_RETIMED, RECORDING_SPORTS_SECOND, RECORDING_SPORTS_TIME_KEPT,
    RECORDING_START_NOT_ARMED, RULE_MATCH_SCHEDULED, WINDOW_OPEN_STATUSES, Recording, RecordingEvent,
    RecordingProfile, RecordingSports, RuleMatch, SportsEvent, SportsLeague, add_recording_event)
from .db_utils import retry_on_locked
from .auto_rules_sports import START_FROM_TSDB
from .fmt_utils import counted, iso_seconds_or_none

log = logging.getLogger(__name__)

KIND_RULE = 'rule'      # an auto-record rule's recording
KIND_RECORD = 'record'  # a league page's Record made it
KIND_SECOND = 'second'  # Cover both's second recording

#: RECORDING_SPORTS_SECOND's extra['kind'].
SECOND_ADDED = 'added'
SECOND_MOVED = 'moved'
SECOND_REMOVED = 'removed'

#: RECORDING_SPORTS_RETIMED's extra['kind']: a SCHEDULED recording's window moved, or a running one's
#: end moved later.
RETIMED_MOVED = 'moved'
RETIMED_EXTENDED = 'extended'
#: RECORDING_SPORTS_TIME_KEPT's extra['reason']: a person set the times, the recording was already
#: under way when the game's time moved, or one of the reasons a recording made on TheSportsDB's
#: time is not moved to its new one.
KEPT_EDITED = 'edited'
KEPT_UNDER_WAY = 'under_way'
KEPT_LISTING_THERE = 'listing_there'   # its rule's ledger already holds a listing at the new start
KEPT_POSTPONED = 'postponed'           # TheSportsDB marks the game postponed
KEPT_OVER = 'over'                     # the new time has already passed

#: The plan warnings that say the account has no connection for the window (auto_rules.NO_SEAT_WARNINGS).
_NO_SEAT = ('connection_limit_warning', 'account_block_warning')

#: _move_named()'s answer when a rule's ledger already holds the anchor the move would take.
_BLOCKED = object()


@dataclass
class _Game:
    event: SimpleNamespace    # a SportsEvent's columns, read once: the pass rolls back between writes
    league: SimpleNamespace

    @property
    def one(self) -> str:
        """The word for this event: game, match, race or event (app/sports_words.py)."""
        return sports_words.noun(self.league.sport, bool(self.event.home_team_id or self.event.away_team_id))[0]


@dataclass
class _Pass:
    """What one pass reads once and every write uses."""
    rules: object
    cfg: dict
    now: datetime
    tz: object
    h24: bool

    def clock(self, dt) -> str:
        from .tz_utils import format_local
        return format_local(dt, 'day_datetime', tz=self.tz, h24=self.h24)


def _snap(row) -> SimpleNamespace:
    return SimpleNamespace(**{c.key: getattr(row, c.key) for c in row.__mapper__.column_attrs})


def _minutes(td) -> int:
    return int(td.total_seconds() // 60)


def padding(rec, kind, profiles: dict, stored: dict) -> tuple:
    """(pre, post) for a recording: its profile's, as retrack pads a moved recording
    (auto_rules._move_recording). With no profile, the padding ChannelBin last gave it, kept in the
    event that moved or made it, since the gap to its program is the move itself once it has moved.
    Before any such event a rule's recording is at its listing's window, so the gap is its padding,
    and a Record recording was padded by no profile, which is none."""
    profile = profiles.get(rec.profile_id) if rec.profile_id is not None else None
    if profile is not None:
        return profile
    if rec.id in stored:
        return stored[rec.id]
    if kind == KIND_RULE:
        return (max(timedelta(0), rec.program_start_time - rec.start_time),
                max(timedelta(0), rec.stop_time - rec.program_stop_time))
    return timedelta(0), timedelta(0)


def padded(wins, pre, post) -> list:
    return [(start - pre, stop + post) for start, stop in wins]


def game_windows(rec, game: _Game, rules, now) -> list:
    """The unpadded windows the league's answer gives the listing `rec` was made from."""
    from .sports_times import event_windows
    return event_windows(rec.program_start_time, rec.program_stop_time, game.event,
                         rules.of(game.league.tsdb_id), now)


def record_window(sports_record_tsdb_id, entry, profile_id, now=None, rules=None):
    """(start, stop) a league page's Record suggests for `entry` under `profile_id`: the first window
    the league's answer gives the listing, padded by the profile as the dialog pads it
    (guide.js::applyProfilePadding). None when the game is not stored."""
    from .sports_times import Rules, event_windows
    now = now or datetime.utcnow()
    row = (db.session.query(SportsEvent, SportsLeague.tsdb_id)
           .join(SportsLeague, SportsLeague.id == SportsEvent.league_id)
           .filter(SportsEvent.tsdb_id == sports_record_tsdb_id).first())
    if row is None:
        return None
    event, league_tsdb_id = row
    rules = rules or Rules()
    start, stop = event_windows(entry.start_time, entry.stop_time, event, rules.of(league_tsdb_id), now)[0]
    profile = db.session.get(RecordingProfile, profile_id) if profile_id is not None else None
    pre = timedelta(minutes=(profile.pre_padding_minutes or 0) if profile else 0)
    post = timedelta(minutes=(profile.post_padding_minutes or 0) if profile else 0)
    return start - pre, stop + post


def edit_window(recording_id, now=None):
    """(kind, owned, (start, stop)) for a SCHEDULED recording the pass follows, read as the pass reads
    it: which surface made it, whether its window is still the one ChannelBin gave it, and the unpadded
    window the league's answer gives its listing. None for a recording the pass does not follow. An
    edit's profile change re-pads from it (app/edit_window.py, dev/changelog/1652)."""
    from .config import load_config
    from .sports_times import Rules
    now = now or datetime.utcnow()
    ctx = _Context.read([recording_id], now)
    rec = next((r for r in ctx.primaries if r.id == recording_id), None)
    if rec is None:
        return None
    kind, owned = _owner(rec, ctx)
    return kind, owned, game_windows(rec, ctx.games[ctx.game_of[rec.id]], Rules(load_config()), now)[0]


def same_minute(a, b) -> bool:
    """The dialog's pickers hold whole minutes."""
    return a.replace(second=0, microsecond=0) == b.replace(second=0, microsecond=0)


def apart(first, second) -> tuple:
    """`second` trimmed so it does not overlap `first`: the two windows are apart before padding
    (sports_times.windows), and two recordings on one channel that overlap hand off, which would cut
    the earlier one short. Only padding is trimmed."""
    start, stop = second
    if start < first[0]:
        return start, min(stop, first[0])
    return max(start, first[1]), stop


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

def follow(recording_ids=None, now: datetime = None) -> dict:
    """Bring every followed recording (or only `recording_ids`) to its game's window. Returns counts
    {moved, kept, added, removed}. A recording that cannot be brought up to date is logged at ERROR,
    which raises an alert naming it, and the rest go on."""
    from .config import load_config
    from .sports_times import Rules
    from .tz_utils import get_display_tz, is_24h
    now = now or datetime.utcnow()
    counts = {'moved': 0, 'kept': 0, 'added': 0, 'removed': 0, 'extended': 0}
    cfg = load_config()
    run = _Pass(rules=Rules(cfg), cfg=cfg, now=now, tz=get_display_tz(), h24=is_24h())
    ctx = _Context.read(recording_ids, now)
    db.session.rollback()   # no read transaction held across the writes below
    for rec in ctx.primaries:
        try:
            _follow_one(rec, ctx, run, counts)
        except Exception:
            db.session.rollback()
            log.error('Recording %d: could not bring it to its game\'s time', rec.id, exc_info=True,
                      extra={'recording_id': rec.id})
    for rec in ctx.running:
        try:
            _follow_running(rec, ctx, run, counts)
        except Exception:
            db.session.rollback()
            log.error('Recording %d: could not bring its end to its game\'s time', rec.id, exc_info=True,
                      extra={'recording_id': rec.id})
    for rec in ctx.named_scheduled:
        try:
            _follow_named_scheduled(rec, ctx, run, counts)
        except Exception:
            db.session.rollback()
            log.error('Recording %d: could not bring it to TheSportsDB\'s time for its game', rec.id,
                      exc_info=True, extra={'recording_id': rec.id})
    for rec in ctx.named_running:
        try:
            _follow_named(rec, ctx, run, counts)
        except Exception:
            db.session.rollback()
            log.error('Recording %d: could not bring its end to its game\'s time', rec.id, exc_info=True,
                      extra={'recording_id': rec.id})
    for second in ctx.orphans:
        try:
            if _owned_second(second) and _remove_second(second.id, None, '', run):
                counts['removed'] += 1
        except Exception:
            db.session.rollback()
            log.error('Recording %d: could not remove a second recording nobody needs', second.id,
                      exc_info=True, extra={'recording_id': second.id})
    if any(counts.values()):
        log.info('Sports times: moved %d, ended %d under way later, left %d as they were, added %d second '
                 'and removed %d', counts['moved'], counts['extended'], counts['kept'], counts['added'],
                 counts['removed'])
    return counts


def follow_guarded(recording_ids=None, now=None) -> dict:
    """follow() for a caller whose own work must not stop for it: the matcher, a rule run, a save."""
    try:
        return follow(recording_ids, now)
    except Exception:
        db.session.rollback()
        log.exception('Sports times: following the games\' times stopped')
        return {}


class _Context:
    """Everything the pass reads, in a fixed number of queries plus one listing check per recording."""

    @classmethod
    def read(cls, recording_ids, now):
        from sqlalchemy import and_, or_
        self = cls()
        automated = or_(Recording.rule_id.isnot(None), Recording.sports_record_tsdb_id.isnot(None))
        # SCHEDULED first recordings, and every automated recording under way, a second included: the
        # one that turns out to hold the game must not stop at its old end either.
        q = Recording.query.filter(
            Recording.program_start_time.isnot(None), Recording.program_stop_time.isnot(None),
            or_(and_(Recording.status == REC_STATUS_SCHEDULED, Recording.sports_cover_for_id.is_(None), automated),
                and_(Recording.status.in_(WINDOW_OPEN_STATUSES),
                     or_(automated, Recording.sports_cover_for_id.isnot(None)))))
        if recording_ids is not None:
            q = q.filter(Recording.id.in_(list(recording_ids)))
        recs = q.all()
        ids = [r.id for r in recs]
        links = {rid: tsdb for rid, tsdb in db.session.query(
            RecordingSports.recording_id, RecordingSports.tsdb_event_id).filter(
            RecordingSports.recording_id.in_(ids), RecordingSports.removed_at.is_(None))} if ids else {}
        # A rule's recording from an event channel's name is its ledger row's game, whatever its
        # link says: the link is found by how near the game starts, and the game may have moved.
        anchored = {rid: tsdb for rid, tsdb in db.session.query(
            RuleMatch.recording_id, RuleMatch.sports_event_tsdb_id).filter(
            RuleMatch.recording_id.in_(ids), RuleMatch.action_taken == RULE_MATCH_SCHEDULED,
            RuleMatch.sports_event_tsdb_id.isnot(None))} if ids else {}
        self.game_of = {}
        for r in recs:
            tsdb = (r.sports_record_tsdb_id if r.sports_record_tsdb_id is not None
                    else anchored.get(r.id, links.get(r.id)))
            if tsdb is not None:
                self.game_of[r.id] = tsdb
        games = {}
        if self.game_of:
            for event, league in (db.session.query(SportsEvent, SportsLeague)
                                  .join(SportsLeague, SportsLeague.id == SportsEvent.league_id)
                                  .filter(SportsEvent.tsdb_id.in_(set(self.game_of.values())))):
                games[event.tsdb_id] = _Game(_snap(event), SimpleNamespace(name=league.name, tsdb_id=league.tsdb_id,
                                                                           sport=league.sport))
        self.games = games
        followed, unlisted, waiting = [], [], []
        for r in recs:
            if self.game_of.get(r.id) not in games:
                continue
            if _from_listing(r, games[self.game_of[r.id]]):
                followed.append(_snap(r))
            elif (r.status in WINDOW_OPEN_STATUSES and _running_kind(r) == KIND_RULE):
                unlisted.append(_snap(r))
            elif r.status == REC_STATUS_SCHEDULED:
                waiting.append(_snap(r))
        self.primaries = [r for r in followed if r.status == REC_STATUS_SCHEDULED]
        self.running = [r for r in followed if r.status in WINDOW_OPEN_STATUSES]
        ids = [r.id for r in followed + unlisted + waiting]
        # A recording can carry more than one ledger row (a rule edited since); the SCHEDULED one is
        # the rule's window.
        self.ledger, from_name, on_tsdb = {}, set(), set()
        if ids:
            for m in RuleMatch.query.filter(RuleMatch.recording_id.in_(ids),
                                            RuleMatch.action_taken == RULE_MATCH_SCHEDULED):
                self.ledger.setdefault(m.recording_id, []).append((m.rec_start_time, m.rec_stop_time))
                if m.sports_event_tsdb_id is not None and m.sports_event_tsdb_id == self.game_of.get(m.recording_id):
                    from_name.add(m.recording_id)
                    if m.sports_start_from == START_FROM_TSDB:
                        on_tsdb.add(m.recording_id)
        # A rule's recording from an event channel's name, under way: its ledger row is anchored on
        # the game itself (RuleMatch.sports_event_tsdb_id), the one fact that says so.
        self.named_running = [r for r in unlisted if r.id in from_name]
        # Still to start, made on TheSportsDB's own time: a rule's by its ledger row, a Record one
        # by its own column.
        self.named_scheduled = [
            r for r in waiting
            if (r.id in on_tsdb if _running_kind(r) == KIND_RULE
                else r.sports_record_tsdb_id is not None and r.sports_start_from == START_FROM_TSDB)]
        self.seconds = {}
        primary_ids = [r.id for r in self.primaries]
        if primary_ids:
            for s in Recording.query.filter(Recording.sports_cover_for_id.in_(primary_ids)):
                self.seconds.setdefault(s.sports_cover_for_id, []).append(_snap(s))
        every = followed + self.named_running + self.named_scheduled + [s for group in self.seconds.values() for s in group]
        self.profiles = {p.id: (timedelta(minutes=p.pre_padding_minutes or 0), timedelta(minutes=p.post_padding_minutes or 0))
                         for p in RecordingProfile.query.filter(RecordingProfile.id.in_(
                             {r.profile_id for r in every if r.profile_id is not None}))} if every else {}
        self.stored = _stored_padding([r.id for r in every if r.profile_id is None or r.profile_id not in self.profiles])
        # A second whose first recording is gone (a person deleted or cancelled it).
        oq = Recording.query.filter(Recording.status == REC_STATUS_SCHEDULED, Recording.sports_cover_for_id.isnot(None))
        if recording_ids is not None:
            oq = oq.filter(Recording.sports_cover_for_id.in_(list(recording_ids)))
        pending = oq.all()
        alive = {i for (i,) in db.session.query(Recording.id).filter(
            Recording.id.in_({s.sports_cover_for_id for s in pending}))} if pending else set()
        self.orphans = [_snap(s) for s in pending if s.sports_cover_for_id not in alive]
        return self


def _stored_padding(ids) -> dict:
    """{recording id: (pre, post)} from the last event in which ChannelBin set its window, for the
    recordings with no profile to read it from. One query. An edit that moved the window to a profile's
    padding is one (its RECORDING_EDITED carries the minutes, app/edit_window.py); any other edit carries
    none and is skipped."""
    if not ids:
        return {}
    out = {}
    for rid, extra in (db.session.query(RecordingEvent.recording_id, RecordingEvent.extra_data)
                       .filter(RecordingEvent.recording_id.in_(ids),
                               RecordingEvent.event_type.in_((RECORDING_SPORTS_RETIMED, RECORDING_SPORTS_SECOND,
                                                              RECORDING_EDITED)))
                       .order_by(RecordingEvent.id)):
        try:
            data = json.loads(extra or '{}')
        except ValueError:
            continue
        if isinstance(data.get('pre_minutes'), int) and isinstance(data.get('post_minutes'), int):
            out[rid] = (timedelta(minutes=data['pre_minutes']), timedelta(minutes=data['post_minutes']))
    return out


def _from_listing(rec, game: _Game) -> bool:
    """Was `rec` made from a guide listing of this game? An event channel's recording was not, and
    keeps the window its pick gave it."""
    from .sports_data import recording_matches
    return any(m.event_id == game.event.id for m in recording_matches(rec))


def _owner(rec, ctx) -> tuple:
    """(kind, owned): which automated surface made it, and whether its window is still the one
    ChannelBin last gave it."""
    if rec.rule_id is not None and rec.sports_record_tsdb_id is None:
        return KIND_RULE, (rec.start_time, rec.stop_time) in (ctx.ledger.get(rec.id) or [])
    return KIND_RECORD, (rec.sports_window_start is not None and rec.sports_window_start == rec.start_time
                         and rec.sports_window_stop == rec.stop_time)


def _owned_second(s) -> bool:
    return (s.status == REC_STATUS_SCHEDULED and s.sports_window_start is not None
            and s.sports_window_start == s.start_time and s.sports_window_stop == s.stop_time)


def _follow_one(rec, ctx, run, counts):
    game = ctx.games[ctx.game_of[rec.id]]
    raw = game_windows(rec, game, run.rules, run.now)
    kind, owned = _owner(rec, ctx)
    pad = padding(rec, kind, ctx.profiles, ctx.stored)
    wins = padded(raw, *pad)
    listing = padded([(rec.program_start_time, rec.program_stop_time)], *pad)[0]
    want = (max(wins[0][0], run.now), wins[0][1])
    current = (rec.start_time, rec.stop_time)
    seconds = ctx.seconds.get(rec.id) or []
    if not owned:
        # Unclamped, so a start already passed does not read as a new window on every pass.
        if wins[0] != listing and wins[0] != current and _kept_new(rec.id, wins[0]):
            _note_kept(rec.id, kind, game, run, raw, wins[0])
            counts['kept'] += 1
        return
    if want != current:
        if _move(rec.id, kind, current, want, pad, game, run, raw):
            counts['moved'] += 1
            _rearm(rec.id, want[0], want[1], want[0] != current[0])
    live = [s for s in seconds if s.status == REC_STATUS_SCHEDULED]
    if len(wins) == 2:
        target = apart(want, wins[1])
        target = (max(target[0], run.now), target[1])
        if not seconds:
            if not _second_was_removed(rec.id) and _add_second(rec.id, target, pad, game, run, raw):
                counts['added'] += 1
            return
        for s in live:
            if _owned_second(s) and (s.start_time, s.stop_time) != target:
                if _move_second(s.id, rec.id, (s.start_time, s.stop_time), target, pad, game, run, raw):
                    counts['moved'] += 1
                    _rearm(s.id, target[0], target[1], target[0] != s.start_time)
        return
    for s in live:
        if _owned_second(s):
            from .sports_times import GO_GUIDE
            why = (f'{game.league.name} goes by your guide now' if run.rules.of(game.league.tsdb_id).go_by == GO_GUIDE
                   else 'one recording covers both times now')
            if _remove_second(s.id, rec.id, why, run):
                counts['removed'] += 1


def _owned_running(rec, ctx) -> bool:
    """A recording under way still has the end ChannelBin last gave it. Its start is read from
    scheduled_start_time, since the recorder moves start_time to the moment a late start began."""
    start = rec.scheduled_start_time or rec.start_time
    if rec.rule_id is not None and rec.sports_record_tsdb_id is None and rec.sports_cover_for_id is None:
        return (start, rec.stop_time) in (ctx.ledger.get(rec.id) or [])
    return (rec.sports_window_start is not None and rec.sports_window_start == start
            and rec.sports_window_stop == rec.stop_time)


def _running_kind(rec) -> str:
    if rec.sports_cover_for_id is not None:
        return KIND_SECOND
    return KIND_RULE if rec.rule_id is not None and rec.sports_record_tsdb_id is None else KIND_RECORD


def _follow_running(rec, ctx, run, counts):
    """A recording under way: its start stays, its end moves only later, to the end the league's
    answer now gives its listing (a second's: the later of the two windows, the one it holds)."""
    from .sports_times import AGREE
    game = ctx.games[ctx.game_of[rec.id]]
    if rec.stop_time <= run.now:
        return
    raw = game_windows(rec, game, run.rules, run.now)
    kind = _running_kind(rec)
    pad = padding(rec, kind, ctx.profiles, ctx.stored)
    wins = padded(raw, *pad)
    stop = max(w[1] for w in wins) if kind == KIND_SECOND else wins[0][1]
    begin = wins[-1][0] if kind == KIND_SECOND else wins[0][0]
    start = rec.scheduled_start_time or rec.start_time
    if stop > rec.stop_time:
        if not _owned_running(rec, ctx):
            if _kept_new(rec.id, (start, stop)):
                _note_kept(rec.id, kind, game, run, raw, (start, stop))
                counts['kept'] += 1
            return
        if _extend(rec.id, kind, start, rec.stop_time, stop, pad, game, run, raw):
            counts['extended'] += 1
            _rearm_stop(rec.id, stop)
        return
    ev = game.event
    moved_earlier = (ev.start_at is not None and ev.previous_start is not None and _kept(ev)
                     and ev.start_at < ev.previous_start - AGREE)
    if (begin < start or moved_earlier) and ev.start_at is not None:
        want = (ev.start_at, rec.stop_time)
        if _kept_new(rec.id, want):
            _note_under_way(rec.id, game, run, start, want)
            counts['kept'] += 1


def _follow_named(rec, ctx, run, counts):
    """A rule's recording from an event channel's name, under way: its end moves later by what
    sports_times.name_late() says, from the end its pick gave it (the program stop and its padding),
    so a second pass lands on the same end and never adds the delay twice. Its start stays."""
    from .sports_times import name_late
    game = ctx.games[ctx.game_of[rec.id]]
    if rec.stop_time <= run.now:
        return
    late = name_late(game.event, rec.program_start_time)
    if not late:
        return
    pad = padding(rec, KIND_RULE, ctx.profiles, ctx.stored)
    stop = rec.program_stop_time + pad[1] + late
    if stop <= rec.stop_time:
        return
    start = rec.scheduled_start_time or rec.start_time
    ev = game.event
    why = (f'TheSportsDB now has the {game.one} at {run.clock(ev.start_at)}, later than the '
           f"{run.clock(rec.program_start_time)} the recording started from, and it changed that after the "
           f'recording had started, so it should end about {counted(_minutes(late), "minute")} later.')
    if not _owned_running(rec, ctx):
        if _kept_new(rec.id, (start, stop)):
            _note_kept(rec.id, KIND_RULE, game, run, None, (start, stop), why=why)
            counts['kept'] += 1
        return
    if _extend(rec.id, KIND_RULE, start, rec.stop_time, stop, pad, game, run, None, why=why):
        counts['extended'] += 1
        _rearm_stop(rec.id, stop)


def _follow_named_scheduled(rec, ctx, run, counts):
    """A recording still to start that was made from an event channel's name on TheSportsDB's own
    time: when TheSportsDB has the game at another time, the recording moves to it, the length its
    pick gave it and its padding kept, and its program times with it, since they are the game's time
    and no listing's. Nothing to do when TheSportsDB has no time to go by now or the league goes by
    the guide now: no pick stands on TheSportsDB's time then. A postponed game and a new time
    already over leave the recording where it is and say so once, as times a person set do."""
    from .sports_data import event_shape_is_matchup
    from .sports_times import GO_GUIDE
    game = ctx.games[ctx.game_of[rec.id]]
    ev = game.event
    start = ev.start_at
    if (start is None or start == rec.program_start_time
            or (not event_shape_is_matchup(ev) and (start.hour, start.minute) == (0, 0))
            or run.rules.of(game.league.tsdb_id).go_by == GO_GUIDE):
        return
    kind = _running_kind(rec)
    pad = padding(rec, kind, ctx.profiles, ctx.stored)
    program = (start, start + (rec.program_stop_time - rec.program_start_time))
    # Unclamped, so a start already passed does not read as a new window on every pass.
    window = (program[0] - pad[0], program[1] + pad[1])
    current = (rec.start_time, rec.stop_time)
    why = (f'TheSportsDB now has the {game.one} at {run.clock(start)}, not {run.clock(rec.program_start_time)}. This '
           "recording was made from an event channel's name on TheSportsDB's time, with no guide listing to go "
           'by, so it goes by TheSportsDB.')

    def kept(whose=None, reason=KEPT_EDITED):
        if _kept_new(rec.id, window):
            _note_kept(rec.id, kind, game, run, None, window, why=why, whose=whose, reason=reason)
            counts['kept'] += 1

    if ev.postponed:
        return kept(f'TheSportsDB marks the {game.one} postponed, and a postponed start is not one to record at',
                    KEPT_POSTPONED)
    if window[1] <= run.now:
        return kept('that time is already over', KEPT_OVER)
    if not _owner(rec, ctx)[1]:
        return kept()
    want = (max(window[0], run.now), window[1])
    moved = _move_named(rec.id, kind, current, want, program, pad, game, run, why)
    if moved is _BLOCKED:
        kept('its rule already judged a listing of that title at that start', KEPT_LISTING_THERE)
    elif moved:
        counts['moved'] += 1
        _rearm(rec.id, want[0], want[1], want[0] != current[0])


def _kept(event) -> bool:
    from .sports_match import kept_start
    return kept_start(event)


# ---------------------------------------------------------------------------
# Sentences
# ---------------------------------------------------------------------------

def _why(game, run, raw, listing) -> str:
    """Why the window is what it is: the two starts and the league's answer. `listing` is the
    unpadded (start, stop) of the listing the recording was made from."""
    from .sports_times import GO_BOTH, GO_GUIDE, GO_TSDB, AGREE, acts, listed_start, tsdb_start
    rule = run.rules.of(game.league.tsdb_id)
    tsdb, listed = tsdb_start(game.event), listed_start(game.event)
    clock = run.clock
    if tsdb is None or listed is None:
        return "TheSportsDB has no time for it to go by, so it records the listing's times."
    if rule.go_by == GO_GUIDE:
        return f"{game.league.name} goes by your guide, so it records the listing's times."
    if not acts(listed, tsdb, rule, run.now):
        return (f"TheSportsDB's time is used only from {rule.wait_hours} hours before the {game.one}, so it records "
                "the listing's times until then.")
    if raw == [listing]:
        if abs(tsdb - listing[0]) <= AGREE:
            return f"TheSportsDB and your guide agree on {clock(tsdb)}, so it records the listing's times."
        return (f"TheSportsDB has the {game.one} at {clock(tsdb)}, too far from the listing's {clock(listing[0])} to be "
                "the same airing, so it records the listing's times.")
    verb = {GO_TSDB: 'goes by TheSportsDB', GO_BOTH: 'covers both times'}.get(rule.go_by)
    if verb is None:
        raise ValueError(f'unknown answer {rule.go_by!r}')
    return f'TheSportsDB has the {game.one} at {clock(tsdb)} and the listing at {clock(listing[0])}; {game.league.name} {verb}.'


def _collision(rec_id, start, stop, exclude) -> str:
    """The sentence naming a full account the window `rec_id`'s channel would hold runs into, or ''.
    The start's own seat rules decide who records (dev/changelog/1611)."""
    from .routes.recordings import _pending_recording_warnings
    rec = db.session.get(Recording, rec_id)
    if rec is None:
        return ''
    warnings = _pending_recording_warnings(rec.channel_id, rec.group_id, start, stop, exclude_recording_id=exclude)
    said = [w['message'] for k, w in warnings.items() if k in _NO_SEAT and isinstance(w, dict) and w.get('message')]
    db.session.rollback()
    return (' It runs into another recording on the same account: ' + ' '.join(said)) if said else ''


def _handoff_ahead(rec_id, new_stop, run) -> str:
    """The sentence naming a recording on the same channel or group that starts inside a running
    recording's later end, or ''. Its start hands off and stops this one there
    (recorder.start_recording's same-source handoff), so the later end is kept only up to it."""
    from sqlalchemy import or_
    rec = db.session.get(Recording, rec_id)
    same = [Recording.channel_id == rec.channel_id] if rec is not None and rec.channel_id is not None else []
    if rec is not None and rec.group_id is not None:
        same.append(Recording.group_id == rec.group_id)
    nxt = (Recording.query.filter(or_(*same), Recording.status == REC_STATUS_SCHEDULED, Recording.id != rec_id,
                                  Recording.start_time < new_stop, Recording.start_time >= run.now)
           .order_by(Recording.start_time, Recording.id).first()) if same else None
    found = (nxt.id, nxt.start_time) if nxt is not None else None
    db.session.rollback()
    if found is None:
        return ''
    return (f' Recording #{found[0]} on the same channel starts at {run.clock(found[1])} and takes over from this one '
            'then, so this one stops there unless that one is moved or removed.')


def _extra(old, new, pad, game, run, kind) -> dict:
    from .sports_times import listed_start, tsdb_start
    out = {'kind': kind, 'go_by': run.rules.of(game.league.tsdb_id).go_by, 'sports_event_tsdb_id': game.event.tsdb_id,
           'tsdb_start': iso_seconds_or_none(tsdb_start(game.event)),
           'listed_start': iso_seconds_or_none(listed_start(game.event)),
           'old_start': iso_seconds_or_none(old[0]) if old else None,
           'old_stop': iso_seconds_or_none(old[1]) if old else None,
           'new_start': iso_seconds_or_none(new[0]), 'new_stop': iso_seconds_or_none(new[1])}
    if pad is not None:
        out['pre_minutes'], out['post_minutes'] = _minutes(pad[0]), _minutes(pad[1])
    return out


def _listing_of(rec) -> tuple:
    return rec.program_start_time, rec.program_stop_time


# ---------------------------------------------------------------------------
# Writes, one unit each
# ---------------------------------------------------------------------------

def _move(rec_id, kind, old, new, pad, game, run, raw) -> bool:
    rec = db.session.get(Recording, rec_id)
    listing = _listing_of(rec) if rec is not None else None
    db.session.rollback()
    if listing is None:
        return False
    why = _why(game, run, raw, listing)
    bump = _collision(rec_id, *new, exclude=rec_id)
    clock = run.clock

    @retry_on_locked()
    def _write():
        rec = db.session.get(Recording, rec_id, populate_existing=True)
        if rec is None or rec.status != REC_STATUS_SCHEDULED or (rec.start_time, rec.stop_time) != old:
            db.session.rollback()
            return False
        rows = []
        if kind == KIND_RULE:
            rows = [m for m in RuleMatch.query.filter_by(recording_id=rec_id, action_taken=RULE_MATCH_SCHEDULED)
                    if (m.rec_start_time, m.rec_stop_time) == old]
            if not rows:
                db.session.rollback()
                return False
        elif (rec.sports_window_start, rec.sports_window_stop) != old:
            db.session.rollback()
            return False
        rec.start_time = rec.scheduled_start_time = new[0]
        rec.stop_time = rec.scheduled_stop_time = new[1]
        sentence = f'Moved to {clock(new[0])} to {clock(new[1])}, from {clock(old[0])} to {clock(old[1])}.'
        for m in rows:
            m.rec_start_time, m.rec_stop_time = new
            m.detail = f'{m.detail or ""} {sentence} {why}'.strip()
            m.updated_at = datetime.utcnow()
        if kind == KIND_RECORD:
            rec.sports_window_start, rec.sports_window_stop = new
        add_recording_event(rec_id, RECORDING_SPORTS_RETIMED, detail=f'{why} {sentence}{bump}',
                            extra=_extra(old, new, pad, game, run, RETIMED_MOVED))
        db.session.commit()
        return True

    return _write()


def _move_named(rec_id, kind, old, new, program, pad, game, run, why) -> bool:
    """Move a SCHEDULED recording made on TheSportsDB's time to TheSportsDB's new time: its window,
    its program times and ChannelBin's own record of both, in one unit. A rule's ledger row moves
    its anchor start too, so the next check reads the recording where it now sits; when another
    row of the rule already holds that anchor (a listing of the same title judged at that very
    start), nothing moves and the answer is _BLOCKED, which the caller says on the recording."""
    bump = _collision(rec_id, *new, exclude=rec_id)
    clock = run.clock
    tsdb_id = game.event.tsdb_id

    @retry_on_locked()
    def _write():
        rec = db.session.get(Recording, rec_id, populate_existing=True)
        if rec is None or rec.status != REC_STATUS_SCHEDULED or (rec.start_time, rec.stop_time) != old:
            db.session.rollback()
            return False
        rows = []
        if kind == KIND_RULE:
            rows = [m for m in RuleMatch.query.filter_by(recording_id=rec_id, action_taken=RULE_MATCH_SCHEDULED)
                    if (m.rec_start_time, m.rec_stop_time) == old and m.sports_event_tsdb_id == tsdb_id]
            if not rows:
                db.session.rollback()
                return False
            for m in rows:
                if RuleMatch.query.filter(
                        RuleMatch.rule_id == m.rule_id, RuleMatch.id != m.id, RuleMatch.title_key == m.title_key,
                        RuleMatch.episode_key == m.episode_key,
                        RuleMatch.program_start_time == program[0]).first() is not None:
                    db.session.rollback()
                    return _BLOCKED
        elif (rec.sports_window_start, rec.sports_window_stop) != old:
            db.session.rollback()
            return False
        rec.start_time = rec.scheduled_start_time = new[0]
        rec.stop_time = rec.scheduled_stop_time = new[1]
        rec.program_start_time, rec.program_stop_time = program
        sentence = f'Moved to {clock(new[0])} to {clock(new[1])}, from {clock(old[0])} to {clock(old[1])}.'
        for m in rows:
            m.rec_start_time, m.rec_stop_time = new
            m.program_start_time, m.program_stop_time = program
            m.detail = f'{m.detail or ""} {sentence} {why}'.strip()
            m.updated_at = datetime.utcnow()
        if kind == KIND_RECORD:
            rec.sports_window_start, rec.sports_window_stop = new
        add_recording_event(rec_id, RECORDING_SPORTS_RETIMED, detail=f'{why} {sentence}{bump}',
                            extra={**_extra(old, new, pad, game, run, RETIMED_MOVED), 'start_from': START_FROM_TSDB,
                                   'program_start': iso_seconds_or_none(program[0])})
        db.session.commit()
        return True

    return _write()


def _kept_new(rec_id, want) -> bool:
    """Not said already for this window: the pass runs after every matcher run and rule check."""
    last = (RecordingEvent.query.filter_by(recording_id=rec_id, event_type=RECORDING_SPORTS_TIME_KEPT)
            .order_by(RecordingEvent.id.desc()).first())
    db.session.rollback()
    if last is None:
        return True
    try:
        extra = json.loads(last.extra_data or '{}')
    except ValueError:
        return True
    return (extra.get('new_start'), extra.get('new_stop')) != (iso_seconds_or_none(want[0]), iso_seconds_or_none(want[1]))


def _note_kept(rec_id, kind, game, run, raw, want, why=None, whose=None, reason=KEPT_EDITED):
    rec = db.session.get(Recording, rec_id)
    listing = _listing_of(rec) if rec is not None else None
    db.session.rollback()
    if listing is None:
        return
    why = why or _why(game, run, raw, listing)
    # A Record recording with no window of ChannelBin's was saved at other times, changed since, or
    # made before ChannelBin kept the window it gave one; the sentence names all three, since a row
    # cannot tell which.
    whose = whose or ("its times were changed since its rule set them" if kind == KIND_RULE
                      else "ChannelBin did not set its times: they were chosen when it was saved or changed since")

    @retry_on_locked()
    def _write():
        if db.session.get(Recording, rec_id) is None:
            db.session.rollback()
            return
        add_recording_event(
            rec_id, RECORDING_SPORTS_TIME_KEPT,
            detail=(f'{why} ChannelBin would record {run.clock(want[0])} to {run.clock(want[1])}, but {whose}, so '
                    f"it is left as it is. Edit its times to record the {game.one}'s."),
            extra={**_extra(None, want, None, game, run, 'kept'), 'reason': reason})
        db.session.commit()

    _write()


def _late_why(game, run, raw, listing) -> str:
    """Why a recording under way ends later: the move TheSportsDB made after the game started, or,
    for a game that had not started by the time it used, the window the league's answer gives."""
    from .sports_times import running_late
    ev = game.event
    late = running_late(ev)
    if late:
        return (f'TheSportsDB moved the {game.one} from {run.clock(ev.previous_start)} to {run.clock(ev.start_at)} after '
                f'it had started, so it should end about {counted(_minutes(late), "minute")} later.')
    return _why(game, run, raw, listing)


def _extend(rec_id, kind, start, old_stop, new_stop, pad, game, run, raw, why=None) -> bool:
    """Move a running recording's end later, with ChannelBin's own record of its window, in one unit.
    The window it was planned for moves too (scheduled_stop_time): the end is a new plan, and a
    capture that ran to it is not one that ran over. `why` is said in place of the listing's
    reason, for a recording made from no listing."""
    rec = db.session.get(Recording, rec_id)
    listing = _listing_of(rec) if rec is not None else None
    db.session.rollback()
    if listing is None:
        return False
    why = why or _late_why(game, run, raw, listing)
    bump = _collision(rec_id, start, new_stop, exclude=rec_id) + _handoff_ahead(rec_id, new_stop, run)
    clock = run.clock

    @retry_on_locked()
    def _write():
        rec = db.session.get(Recording, rec_id, populate_existing=True)
        if (rec is None or rec.status not in WINDOW_OPEN_STATUSES or rec.stop_time != old_stop
                or (rec.scheduled_start_time or rec.start_time) != start):
            db.session.rollback()
            return False
        rows = []
        if kind == KIND_RULE:
            rows = [m for m in RuleMatch.query.filter_by(recording_id=rec_id, action_taken=RULE_MATCH_SCHEDULED)
                    if (m.rec_start_time, m.rec_stop_time) == (start, old_stop)]
            if not rows:
                db.session.rollback()
                return False
        elif (rec.sports_window_start, rec.sports_window_stop) != (start, old_stop):
            db.session.rollback()
            return False
        rec.stop_time = rec.scheduled_stop_time = new_stop
        sentence = (f'It was already recording, so its start stays at {clock(start)}, and its end moves to '
                    f'{clock(new_stop)}, from {clock(old_stop)}.')
        for m in rows:
            m.rec_stop_time = new_stop
            m.detail = f'{m.detail or ""} {sentence} {why}'.strip()
            m.updated_at = datetime.utcnow()
        if kind != KIND_RULE:
            rec.sports_window_stop = new_stop
        add_recording_event(rec_id, RECORDING_SPORTS_RETIMED, detail=f'{why} {sentence}{bump}',
                            extra=_extra((start, old_stop), (start, new_stop), pad, game, run, RETIMED_EXTENDED))
        db.session.commit()
        return True

    return _write()


def _note_under_way(rec_id, game, run, start, want):
    """TheSportsDB now has the game earlier than a recording already under way: nothing changes, and
    the recording says so once per time."""
    clock = run.clock

    @retry_on_locked()
    def _write():
        if db.session.get(Recording, rec_id) is None:
            db.session.rollback()
            return
        add_recording_event(
            rec_id, RECORDING_SPORTS_TIME_KEPT,
            detail=(f"TheSportsDB now has the {game.one} starting at {clock(want[0])}, before this recording's start at "
                    f'{clock(start)}, but it was already recording, so nothing changes: a recording under way keeps '
                    'its start, and its end never comes earlier.'),
            extra={**_extra(None, want, None, game, run, 'kept'), 'reason': KEPT_UNDER_WAY})
        db.session.commit()

    _write()


def _second_was_removed(primary_id) -> bool:
    """ChannelBin scheduled a second recording that is gone, and not by ChannelBin: its own removal
    writes SECOND_REMOVED in the delete's commit, so a gone second without one was a person's answer,
    and it is not made again for this recording."""
    last = (RecordingEvent.query.filter_by(recording_id=primary_id, event_type=RECORDING_SPORTS_SECOND)
            .order_by(RecordingEvent.id.desc()).first())
    db.session.rollback()
    if last is None:
        return False
    try:
        return json.loads(last.extra_data or '{}').get('kind') != SECOND_REMOVED
    except ValueError:
        return True


def _second_name(name: str) -> str:
    return f"{name} (TheSportsDB time)"


def _add_second(primary_id, window, pad, game, run, raw) -> bool:
    """Schedule the second recording, at TheSportsDB's time, through the one create path."""
    from .routes.recordings import _create_scheduled_recording
    primary = db.session.get(Recording, primary_id)
    if primary is None or primary.status != REC_STATUS_SCHEDULED:
        db.session.rollback()
        return False
    snap = {k: getattr(primary, k) for k in (
        'name', 'url', 'channel_id', 'group_id', 'profile_id', 'move_when_account_full', 'program_start_time',
        'program_stop_time', 'program_title', 'program_sub_title', 'metadata_description', 'metadata_category',
        'metadata_rating')}
    db.session.rollback()
    why = _why(game, run, raw, (snap['program_start_time'], snap['program_stop_time']))
    # The first recording is not excluded: the second is checked against it too.
    bump = _collision(primary_id, *window, exclude=None)
    start, stop = window
    clock = run.clock

    def link(r):
        # The create's own write lock is held from its INSERT, so this check and the insert are one.
        if Recording.query.filter(Recording.sports_cover_for_id == primary_id, Recording.id != r.id).first():
            return False
        if db.session.get(Recording, primary_id) is None:
            return False
        r.sports_cover_for_id = primary_id
        r.sports_window_start, r.sports_window_stop = r.start_time, r.stop_time
        for k in ('program_start_time', 'program_stop_time', 'program_title', 'program_sub_title',
                  'metadata_description', 'metadata_category', 'metadata_rating'):
            setattr(r, k, snap[k])
        text = (f'{why} The two starts are more than {run.rules.of(game.league.tsdb_id).cover_both_minutes} '
                'minutes apart, too far for one recording')
        new = (r.start_time, r.stop_time)
        add_recording_event(
            r.id, RECORDING_SPORTS_SECOND,
            detail=(f'{text}, so ChannelBin scheduled this second recording at TheSportsDB\'s time in case it is '
                    f'the right one, beside recording #{primary_id} at the listing\'s time.{bump}'),
            extra={**_extra(None, new, pad, game, run, SECOND_ADDED), 'first_id': primary_id})
        add_recording_event(
            primary_id, RECORDING_SPORTS_SECOND,
            detail=(f'{text}, so ChannelBin scheduled recording #{r.id} at TheSportsDB\'s time, '
                    f'{clock(new[0])} to {clock(new[1])}, in case it is the right one.'),
            extra={**_extra(None, new, None, game, run, SECOND_ADDED), 'second_id': r.id})
        return True

    rec = _create_scheduled_recording(
        run.cfg, name=_second_name(snap['name']), url=snap['url'], start_utc=start, stop_utc=stop,
        channel_id=snap['channel_id'], group_id=snap['group_id'], profile_id=snap['profile_id'],
        move_when_full=snap['move_when_account_full'], link=link)
    if rec is None:
        return False
    from .sports_data import link_recording
    link_recording(rec.id)
    return True


def _move_second(second_id, primary_id, old, new, pad, game, run, raw) -> bool:
    s = db.session.get(Recording, second_id)
    listing = _listing_of(s) if s is not None else None
    db.session.rollback()
    if listing is None:
        return False
    why = _why(game, run, raw, listing)
    bump = _collision(second_id, *new, exclude=second_id)
    clock = run.clock

    @retry_on_locked()
    def _write():
        s = db.session.get(Recording, second_id, populate_existing=True)
        if s is None or not _owned_second(s) or (s.start_time, s.stop_time) != old:
            db.session.rollback()
            return False
        s.start_time = s.scheduled_start_time = s.sports_window_start = new[0]
        s.stop_time = s.scheduled_stop_time = s.sports_window_stop = new[1]
        sentence = f'Moved to {clock(new[0])} to {clock(new[1])}, from {clock(old[0])} to {clock(old[1])}.'
        add_recording_event(second_id, RECORDING_SPORTS_RETIMED, detail=f'{why} {sentence}{bump}',
                            extra=_extra(old, new, pad, game, run, RETIMED_MOVED))
        add_recording_event(primary_id, RECORDING_SPORTS_SECOND,
                            detail=f'The second recording, #{second_id}, at TheSportsDB\'s time: {sentence}',
                            extra={**_extra(old, new, None, game, run, SECOND_MOVED), 'second_id': second_id})
        db.session.commit()
        return True

    return _write()


def _remove_second(second_id, primary_id, why, run) -> bool:
    """Delete a second recording nobody needs, as cancelling any SCHEDULED recording does
    (dev/changelog/814). The first one's SECOND_REMOVED event lands in the delete's own commit, so a
    gone second without one is never ChannelBin's doing (_second_was_removed)."""
    from .scheduler import unschedule_recording
    from .database import detach_recording_references
    s = db.session.get(Recording, second_id, populate_existing=True)
    if s is None or not _owned_second(s):
        db.session.rollback()
        return False
    when = (s.start_time, s.stop_time)
    db.session.rollback()
    unschedule_recording(second_id)
    clock = run.clock if run is not None else (lambda dt: iso_seconds_or_none(dt))

    @retry_on_locked()
    def _write():
        s = db.session.get(Recording, second_id, populate_existing=True)
        if s is None or not _owned_second(s):
            db.session.rollback()
            return False
        detach_recording_references(second_id)
        db.session.delete(s)  # hidden-recompute-ok: a Recording, not a group or membership
        if primary_id is not None and db.session.get(Recording, primary_id) is not None:
            add_recording_event(
                primary_id, RECORDING_SPORTS_SECOND,
                detail=(f'Removed the second recording, #{second_id}, at {clock(when[0])} to {clock(when[1])}: '
                        f'{why}.'),
                extra={'kind': SECOND_REMOVED, 'second_id': second_id, 'old_start': iso_seconds_or_none(when[0]),
                       'old_stop': iso_seconds_or_none(when[1])})
        db.session.commit()
        return True

    done = _write()
    if done and primary_id is None:
        log.info('Recording %d: removed, the second recording of a game whose first recording was removed',
                 second_id)
    return done


def drop_seconds_of(primary_id):
    """A person removed a recording: its second recording, while ChannelBin's, goes with it."""
    ids = [sid for (sid,) in db.session.query(Recording.id).filter(Recording.sports_cover_for_id == primary_id)]
    db.session.rollback()
    for sid in ids:
        _remove_second(sid, None, '', None)


def _rearm_stop(rec_id, stop):
    """After a running recording's end moved: its stop job alone, as a resume re-arms it
    (scheduler.reschedule_recording_stop). Left at the old time it would stop the capture there."""
    from .scheduler import reschedule_recording_stop
    for attempt in (1, 2):
        try:
            reschedule_recording_stop(rec_id, stop)
            return
        except Exception:
            if attempt == 2:
                log.error('Recording %d: its end moved later for its game, but its stop could not be re-armed, '
                          'so it may stop at its old end', rec_id, exc_info=True, extra={'recording_id': rec_id})


def _rearm(rec_id, start, stop, start_moved):
    """After a move committed: the re-plan tail a person's edit runs (routes/recordings.py)."""
    from flask import current_app
    from .routes import recordings as rec_routes
    try:
        rec_routes._rearm_replanned(rec_id, start, stop, start_moved)
        return
    except Exception:
        log.exception('Recording %d: moved to its game\'s time, but its start could not be re-armed; '
                      'trying once more', rec_id)
    try:
        rec_routes.schedule_recording(current_app._get_current_object(), rec_id, start, stop)
        return
    except Exception:
        log.exception('Recording %d: its start could not be armed', rec_id)

    @retry_on_locked()
    def _note():
        add_recording_event(rec_id, RECORDING_START_NOT_ARMED,
                            detail=('This recording was moved to its event\'s time, but its start could not be armed, '
                                    'so it will not start on its own. Restarting ChannelBin arms it.'))
        db.session.commit()

    _note()
