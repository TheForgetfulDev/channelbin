"""Which time a game goes by when TheSportsDB and the guide list it at different times
(dev/docs/DESIGN-sports-data.md §16, dev/changelog/1630).

The answer is a person's, per league with a default for every league, and this module is its only
reader and writer. Three answers, every one named wherever it is read:

  GO_TSDB   a listing of the game near its start starts at TheSportsDB's time; its end moves by the
            same amount and never comes earlier than listed.
  GO_GUIDE  every listing is as listed.
  GO_BOTH   from the earlier start to the later end, in one window when the two starts are within the
            limit or the two windows touch, else the listing and TheSportsDB's time as two windows.

Under GO_TSDB and GO_BOTH a game further off than the wait goes by the listing, because TheSportsDB
often adds a game before it knows the time. A replay (a listing more than OFF_LIMIT from the guide's
time for the game) never moves, and each listing moves by its own gap to TheSportsDB's start, not the
game's: on Nov 18 Texans at Colts was listed at 7:00 PM on 57 channels and at 8:00 PM on 4, with
TheSportsDB at 8:15 PM, and a whole-game shift would have put the 8:00 listings at 9:15.

Read by every automated surface: the sports channels on the output feed
(sports_match.listing_windows/airing_now), a league page's Record (sports_view.record_offer) and an
event-channel pick (auto_rules_sports.event_pick), where the wait holds only while the guide lists the
game (dev/changelog/1660). A recording made from the TV Guide or by hand keeps the times it was given.

Stored in config.yaml under sports_data.game_times: `go_by`, `cover_both_minutes` and `wait_hours`
for the default, and `leagues`, keyed by TheSportsDB's league id, holding only the answers a league
changed. Written only through set_default(), set_league() and forget_league(), each under
config_write_lock across its whole read-modify-write.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import config_default, config_write_lock, load_config, load_for_edit, save_config, set_nested
from .sports_data import event_shape_is_matchup
# AGREE: starts this close are the same start. OFF_LIMIT: a listing further than this from the guide's
# time for the game is another airing of it, a replay or a re-air, and never moves.
# WINDOW: the matcher's reach around TheSportsDB's start; a gap past it is no listing of this game.
from .sports_match import AGREE, MAX_DELAY, OFF_LIMIT, WINDOW, kept_start

GO_TSDB = 'tsdb'
GO_GUIDE = 'guide'
GO_BOTH = 'both'
GO_BY = (GO_TSDB, GO_GUIDE, GO_BOTH)
GO_BY_LABELS = {GO_TSDB: 'TheSportsDB', GO_GUIDE: 'Your guide', GO_BOTH: 'Cover both'}
#: The answers by name, for a template to compare against rather than a re-typed word.
GO = {'TSDB': GO_TSDB, 'GUIDE': GO_GUIDE, 'BOTH': GO_BOTH}

#: The largest numbers a person may save. A gap past the matcher's 13-hour window is no game's, and a
#: wait past the schedule's horizon waits for nothing.
MAX_COVER_BOTH_MINUTES = 13 * 60
MAX_WAIT_HOURS = 30 * 24

_PATH = 'sports_data.game_times'
_FIELDS = ('go_by', 'cover_both_minutes', 'wait_hours')


@dataclass(frozen=True)
class Rule:
    """What one league goes by, with the default filled in where it changed nothing."""
    go_by: str
    cover_both_minutes: int
    wait_hours: int
    own: dict = field(default_factory=dict)   # the league's own answers, only those it changed

    @property
    def limit(self) -> timedelta:
        return timedelta(minutes=self.cover_both_minutes)

    @property
    def wait(self) -> timedelta:
        return timedelta(hours=self.wait_hours)

    @property
    def uses_default(self) -> bool:
        return 'go_by' not in self.own


def _section(cfg: dict) -> dict:
    return (cfg.get('sports_data') or {}).get('game_times') or {}


def _clean(raw: dict) -> dict:
    """The answers in `raw` that are valid, dropping any other: a hand-edited config.yaml with a
    typo falls back to the default for that answer rather than failing every sports surface."""
    out = {}
    if raw.get('go_by') in GO_BY:
        out['go_by'] = raw['go_by']
    for key, top in (('cover_both_minutes', MAX_COVER_BOTH_MINUTES), ('wait_hours', MAX_WAIT_HOURS)):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= top:
            out[key] = value
    return out


class Rules:
    """Every league's rule from one config read. Build one per request or per pass and ask it per
    row: it never reads the config again."""

    def __init__(self, cfg: dict = None):
        section = _section(cfg if cfg is not None else load_config())
        base = {k: config_default(f'{_PATH}.{k}') for k in _FIELDS}
        self.default = Rule(**{**base, **_clean(section)})
        # YAML reads an unquoted 4387 as an int, and the writer stores '4387': keyed by the string.
        self._own = {str(k): _clean(v) for k, v in (section.get('leagues') or {}).items() if isinstance(v, dict)}

    def of(self, league_tsdb_id) -> Rule:
        own = self._own.get(str(league_tsdb_id)) or {}
        d = self.default
        return Rule(go_by=own.get('go_by', d.go_by),
                    cover_both_minutes=own.get('cover_both_minutes', d.cover_both_minutes),
                    wait_hours=own.get('wait_hours', d.wait_hours), own=dict(own))

    def overrides(self) -> dict:
        """{TheSportsDB league id (str): its own answers}, only leagues that changed one."""
        return {k: dict(v) for k, v in self._own.items() if v}


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------

def tsdb_start(event):
    """TheSportsDB's start as this module goes by it, or None when it has no time to go by: no start,
    or a field event at exactly 00:00, TheSportsDB's placeholder (design 7). A game TheSportsDB
    moved after it had started keeps the start it started at (sports_match.kept_start,
    dev/changelog/1626)."""
    start = event.start_at
    if start is None or (not event_shape_is_matchup(event) and (start.hour, start.minute) == (0, 0)):
        return None
    if kept_start(event):
        return event.guide_start
    return start


def listed_start(event):
    """The guide's time for the game: the old time it still lists when it did not follow a move
    (dev/changelog/1623), else the start the matcher gave it. None before the matcher has found it."""
    return event.guide_behind_start or event.guide_start


def acts(listed, tsdb, rule: Rule, now: datetime) -> bool:
    """Whether the answer applies yet: TheSportsDB has a time, the guide has one, the answer is not
    the guide, and the earlier of the two is within the wait."""
    if rule.go_by == GO_GUIDE or tsdb is None or listed is None:
        return False
    return now >= min(tsdb, listed) - rule.wait


def running_late(event) -> timedelta:
    """How much later TheSportsDB now has a game that had started when it moved it (kept_start): its
    start keeps where it was, and its end goes later by the move (dev/changelog/1632). Zero for a game
    not under way, one moved earlier, or a move within AGREE. A rain delay entered at the green flag
    says the race ends that much later. A move past sports_match.MAX_DELAY is a new date and is not
    held, and a postponed game is not running late at all."""
    if (event.start_at is None or event.previous_start is None or event.postponed
            or not kept_start(event)):
        return timedelta(0)
    late = event.start_at - event.previous_start
    return late if late > AGREE else timedelta(0)


def name_late(event, start) -> timedelta:
    """running_late() for a recording made from an event channel's name at `start` (its program
    start, the pick's): how much later TheSportsDB now has the game, when it moved it after `start`
    had passed. An unlisted game never has the guide start kept_start() reads, and its
    previous_start is overwritten at each step of a delay entered in steps, so the recording's own
    start is the anchor, and the same answer comes back on every pass (dev/changelog/1643). Zero for
    a move read before `start`, one earlier or within AGREE, one past MAX_DELAY (a new date), and a
    postponed game."""
    if (start is None or event.start_at is None or event.postponed or event.start_changed_at is None
            or event.start_changed_at < start):
        return timedelta(0)
    late = event.start_at - start
    return late if AGREE < late <= MAX_DELAY else timedelta(0)


def windows(start, stop, listed, tsdb, rule: Rule, now: datetime, late: timedelta = timedelta(0)) -> list:
    """[(start, stop)] for one listing of a game, by the rule (module docstring). One window, or two
    under GO_BOTH when the starts are further apart than the limit and the windows do not touch.
    `late` (running_late) goes on the end of each window of a listing near the game's start under
    GO_TSDB and GO_BOTH; a replay and GO_GUIDE never take it."""
    if start is None or stop is None or not acts(listed, tsdb, rule, now) or abs(start - listed) > OFF_LIMIT:
        return [(start, stop)]
    return [(begin, end + late) for begin, end in _windows(start, stop, tsdb, rule)]


def _windows(start, stop, tsdb, rule: Rule) -> list:
    gap = tsdb - start
    # Past WINDOW the listing is not this game's at TheSportsDB's time: TheSportsDB postponed it, and
    # the matcher has not run since to say where the guide has it now. airing_now's query reach
    # relies on this bound.
    if abs(gap) <= AGREE or abs(gap) > WINDOW:
        return [(start, stop)]
    if rule.go_by == GO_TSDB:
        return [(tsdb, max(stop, stop + gap))]
    if rule.go_by == GO_BOTH:
        shifted_stop = stop + gap
        touch = tsdb <= stop and start <= shifted_stop
        if abs(gap) <= rule.limit or touch:
            return [(min(start, tsdb), max(stop, shifted_stop))]
        return [(start, stop), (tsdb, shifted_stop)]
    raise ValueError(f'unknown answer {rule.go_by!r}')


def event_windows(start, stop, event, rule: Rule, now: datetime) -> list:
    """windows() for a listing of `event`."""
    return windows(start, stop, listed_start(event), tsdb_start(event), rule, now, running_late(event))


def kickoff(event, rule: Rule, now: datetime, listed_fallback=None):
    """The start to name for a coming game: TheSportsDB's once the answer acts on it, the guide's
    before then and under GO_GUIDE, and whichever exists when only one does. `listed_fallback` is
    the earliest listing, for a game the matcher has not given a start yet."""
    tsdb, listed = tsdb_start(event), listed_start(event) or listed_fallback
    if listed is None:
        return tsdb if tsdb is not None else event.start_at
    if tsdb is None or not acts(listed, tsdb, rule, now):
        return listed
    return tsdb


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------

def problem(changes: dict, league: bool) -> str:
    """Why `changes` cannot be saved, or ''. A league may send None for an answer to use the
    default again; the default may not."""
    if not changes:
        return 'Nothing to change.'
    for key, value in changes.items():
        if key not in _FIELDS:
            return f'There is no setting called {key}.'
        if value is None and league:
            continue
        if key == 'go_by':
            if value not in GO_BY:
                return 'Choose TheSportsDB, Your guide or Cover both.'
            continue
        top = MAX_COVER_BOTH_MINUTES if key == 'cover_both_minutes' else MAX_WAIT_HOURS
        unit = 'minutes' if key == 'cover_both_minutes' else 'hours'
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= top:
            return f'Give a whole number of {unit} from 0 to {top}.'
    return ''


def set_default(changes: dict) -> Rule:
    """Save the default every league goes by. Raises ValueError with the reason when invalid."""
    why = problem(changes, league=False)
    if why:
        raise ValueError(why)
    with config_write_lock:
        _cfg, file_cfg = load_for_edit()
        for key, value in changes.items():
            set_nested(file_cfg, f'{_PATH}.{key}', value)
        save_config(file_cfg)
    return Rules().default


def _put_leagues(file_cfg: dict, leagues: dict):
    """Write the per-league answers into the file's dict. With none left, the key goes, and the
    game_times block with it when nothing else is in it, so config.yaml holds only what was chosen."""
    if leagues:
        set_nested(file_cfg, f'{_PATH}.leagues', leagues)
        return
    section = (file_cfg.get('sports_data') or {}).get('game_times')
    if isinstance(section, dict):
        section.pop('leagues', None)
        if not section:
            file_cfg['sports_data'].pop('game_times', None)


def set_league(league_tsdb_id, changes: dict) -> Rule:
    """Save one league's own answers; None for an answer goes back to the default. A league left
    with no answer of its own loses its entry. Raises ValueError with the reason when invalid."""
    why = problem(changes, league=True)
    if why:
        raise ValueError(why)
    key = str(league_tsdb_id)
    with config_write_lock:
        _cfg, file_cfg = load_for_edit()
        leagues = {str(k): dict(v) for k, v in (_section(file_cfg).get('leagues') or {}).items()
                   if isinstance(v, dict)}
        own = leagues.get(key, {})
        for field_name, value in changes.items():
            if value is None:
                own.pop(field_name, None)
            else:
                own[field_name] = value
        if own:
            leagues[key] = own
        else:
            leagues.pop(key, None)
        _put_leagues(file_cfg, leagues)
        save_config(file_cfg)
    return Rules().of(league_tsdb_id)


def forget_league(league_tsdb_id) -> bool:
    """Drop a deleted league's answers. Returns whether it had any. Stop following keeps them: the
    league stays in the list to be followed again."""
    key = str(league_tsdb_id)
    with config_write_lock:
        _cfg, file_cfg = load_for_edit()
        leagues = _section(file_cfg).get('leagues') or {}
        if key not in {str(k) for k in leagues}:
            return False
        _put_leagues(file_cfg, {str(k): v for k, v in leagues.items() if str(k) != key})
        save_config(file_cfg)
    return True
