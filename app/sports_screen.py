"""The picture a sports channel shows when it has no game to play: its waiting screen.

The look is dev/mockups/55's look 7, Matchup schedule (dev/changelog/1528, 1531): the next game in
its two teams' colors and badges, the later games as stripes. This module decides what is drawn;
the colors and badges come from TheSportsDB's team lookup (SportsTeam, app/sports_images.py).
"""
import logging
import math
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .fmt_utils import counted, fmt_bytes

log = logging.getLogger(__name__)

#: The sides of a team with no colors: graphite, a lighter one for home, so a game between two
#: teams TheSportsDB has no colors for still shows two sides. (first, second) as on a known team.
NEUTRAL_AWAY = ('#3a3f48', '#8d94a1')
NEUTRAL_HOME = ('#4b515c', '#9aa1ad')
#: A team's second color when it has only one.
NO_SECOND = '#ffffff'
#: Two colors nearer than this in RGB are too alike to tell two sides apart: Nottingham Forest's
#: red and Manchester United's, the Chiefs' and the Broncos' (measured in mockup 55).
CLOSE_DISTANCE = 90


@dataclass(frozen=True)
class SideColors:
    """A game's two sides, each (first, second): the first fills its half, the second edges its
    badge. `swapped` says home is in its second color because the first ones were too alike."""
    away: tuple
    home: tuple
    swapped: bool = False


def team_colors(team) -> list:
    """A SportsTeam's known colors in order, the unknown ones left out."""
    if team is None:
        return []
    return [c for c in (team.color1, team.color2, team.color3) if c]


def _rgb(color: str) -> tuple:
    n = int(color[1:], 16)
    return n >> 16, (n >> 8) & 255, n & 255


def colors_close(a: str, b: str) -> bool:
    return math.dist(_rgb(a), _rgb(b)) < CLOSE_DISTANCE


def side_colors(away: list, home: list) -> SideColors:
    """The two sides' colors from each team's known colors (team_colors()). A team with none is
    graphite. When both first colors are close, home switches to its second, the way a broadcast
    graphic would, unless that one is close to away's too."""
    away_side = (away[0], away[1] if len(away) > 1 else NO_SECOND) if away else NEUTRAL_AWAY
    home_side = (home[0], home[1] if len(home) > 1 else NO_SECOND) if home else NEUTRAL_HOME
    swapped = bool(away and len(home) > 1 and colors_close(away_side[0], home_side[0])
                   and not colors_close(away_side[0], home[1]))
    if swapped:
        home_side = (home[1], home[0])
    return SideColors(away_side, home_side, swapped)


def short_code(name: Optional[str], short: Optional[str] = None) -> str:
    """What a badge disc says when there is no badge to draw: TheSportsDB's short name, else the
    first three letters of the team's name, uppercased."""
    if short and short.strip():
        return short.strip().upper()
    letters = ''.join(ch for ch in (name or '') if ch.isalnum())
    return letters[:3].upper() or '?'


# ---------------------------------------------------------------------------
# What the screen shows
# ---------------------------------------------------------------------------

#: What a waiting screen is waiting for. Every value is named wherever a state is read.
STATE_NEXT = 'next'          # no game near; the next one is known
STATE_UNLISTED = 'unlisted'  # a game is near or on, but no channel's guide lists it
STATE_NONE = 'none'          # nothing in the schedule
STATE_HELD = 'held'          # the game is on and listed, but cannot start here yet (proxy only)
STATES = (STATE_NEXT, STATE_UNLISTED, STATE_NONE, STATE_HELD)

#: Why a game that is on cannot start on the screen yet (STATE_HELD).
HELD_NO_SLOT = 'no_slot'              # every "IPTV players at once" slot is in use
HELD_NO_CONNECTION = 'no_connection'  # every account listing it is out of connections
HELD_REASONS = (HELD_NO_SLOT, HELD_NO_CONNECTION)

