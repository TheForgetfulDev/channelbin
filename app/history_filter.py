"""Filter chips over a history or event-log card - one chip per kind of entry, each with
its count, the active one marked, All always present.

The auto-record rule page's History card had the first row (dev/changelog/1189); this is the
one implementation it, the recording's Event log, the channel's and the group's Activity
timelines and the account's Activity card all call (dev/changelog/1474). The sports league
page's "Your teams / All" row shares the `.rl-filter` markup but is a scope toggle, not a
filter over one list, and keeps its own two links. The shape every caller shares:

* the route reads `?hist=<key>` through `pick()`, so an unknown key is All rather than an
  empty card;
* `apply()` buckets every entry ONCE through the caller's `bucket_of` (a pure function of the
  row - never a query or a config read, since it runs per row), counts each chip, drops a chip
  whose count is zero, and returns the entries the active chip keeps;
* the template draws the chips with `_macros.html::history_chips()` and the page's script
  swaps only the card on a plain click (`util.js::bindHistoryChips`), so the chips are links
  that survive a reload and a Ctrl-click.

The bucket tables for the recording Event log and the two timelines live here too, beside
the mechanism, so a new event type picks its chip in one place
(tests/test_history_filter.py holds the recording table complete).
"""
from __future__ import annotations

from typing import Callable, Iterable, Sequence

from . import database as dbm

ALL = 'all'

#: The chips over a channel's or a group's Activity timeline, keyed on the entry's `kind`
#: (routes/channels.py::_build_channel_timeline, routes/channel_groups.py::_build_group_timeline).
TIMELINE_FILTERS = (
    (ALL, 'All'),
    ('tests', 'Health tests'),
    ('recordings', 'Recordings'),
    ('changes', 'Changes'),
)
TIMELINE_KIND_BUCKETS = {
    'test': 'tests',
    'recording': 'recordings',
    'channel_event': 'changes',
    'group_event': 'changes',
    'recording_event': 'changes',
}

