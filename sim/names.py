"""Invented names, decorated the way real lineups decorate theirs.

Every stem is assembled from syllables that belong to no network, so no generated lineup can
match or fingerprint a real one. The decoration around a stem - a country code and its
separator, quality and superscript marks, event slots, separator rows - is generic
vocabulary, and how often each appears comes from the calibration profile.
"""
from .rand import Weighted

_ONSETS = ('b', 'br', 'c', 'cl', 'd', 'dr', 'f', 'g', 'gr', 'h', 'k', 'l', 'm', 'n', 'p', 'qu',
           'r', 's', 'st', 't', 'tr', 'v', 'w', 'z')
_VOWELS = ('a', 'e', 'i', 'o', 'u', 'ai', 'ea', 'oa')
_CODAS = ('', 'l', 'n', 'r', 's', 'x', 'nd', 'rk', 'lt', 'm')

COUNTRIES = ('US', 'UK', 'CA', 'AU', 'IE', 'DE', 'FR', 'ES', 'IT', 'NL', 'PT', 'MX', 'BR', 'IN')
# The larger lineups are dominated by a few countries; the order above is that ranking.
_COUNTRY_WEIGHTS = (40, 25, 8, 5, 3, 3, 3, 3, 2, 2, 2, 2, 1, 1)

GENRES = ('News', 'Sports', 'Movies', 'Cinema', 'Kids', 'Music', 'Docs', 'Life', 'Drama',
          'Comedy', 'Action', 'Family', 'Nature', 'Travel', 'Food', 'Weather', 'Business',
          'Classic', 'Gold', 'One', 'Two', 'Max', 'Local', 'World')
SPORT_WORDS = ('FOOTBALL', 'BASKETBALL', 'HOCKEY', 'BASEBALL', 'SOCCER', 'TENNIS', 'BOXING',
               'RUGBY', 'CRICKET', 'GOLF', 'RACING', 'FIGHT NIGHT')
_REGIONS = ('EAST', 'WEST', 'PACIFIC', 'CENTRAL', 'LOCAL', 'NORTH', 'SOUTH')
_QUALITY = ('HD', 'FHD', 'UHD', '4K', 'SD', 'HEVC', '1080P', '720P', 'HDR', '50 FPS')
_QUALITY_WEIGHTS = (40, 20, 6, 8, 10, 4, 4, 3, 2, 3)
_SUPERSCRIPT = ('ᴴᴰ', 'ᶠᴴᴰ', 'ᵁᴴᴰ', 'ˢᴰ', '⁴ᴷ')
_SUPERSCRIPT_WEIGHTS = (50, 25, 8, 10, 7)
_TIERS = ('VIP', 'BACKUP', 'ALT')
# Words a channel's own name sometimes carries after its genre.
NAME_TAILS = ('TV', 'Network', 'Channel', 'Live', 'Plus', 'Extra', 'East', 'West', 'Prime')
_SYMBOLS = ('⚽', '🎬', '📺', '🎵', '🏀', '🏒', '🎾', '📰', '⭐')
_SEPARATOR_CHARS = ('#', '=', '-', '*')

# Cyrillic letters stand in for each latin onset and vowel, so a stem in another script is
# still invented and still pronounceable.
_CYRILLIC = str.maketrans({'a': 'а', 'b': 'б', 'c': 'к', 'd': 'д', 'e': 'е', 'f': 'ф',
                           'g': 'г', 'h': 'х', 'i': 'и', 'k': 'к', 'l': 'л', 'm': 'м',
                           'n': 'н', 'o': 'о', 'p': 'п', 'q': 'к', 'r': 'р', 's': 'с',
                           't': 'т', 'u': 'у', 'v': 'в', 'w': 'в', 'x': 'кс', 'z': 'з'})

_PREFIX_STYLES = ('code_colon', 'code_pipe', 'code_then_paren', 'leading_colon',
                  'code_bracketed', 'code_dash', 'none')

_country = Weighted(zip(COUNTRIES, _COUNTRY_WEIGHTS))
_quality = Weighted(zip(_QUALITY, _QUALITY_WEIGHTS))
_superscript = Weighted(zip(_SUPERSCRIPT, _SUPERSCRIPT_WEIGHTS))


def invented_stem(r, syllables: int | None = None) -> str:
    """One capitalized invented word, two or three syllables long."""
    n = syllables or (2 if r.random() < 0.7 else 3)
    word = ''.join(r.choice(_ONSETS) + r.choice(_VOWELS) for _ in range(n)) + r.choice(_CODAS)
    return word.capitalize()


def pick_country(r) -> str:
    return _country.pick(r)