#: The feed's two modes, spelled as app/output_feed.py's MODE_PROXY / MODE_REDIRECT.
MODE_PROXY = 'proxy'
MODE_REDIRECT = 'redirect'

#: ChannelBin's accent: a channel's color when nothing gives it one.
ACCENT = '#4f9cf7'

#: A league's own color, by TheSportsDB's league id, for a screen that would otherwise fall back
#: to the accent: a league's channel with no game to show, a field event's hero and stripes, a
#: team with no colors of its own. TheSportsDB's league lookup has no color (checked on 4370,
#: dev/changelog/1534), so each entry is the league's primary color as its own website draws it,
#: read on 2026-11-13, with where it was read beside it (no league published a brand guide). A
#: league with no entry gets the accent; a color is never guessed, so a league whose site gave
#: none, or only a parent brand's several colors (NASCAR's three series), has none.
LEAGUE_COLORS = {
    4370: '#E10600',   # Formula 1: formula1.com's logo SVG and its f1-red-500-brand token
    4371: '#6234E0',   # Formula E: fiaformulae.com CSS, --p-color-primary-1
    4373: '#DA291C',   # IndyCar Series: indycar.com design-system.css, the accent background
    4407: '#C80502',   # MotoGP: motogp.com screen.css, --g-color-brand-primary
    4409: '#FC4C02',   # WRC: wrc.com, --primary-color
    4410: '#E31E12',   # British GT: britishgt.com theme CSS, --color--primary
    4412: '#D80C18',   # Super GT: supergt.net, the 2026 series logo SVG
    4413: '#00B9FF',   # WEC: fiawec.com CSS, --bs-primary
    4436: '#E3000F',   # Moto2: motogp.com screen.css, the Moto2 schedule color
    4437: '#FF6B00',   # Moto3: motogp.com screen.css, the Moto3 schedule color
    4439: '#E31E12',   # GT World Challenge Europe Endurance Cup: gt-world-challenge-europe.com CSS, --color--primary
    4440: '#E31E12',   # GT World Challenge Europe Sprint Cup: the same
    4447: '#C4390E',   # Dakar Rally: dakar.com CSS, --primary-color
    4454: '#FF0000',   # WorldSBK: worldsbk.com screen.css, --championship-sbk
    4468: '#0072CE',   # AMA Supercross: supercrosslive.com theme CSS, the main accent
    4469: '#CE0E2D',   # Pro Motocross: promotocross.com, the championship logo SVG
    4486: '#004267',   # Formula 2: fiaformula2.com CSS, the F2 deep navy brand token
    4487: '#E35205',   # Formula 3: fiaformula3.com CSS, the F3 orange brand token
    4489: '#EE3123',   # Supercars: supercars.com CSS, .fill-primary
    4587: '#E2001A',   # MXGP: mxgp.com stylesheet, headings and links
    4712: '#02FD9C',   # Extreme E: extreme-e.com style.css, the main buttons
    4730: '#122246',   # World Rallycross: fia-rallycross.com, theme-color
    4732: '#A22424',   # Isle of Man TT: iomtt.com CSS, buttons and headers
    5094: '#D6002D',   # ARCA Menards Series: arcaracing.com (archived copy), the navigation
    5264: '#00A9E0',   # British Superbike: britishsuperbike.com, theme-color
    5309: '#DD3943',   # NHRA Drag Racing: nhra.com CSS, the menu highlight
    5382: '#6A00EB',   # F1 Academy: f1academy.com CSS, .f1-bg--primary
    5412: '#5425AF',   # SuperMotocross World Championship: supermotocross.com theme CSS, .btn-purple
    5600: '#0D4795',   # British Speedway Premiership: britishspeedway.co.uk, the theme's primary
    5873: '#00D200',   # WorldSSP: worldsbk.com screen.css, --championship-ssp
}


def league_color(league) -> str:
    """The color a screen uses for `league` (a SportsLeague) when nothing nearer gives one."""
    return LEAGUE_COLORS.get(getattr(league, 'tsdb_id', None)) or ACCENT


