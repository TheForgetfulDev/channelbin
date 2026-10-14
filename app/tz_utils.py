import logging
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

UTC = ZoneInfo('UTC')
_FALLBACK_TZ = 'America/New_York'


def get_display_tz() -> ZoneInfo:
    from .config import load_config
    cfg = load_config()
    tz_name = cfg.get('display', {}).get('timezone', _FALLBACK_TZ)
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        log.warning('Invalid display timezone %r, falling back to %s', tz_name, _FALLBACK_TZ)
        return ZoneInfo(_FALLBACK_TZ)


def get_display_tz_name() -> str:
    from .config import load_config
    cfg = load_config()
    return cfg.get('display', {}).get('timezone', _FALLBACK_TZ)


def get_time_format() -> str:
    from .config import load_config
    cfg = load_config()
    return cfg.get('display', {}).get('time_format', '12h')


def is_24h() -> bool:
    return get_time_format() == '24h'


def to_local(dt: datetime, tz: ZoneInfo = None) -> datetime:
    """Naive-UTC (or aware) datetime → aware datetime in the display timezone.

    Pass `tz` (from one `get_display_tz()` call) inside a per-row loop - without it every
    call re-reads the config."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz or get_display_tz())


def to_naive_utc(dt: datetime) -> datetime:
    """Aware datetime (any tz) → naive UTC for storage. Naive input is assumed display-tz."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_display_tz())
    return dt.astimezone(UTC).replace(tzinfo=None)


def utc_offset_segments(tz: ZoneInfo, lo: datetime, hi: datetime) -> tuple:
    """`tz`'s UTC offset over naive-UTC [lo, hi], as `((until, seconds), ..., (None, seconds))`.

    Each `seconds` is the offset in force for instants before its `until` (naive UTC) and at
    or after the previous one; the last entry has no `until` and holds from its start on, so
    an instant outside [lo, hi] still gets the offset of the segment nearest it. A zone with
    no change inside the span is one entry.

    This is what lets SQL read a stored naive-UTC time as a LOCAL wall-clock time without a
    timezone database: the channel search's day-of-week and time-of-day filters add the
    offset in force at each showing's start, so 7 PM means 7 PM on both sides of a DST change
    (dev/changelog/1187). Sampled a day apart and bisected to the second - no zone changes its
    offset twice inside one day at any date a guide can hold. Cached on the span's dates,
    which is how often its answer can change.
    """
    return _offset_segments(tz, lo.date(), hi.date())


@lru_cache(maxsize=64)
def _offset_segments(tz: ZoneInfo, lo_day, hi_day) -> tuple:
    def offset(at: datetime) -> int:
        return int(at.replace(tzinfo=UTC).astimezone(tz).utcoffset().total_seconds())

    step = timedelta(days=1)
    at = datetime.combine(lo_day, datetime.min.time()) - step
    end = datetime.combine(hi_day, datetime.min.time()) + 2 * step
    current = offset(at)
    segments = []
    while at < end:
        nxt = at + step
        changed = offset(nxt)
        if changed != current:
            # The first second at which the new offset holds, somewhere in (at, nxt].
            before, after = at, nxt
            while after - before > timedelta(seconds=1):
                mid = before + (after - before) / 2
                mid = mid.replace(microsecond=0)
                if offset(mid) == current:
                    before = mid
                else:
                    after = mid
            segments.append((after, current))
            current = changed
        at = nxt
    segments.append((None, current))
    return tuple(segments)


def parse_local_to_utc(value: str) -> datetime:
    """Parse a datetime-local form value ('YYYY-MM-DDTHH:MM[:SS]'), interpreted in the
    display timezone, into naive UTC. Raises ValueError on any bad input - None and
    non-string inputs are normalized to ValueError so callers catch one exception."""
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, AttributeError) as exc:
        raise ValueError(f'invalid datetime value: {value!r}') from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_display_tz())
    return dt.astimezone(UTC).replace(tzinfo=None)


def format_clock(hour: int, minute: int, h24: bool = None) -> str:
    """A bare time-of-day (no date) as 'H:MM' (24h) or 'H:MM AM/PM' (12h), per the display
    time format setting - the formatting _recur_label already did inline for recur_hour/
    recur_minute, now shared with the maintenance window's start/end times. A per-row caller
    passes `h24`, read once."""
    if h24 is None:
        h24 = is_24h()
    if h24:
        return f'{hour:02d}:{minute:02d}'
    suffix = 'AM' if hour < 12 else 'PM'
    disp_h = hour % 12 or 12
    return f'{disp_h}:{minute:02d} {suffix}'


def local_input_value(dt_utc: datetime) -> str:
    """Naive-UTC → 'YYYY-MM-DDTHH:MM' in the display tz, for datetime-local input prefill."""
    return to_local(dt_utc).strftime('%Y-%m-%dT%H:%M')


def parse_hhmm(value: str):
    """Parse an 'HH:MM' time-picker value. Returns (hour, minute); raises ValueError on
    any bad input (including None/non-string)."""
    if not isinstance(value, str) or ':' not in value:
        raise ValueError(f'invalid HH:MM value: {value!r}')
    hh, mm = value.split(':')
    hour, minute = int(hh), int(mm)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f'invalid HH:MM value: {value!r}')
    return hour, minute


