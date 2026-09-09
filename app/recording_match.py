"""Matching a scheduled or finished recording to a program slot.

"Is anything already recording this showing, and if so which recording" is asked by every
surface that lists programs: the TV Guide grid, the old EPG deep search, and the airing grain
of the channel search (`app/channel_search_rows.py`). It lived in `app/routes/guide.py` until
the airing grain needed it too (dev/changelog/412); a row builder importing from a route
module is backwards layering, and copying it would have been a fifth private copy of search
mechanics in an area that already had four.

**Identity first, URL only as a fallback.** `Recording.url` is frozen at creation, so when a
provider rewrites its stream URLs (`dev/docs/DESIGN-url-drift.md`) a URL-keyed lookup stops
matching the channel's current `stream_url` and the guide silently loses its scheduled
indicators. `channel_id` and `group_id` survive that drift.
"""
from .accounts import normalize_url_loose


def build_rec_indexes(recordings):
    """Index recordings for guide matching: (by_group_id, by_channel_id, by_url).

    A recording lands in exactly one index (group > channel > url), so composing all three at
    a call site cannot list the same recording twice.
    """
    by_group: dict[int, list] = {}
    by_channel: dict[int, list] = {}
    by_url: dict[str, list] = {}
    for rec in recordings:
        if rec.group_id is not None:
            by_group.setdefault(rec.group_id, []).append(rec)
        elif rec.channel_id is not None:
            by_channel.setdefault(rec.channel_id, []).append(rec)
        else:
            # Manual URL-only recording - no channel identity to match on.
            for key in (rec.url, normalize_url_loose(rec.url)):
                by_url.setdefault(key, []).append(rec)
    return by_group, by_channel, by_url


def candidate_recs(indexes, ch, stream_url, group=None):
    """Recordings that could belong to this program row, most-specific source first.

    `ch` is the row's recording target - a group row's current best member, or the channel
    itself.
    """
    by_group, by_channel, by_url = indexes
    recs = by_group.get(group.id, []) if group is not None else []
    recs = recs + by_channel.get(ch.id, [])
    return recs + by_url.get(stream_url, []) + by_url.get(normalize_url_loose(ch.stream_url), [])


def match_recording(candidates, start, stop):
    """First candidate overlapping [start, stop), or None."""
    for rec in candidates:
        if rec.start_time < stop and rec.stop_time > start:
            return rec
    return None