@dataclass(frozen=True)
class Side:
    """One team as the screen draws it."""
    name: str
    code: str
    colors: tuple
    #: The downloaded badge file, or None to draw the code in the team's color.
    badge: Optional[str] = None


@dataclass(frozen=True)
class Game:
    name: str
    day: str
    clock: str
    venue: str = ''
    #: None for a field event, such as a race: no sides to draw.
    away: Optional[Side] = None
    home: Optional[Side] = None

    @property
    def field(self) -> bool:
        return self.away is None or self.home is None


@dataclass(frozen=True)
class Words:
    """Everything the screen says in words. `ticker` is redirect mode's crawl, its sentences in
    order, and `ticker_label` the still label beside it; both empty in proxy mode."""
    kicker: str
    headline: str
    when: str
    note: str
    live: bool = False
    ticker_label: str = ''
    ticker: tuple = ()


@dataclass(frozen=True)
class Screen:
    """One waiting screen: what dev/mockups/55's look 7 draws, and nothing it does not, so a hash
    of it keys the clip (clip_key())."""
    channel: str
    #: The color of a one-color hero (no game, or a field event) and of a field event's stripes.
    color: str
    state: str
    mode: str
    words: Words
    game: Optional[Game] = None
    later: tuple = ()
    #: The channel's games are field events, for the hero with no game to show.
    field: bool = False


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def before_words(minutes: int) -> str:
    """How far ahead of a game its window opens, in a sentence: "an hour", "90 minutes"."""
    if minutes == 60:
        return 'an hour'
    if minutes and minutes % 60 == 0:
        return f'{minutes // 60} hours'
    return f'{minutes} minutes' if minutes != 1 else 'a minute'


def stop_by(start: datetime, before_minutes: int, now: datetime, tz, h24: bool) -> tuple:
    """(inline, label) for when redirect mode's stream stops ahead of a game starting at `start`:
    the game's start less the window's `before`. "6:00 PM" / "6:00 PM", "6:00 PM tomorrow" /
    "6:00 PM tomorrow", "6:00 PM on Sun Nov 15" / "Sun Nov 15 6:00 PM". It says "by": the guide
    listing the window really opens from may start earlier, or may not exist yet."""
    from .tz_utils import day_label, format_local
    opens = start - timedelta(minutes=before_minutes)
    clock = format_local(opens, 'clock', tz=tz, h24=h24)
    day = day_label(opens, now, tz)
    if day == 'Today':
        return clock, clock
    if day == 'Tomorrow':
        return f'{clock} tomorrow', f'{clock} tomorrow'
    return f'{clock} on {day}', f'{day} {clock}'