# (12h_fmt, 24h_fmt) pairs. Each style preserves the exact output of the call sites it
# consolidated - don't "tidy" a format string without checking every page that uses it.
_STYLES = {
    'datetime':        ('%b %d, %Y %I:%M %p %Z', '%b %d, %Y %H:%M %Z'),
    # Same as 'datetime' plus seconds - for logs where events can land in the same minute
    # (a stall/restart burst) and must stay distinguishable.
    'datetime_sec':    ('%b %d, %Y %I:%M:%S %p %Z', '%b %d, %Y %H:%M:%S %Z'),
    'date':            ('%b %d, %Y',             '%b %d, %Y'),
    'time':            ('%I:%M %p %Z',           '%H:%M %Z'),
    'clock':           ('%-I:%M %p',             '%H:%M'),
    'day_datetime':    ('%a %b %-d, %-I:%M %p %Z', '%a %b %-d, %H:%M %Z'),
    'short_datetime':  ('%-m/%-d/%y %-I:%M %p',  '%-m/%-d/%y %H:%M'),
    'monthday_time':   ('%-m/%-d %-I:%M %p',     '%-m/%-d %H:%M'),
    'iso_datetime':    ('%Y-%m-%d %I:%M %p',     '%Y-%m-%d %H:%M'),
    'iso_datetime_tz': ('%Y-%m-%d %I:%M %p %Z',  '%Y-%m-%d %H:%M %Z'),
}


def parse_epoch_utc(value) -> datetime | None:
    """Epoch-seconds (str or int) -> naive UTC datetime, or None if value is falsy/unparseable.
    Providers commonly send this as a JSON string (e.g. Xtream's `user_info.exp_date`)."""
    if not value:
        return None
    try:
        return datetime.utcfromtimestamp(int(value))
    except (TypeError, ValueError, OSError):
        return None


def format_local(dt, style: str = 'datetime', none_value='-', *, tz: ZoneInfo = None,
                 h24: bool = None):
    """Format a naive-UTC (or aware) datetime in the display timezone.
    None → none_value (pass none_value=None where JSON callers must keep nulls).
    A per-row loop passes `tz` and `h24`, read once, so no row reaches the config."""
    if dt is None:
        return none_value
    fmt_12h, fmt_24h = _STYLES[style]
    if h24 is None:
        h24 = is_24h()
    return to_local(dt, tz).strftime(fmt_24h if h24 else fmt_12h)


def day_label(dt_utc: datetime, now: datetime, tz: ZoneInfo) -> str:
    """'Today' / 'Tomorrow' / 'Yesterday', else 'Sun Oct 18' (with the year when it is not
    this year), for a naive-UTC instant in the display timezone. The one spelling of a day
    heading beside a clock time - the recordings list and the Rules pages both use it."""
    local = to_local(dt_utc, tz)
    today = to_local(now, tz).date()
    delta = (local.date() - today).days
    if delta == 0:
        return 'Today'
    if delta == 1:
        return 'Tomorrow'
    if delta == -1:
        return 'Yesterday'
    if local.year == today.year:
        return local.strftime('%a %b ') + str(local.day)
    return local.strftime('%b ') + f'{local.day}, {local.year}'


def ago(when: datetime, now: datetime, none_value: str = 'never') -> str:
    """'just now' / '5 minutes ago' / '3 hours ago' / '2 days ago' for a past naive-UTC
    instant: rounded, one unit, the register a person uses for "when did this last happen".
    Hours run to 48 so "yesterday evening" does not read as "1 day ago"."""
    if when is None:
        return none_value
    minutes = (now - when).total_seconds() / 60
    if minutes < 1:
        return 'just now'
    if minutes < 60:
        m = int(round(minutes))
        return f'{m} minute{"" if m == 1 else "s"} ago'
    hours = minutes / 60
    if hours < 48:
        h = int(round(hours))
        return f'{h} hour{"" if h == 1 else "s"} ago'
    d = int(round(hours / 24))
    return f'{d} day{"" if d == 1 else "s"} ago'


def relative(dt_utc: datetime, *, style: str = 'compact', now: datetime = None) -> str:
    """Relative phrase for a future naive-UTC datetime; past → 'overdue'.
    style='compact' ('in 5m', dashboard wording); style='long' ('in 5 minutes', jobs wording).

    Two other relative-time formatters exist and are each a deliberate different display
    register, not accidental duplication (dev/changelog/623): `_humanize_secs` in
    `app/routes/recordings.py` is loose/approximate wording ('3.5 hours') for the recordings
    list, where many rows are shown at once and exact seconds don't matter; the `time_ago`/
    `time_until` Jinja filters (same file) are exact combined-unit wording ('1d 4h ago') for
    account sync timestamps, where precision is the point. A fourth, `ago()` above, is
    past-tense rounded single-unit prose ('3 hours ago') for a sentence read once - the
    readiness sentences (dev/changelog/971) and the Rules pages (dev/changelog/1189)."""
    if now is None:
        now = datetime.utcnow()
    diff = (dt_utc - now).total_seconds()
    if diff < 0:
        return 'overdue'
    if diff < 60:
        return f'in {int(diff)}s'
    if diff < 3600:
        m = int(diff / 60)
        if style == 'long':
            return f'in {m} minute{"s" if m != 1 else ""}'
        return f'in {m}m'
    if diff < 86400:
        h = int(diff / 3600)
        m = int((diff % 3600) / 60)
        if m:
            return f'in {h}h {m}m'
        if style == 'long':
            return f'in {h} hour{"s" if h != 1 else ""}'
        return f'in {h}h'
    d = int(diff / 86400)
    if style == 'long':
        return f'in {d} day{"s" if d != 1 else ""}'
    return f'in {d}d'
