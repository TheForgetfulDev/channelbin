"""The words and lengths that keep a guide listing from counting as a league's game or race
(dev/docs/DESIGN-sports-data.md §17, dev/changelog/1634).

A listing that names the league, and for a game both teams, is still not the game when it says
"highlights" or "pregame", or is shorter than any real airing of one: a highlights show, a pre-race
show and a driver's onboard camera all name the race too. The same words keep an event channel's
name from counting.

The built-in lists are here, written as plain phrases a person can read and change. A person
changes them per league on its page, and only the changes are stored, in config.yaml under
sports_data.skip_words.leagues keyed by TheSportsDB's league id: `added` and `removed` words and
`minutes` for any length changed. A word a release adds to a built-in list therefore still reaches
a league someone edited. Written only through add_word(), remove_word(), set_minutes(), restore()
and forget_league(), each under config_write_lock across its whole read-modify-write; every write
asks for a matcher run.

A word is read the way the matcher reads text (sports_match.normalize): whole words, ignoring
case, accents and punctuation. A space in a word also matches none, so "pre game" catches
"Pregame" and "on board" catches "Onboard", as the patterns this list replaced did.
"""
import re
from dataclasses import dataclass, field

from .config import config_write_lock, load_config, load_for_edit, save_config, set_nested

#: Which list a league reads: a game's (matchups) or a race's (field events, sessions included).
KIND_GAME = 'game'
KIND_RACE = 'race'
KINDS = (KIND_GAME, KIND_RACE)

#: The lengths, each read where a listing of that kind is judged.
LENGTH_GAME = 'game'
LENGTH_RACE = 'race'
LENGTH_SESSION = 'session'
LENGTHS = (LENGTH_GAME, LENGTH_RACE, LENGTH_SESSION)
#: A session's listing is shorter than a race's: prod's F1 practice listings run 55 to 115 minutes
#: and the shortest real session listing was 30; a 7-minute pre-show stays out (dev/changelog/1633).
BUILTIN_MINUTES = {LENGTH_GAME: 90, LENGTH_RACE: 120, LENGTH_SESSION: 30}
MAX_MINUTES = 240
MAX_WORD = 60

BUILTIN_WORDS = {
    KIND_GAME: ('highlight', 'highlights', 'preview', 'pre game', 'post game', 'betting', 'odds', 'recap',
                'condensed', 'countdown', 'in game', 'analysis'),
    # The shows around a session (`Formula 1 Pre Qualifying`, `Las Vegas F1 GP: Qualifying Hlts`); a
    # race's side feeds, never its main one, which is what is recorded and played, as prod's providers
    # name them (`IT: F1 TV PIT LANE`, `Live | Onboard | Formel 1 ...`, `Driver Tracker`, `F1 DATA
    # CHANNEL`; dev/changelog/1627); and an event channel's slate for a session still to come, listed
    # every few hours for days (`Next EVENT: Las Vegas Grand Prix Practice-1 on 2026-11-19 at 07:30PM
    # EST`, dev/changelog/1633).
    KIND_RACE: ('qualifier', 'pole', 'classic', 'classics', 'highlight', 'highlights', 'hlts', 'best of',
                'countdown', 'preview', 'recap', 'pre race', 'pre qualifying', 'pre quali', 'pre practice',
                'post race', 'post qualifying', 'post quali', 'post practice', 'on board', 'driver tracker',
                'driver data', 'driver cam', 'driver cams', 'driver camera', 'driver cameras', 'data channel',
                'pit lane', 'next event'),
}
#: Built in for one league only, by TheSportsDB's league id. Formula 1 (4370): F1 Academy is another
#: series whose listings say F1 too (82 channels of `Live F1 Academy: Qualifying` landed on Formula 1's
#: qualifying on prod), and guides spell it both ways (`F1 Académie`).
LEAGUE_WORDS = {'4370': {'words': ('academy', 'academie'),
                         'why': 'F1 Academy is another series, and its listings say F1 too.'}}

_PATH = 'sports_data.skip_words.leagues'


def clean_word(typed) -> str:
    """A word as matching reads it, or '' when nothing is left."""
    from .sports_match import normalize
    return normalize(str(typed or ''))[:MAX_WORD].strip()


def builtin_words(kind: str, league_tsdb_id) -> tuple:
    if kind not in KINDS:
        raise ValueError(f'unknown list {kind!r}')
    return BUILTIN_WORDS[kind] + league_words(league_tsdb_id)