def words_for(state: str, mode: str, noun: str, *, carries: str = '', game: str = '', when: str = '',
              clock: str = '', started: bool = True, stops: tuple = None, before_minutes: int = 60,
              held: str = None, slots: int = 1) -> Words:
    """The screen's words, the one builder (dev/mockups/55-page.js::copyFor(), look 7's kicker).
    `noun` is the league's ("game", "race"), so a race is never a game. `game` is the game's
    name, `when` its day and time, `clock` its time alone, `started` whether it has, `stops`
    stop_by()'s answer for redirect mode's next game, `held` and `slots` what keeps a game that is
    on from starting here (STATE_HELD)."""
    if state not in STATES:
        raise ValueError(f'Unknown waiting-screen state "{state}"')
    if mode not in (MODE_PROXY, MODE_REDIRECT):
        raise ValueError(f'Unknown feed mode "{mode}"')
    proxy = mode == MODE_PROXY
    cant = f"You're using redirect mode, so ChannelBin can't tune you in to the {noun}"
    if state == STATE_NONE:
        return Words(
            kicker=f'No {noun}s scheduled', headline=carries, when='',
            note=(f'TheSportsDB lists no upcoming {noun}s for this channel. The season may be over, '
                  f'or the next {noun}s are not listed yet.'),
            ticker_label='' if proxy else 'Redirect mode',
            ticker=() if proxy else (
                f"You're using redirect mode, so ChannelBin can't tune you in to a {noun} by itself",
                f'When one is scheduled, tune in to this channel again at {noun} time'))
    on_when = f'Started {clock}' if started else f'Starts {clock}'
    if state == STATE_UNLISTED:
        return Words(
            kicker=('On now' if started else 'Starting soon') + ' - no stream found yet',
            headline=game, when=on_when, live=True,
            note=("None of your channels' guides lists it right now. "
                  + ('It starts here by itself the moment one does.' if proxy
                     else 'This stream stops when one does.')),
            ticker_label='' if proxy else 'This stream will stop',
            ticker=() if proxy else (
                f'{cant} when a stream for it turns up',
                f"This stream stops as soon as one of your channels' guides lists {game}",
                'Then tune in to this channel again to watch it'))
    if state == STATE_HELD:
        if not proxy:
            raise ValueError('A held game is proxy mode only: redirect mode never starts a game here')
        if held == HELD_NO_SLOT:
            note = ("Another IPTV player is using the IPTV output's one slot. "
                    'It starts here by itself when that one stops.' if slots == 1 else
                    f"All {slots} of the IPTV output's slots are in use. "
                    'It starts here by itself when one is free.')
        elif held == HELD_NO_CONNECTION:
            note = ('Every account whose guide lists it is using all of its connections right now. '
                    'It starts here by itself when one frees up.')
        else:
            raise ValueError(f'Unknown reason a game is held "{held}"')
        return Words(kicker=('On now' if started else 'Starting soon') + " - can't start here yet",
                     headline=game, when=on_when, live=True, note=note)
    inline, label = stops if stops else ('', '')
    ahead = (f'{before_words(before_minutes)} before the {noun}' if before_minutes
             else f'when the {noun} starts')
    return Words(
        kicker=f'No {noun} on right now - next {noun}', headline=game, when=when,
        note=(f'It starts here by itself at {noun} time. You can leave this on.' if proxy
              else f'This stream stops by {inline}. Tune in again then.'),
        ticker_label='' if proxy else f'Stops by {label}',
        ticker=() if proxy else (
            f'{cant} when it starts',
            f'This stream stops by {inline}, {ahead}',
            f'Then tune in to this channel again to watch {game}'))


def carries(team_name: Optional[str], league: str, number: int, noun: str) -> str:
    """What a sports channel carries, as the headline when it has nothing scheduled."""
    if team_name:
        return f'Every {team_name} {noun}'
    if number <= 1:
        return f'{league} {noun}s'
    others = {2: 'another', 3: 'two others', 4: 'three others'}.get(number, f'{number - 1} others')
    return f'The {league} {noun}s that overlap {others}'


# ---------------------------------------------------------------------------
# A served channel's screen, from the database
# ---------------------------------------------------------------------------

def _side(name: str, team, colors: tuple, cfg: dict, badges: dict) -> Side:
    code = short_code(name or (team.full_name if team else ''), team.short_name if team else None)
    url = team.badge_url if team is not None else None
    if url and url not in badges:
        from .sports_images import badge_path
        badges[url] = badge_path(cfg, url)
    return Side(name or (team.full_name if team else '') or code, code, colors, badges.get(url) if url else None)


def _game(event, teams: dict, cfg: dict, now: datetime, tz, h24: bool, badges: dict) -> Game:
    from .sports_data import event_game_name, event_shape_is_matchup
    from .sports_view import event_when
    day, clock = event_when(event, now, tz, h24)
    venue = ', '.join(x for x in (event.venue, event.city) if x)
    if not event_shape_is_matchup(event):
        return Game(event_game_name(event), day, clock, venue)
    away_team, home_team = teams.get(event.away_team_id), teams.get(event.home_team_id)
    sides = side_colors(team_colors(away_team), team_colors(home_team))
    return Game(event_game_name(event), day, clock, venue,
                _side(event.away_name, away_team, sides.away, cfg, badges),
                _side(event.home_name, home_team, sides.home, cfg, badges))