def to_cyrillic(word: str) -> str:
    return word.lower().translate(_CYRILLIC).capitalize()


def _prefix(style: str, country: str, r) -> str:
    if style == 'code_colon':
        return f'{country}: '
    if style == 'code_pipe':
        return f'{country}| '
    if style == 'code_then_paren':
        return f'{country} ({r.choice(_REGIONS)}) '
    if style == 'leading_colon':
        return ':'
    if style == 'code_bracketed':
        return f'[{country}] '
    if style == 'code_dash':
        return f'{country} - '
    return ''


class Decorator:
    """Turns a bare name into a lineup row's name at the profile's measured rates.

    `prefix_style` and `rates` are one install's numbers for channel or category names
    (`calibration.Calibration.channel_prefix_style` / `.channel_rates`, or the category pair).
    `mark_scale` lowers the rate a name draws a quality mark at, for a lineup where some names
    carry one regardless (every variant after a channel's first), so the lineup as a whole
    still shows the measured share."""

    def __init__(self, prefix_style: dict, rates: dict, mark_scale: float = 1.0):
        self._style = Weighted((s, prefix_style.get(s, 0.0)) for s in _PREFIX_STYLES)
        self.rates = rates
        q, s = rates['quality_mark'], rates['superscript_mark']
        # The measured either-or share is nearly the sum of the two, so the marks are drawn as
        # alternatives rather than independently.
        self._kind = Weighted((('quality', q), ('superscript', s)))
        drawn = (q + s) * mark_scale
        self._mark = Weighted((('mark', drawn), ('', max(0.0, 1.0 - drawn))))

    def style(self, r) -> str:
        return self._style.pick(r)

    def mark(self, r) -> str:
        return self.forced_mark(r) if self._mark.pick(r) else ''

    def forced_mark(self, r) -> str:
        """A mark drawn the way mark() draws one, never none."""
        return _quality.pick(r) if self._kind.pick(r) == 'quality' else _superscript.pick(r)

    def form(self, r, core: str) -> str:
        """The script and case a name is written in. Drawn once per channel, so every quality
        variant of it keeps the same stem."""
        if r.random() < self.rates['non_latin_script']:
            head, _, tail = core.partition(' ')
            core = f'{to_cyrillic(head)} {tail}'.strip()
        if r.random() < self.rates['all_caps']:
            core = core.upper()
        return core

    def _finish(self, r, prefix: str, body: str) -> str:
        # A symbol goes after a country code rather than before it, so the code still reads as
        # the name's prefix; real names put it either side, and the profile's prefix shares
        # were measured with it wherever it fell.
        if r.random() < self.rates['pictographic_symbol']:
            symbol = r.choice(_SYMBOLS)
            return f'{prefix}{symbol} {body}' if prefix else f'{symbol} {body}'
        return prefix + body

    def decorate(self, r, formed: str, country: str, mark: str | None = None,
                 style: str | None = None) -> str:
        """`formed` is a name from form(). `mark` forces a quality mark (a variant of a channel
        in several qualities; '' for none); None draws one."""
        parts = [formed]
        mark = self.mark(r) if mark is None else mark
        if mark:
            parts.append(mark)
        if r.random() < self.rates['tier_mark']:
            parts.append(r.choice(_TIERS))
        return self._finish(r, _prefix(style or self.style(r), country, r), ' '.join(parts))

    def separator(self, r, country: str, genre: str) -> str:
        ch = r.choice(_SEPARATOR_CHARS)
        bar = ch * r.choice((3, 4, 5))
        return f'{bar} {country} {genre.upper()} {bar}'

    def event(self, r, country: str, sport: str, slot: int, with_time: bool, teams,
              occasion: str | None) -> str:
        """An event slot: a numbered placeholder channel a provider retitles per event."""
        caps = r.random() < self.rates['all_caps']
        body = f'{sport} {r.choice(_SLOT_WORDS)} {slot:02d}'
        if with_time:
            home, away = (t.upper() for t in teams) if caps else teams
            body += f': {away} vs {home}'
            if occasion:
                body += f' ({occasion.upper() if caps else occasion})'
            body += f' {r.randrange(10, 23):02d}:{r.choice((0, 15, 30, 45)):02d}'
        mark = self.mark(r)
        if mark:
            body += f' {mark}'
        return self._finish(r, _prefix(self.style(r), country, r), body)


_SLOT_WORDS = ('EVENT', 'GAME', 'MATCH', 'PPV')
_OCCASIONS = ('Cup', 'Series', 'Final', 'Classic', 'Open', 'Derby', 'Championship', 'Invitational')


def event_occasion(r) -> str:
    return f'{invented_stem(r)} {r.choice(_OCCASIONS)}'