def league_words(league_tsdb_id) -> tuple:
    """The words built in for this league alone."""
    return (LEAGUE_WORDS.get(str(league_tsdb_id)) or {}).get('words', ())


def _pattern(words) -> re.Pattern | None:
    if not words:
        return None
    parts = sorted((' ?'.join(re.escape(t) for t in w.split()) for w in words), key=len, reverse=True)
    return re.compile(r'(?<!\S)(?:' + '|'.join(parts) + r')(?!\S)')


@dataclass(frozen=True)
class Skips:
    """One league's words and lengths, with its own changes. Build them once per matcher run or
    page, through SkipRules, and ask them per row: they never read the config again."""
    league_tsdb_id: str
    added: tuple = ()
    removed: tuple = ()
    own_minutes: dict = field(default_factory=dict)

    def __post_init__(self):
        for kind in KINDS:
            object.__setattr__(self, f'_{kind}_words', self.words(kind))
            object.__setattr__(self, f'_{kind}_re', _pattern(self.words(kind)))

    def words(self, kind: str) -> tuple:
        """The words in force for `kind`: the built-ins still on, then the ones added."""
        kept = tuple(w for w in builtin_words(kind, self.league_tsdb_id) if w not in self.removed)
        return kept + tuple(w for w in self.added if w not in kept)

    def says(self, kind: str, text: str) -> bool:
        """Whether normalized text says one of `kind`'s words."""
        pattern = getattr(self, f'_{kind}_re')
        return pattern is not None and pattern.search(text) is not None

    def said(self, kind: str, text: str) -> list:
        """Every one of `kind`'s words that normalized text says, in list order."""
        if not self.says(kind, text):
            return []
        return [w for w in getattr(self, f'_{kind}_words') if _pattern((w,)).search(text)]

    def minutes(self, length: str) -> int:
        if length not in LENGTHS:
            raise ValueError(f'unknown length {length!r}')
        return self.own_minutes.get(length, BUILTIN_MINUTES[length])

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.own_minutes)


def _clean(raw: dict) -> dict:
    """The changes in `raw` that are valid, dropping any other: a hand-edited config.yaml with a typo
    falls back to the built-in for that entry rather than failing the matcher."""
    def words(key):
        out = []
        for w in raw.get(key) or []:
            w = clean_word(w) if isinstance(w, str) else ''
            if w and w not in out:
                out.append(w)
        return tuple(out)
    minutes = {}
    for k, v in (raw.get('minutes') or {}).items():
        if k in LENGTHS and isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_MINUTES:
            minutes[k] = v
    return {'added': words('added'), 'removed': words('removed'), 'own_minutes': minutes}


def _leagues(cfg: dict) -> dict:
    section = ((cfg.get('sports_data') or {}).get('skip_words') or {}).get('leagues') or {}
    # YAML reads an unquoted 4370 as an int, and the writer stores '4370': keyed by the string.
    return {str(k): v for k, v in section.items() if isinstance(v, dict)} if isinstance(section, dict) else {}


class SkipRules:
    """Every league's words and lengths from one config read."""

    def __init__(self, cfg: dict = None):
        self._own = {k: _clean(v) for k, v in _leagues(cfg if cfg is not None else load_config()).items()}
        self._built = {}

    def of(self, league_tsdb_id) -> Skips:
        key = str(league_tsdb_id)
        got = self._built.get(key)
        if got is None:
            got = self._built[key] = Skips(key, **self._own.get(key, {}))
        return got


#: The built-in lists alone, for a caller with no config to hand. Never the matcher's: a run reads
#: the person's changes through SkipRules.
BUILTIN = SkipRules({})


# ---------------------------------------------------------------------------
# What a skipped listing is told
# ---------------------------------------------------------------------------

def reason(skips: Skips, said: list, minutes: int, length: str, one: str) -> str:
    """The sentence the listings dialog gives a listing kept out, or '' when nothing keeps it out:
    every word it says, which of them a person added, and its length when it is too short. `one` is
    what the page calls the event (game, race, event), from sports_view._noun()."""
    parts = []
    if said:
        named = [f'"{w}"' + (', a word you added' if w in skips.added else '') for w in said]
        parts.append('It says ' + (', '.join(named[:-1]) + ' and ' + named[-1] if len(named) > 1 else named[0]))
    need = skips.minutes(length)
    if minutes < need:
        what = ('a practice, qualifying or sprint listing' if length == LENGTH_SESSION
                else f'{"an" if one[:1] in "aeiou" else "a"} {one} listing')
        parts.append(f'{"and it is" if said else "It is"} {minutes} minutes long, shorter than the {need} {what} needs')
    return ', '.join(parts) + '.' if parts else ''


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------