def screen_for(served, playing, now: datetime, cfg: dict, *, mode: str, before_minutes: int,
               held: str = None, slots: int = 1, events: list = None) -> Screen:
    """The waiting screen of `served` (a sports_feed.Served) at `now`, from `playing`, its
    sports_feed.playing_now() answer. A game `playing` names with channels to play it from is on:
    the screen shows it only when it cannot start here (`held`, a HELD_* reason). One it names
    with no channel is the unlisted one; with none named, the next game not yet over is next.
    `events` is the channel's sports_feed.screen_events() list when the caller already has it.
    A handful of queries: the next games, their teams, and each badge's row."""
    from .database import SportsTeam
    from .sports_feed import screen_events
    from .sports_view import _noun
    from .tz_utils import get_display_tz, is_24h
    tz, h24 = get_display_tz(), is_24h()
    if events is None:
        events = screen_events([served], now).get(served.row.id, [])
    shown = playing.event if playing is not None and playing.event is not None else None
    if shown is not None and playing.channels and not held:
        raise ValueError('A game with a channel to play it from is on: there is no waiting screen for it')
    if shown is None and held:
        raise ValueError('A held game needs the game playing_now() found')
    ordered = ([shown] + [e for e in events if e.id != shown.id]) if shown is not None else list(events)
    team_ids = {i for e in ordered for i in (e.home_team_id, e.away_team_id) if i}
    teams = ({t.tsdb_id: t for t in SportsTeam.query.filter(SportsTeam.tsdb_id.in_(team_ids)).all()}
             if team_ids else {})
    one = _noun(served.league, ordered)[0]
    badges = {}
    games = [_game(e, teams, cfg, now, tz, h24, badges) for e in ordered[:SCREEN_GAMES_DRAWN]]
    team_name = served.name if served.is_team else None
    if team_name is not None:
        team = teams.get(served.row.team_tsdb_id)
        color = (team_colors(team) or [league_color(served.league)])[0]
    elif games and not games[0].field:
        color = games[0].home.colors[0]
    else:
        color = league_color(served.league)
    field = games[0].field if games else served.league.sport == 'Motorsport'
    if shown is not None:
        state = STATE_HELD if held else STATE_UNLISTED
        start = shown.guide_start or shown.start_at
        words = words_for(state, mode, one, game=games[0].name, clock=games[0].clock,
                          started=start is not None and start <= now, held=held, slots=slots)
    elif games:
        state = STATE_NEXT
        start = ordered[0].guide_start or ordered[0].start_at
        words = words_for(state, mode, one, game=games[0].name, when=f'{games[0].day} {games[0].clock}'.strip(),
                          stops=stop_by(start, before_minutes, now, tz, h24), before_minutes=before_minutes)
    else:
        state = STATE_NONE
        words = words_for(state, mode, one, carries=carries(team_name, served.league.name, served.row.number, one))
    return Screen(served.name, color, state, mode, words, games[0] if games else None, tuple(games[1:]), field)


#: The next game and the three stripes under it.
SCREEN_GAMES_DRAWN = 4


# ---------------------------------------------------------------------------
# The clip cache
# ---------------------------------------------------------------------------

#: Part of every clip's key: raise it when the drawing or the encode changes, so no clip made the
#: old way is served again.
LAYOUT_VERSION = 1
#: A clip nothing has asked for in this long is pruned (unless a live session plays it).
CLIP_KEEP = timedelta(days=1)
#: The longest a make may take before it is stopped as stuck. A 30 s clip takes about 10 s here.
MAKE_TIMEOUT_SECONDS = 180
#: The quick still: the picture with nothing moving, made in under a second, for a tune-in to
#: play while the moving clip is made (a whole number of draw.LOOP_UNITs).
STILL_FRAMES = 64

_CLIP_RE = re.compile(r'^(\d+)-([0-9a-f]{20})(?:-still)?\.ts$')


