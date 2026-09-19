"""The one declared list of channel health-score bands, and the one place they are read.

A health score is a 0-100 number (app/health_score.py). Turning that number into a word and
a color - Great / Good / Fair / Poor - used to be an inline `< 50 ? ... : < 80 ? ...` ternary
re-typed at eleven sites across Python, Jinja and JS, which is the "one flag, one meaning"
defect class in CLAUDE.md wearing a different hat: eleven copies of one rule cannot be
changed together, and the trailing `else` in each of them was the rendering of a real band,
so adding a fourth band would have landed in it silently at every site that was missed.

Everything here is pure - it takes a config dict and returns values. That matters because
banding happens inside per-row loops (a guide row, a search result, a group card): the bands
are resolved ONCE per request from the already-loaded config and handed down, never re-read
per row.

The band *set* is fixed at four and their order is fixed; only the cut points are
configurable (`channel_testing.health_bands`). That is deliberate. A user-defined list of
arbitrary bands would make it impossible for a CSS file or a branch chain to name every band
explicitly, which is the property that stops the next band added from being swallowed.

Shipped in dev/changelog/771.
"""
import logging

log = logging.getLogger(__name__)

GREAT = 'great'
GOOD = 'good'
FAIR = 'fair'
POOR = 'poor'

#: Highest band first. Every consumer that walks bands walks them in this order, so "the
#: first band whose floor the score reaches" is the answer without a second sort.
BAND_KEYS = (GREAT, GOOD, FAIR, POOR)

BAND_NAMES = {GREAT: 'Great', GOOD: 'Good', FAIR: 'Fair', POOR: 'Poor'}

#: Floors for the three configurable bands. POOR is always 0 - a band scale needs a bottom
#: that nothing can fall through, and making it configurable would create scores that belong
#: to no band at all.
DEFAULT_FLOORS = {GREAT: 90, GOOD: 80, FAIR: 50}

#: A channel that has never been tested. Not a band: it is the absence of a measurement, and
#: rendering it as the worst band would fabricate a judgment from no data (DESIGN.md 12.4).
UNTESTED = 'untested'
UNTESTED_LABEL = 'Never tested'

#: `channel_testing.failing_band` value meaning "no band counts as failing".
FAILING_NONE = 'none'
FAILING_VALUES = BAND_KEYS + (FAILING_NONE,)
DEFAULT_FAILING_BAND = POOR


class Band:
    """One band: its key, its display label, the score at which it starts, and the CSS
    modifier class that colors it (`.hb-great` and friends in static/css/style.css)."""

    __slots__ = ('key', 'label', 'floor', 'ceiling')

    def __init__(self, key, label, floor, ceiling):
        self.key = key
        self.label = label
        self.floor = floor
        #: Exclusive upper bound, or None for the top band. Only the label and the failing
        #: threshold need it; banding itself is floor-only.
        self.ceiling = ceiling

    @property
    def css(self):
        return f'hb-{self.key}'

    @property
    def name(self):
        """The bare band name ("Fair"), where `label` carries its range ("Fair (50-79)")."""
        return BAND_NAMES[self.key]

    def as_dict(self):
        """The wire shape handed to JS and to templates. `css` is included so no consumer
        rebuilds the class name by gluing a prefix to the key."""
        return {'key': self.key, 'name': self.name, 'label': self.label,
                'floor': self.floor, 'ceiling': self.ceiling, 'css': self.css}

    def __repr__(self):
        return f'<Band {self.key} {self.floor}..{self.ceiling}>'


def _label(key, floor, ceiling):
    name = BAND_NAMES[key]
    if ceiling is None:
        return f'{name} ({floor}+)'
    if floor <= 0:
        return f'{name} (under {ceiling})'
    return f'{name} ({floor}-{ceiling - 1})'


def _floors_from(cfg):
    """The three configured floors, or the defaults when what is configured cannot describe
    a band scale.

    Falling back is loud on purpose. A silently-repaired cut point would leave the settings
    page showing one number while every badge in the app used another, which is the exact
    "a number the user cannot explain" failure product principle 1 exists to prevent.
    """
    raw = (cfg.get('channel_testing') or {}).get('health_bands') or {}
    floors = {}
    for key in (GREAT, GOOD, FAIR):
        value = raw.get(key, DEFAULT_FLOORS[key])
        try:
            floors[key] = int(value)
        except (TypeError, ValueError):
            log.warning('channel_testing.health_bands.%s is %r, which is not a number - '
                        'falling back to the default band cut points %r', key, value,
                        DEFAULT_FLOORS)
            return dict(DEFAULT_FLOORS)
    problem = validate_floors(floors)
    if problem:
        log.warning('channel_testing.health_bands is unusable (%s) - falling back to the '
                    'default band cut points %r', problem, DEFAULT_FLOORS)
        return dict(DEFAULT_FLOORS)
    return floors