def _rewrite(league_tsdb_id, change) -> Skips:
    """Apply `change(own)` to one league's stored changes and save; a league left with none loses its
    entry, and the block goes when no league has any, so config.yaml holds only what was chosen.
    `change` raises ValueError to refuse, before anything is written."""
    key = str(league_tsdb_id)
    with config_write_lock:
        _cfg, file_cfg = load_for_edit()
        leagues = {k: dict(v) for k, v in _leagues(file_cfg).items()}
        now = _clean(leagues.get(key, {}))
        own = {'added': list(now['added']), 'removed': list(now['removed']), 'minutes': dict(now['own_minutes'])}
        change(own)
        own = {k: v for k, v in own.items() if v}
        if own:
            leagues[key] = own
        else:
            leagues.pop(key, None)
        _put(file_cfg, leagues)
        save_config(file_cfg)
    _rematch('a league\'s skip words or lengths changed')
    return SkipRules().of(league_tsdb_id)


def _put(file_cfg: dict, leagues: dict):
    if leagues:
        set_nested(file_cfg, _PATH, leagues)
        return
    data = file_cfg.get('sports_data')
    section = data.get('skip_words') if isinstance(data, dict) else None
    if isinstance(section, dict):
        section.pop('leagues', None)
        if not section:
            data.pop('skip_words', None)


def _rematch(reason_text: str):
    from .scheduler import queue_sports_match
    queue_sports_match(reason=reason_text)


def add_word(league_tsdb_id, kind: str, typed) -> tuple:
    """(Skips, the word as stored). A built-in word a person removed is put back rather than added
    twice. Raises ValueError with the reason when it cannot be added."""
    word = clean_word(typed)
    if not word:
        raise ValueError('Type a word: letters or numbers.')
    builtin = builtin_words(kind, league_tsdb_id)

    def change(own):
        if word in own['removed']:
            own['removed'].remove(word)
        elif word in own['added'] or word in builtin:
            raise ValueError(f'"{word}" is already on the list.')
        else:
            own['added'].append(word)
    return _rewrite(league_tsdb_id, change), word


def remove_word(league_tsdb_id, kind: str, word) -> Skips:
    """Take a word off a league's list: an added one is forgotten, a built-in one is stored as
    removed. Raises ValueError when it is not on the list."""
    word = clean_word(word)
    builtin = builtin_words(kind, league_tsdb_id)

    def change(own):
        if word in own['added']:
            own['added'].remove(word)
        elif word in builtin and word not in own['removed']:
            own['removed'].append(word)
        else:
            raise ValueError(f'"{word}" is not on the list.')
    return _rewrite(league_tsdb_id, change)


def set_minutes(league_tsdb_id, length: str, value) -> Skips:
    """A league's own shortest listing for `length`; None goes back to the built-in."""
    if length not in LENGTHS:
        raise ValueError(f'There is no length called {length}.')
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                              or not 0 <= value <= MAX_MINUTES):
        raise ValueError(f'Give a whole number of minutes from 0 to {MAX_MINUTES}.')

    def change(own):
        if value is None:
            own['minutes'].pop(length, None)
        else:
            own['minutes'][length] = value
    return _rewrite(league_tsdb_id, change)


def restore(league_tsdb_id) -> Skips:
    """Every added word gone, every removed built-in back, every length the built-in."""
    def change(own):
        own.clear()
    return _rewrite(league_tsdb_id, change)


def forget_league(league_tsdb_id) -> bool:
    """Drop a deleted league's changes, without a matcher run. Returns whether it had any. Stop
    following keeps them: the league stays in the list to be followed again."""
    key = str(league_tsdb_id)
    with config_write_lock:
        _cfg, file_cfg = load_for_edit()
        leagues = _leagues(file_cfg)
        if key not in leagues:
            return False
        _put(file_cfg, {k: v for k, v in leagues.items() if k != key})
        save_config(file_cfg)
    return True