class ClipFailed(Exception):
    """A clip could not be made. The message names why, for a log line or a surface."""


def clip_key(screen: Screen) -> str:
    """A hash of everything drawn: the words, the names, the colors, the layout, and each badge
    file as it is on disk now, so a stale clip is never served and a day word that changes at
    midnight is simply a new key."""
    import hashlib
    parts = [f'layout {LAYOUT_VERSION}', repr(screen)]
    for game in ([screen.game] if screen.game else []) + list(screen.later):
        for side in (game.away, game.home):
            if side is not None and side.badge:
                try:
                    st = os.stat(side.badge)
                    parts.append(f'{side.badge} {st.st_size} {int(st.st_mtime)}')
                except OSError:
                    parts.append(f'{side.badge} gone')
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest()[:20]


def clip_dir(cfg: dict) -> str:
    from .storage_dirs import SPORTS_SCREENS, image_dir
    return image_dir(cfg, SPORTS_SCREENS)


def clip_path(cfg: dict, row_id: int, key: str, still: bool = False) -> str:
    """Where a clip of channel row `row_id` is kept: the row id first, so a channel's clips can be
    found without reading them."""
    return os.path.join(clip_dir(cfg), f'{int(row_id)}-{key}{"-still" if still else ""}.ts')


def ready_clip(cfg: dict, row_id: int, screen: Screen, still: bool = False) -> Optional[str]:
    """The made clip of `screen`, or None. Marks it asked for, which is what keeps it from the
    prune."""
    path = clip_path(cfg, row_id, clip_key(screen), still)
    try:
        os.utime(path)
    except OSError:
        return None
    return path


_making_lock = threading.Lock()
_making = {}        # clip path -> threading.Event set when its one maker finishes
_procs = set()      # every make's ffmpeg while it runs, for stop_makes()


def make_clip(cfg: dict, row_id: int, screen: Screen, *, still: bool = False,
              background: bool = False) -> str:
    """The clip of `screen`, made now unless it already is, and its path. One maker per clip at a
    time: a second caller waits for the first's and gets its answer. `background` runs ffmpeg at
    a low priority, for clips made ahead. Raises ClipFailed, naming why, when none can be made.
    Never call it holding a lock or inside a retry_on_locked unit: it spawns ffmpeg and waits."""
    path = ready_clip(cfg, row_id, screen, still)
    if path:
        return path
    path = clip_path(cfg, row_id, clip_key(screen), still)
    with _making_lock:
        done = _making.get(path)
        mine = done is None
        if mine:
            done = _making[path] = threading.Event()
    if not mine:
        done.wait(MAKE_TIMEOUT_SECONDS + 30)
        if os.path.exists(path):
            return path
        raise ClipFailed(f'the waiting screen for {screen.channel} could not be made (see the log)')
    try:
        render_clip(cfg, screen, path, still=still, background=background)
    finally:
        with _making_lock:
            _making.pop(path, None)
        done.set()
    return path