#: The chips over a recording's Event log. Disjoint, so the counts add up to All.
RECORDING_FILTERS = (
    (ALL, 'All'),
    ('stream', 'Stream trouble'),
    ('capture', 'Capture'),
    ('file', 'File'),
    ('details', 'Schedule and details'),
    ('tech', 'Technical detail'),
)
RECORDING_BUCKETS = {
    # The stream misbehaving and what the recorder did about it.
    dbm.STALL_DETECTED: 'stream', dbm.RESTART_ATTEMPTED: 'stream',
    dbm.RESTART_SUCCEEDED: 'stream', dbm.RESTART_FAILED: 'stream',
    dbm.GROUP_FAILOVER: 'stream', dbm.RECORDING_HOST_ROLLED: 'stream',
    dbm.RECORDING_LOGIN_REFUSED: 'stream', dbm.RECORDING_REPOINTED: 'stream',
    dbm.RECORDING_URL_RERESOLVED: 'stream', dbm.FAST_DELIVERY_DETECTED: 'stream',
    dbm.CAPTURE_PACING_ENABLED: 'stream', dbm.CAPTURE_VIDEO_STOPPED: 'stream',
    dbm.CAPTURE_VIDEO_RESUMED: 'stream', dbm.SEGMENT_DISCARDED: 'stream',
    dbm.RECORDING_FAILED_DEAD_STREAM: 'stream', dbm.RECORDING_RETRY_SCHEDULED: 'stream',
    dbm.CAPTURE_GAVE_UP: 'stream', dbm.RECORDING_FORMAT_OVERRIDE: 'stream',
    dbm.RECORDING_FORMAT_CHANGED: 'stream', dbm.PRE_CHECK_FAILED: 'stream',
    dbm.PROVIDER_STALLS_CORRELATED: 'stream',
    # The capture's own run: segments, start, stop, the channel it ran on.
    dbm.SEGMENT_STARTED: 'capture', dbm.SEGMENT_ENDED: 'capture',
    dbm.CAPTURE_COMPLETE: 'capture', dbm.PRE_CHECK_PASSED: 'capture',
    dbm.PRE_CHECK_SKIPPED: 'capture', dbm.GROUP_MEMBER_SELECTED: 'capture',
    dbm.RECORDING_HANDOFF: 'capture', dbm.RECORDING_STARTED_LATE: 'capture',
    dbm.RECORDING_STOPPED_EARLY: 'capture', dbm.RECORDING_START_DEFERRED: 'capture',
    dbm.RECORDING_START_NOT_ARMED: 'capture', dbm.RECORDING_RESUMED: 'capture',
    dbm.RECORDING_RESUME_REFUSED: 'capture', dbm.CAPTURE_LOST_TO_OUTAGE: 'capture',
    dbm.RECORDING_ABORTED: 'capture', dbm.RECORDING_PAUSED: 'capture',
    dbm.RECORDING_MANUALLY_STOPPED: 'capture', dbm.RECORDING_FAILED: 'capture',
    dbm.RECORDING_CREATED_AFTER_EVENT_START: 'capture',
    dbm.RECORDING_REPLACED_OTHER: 'capture',
    # What happened to the file once capture was over.
    dbm.CONCATENATION_STARTED: 'file', dbm.CONCATENATION_DONE: 'file',
    dbm.CONCATENATION_FAILED: 'file', dbm.SEEK_DAMAGE_DETECTED: 'file',
    dbm.MIXED_FRAME_RATE_DETECTED: 'file', dbm.FORMAT_NORMALIZE_NEEDED: 'file',
    dbm.POSTCAPTURE_ANALYSIS_STARTED: 'file', dbm.POSTCAPTURE_ANALYSIS_SKIPPED: 'file',
    dbm.CONVERSION_STARTED: 'file', dbm.CONVERSION_RESTARTED: 'file',
    dbm.CONVERSION_YIELDED: 'file', dbm.CONVERSION_RESUMED: 'file',
    dbm.CONVERSION_DONE: 'file', dbm.CONVERSION_FAILED: 'file',
    dbm.CONVERSION_CANCELLED: 'file', dbm.CONVERTED_FILE_GAPS: 'file',
    dbm.SOURCE_DELETED: 'file', dbm.SOURCE_KEPT: 'file',
    dbm.FILE_MOVED: 'file', dbm.FILE_MOVE_FAILED: 'file', dbm.SCRIPT_EXECUTED: 'file',
    dbm.METADATA_SIDECAR_WRITTEN: 'file', dbm.METADATA_SIDECAR_FAILED: 'file',
    # The schedule and the description, edited by a person, a rule or the guide.
    dbm.RECORDING_EDITED: 'details', dbm.RECORDING_STOP_TIME_ADJUSTED: 'details',
    dbm.RECORDING_RULE_SCHEDULED: 'details', dbm.RECORDING_RULE_RETRACKED: 'details',
    dbm.RECORDING_RULE_PROGRAM_MOVED: 'details', dbm.RECORDING_RULE_RELEASED: 'details',
    dbm.RECORDING_METADATA_REFRESHED: 'details',
    dbm.RECORDING_METADATA_REFRESH_SKIPPED: 'details',
    dbm.RECORDING_METADATA_EDITED: 'details', dbm.RECORDING_SPORTS_LINKED: 'details',
    dbm.RECORDING_SPORTS_REMOVED: 'details',
    dbm.DIAGNOSTICS: 'tech',
}


def pick(value, filters: Sequence[tuple]) -> str:
    """The chip `?hist=` names, or All for anything the card does not offer."""
    keys = {k for k, _label in filters}
    return value if value in keys else ALL


def apply(entries: Iterable, bucket_of: Callable, filters: Sequence[tuple], active: str,
          url: Callable[[str], str]) -> tuple[list, list]:
    """(chips, kept): the chip row for `entries` and the entries the `active` chip keeps.

    `bucket_of(entry)` names the chip an entry falls under, or None for an entry only All
    shows. A chip with nothing under it is left out; All always stands, with the total.
    `url(key)` is the chip's link - the page's own URL with `hist=<key>`."""
    entries = list(entries)
    counts = {k: 0 for k, _label in filters}
    buckets = []
    for e in entries:
        b = bucket_of(e)
        buckets.append(b)
        if b in counts and b != ALL:
            counts[b] += 1
    counts[ALL] = len(entries)
    chips = [{'key': k, 'label': label, 'count': counts[k], 'href': url(k),
              'active': k == active}
             for k, label in filters if k == ALL or counts[k]]
    kept = entries if active == ALL else [e for e, b in zip(entries, buckets) if b == active]
    return chips, kept
