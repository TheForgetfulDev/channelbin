import logging
from datetime import datetime
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


def format_clock(hour: int, minute: int) -> str:
    """A bare time-of-day (no date) as 'H:MM' (24h) or 'H:MM AM/PM' (12h), per the display
    time format setting - the formatting _recur_label already did inline for recur_hour/
    recur_minute, now shared with the maintenance window's start/end times."""
    if is_24h():
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


def format_local(dt, style: str = 'datetime', none_value='-'):
    """Format a naive-UTC (or aware) datetime in the display timezone.
    None → none_value (pass none_value=None where JSON callers must keep nulls)."""
    if dt is None:
        return none_value
    fmt_12h, fmt_24h = _STYLES[style]
    return to_local(dt).strftime(fmt_24h if is_24h() else fmt_12h)


def relative(dt_utc: datetime, *, style: str = 'compact', now: datetime = None) -> str:
    """Relative phrase for a future naive-UTC datetime; past → 'overdue'.
    style='compact' ('in 5m', dashboard wording); style='long' ('in 5 minutes', jobs wording).

    Two other relative-time formatters exist and are each a deliberate different display
    register, not accidental duplication (dev/changelog/623): `_humanize_secs` in
    `app/routes/recordings.py` is loose/approximate wording ('3.5 hours') for the recordings
    list, where many rows are shown at once and exact seconds don't matter; the `time_ago`/
    `time_until` Jinja filters (same file) are exact combined-unit wording ('1d 4h ago') for
    account sync timestamps, where precision is the point. A fourth, `_ago`/`_ahead` in
    `app/readiness.py`, is past-tense rounded single-unit prose ('3 hours ago') for the
    readiness sentences (dev/changelog/971)."""
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