def render_clip(cfg: dict, screen: Screen, path: str, *, still: bool = False, background: bool = False):
    """Make the clip of `screen` at `path`, uncached: make_clip() is the cache's way in, and
    dev/tools/sports_screen.py renders a sample with this. Raises ClipFailed."""
    import shutil
    import tempfile
    from . import sports_screen_draw as draw
    from .proc_utils import build_screen_clip_cmd, read_stderr_tail, stderr_cause, terminate_or_kill
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        work = tempfile.mkdtemp(prefix='.make-', dir=folder)
    except OSError as exc:
        raise ClipFailed(f'the waiting screen folder {folder} cannot be written ({exc.strerror or exc})') from exc
    started = time.monotonic()
    try:
        if still:
            pictures, graph, frames = draw.still_layers(screen, work), draw.still_graph(), STILL_FRAMES
        else:
            spec = draw.layers(screen, work)
            pictures, graph, frames = [spec['base']] + draw.layer_inputs(spec), draw.filtergraph(spec), spec['frames']
        drawn = time.monotonic() - started
        part = os.path.join(work, 'clip.ts')
        cmd = build_screen_clip_cmd(cfg, pictures, graph, frames, draw.FPS, part)
        nice = shutil.which('nice') if background else None
        if nice:
            cmd = [nice, '-n', '10'] + cmd
        stderr_path = os.path.join(work, 'ffmpeg.log')
        with open(stderr_path, 'wb') as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
        with _making_lock:
            _procs.add(proc)
        try:
            code = proc.wait(timeout=MAKE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            terminate_or_kill(proc)
            raise ClipFailed(f'making the waiting screen for {screen.channel} took longer than '
                             f'{MAKE_TIMEOUT_SECONDS} s and was stopped')
        finally:
            with _making_lock:
                _procs.discard(proc)
            if proc.poll() is None:
                terminate_or_kill(proc)
        if code != 0 or not os.path.getsize(part):
            cause = stderr_cause(read_stderr_tail(stderr_path)) or f'ffmpeg exited with code {code}'
            raise ClipFailed(f'ffmpeg could not make the waiting screen for {screen.channel}: {cause}')
        os.replace(part, path)
        log.info('Made the %swaiting screen for %s (%s, %s mode): %d frames, %s, in %.1f s (%.1f s drawing)',
                 'still ' if still else '', screen.channel, screen.state, screen.mode, frames,
                 fmt_bytes(os.path.getsize(path)), time.monotonic() - started, drawn)
    except ClipFailed as exc:
        log.warning('%s', exc)
        raise
    except (OSError, ValueError) as exc:
        log.warning('The waiting screen for %s could not be made: %s', screen.channel, exc)
        raise ClipFailed(f'the waiting screen for {screen.channel} could not be made: {exc}') from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)


def stop_makes():
    """Stop every make's ffmpeg: the teardown rule's half for a make, called from
    output_feed.stop_all(). Killed outright: a clip stopped halfway is thrown away, so there is
    nothing a graceful stop would finish. Its make_clip() caller then raises ClipFailed."""
    from .proc_utils import terminate_or_kill
    with _making_lock:
        procs = list(_procs)
    for proc in procs:
        terminate_or_kill(proc, hard=True)


def discard_clip(path: str) -> bool:
    """Delete one clip that is no longer any channel's current screen and that nothing plays
    (app/output_feed.py decides both). The prune is the backstop for whatever this misses."""
    if not _CLIP_RE.match(os.path.basename(path or '')):
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning('Could not delete the old waiting screen %s: %s', path, exc)
        return False
    return True


def prune_clips(cfg: dict, keep=(), now: datetime = None) -> int:
    """Delete the clips nothing has asked for in CLIP_KEEP, except `keep` (the paths live
    sessions play), every clip of a channel row that no longer exists, and a make's leftovers an
    hour old. Returns how many files went. One query."""
    import shutil
    from .database import SportsFeedChannel
    folder = clip_dir(cfg)
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return 0
    rows = {i for (i,) in SportsFeedChannel.query.with_entities(SportsFeedChannel.id).all()}
    keep = set(keep)
    now_ts = time.time() if now is None else (now - datetime(1970, 1, 1)).total_seconds()
    gone = 0
    for name in names:
        path = os.path.join(folder, name)
        try:
            age = now_ts - os.stat(path).st_mtime
        except OSError:
            continue
        if name.startswith('.make-'):
            if age > 3600:
                shutil.rmtree(path, ignore_errors=True)
            continue
        found = _CLIP_RE.match(name)
        if found is None or path in keep:
            continue
        if int(found.group(1)) in rows and age <= CLIP_KEEP.total_seconds():
            continue
        try:
            os.unlink(path)
            gone += 1
        except OSError as exc:
            log.warning('Could not delete the old waiting screen %s: %s', path, exc)
    if gone:
        log.info('Pruned %s', counted(gone, 'old waiting-screen clip'))
    return gone