def validate_floors(floors):
    """Return a human-readable problem with these cut points, or '' if they are usable.

    Shared by the settings save path (which refuses the save and shows this string) and by
    `_floors_from` (which logs it and falls back), so a value the GUI rejects and a value
    hand-edited into config.yaml are judged by exactly one rule.
    """
    for key in (GREAT, GOOD, FAIR):
        value = floors.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            return f'{key} is not a whole number'
        if not 1 <= value <= 100:
            return f'{key} is {value}, outside 1-100'
    if not floors[GREAT] > floors[GOOD] > floors[FAIR]:
        return (f'the cut points must descend: great ({floors[GREAT]}) > good '
                f'({floors[GOOD]}) > fair ({floors[FAIR]})')
    return ''


def resolve_bands(cfg):
    """The four bands, highest first, for this config. Resolve once per request.

    Pure and cheap - no disk, no DB - so it is safe to call from a route or a context
    processor that has already loaded config, and never from inside a per-row loop.
    """
    floors = _floors_from(cfg)
    ordered = [(GREAT, floors[GREAT]), (GOOD, floors[GOOD]), (FAIR, floors[FAIR]), (POOR, 0)]
    bands = []
    ceiling = None
    for key, floor in ordered:
        bands.append(Band(key, _label(key, floor, ceiling), floor, ceiling))
        ceiling = floor
    return tuple(bands)


def band_for(score, bands):
    """The band key for one score, or UNTESTED for None.

    Every band is named explicitly by the loop - there is no trailing `else` rendering a
    real band, so a fifth band added to BAND_KEYS cannot land here silently. The return
    after the loop is reachable only for a score below zero, which no band claims: manual
    health adjustment is applied unclamped (see channel_search.effective_health), so a
    heavily-penalized channel can sit below the bottom band's floor.
    """
    if score is None:
        return UNTESTED
    for band in bands:
        if score >= band.floor:
            return band.key
    return POOR


def band_by_key(bands, key):
    for band in bands:
        if band.key == key:
            return band
    return None


def bands_payload(bands):
    """The full vocabulary as JSON-ready data: the four bands plus the untested pseudo-band.

    This is what base.html hands to JS and what /api/channel-search/meta serves, so the
    browser bands a score from the same declared list the server does rather than from a
    second copy of the numbers.
    """
    return {'bands': [b.as_dict() for b in bands],
            'untested': {'key': UNTESTED, 'name': UNTESTED_LABEL, 'label': UNTESTED_LABEL,
                         'css': 'hb-none'}}


def failing_band_key(cfg):
    """Which band the user has declared to count as failing, or FAILING_NONE."""
    value = (cfg.get('channel_testing') or {}).get('failing_band', DEFAULT_FAILING_BAND)
    if value in FAILING_VALUES:
        return value
    log.warning('channel_testing.failing_band is %r, which is not one of %r - treating it '
                'as %r', value, FAILING_VALUES, DEFAULT_FAILING_BAND)
    return DEFAULT_FAILING_BAND


def failing_threshold(cfg, bands=None):
    """The score below which a channel counts as failing, or None when nothing does.

    "Failing" is declared as a band ("Poor counts as failing"), because that is the
    vocabulary the rest of the app shows the user; the numeric threshold is derived from it
    rather than stored a second time. It is the floor of the band ABOVE the failing one -
    with `failing_band: poor` and the default cut points, a channel is failing below 50.
    """
    key = failing_band_key(cfg)
    if key == FAILING_NONE:
        return None
    if bands is None:
        bands = resolve_bands(cfg)
    index = BAND_KEYS.index(key)
    if index == 0:
        # The top band counts as failing, i.e. every channel does. Nonsensical but a legal
        # choice, and 101 is what "every possible score is below this" spells.
        return 101
    return bands[index - 1].floor


def band_is_failing(band_key, cfg):
    """Whether a banded score counts as failing - the band itself or any band below it.

    Needs no cut points: BAND_KEYS is ordered best-first, so "at or below the failing band"
    is an index comparison. UNTESTED never counts - no measurement is not a bad measurement.
    """
    if band_key not in BAND_KEYS:
        return False
    failing = failing_band_key(cfg)
    if failing == FAILING_NONE:
        return False
    return BAND_KEYS.index(band_key) >= BAND_KEYS.index(failing)
