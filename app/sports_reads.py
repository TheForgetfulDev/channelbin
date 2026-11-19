"""Reads of TheSportsDB around a followed game (dev/docs/DESIGN-sports-data.md §16.2, dev/changelog/1632).

TheSportsDB's schedule is otherwise read about once a day (sports_data._round_next_due), so a move
entered during a game, a rain delay moved to the green flag, would reach ChannelBin only if that read
happened to land in it. A followed game in a league that goes by TheSportsDB or covers both is read
every READ_EVERY from READ_LEAD before the earlier of its two starts until its window ends.

Followed means automated, as everything that acts on a game's time is: a game with a recording an
auto-record rule or a league page's Record made (or Cover both's second), SCHEDULED or under way, or a
game a sports channel on the output feed carries. A recording made from the TV Guide or by hand does
not make a game followed, though recording_sports links it.

A read is a queue row brought forward, nothing more: its round's row (sports_data's own call), or the
game's own `event:` row when the round's last answer was cut and may leave the game out. A round
holding several followed games is one call. The worker makes the call at its own pace, and the
matcher, queued after every answer, does the rest. Nothing here calls out.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from . import db
from .database import (
    REC_STATUS_SCHEDULED, SPORTS_FETCH_CAPPED, SPORTS_FETCH_CAPPED_PAST, SPORTS_KIND_EVENT, SPORTS_KIND_ROUND,
    WINDOW_OPEN_STATUSES, Recording, RecordingSports, SportsEvent, SportsFetch, SportsLeague)
from .db_utils import retry_on_locked

log = logging.getLogger(__name__)

#: How often a followed game is read while it is in reach (dev/changelog/1624).
READ_EVERY = timedelta(minutes=30)
#: How long before the earlier of a game's two starts the reads begin: a move earlier by up to the
#: 90-minute Cover both limit is seen before the game, with time left for the matcher (queued 3
#: minutes after an answer) and for the start to be moved. Wider than the sports channels'
#: 60-minute pregame reach.
READ_LEAD = timedelta(hours=2)


def _starts(event) -> list:
    from .sports_times import listed_start, tsdb_start
    return [s for s in (tsdb_start(event), listed_start(event)) if s is not None]


def _recorded_ids(tsdb_ids: set) -> set:
    """The games among `tsdb_ids` an automated recording not yet over is of. Two queries."""
    if not tsdb_ids:
        return set()
    live = Recording.status.in_((REC_STATUS_SCHEDULED, *WINDOW_OPEN_STATUSES))
    automated = or_(Recording.rule_id.isnot(None), Recording.sports_record_tsdb_id.isnot(None),
                    Recording.sports_cover_for_id.isnot(None))
    marked = {t for (t,) in db.session.query(Recording.sports_record_tsdb_id).filter(
        live, Recording.sports_record_tsdb_id.in_(list(tsdb_ids)))}
    linked = {t for (t,) in db.session.query(RecordingSports.tsdb_event_id)
              .join(Recording, Recording.id == RecordingSports.recording_id)
              .filter(live, automated, RecordingSports.removed_at.is_(None),
                      RecordingSports.tsdb_event_id.in_(list(tsdb_ids)))}
    return marked | linked


def _carried_ids(events: list, cfg: dict) -> set:
    """The games among `events` a sports channel the output feed serves carries, while it is on."""
    from .output_feed import feed_config, is_enabled
    if not events or not is_enabled(feed_config(cfg)):
        return set()
    from .sports_feed import carriers, served_channels
    return set(carriers(served_channels(), events))


def followed_in_reach(now: datetime, cfg: dict = None, rules=None) -> list:
    """[(SportsEvent, SportsLeague)] for each followed game whose reads are due to run at `now`: in a
    league going by TheSportsDB or covering both, from READ_LEAD before its earlier start until its
    window ends. A fixed number of queries whatever is stored."""
    from .config import load_config
    from .sports_feed import MAX_LENGTH, event_window
    from .sports_match import listing_windows
    from .sports_times import GO_BOTH, GO_GUIDE, GO_TSDB, Rules
    cfg = cfg if cfg is not None else load_config()
    rules = rules or Rules(cfg)
    earliest = func.min(SportsEvent.start_at, func.coalesce(SportsEvent.guide_behind_start, SportsEvent.guide_start,
                                                            SportsEvent.start_at))
    rows = (db.session.query(SportsEvent, SportsLeague)
            .join(SportsLeague, SportsLeague.id == SportsEvent.league_id)
            .filter(SportsEvent.postponed.is_(False), SportsEvent.start_at.isnot(None),
                    earliest <= now + READ_LEAD, earliest > now - MAX_LENGTH).all())
    out = []
    for event, league in rows:
        go_by = rules.of(league.tsdb_id).go_by
        if go_by == GO_GUIDE:
            continue
        if go_by not in (GO_TSDB, GO_BOTH):
            raise ValueError(f'unknown answer {go_by!r}')
        starts = _starts(event)
        if starts and min(starts) - READ_LEAD <= now:
            out.append((event, league))
    if not out:
        return []
    followed = _recorded_ids({e.tsdb_id for e, _l in out})
    carried = _carried_ids([e for e, _l in out if e.tsdb_id not in followed], cfg)
    out = [(e, lg) for e, lg in out if e.tsdb_id in followed or e.id in carried]
    if not out:
        return []
    listings = listing_windows([e.id for e, _l in out], now, rules)
    kept = []
    for event, league in out:
        window = event_window(event, listings.get(event.id, ()))
        if window is not None and now < window[1]:
            kept.append((event, league))
    return kept


def _target(event, league, cut: set) -> tuple:
    """(target, kind) of the call that reads this game."""
    if event.season and event.round is not None:
        target = f'round:{league.tsdb_id}:{event.season}:{event.round}'
        if target not in cut:
            return target, SPORTS_KIND_ROUND
    return f'event:{event.tsdb_id}', SPORTS_KIND_EVENT


def arm(now: datetime = None, cfg: dict = None) -> int:
    """Bring forward the call that reads each followed game in reach to READ_EVERY after it last ran,
    never later than it was already due. Returns how many calls it brought forward. One write unit."""
    now = now or datetime.utcnow()
    games = followed_in_reach(now, cfg)
    if not games:
        db.session.rollback()
        return 0
    rounds = {f'round:{lg.tsdb_id}:{e.season}:{e.round}' for e, lg in games if e.season and e.round is not None}
    cut = {t for (t,) in db.session.query(SportsFetch.target).filter(
        SportsFetch.target.in_(list(rounds)), SportsFetch.outcome.in_((SPORTS_FETCH_CAPPED, SPORTS_FETCH_CAPPED_PAST)))}
    # A round with no row yet is asked by game too: a row made here would start a round walk.
    have = {t for (t,) in db.session.query(SportsFetch.target).filter(SportsFetch.target.in_(list(rounds)))}
    wanted = {}
    for event, league in games:
        target, kind = _target(event, league, cut | (rounds - have))
        wanted.setdefault(target, (kind, league.id, event.name))
    db.session.rollback()
    return _bring_forward(wanted, now)


@retry_on_locked()
def _bring_forward(wanted: dict, now: datetime) -> int:
    rows = {r.target: r for r in SportsFetch.query.filter(SportsFetch.target.in_(list(wanted)))}
    moved = []
    for target, (kind, league_id, name) in wanted.items():
        row = rows.get(target)
        if row is None:
            db.session.add(SportsFetch(target=target, kind=kind, league_id=league_id, next_due_at=now))
            moved.append((target, name, now))
            continue
        due = max(now, row.last_at + READ_EVERY) if row.last_at else now
        if row.next_due_at is None or row.next_due_at > due:
            row.next_due_at = due
            moved.append((target, name, due))
    db.session.commit()
    for target, name, due in moved:
        log.info('TheSportsDB %s: due %s UTC, read every %d minutes while %s is followed', target,
                 due.strftime('%H:%M'), READ_EVERY.seconds // 60, name)
    return len(moved)


def arm_guarded(now: datetime = None, cfg: dict = None) -> int:
    """arm() for the worker's tick, whose own call must not stop for it."""
    try:
        return arm(now, cfg)
    except Exception:
        db.session.rollback()
        log.exception('TheSportsDB: the reads of followed games could not be brought forward')
        return 0
