"""Window anchors for tests that post times as local wall-clock form values.

`local_input_value()` renders a time with no offset, so a window inside the hour a DST
fall-back repeats posts stop == start and is refused - "now + 2h" lands there one evening a
year (dev/docs/BUGS.md 2026-10-31 @ 08:47:34 PM ET). Local noon never falls inside a DST
transition.
"""
from datetime import datetime, timedelta

from app.tz_utils import to_local, to_naive_utc


def tomorrow_local_noon():
    """Tomorrow at noon in the display timezone, as naive UTC."""
    noon = (to_local(datetime.utcnow()) + timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    return to_naive_utc(noon)
