"""Reading an event channel's name: its status word and the clock in it (DESIGN-sports-data.md 6.8).

Many provider channels are reused event slots with no guide rows, and the name is the only thing
saying what is on: `NEXT | NHL TORONTO - MONTREAL | Sat 07 Nov 19:05 EST (US) | ...`, `NHL: Florida vs
Washington @ Nov 7 12:30 PM :Sportsnet+ 08`, `NFL | 03 - 1pm Cowboys at Colts`. The shapes belong to
the providers and change without notice, so this is one reader for all of them, never one parser per
provider, and anything it cannot read for certain is left unread rather than guessed.

A clock is read at one of three levels:

- `tz`: a date and a clock followed by a timezone it can name for certain. Measured on 2026-11-04, 60
  of 61 such names were within 45 minutes of the real start.
- `date`: a date, with or without a clock but with no timezone. A clock with no timezone is local to
  somewhere unknown (`Nov 5 2:30 AM :Viaplay FI` is Finland's time; ESPN+ stamps mark when the stream
  opens), so only its date is used, and only to within a day.
- `none`: nothing usable. A clock with no date (`7pm`) is in here too.

Nothing here touches the database or the network.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .tz_utils import to_naive_utc

LEVEL_TZ = 'tz'
LEVEL_DATE = 'date'
LEVEL_NONE = 'none'

STATUS_NEXT = 'next'
STATUS_LIVE = 'live'
STATUS_END = 'end'

#: Abbreviations read only when they directly follow a clock, and only in capitals, the way every
#: provider writes them: `19:00 et 21:00` is French and `7:00 PM West Coast Feed` is a feed. Each names
#: one fixed offset and nothing else in practice. CST (US Central, China) and IST (India, Ireland,
#: Israel) mean more than one place, and WET/WEST are words, so a name using them is read as having
#: no timezone. So is the generic US ET/CT/MT/PT: which offset it means depends on daylight time, and
#: no zone is named in app code outside app/tz_utils.py and app/config.py (CLAUDE.md, Timezones).
_FIXED = {'UTC': 0, 'GMT': 0, 'BST': 1, 'CET': 1, 'CEST': 2, 'EET': 2, 'EEST': 3, 'MSK': 3, 'EST': -5,
          'EDT': -4, 'CDT': -5, 'MST': -7, 'MDT': -6, 'PST': -8, 'PDT': -7, 'AKST': -9, 'AKDT': -8,
          'HST': -10, 'AEST': 10, 'AEDT': 11, 'NZST': 12, 'NZDT': 13}
_TZ_WORDS = '|'.join(sorted(_FIXED, key=len, reverse=True))

_MONTHS = {m: i for i, m in enumerate(
    'jan feb mar apr may jun jul aug sep oct nov dec'.split(), start=1)}
#: Whole month names only, so `Marlins 3` is not March 3.
_MONTH_WORD = (r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|'
               r'sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?(?![a-z])')

# A clock: 19:05, 7:00pm, 7pm, 9:30 AM, 19:04:05. A bare number is never a clock.
_CLOCK = re.compile(
    r'(?<![\d:])(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*([aApP]\.?[mM]\.?)?(?![\d])'
    rf'(?:\s*\(?\s*({_TZ_WORDS})(?![A-Za-z])\s*\)?)?')
_ISO_DATE = re.compile(r'(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)')
_NUM_DATE = re.compile(r'(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?!\d)')
#: A day and month with no year counts only right before a clock: `23/10 19:15`. On its own a
#: `1/2` is as likely a score or a part number.
_SHORT_DATE = re.compile(r'(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])(?=\s+\d{1,2}:\d{2})')
_DAY_MONTH = re.compile(rf'(?<!\d)(\d{{1,2}})\s+{_MONTH_WORD}(?:\s+(\d{{4}})(?![\d:]))?', re.I)
#: The day may not run into a colon: in `Sat 07 Nov 13:05` the 13 is the hour.
_MONTH_DAY = re.compile(rf'(?<![a-z]){_MONTH_WORD}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?![\d:])'
                        rf'(?:,?\s+(\d{{4}})(?![\d:]))?', re.I)
_STATUS = re.compile(r'^\W*(next|live|end(?:ed)?)\b\s*[|:\-]', re.I)

#: Two readings of a numeric date are told apart only when one is near and the other is not.
_NEAR = timedelta(days=21)


@dataclass(frozen=True)
class NameClock:
    level: str
    #: Naive UTC, only at the `tz` level.
    at: datetime | None = None
    #: The date as written, at the `tz` and `date` levels: local to wherever the name was written.
    day: date | None = None
    #: The clock as written, for a reason sentence: `Sat 07 Nov 19:05 EST`.
    text: str = ''


@dataclass(frozen=True)
class ReadName:
    status: str | None
    clock: NameClock


def read(name: str | None, now: datetime) -> ReadName:
    """What a channel's name says about when. `now` (naive UTC) picks the year of a date written
    without one, and tells apart the two readings of a numeric date."""
    text = name or ''
    m = _STATUS.match(text)
    status = None
    if m:
        word = m.group(1).lower()
        status = STATUS_END if word.startswith('end') else word
    return ReadName(status, _clock(text, now))


def _clock(text: str, now: datetime) -> NameClock:
    found = _date(text, now)
    if found is None:
        return NameClock(LEVEL_NONE)
    day, day_start, day_end = found
    m = _CLOCK.search(text, day_end)
    # The clock has to follow the date, with nothing but separators between: `2026-11-04 | 12:30 (GMT)`.
    if m is None or text[day_end:m.start()].strip(' ,|@-'):
        return NameClock(LEVEL_DATE, day=day)
    hour, minute, ampm, tz = m.group(1), m.group(2), m.group(3), m.group(4)
    if minute is None and ampm is None:
        return NameClock(LEVEL_DATE, day=day)
    hour, minute = int(hour), int(minute or 0)
    if ampm and 1 <= hour <= 12:
        hour = hour % 12 + (12 if ampm.lower().startswith('p') else 0)
    # Otherwise `14:30 PM`, a real shape, is read as 24-hour with the mark ignored.
    if hour > 23 or minute > 59 or not tz:
        return NameClock(LEVEL_DATE, day=day)
    at = _to_utc(datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute), tz)
    if at is None:
        return NameClock(LEVEL_DATE, day=day)
    return NameClock(LEVEL_TZ, at=at, day=day, text=text[day_start:m.end()].strip())


def _to_utc(local: datetime, tz: str) -> datetime | None:
    if tz not in _FIXED:
        return None
    return to_naive_utc(local.replace(tzinfo=timezone(timedelta(hours=_FIXED[tz]))))


def _date(text: str, now: datetime):
    """(date, start, end of its match), the first date the name writes, or None. A date that does not
    exist, or a numeric one that reads two ways near now, is not a date."""
    candidates = []
    for m in _ISO_DATE.finditer(text):
        d = _make(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            candidates.append((m.start(), d, m.end()))
    for m in _NUM_DATE.finditer(text):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        d = _pick([_make(y, b, a), _make(y, a, b)], now)
        if d:
            candidates.append((m.start(), d, m.end()))
    for m in _SHORT_DATE.finditer(text):
        a, b = int(m.group(1)), int(m.group(2))
        d = _pick([_yearless(b, a, now), _yearless(a, b, now)], now)
        if d:
            candidates.append((m.start(), d, m.end()))
    for m in _DAY_MONTH.finditer(text):
        d = _dated(int(m.group(1)), m.group(2), m.group(3), now)
        if d:
            candidates.append((m.start(), d, m.end()))
    for m in _MONTH_DAY.finditer(text):
        d = _dated(int(m.group(2)), m.group(1), m.group(3), now)
        if d:
            candidates.append((m.start(), d, m.end()))
    if not candidates:
        return None
    start, d, end = min(candidates)
    return d, start, end


def _dated(day: int, month_word: str, year, now: datetime):
    month = _MONTHS.get(month_word.lower()[:3])
    if month is None:
        return None
    return _make(int(year), month, day) if year else _yearless(month, day, now)


def _make(year: int, month: int, day: int):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _yearless(month: int, day: int, now: datetime):
    """The year that puts the date nearest now: a name in late December can name a January game."""
    options = [d for d in (_make(now.year + k, month, day) for k in (-1, 0, 1)) if d]
    return min(options, key=lambda d: abs(d - now.date())) if options else None


def _pick(readings: list, now: datetime):
    """One date from the readings of a numeric date (day first, then month first). When both are
    real and different, the one near now wins only if the other is not near; otherwise it is not
    read at all. `03-11-2026` on Nov 7 is Nov 3, not Mar 11."""
    real = list(dict.fromkeys(d for d in readings if d))
    if len(real) == 1:
        return real[0]
    if not real:
        return None
    near = [d for d in real if abs(d - now.date()) <= _NEAR]
    return near[0] if len(near) == 1 else None
