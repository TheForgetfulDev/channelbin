"""Support bundle export (DESIGN-secrets.md §7): a sanitized zip assembled at request
time for remote troubleshooting - never a copy of dvr.db or raw config.yaml.

Every file written into the zip passes through redact_urls_in_text() before it reaches
the zip - any <scheme>:// token anywhere, in any field, in any table, keeps its scheme
and loses everything after it. This is deliberately blunter than the masking used
elsewhere in the app (mask_creds/mask_url_path, which preserve the host for
on-screen/log diagnostics that never leave this machine) - a bundle is something that
might leave the machine, and even a bare domain is considered private in the IPTV
context this app serves, so nothing here judges which URLs matter. This also means no
per-field masking call is needed for known URL columns (Account.m3u_url/epg_url/
base_url, Channel.stream_url, Recording.url) - the sweep catches all of them, plus
anything in a free-text column nobody thought to check.

Structured files are redacted per value and then serialized (_write_json); free-form
text is redacted whole (_write_text). Those two are the only ways into the zip, so the
choke point is preserved, but the JSON half must not be collapsed back into a single
pass over the serialized blob: in serialized JSON a newline is the two characters '\\'
and 'n' rather than whitespace, so the URL pattern's [^\\s...]+ runs straight through
the line break and eats the start of the following line (dev/changelog/837).

The same reasoning reaches the free-text *names* the user chose, not just URLs
(dev/changelog/840): an account name is routinely the provider's own brand, so shipping
it verbatim hands over exactly the identity the URL redaction above exists to remove.
By default account and channel names are replaced with stable pseudonyms - and account
names are additionally swept out of every other string in the bundle, because ~20 alert
types title themselves f'{account.name}: ...'. A user who does not consider that
identity sensitive can opt in to real names (include_names=True); meta.json always
states which mode produced the bundle and what the residual limits are.

How much a table ships is decided by what its size tracks (dev/changelog/841). A table
that grows with the user's own activity ships whole - every row of it is something that
happened on this install, and a report is about one of those. A table that grows with
the provider's catalog does not: its size is set by whichever subscription the user
bought, so it ships as counts over the facets that survive redaction, plus full rows for
the channels something else in the bundle actually points at. channels is the only table
on that side today at 137k rows and 98.5% of a bundle; epg_entries would be the second.
An activity table with a single writer that can fire per catalog row is the third case
(dev/changelog/842): it ships newest-first up to a cap, with its true total stated, so
the ordinary bundle is unchanged and the pathological one stays mailable.

The bundle also answers questions about state that is not in the database at all
(dev/changelog/842): whether the files it names are still on disk, and what the host
had left when it died. Both are best-effort by construction - a stat on a dead network
mount and a /proc read on a non-Linux host must degrade to a stated gap, never to a
failed export.
"""
import json
import logging
import os
import platform
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime
from io import BytesIO

from . import db
from .config import mask_config
from .database import (
    Account, Channel, ChannelEvent, ChannelGroup, ChannelGroupEvent,
    ChannelGroupMember, JobRun, Recording, RecordingEvent,
    RecordingSegment, AccountSyncLog, Alert, ChannelTest, SECRET_ACCOUNT_FIELDS,
    GROUP_FAILOVER, GROUP_MEMBER_SELECTED,
)
from .tz_utils import parse_epoch_utc
from .url_utils import redact_urls_in_text
from .version import __version__

log = logging.getLogger(__name__)

# The explicit opt-in: SECRET_ACCOUNT_FIELDS members known to be URL-shaped, so the
# write-time redact_urls_in_text() choke point catches them and the flat mask below can
# skip them. Everything else in SECRET_ACCOUNT_FIELDS is masked by default - a fail-open
# default here would ship a future non-URL secret (an API key, a token) into the bundle
# in cleartext, since redact_urls_in_text() only matches <scheme>:// tokens.
_ACCOUNT_URL_SECRET_FIELDS = {'base_url', 'm3u_url', 'epg_url'}

# dvr.log tail cap - the write-time redaction pass catches every URL in the tail
# regardless of when the line was logged; capped so an old line can't dominate the
# bundle (see DESIGN-secrets.md §7's log.dvr.log note). Counts *application* records
# kept after noise filtering (dev/changelog/838), not raw lines - see _LOG_SCAN_LINES.
_LOG_TAIL_LINES = 2000

# How many raw lines to scan from the end of dvr.log to fill _LOG_TAIL_LINES worth of
# application records. Most of a raw window is werkzeug/APScheduler noise - an audited
# bundle's ratio was roughly 22 raw lines per application line - so recovering
# _LOG_TAIL_LINES application lines means reading much further back than that count
# itself. This is a heuristic with headroom over that measured ratio, not a guarantee;
# raise it if a bundle still comes up short. _tail_lines() seeks from EOF regardless of
# total file size, so this stays bounded even with log rotation disabled.
_LOG_SCAN_LINES = 80000

# apscheduler.executors.default messages for a routine job start/finish - high-volume,
# no diagnostic value on their own. A job that actually raised logs a different
# message/level from this source and is untouched by the check below.
_APSCHEDULER_ROUTINE_PREFIX = 'Running job '
_APSCHEDULER_ROUTINE_SUFFIX = 'executed successfully'

# Channel columns worth shipping - an allowlist, not the full row: raw_stream_url is the
# same credential-bearing URL as stream_url pre-normalization (dropped entirely, not just
# masked) and logo_url is a third-party CDN link with no diagnostic value.
#
# The health block is here because a channel's score is routinely the collateral damage
# in the incident being reported, and without it the score can only be reconstructed from
# channel_tests.lifetime_score_after. manual_health_adjustment rides along with the four
# measured columns because it is not optional context: every band the UI shows is the
# *effective* score (health_score + manual_health_adjustment), so health_score alone reads
# wrong on any channel the user has nudged by hand.
_CHANNEL_FIELDS = (
    'id', 'account_id', 'name', 'category_name', 'category_id', 'epg_channel_id',
    'in_guide', 'test_enabled', 'is_duplicate_stream_url', 'stream_url',
    'health_score', 'health_score_sample_count', 'health_score_updated_at',
    'manual_health_adjustment', 'consecutive_test_failures',
)

# Account columns worth shipping - everything except the secret fields (handled specially
# below) and the relationship-only columns.
_ACCOUNT_FIELDS = (
    'id', 'name', 'account_type', 'status', 'last_sync_at', 'next_sync_at', 'last_error',
    'channel_count', 'hidden_channel_count', 'epg_entry_count', 'color', 'url_normalization',
    'sync_interval_hours', 'sync_enabled', 'max_connections', 'created_at', 'updated_at',
)

# Which tables ship every row, and which ship an aggregate plus the rows a reader could
# actually follow a thread to. The split is by what the table's size tracks
# (dev/changelog/841): a table that grows with the user's own activity - recordings,
# segments, events, tests, alerts, groups - stays whole, because every row of it is a
# thing that happened on this install. A table that grows with the provider's catalog
# does not, because its size is set by whoever the user bought a subscription from and
# has nothing to do with the report being filed. channels is the only such table today;
# epg_entries would be the second.
#
# The scheme+extension pair below is the whole of what channels.stream_url can still say
# once the write-time redaction has removed its host and path: the shape, not the value.
# The extension is matched against this allowlist rather than read as "whatever follows
# the last dot", because a stream URL with no path at all yields the last piece of its
# HOSTNAME under a naive parse ('co', 'com' both occur on a real 137k-row install) -
# which is exactly the provider identity every other rule in this file exists to strip.
_URL_EXTENSIONS = frozenset({'m3u8', 'ts', 'mp4', 'mp3', 'aac', 'm3u', 'asx'})

_URL_SCHEME_RE = re.compile(r'([a-zA-Z][a-zA-Z0-9+.\-]*)://')

# Where a channel id can appear in the rest of the bundle. A channel named by one of
# these has a row shipped in full; every other channel is a line in the aggregate only.
# Add to this list when a table that names channels is added to the bundle - the point of
# the registry is that adding a table and forgetting its channels is one edit, not two
# files apart.
#
# Alert is deliberately absent: it has no channel_id column at all, and reaches channels
# only through recording_id, which Recording already covers.
#
# ChannelEvent is absent for a different reason: it is the one shipped table that is
# capped, so its ids are collected from the rows actually written into the bundle rather
# than from a whole-table DISTINCT (see _channel_event_rows). Asking the table instead
# would pull a full channel row for every channel a mass URL drift touched - which is the
# 137k-row channels.json dev/changelog/841 removed, arriving by a second route.
_CHANNEL_ID_SOURCES = (
    (Recording, Recording.channel_id),
    (RecordingSegment, RecordingSegment.channel_id),
    (ChannelTest, ChannelTest.channel_id),
    (ChannelGroupMember, ChannelGroupMember.channel_id),
    (ChannelGroupEvent, ChannelGroupEvent.channel_id),
)

# Newest-first cap on channel_events. Every other writer of that table is a user action
# (a group change, a hide override, a guide move) and is bounded by what a human did, but
# accounts.py::_write_channel_url_drift_events writes one row per drifted channel and its
# own docstring notes that a mass domain move drifts thousands in a single sync. 117 rows
# on the install this was measured against, so the cap is invisible in normal use and
# exists only for that one case; channel_events.json states the true total either way.
_CHANNEL_EVENT_LIMIT = 5000

# Path columns the bundle already ships, per model - the input to the stat pass. A path
# is only half an answer without it: "the segment is at /dvr/x.ts" and "that file is 4 GB
# / is gone" are different reports, and telling them apart used to take an email round
# trip. Add a model here when a shipped table grows a path column.
_PATH_FIELDS = {
    Recording: ('output_path',),
    RecordingSegment: ('file_path',),
    ChannelTest: ('screenshot_path',),
}

# extra_data keys on the two RecordingEvent types that name a channel by id rather than
# through a foreign key. Without these, a group-driven recording's bundle names the
# member that served it and the member it failed over from as bare integers with no row
# behind them - which was the single hardest part of the audited incident to follow.
_EVENT_CHANNEL_KEYS = ('channel_id', 'from_channel_id', 'to_channel_id')

# Stated in the file itself, not only here: a reader who opens channels.json and finds
# 40 rows must not conclude the install has 40 channels (product principle 1 - a filtered
# artifact never presents itself as the whole truth).
_CHANNELS_NOTE = (
    'This is NOT every channel on the install. Full rows are shipped only for the '
    'channels something else in this bundle refers to; every channel is counted in '
    '"aggregate", whose counts cover the whole table including the rows listed here. '
    'The catalog is routinely six figures of rows whose only redaction-surviving '
    'difference is the shape recorded in the aggregate, so shipping all of it made the '
    'bundle too large to send while answering nothing (dev/changelog/841).')

_CHANNELS_INCLUSION_RULE = (
    'Referenced by a recording, a recording segment, a channel test or a channel-group '
    'membership in this bundle, or named by id in a GROUP_MEMBER_SELECTED / '
    'GROUP_FAILOVER recording event.')

_FULL_ROW_TABLES = (
    (Recording, 'recordings.json'),
    (RecordingEvent, 'recording_events.json'),
    (RecordingSegment, 'recording_segments.json'),
    (AccountSyncLog, 'account_sync_logs.json'),
    (Alert, 'alerts.json'),
    (ChannelTest, 'channel_tests.json'),
    (ChannelGroupMember, 'channel_group_members.json'),
    (ChannelGroupEvent, 'channel_group_events.json'),
    (JobRun, 'job_runs.json'),
)

_APSCHEDULER_NOTE = (
    'The scheduler\'s persisted job store, id and next run time only. Its third column, '
    'job_state, is a pickle of the job\'s callable and arguments - unreadable here and '
    'able to carry values nothing in this file could sanitize - so it is never shipped. '
    'A recording that never started is usually a missing or misdated row in this table.')

_CHANNEL_EVENTS_NOTE = (
    f'The newest {_CHANNEL_EVENT_LIMIT} channel events at most, oldest-first within that '
    'window. "total_event_count" is the whole table; when it exceeds '
    '"included_event_count", older events exist that are not in this file.')


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class _Sanitizer:
    """The one text transform every string in the bundle passes through: URL redaction,
    plus - unless the user opted into real names - substitution of every account name
    with a stable pseudonym wherever it appears.

    The sweep exists because pseudonymizing the accounts.name column is not enough.
    Around twenty alert types in app/accounts.py title themselves f'{account.name}: ...',
    so the same string reaches the bundle again through alerts.json,
    recording_events.json, account_sync_logs.json and the dvr.log tail; on the install
    this was measured against, 21 alert rows and 9 recording-event rows carried one
    (dev/changelog/840).

    Channel names are deliberately NOT swept, only pseudonymized in their own columns.
    There are six figures of them and most are ordinary words ('News', 'Sports', 'HD'),
    so a substring sweep over that set would carve holes in every free-text value in the
    bundle rather than protect anything. meta.json states that limit rather than leaving
    a reader to assume the sweep was total.
    """

    # An account name shorter than this is a substring of ordinary English ('TV', 'A'),
    # so sweeping it would corrupt the text it was meant to protect. Such a name is
    # counted and disclosed in meta.json instead of swept. Its column is still
    # pseudonymized - only the free-text sweep skips it.
    MIN_SWEEPABLE_LEN = 4

    def __init__(self, accounts, include_names=False):
        self.include_names = include_names
        self.swept = 0
        self.skipped_short = 0
        self._pseudonyms = {}
        pairs = []
        for acct in accounts:
            self._pseudonyms[acct.id] = f'account {acct.id}'
            name = (acct.name or '').strip()
            if not name:
                continue
            if len(name) < self.MIN_SWEEPABLE_LEN:
                self.skipped_short += 1
                continue
            pairs.append((name, self._pseudonyms[acct.id]))
        # Longest first, so an account name that is a prefix of another is replaced
        # whole instead of leaving a mangled remainder behind.
        self._pairs = [(re.compile(re.escape(name), re.IGNORECASE), pseudo)
                       for name, pseudo in sorted(pairs, key=lambda p: -len(p[0]))]

    def account_name(self, acct):
        return acct.name if self.include_names else self._pseudonyms.get(
            acct.id, f'account {acct.id}')

    def text(self, value):
        """Redact URLs, then sweep account names. Safe to run twice: both halves are
        idempotent, so the belt-and-braces pass over serialized output is a no-op."""
        out = redact_urls_in_text(value)
        if self.include_names or not out:
            return out
        for pattern, pseudo in self._pairs:
            out, count = pattern.subn(lambda m, p=pseudo: p, out)
            self.swept += count
        return out


def _redact_deep(value, sanitizer):
    """Sanitize every string reachable from `value`, returning a new structure.

    This has to run on the values rather than on the JSON they serialize into: a
    newline inside a JSON string literal is the two characters '\\' and 'n', neither of
    which terminates _ANY_SCHEME_URL_RE's [^\\s'"<>()\\[\\]]+ run, so a redactor pointed
    at the serialized blob consumes the line break and everything up to the next real
    space - deleting the first line of any traceback that follows a URL.
    """
    if isinstance(value, str):
        return sanitizer.text(value)
    if isinstance(value, dict):
        return {_redact_deep(k, sanitizer): _redact_deep(v, sanitizer)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_deep(v, sanitizer) for v in value]
    return value


def _redacted_default(obj, sanitizer):
    """json.dumps fallback for a value _redact_deep passed over as an opaque scalar.

    Its str() is produced during serialization, after the deep pass has already run, so
    a URL inside a custom type's repr would otherwise reach the zip unredacted.
    """
    return sanitizer.text(str(obj))


def _row_to_dict(row):
    """Every column of `row`, generic column introspection so a schema change is picked
    up automatically rather than needing a hand-maintained field list. No per-field URL
    masking here - the write-time choke point handles that uniformly."""
    return {col.name: _json_value(getattr(row, col.name)) for col in row.__table__.columns}


def _account_to_dict(acct, sanitizer):
    out = {name: _json_value(getattr(acct, name)) for name in _ACCOUNT_FIELDS}
    out['name'] = sanitizer.account_name(acct)
    for name in SECRET_ACCOUNT_FIELDS:
        value = getattr(acct, name, None)
        if name in _ACCOUNT_URL_SECRET_FIELDS:
            out[name] = value   # URL field - write-time redaction handles it
        else:
            out[name] = '***' if value else value
    return out


def _group_to_dict(group, sanitizer):
    """Every column of a channel group, with its user-chosen name pseudonymized.

    A group name is the same class of string as a channel name (dev/changelog/840): the
    user typed it, and it can carry a provider's brand. It follows the channel rule
    rather than the account one - pseudonymized in its own column, not swept out of free
    text, because there are few of them and they are ordinary words ('Sports', 'News'), so
    a substring sweep would carve holes in the text it was meant to protect. meta.json
    states that limit rather than leaving a reader to assume the sweep was total.
    """
    out = _row_to_dict(group)
    if not sanitizer.include_names:
        out['name'] = f'group {group.id}'
    return out


class _PathStatter:
    """One os.stat() per path the bundle already names, emitted beside it.

    Deliberately I/O in a per-row loop, which everywhere else in this app is a defect
    (CLAUDE.md): the tables carrying paths are activity-scaled and therefore bounded by
    what the user did, and the answer is not derivable from the database at all. It is
    also the single most actionable fact in a report about a recording that went wrong -
    "retry the conversion" and "it is gone, re-record" are different advice.

    Two failure directions, told apart on purpose. A missing file is an ANSWER
    (exists: false), not an error - a wiped output directory is exactly what the reader
    is trying to learn. Any other OSError is a broken environment: /dvr is a network
    mount that has genuinely answered with a stale file handle mid-task, so hard errors
    trip a circuit breaker rather than letting a dead mount charge the export thousands
    of failing syscalls. Either way the export continues and meta.json reports what the
    pass managed to do.
    """

    MAX_CONSECUTIVE_ERRORS = 25

    def __init__(self):
        self.checked = 0
        self.errors = 0
        self.stopped = False
        self._consecutive = 0

    def stat(self, path):
        if not path:
            return None
        if self.stopped:
            return {'exists': None, 'error': (
                f'not checked: the stat pass stopped after '
                f'{self.MAX_CONSECUTIVE_ERRORS} consecutive failures')}
        try:
            size = os.stat(path).st_size
        except FileNotFoundError:
            self._consecutive = 0
            self.checked += 1
            return {'exists': False}
        except OSError as exc:
            self.errors += 1
            self._consecutive += 1
            if self._consecutive >= self.MAX_CONSECUTIVE_ERRORS:
                self.stopped = True
            return {'exists': None, 'error': str(exc)}
        self._consecutive = 0
        self.checked += 1
        return {'exists': True, 'size_bytes': size}

    def disclosure(self):
        out = {'paths_checked': self.checked, 'paths_erroring': self.errors}
        if self.stopped:
            out['stopped_early'] = (
                f'Stopped after {self.MAX_CONSECUTIVE_ERRORS} consecutive failures - the '
                'storage the remaining paths live on is probably unreachable. Paths after '
                'that point carry no answer rather than a wrong one.')
        return out


# /proc/meminfo keys worth reporting, and the JSON name each becomes. Values there are
# in kB regardless of the unit column, so each is multiplied up to bytes.
_MEMINFO_FIELDS = {
    'MemTotal': 'memory_total_bytes',
    'MemAvailable': 'memory_available_bytes',
    'SwapTotal': 'swap_total_bytes',
    'SwapFree': 'swap_free_bytes',
}


def _runtime_environment():
    """How this install is deployed, which no table records and every report depends on.

    A traceback reads completely differently depending on the answer. A PermissionError on
    a file inside the application tree is a broken install on a bare-metal host and an
    ordinary consequence of the container layout on a containerized one - /app is root-owned
    there while the app deliberately runs as an unprivileged PUID, so a file that arrived
    without world-read is unopenable and nothing else in the app is affected
    (dev/changelog/981). Without these three values a reader cannot tell those apart, and
    platform.platform() does not answer it: inside a container it reports the host's kernel.

    Deliberately no absolute paths. The symlink targets are the container's own fixed paths
    and identify nobody, while an install root is routinely a home directory carrying the
    user's name - which is the identity the rest of this module exists to remove.
    """
    out = {'containerized': bool(os.environ.get('CHANNELBIN_DOCKER'))}
    for name, fn in (('uid', 'getuid'), ('gid', 'getgid')):
        getter = getattr(os, fn, None)
        if getter is not None:
            out[name] = getter()
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name, rel in (('config_yaml_symlink', 'config.yaml'), ('instance_symlink', 'instance')):
        path = os.path.join(base, rel)
        try:
            # None on a normal install, where neither is a symlink. Inside the container both
            # must point into /config or an image upgrade silently discards what lives there.
            out[name] = os.readlink(path) if os.path.islink(path) else None
        except OSError as exc:
            out[name] = f'unreadable: {exc}'
    return out


def _host_resources():
    """What the host had left - the numbers that let a remote reader say anything about a
    machine that died mid-recording.

    Every value is individually optional and individually guarded. /proc is Linux-only
    and os.getloadavg() is not universal, so a missing key here means "this host could
    not answer", never a failed bundle - and nothing is added to requirements.txt for it.
    """
    out = {}
    cpus = os.cpu_count()
    if cpus:
        out['cpu_count'] = cpus
    try:
        out['load_average'] = [round(v, 2) for v in os.getloadavg()]
    except OSError:
        pass    # not available on this platform - reported by its absence
    try:
        with open('/proc/meminfo') as fh:
            for line in fh:
                key, _, rest = line.partition(':')
                name = _MEMINFO_FIELDS.get(key.strip())
                if not name:
                    continue
                value = rest.strip().split(' ', 1)[0]
                if value.isdigit():
                    out[name] = int(value) * 1024
    except OSError:
        pass    # non-Linux host, or /proc not mounted
    try:
        with open('/proc/uptime') as fh:
            out['uptime_seconds'] = int(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        pass    # same, plus a defensive guard on the file's shape
    try:
        with open('/proc/sys/kernel/random/boot_id') as fh:
            # Regenerated on every boot, so it identifies this run of the host and not
            # the host itself: two bundles sharing one means the machine did not restart
            # between them, which is the question it is here to answer.
            out['boot_id'] = fh.read().strip()
    except OSError:
        pass
    return out


def _apscheduler_jobs():
    """The scheduler's persisted jobs, id and next run time only.

    Not an ORM model - APScheduler owns this table through scheduler.py's
    SQLAlchemyJobStore - so it is read with raw SQL against the session's own bind, and
    its absence is a fact to report rather than an error: no table means the scheduler
    has never run on this database.
    """
    from sqlalchemy import inspect as sa_inspect, text
    if not sa_inspect(db.session.get_bind()).has_table('apscheduler_jobs'):
        return {'note': _APSCHEDULER_NOTE, 'job_count': 0, 'jobs': [],
                'table_missing': 'No apscheduler_jobs table - the scheduler has never '
                                 'started against this database.'}
    rows = db.session.execute(text(
        'SELECT id, next_run_time FROM apscheduler_jobs ORDER BY next_run_time')).all()
    jobs = []
    for job_id, next_run in rows:
        when = parse_epoch_utc(next_run)
        jobs.append({
            'id': job_id,
            # None where APScheduler has paused the job: it keeps the row and clears the
            # time, which is precisely the shape a "my recording never started" report is
            # about, so it must survive as a null rather than be dropped.
            'next_run_time': when.isoformat() if when else None,
        })
    return {'note': _APSCHEDULER_NOTE, 'job_count': len(jobs), 'jobs': jobs}


def _channel_event_rows():
    """(total row count, the newest _CHANNEL_EVENT_LIMIT rows oldest-first).

    Ordered newest-first by the query and reversed, so the window is the most recent
    events - a report is about something that just happened - while reading forwards like
    a log. See _CHANNEL_EVENT_LIMIT for why this one activity table is capped at all.
    """
    total = ChannelEvent.query.count()
    rows = (ChannelEvent.query
            .order_by(ChannelEvent.timestamp.desc(), ChannelEvent.id.desc())
            .limit(_CHANNEL_EVENT_LIMIT).all())
    rows.reverse()
    return total, rows


def _channel_pseudonyms(channels):
    """Stable pseudonyms for the channel columns that carry user- or provider-authored
    text, built in one pass over rows already in memory (no per-row query or config
    read - see CLAUDE.md's no-hidden-I/O rule).

    `name` is keyed on the channel's own id, which is unique by definition and stable
    across bundles from one install, so two bundles from the same user still line up.
    `category_name` and `epg_channel_id` are keyed on an index over their distinct
    values, sorted so the numbering is deterministic, because what makes them
    diagnostic is equality - do these 400 channels share a category, is this EPG id set
    and does it collide - rather than the text itself.
    """
    categories = sorted({c.category_name for c in channels if c.category_name})
    epg_ids = sorted({c.epg_channel_id for c in channels if c.epg_channel_id})
    return (
        {value: f'category {i}' for i, value in enumerate(categories, 1)},
        {value: f'epg {i}' for i, value in enumerate(epg_ids, 1)},
    )


def _channel_to_dict(ch, sanitizer, categories, epg_ids):
    out = {name: _json_value(getattr(ch, name)) for name in _CHANNEL_FIELDS}
    if not sanitizer.include_names:
        out['name'] = f'channel {ch.id}'
        if ch.category_name:
            out['category_name'] = categories[ch.category_name]
        if ch.epg_channel_id:
            out['epg_channel_id'] = epg_ids[ch.epg_channel_id]
    return out


def _implicated_channel_ids():
    """Ids of the channels something else in this bundle points at.

    One batched DISTINCT query per source plus one pass over the recording events
    already in memory - never a lookup per channel. The full table is six figures on a
    real install, so anything shaped like a per-row query here is the no-hidden-I/O
    defect at 137k x scale (CLAUDE.md).
    """
    ids = set()
    for model, column in _CHANNEL_ID_SOURCES:
        rows = db.session.query(column).filter(column.isnot(None)).distinct().all()
        ids.update(row[0] for row in rows)

    events = (RecordingEvent.query
              .filter(RecordingEvent.event_type.in_((GROUP_MEMBER_SELECTED, GROUP_FAILOVER)))
              .all())
    for ev in events:
        try:
            extra = json.loads(ev.extra_data) if ev.extra_data else {}
        except (ValueError, TypeError):
            continue
        if not isinstance(extra, dict):
            continue
        for key in _EVENT_CHANNEL_KEYS:
            value = extra.get(key)
            if isinstance(value, int):
                ids.add(value)
    return ids


def _url_shape(url):
    """(scheme, extension) for a stream URL - everything about it that survives having
    its host and path redacted away. Either half is None when the URL does not have one;
    an extension outside _URL_EXTENSIONS is reported as None rather than passed through,
    since it is far more likely to be a piece of a hostname than a media container."""
    if not url:
        return None, None
    match = _URL_SCHEME_RE.match(url)
    if not match:
        return None, None
    scheme = match.group(1).lower()
    path = url[match.end():].split('?', 1)[0].split('#', 1)[0]
    last = path.rsplit('/', 1)[-1]
    ext = last.rsplit('.', 1)[-1].lower() if '.' in last else None
    return scheme, (ext if ext in _URL_EXTENSIONS else None)


def _channel_aggregate():
    """Every channel counted by the facets that stay diagnostic after redaction.

    Counts cover the whole table, the individually-shipped rows included, so the totals
    answer "how many channels does this install have, and of what shape" honestly rather
    than describing the leftovers. On the 137,283-channel install this was measured
    against, the result is 20 rows.

    Reads five columns rather than whole Channel objects, streamed, because materializing
    six figures of ORM instances to count them is the cost this whole change exists to
    remove.
    """
    query = db.session.query(
        Channel.account_id, Channel.stream_url, Channel.in_guide,
        Channel.test_enabled, Channel.is_duplicate_stream_url,
    ).yield_per(1000)

    counts = Counter()
    total = 0
    for account_id, stream_url, in_guide, test_enabled, is_dup in query:
        scheme, ext = _url_shape(stream_url)
        counts[(account_id, scheme, ext, bool(in_guide),
                bool(test_enabled), bool(is_dup))] += 1
        total += 1

    rows = [{
        'account_id': key[0],
        'url_scheme': key[1],
        'url_extension': key[2],
        'in_guide': key[3],
        'test_enabled': key[4],
        'is_duplicate_stream_url': key[5],
        'count': count,
    } for key, count in counts.items()]
    # Deterministic order so two bundles from one install diff cleanly. None sorts
    # against a string in Python 3, hence the '' stand-ins in the key only.
    rows.sort(key=lambda r: (r['account_id'], r['url_scheme'] or '',
                             r['url_extension'] or '', r['in_guide'],
                             r['test_enabled'], r['is_duplicate_stream_url']))
    return total, rows


def _redaction_disclosure(sanitizer):
    """What this bundle withheld and what it did not, stated in the bundle itself.

    Product principle 1: a filtered artifact must never present itself as the whole
    truth. A reader who finds 'account 2' where a name should be needs to know it was
    pseudonymized rather than empty, and a reader who trusts the sweep needs to know
    where it stops.
    """
    if sanitizer.include_names:
        return {
            'mode': 'names included at the user\'s request',
            'note': ('Real account and channel names are present in this bundle because '
                     'the user explicitly opted in. Every URL is still redacted.'),
        }
    return {
        'mode': 'names pseudonymized',
        'pseudonymized_fields': [
            'accounts.name', 'channels.name', 'channels.category_name',
            'channels.epg_channel_id', 'channel_groups.name',
        ],
        'account_name_occurrences_swept_from_free_text': sanitizer.swept,
        'account_names_too_short_to_sweep_safely': sanitizer.skipped_short,
        'known_limit': (
            'Channel and group names are pseudonymized in their own columns but are not '
            'swept out of free-text values elsewhere (alert bodies, recording names, '
            'event details). There are too many of them and most are ordinary words, so '
            'a substring sweep would corrupt the text it was meant to protect. A channel '
            'or group name the user typed into a recording title can therefore still '
            'appear here.'),
    }


def _build_meta(sanitizer, statter):
    # The app refuses to start if a pending migration's backup/apply fails (see
    # app/migrations.py), so by the time this route can be reached, the live DB's
    # PRAGMA user_version already equals CURRENT_SCHEMA_VERSION - no need for a second,
    # DB-querying code path just to re-derive the same number.
    from .config import load_config
    from .migrations import CURRENT_SCHEMA_VERSION
    cfg = load_config()
    return {
        'generated_at': datetime.utcnow().isoformat(),
        'app_version': __version__,
        'schema_version': CURRENT_SCHEMA_VERSION,
        'config_version': cfg.get('config_version'),
        'python_version': sys.version,
        'platform': platform.platform(),
        'runtime': _runtime_environment(),
        'host': _host_resources(),
        'filesystem_check': statter.disclosure(),
        'redactions': _redaction_disclosure(sanitizer),
    }


def _is_noise_record(source, message):
    """True for a dvr.log record the bundle's log tail should drop: HTTP access
    logging (werkzeug logs every request at INFO regardless of status) or a routine
    APScheduler job start/finish line. Both are high-volume and near-zero diagnostic
    value on their own - a job that actually raised logs a different message/level
    from the same source and survives this check untouched."""
    if source == 'werkzeug':
        return True
    if source == 'apscheduler.executors.default' and (
        message.startswith(_APSCHEDULER_ROUTINE_PREFIX)
        or (message.startswith('Job "') and message.endswith(_APSCHEDULER_ROUTINE_SUFFIX))
    ):
        return True
    return False


def _filter_log_noise(raw_lines, want):
    """Group `raw_lines` (oldest-first) into records - a timestamped header line plus
    any continuation lines that follow it (a traceback frame carries no timestamp of
    its own) - drop the noise records, and keep the last `want` surviving records.

    A continuation line takes the fate of the header it follows; one with no header
    yet in this scan window (the very first line of the window landed mid-traceback,
    or it never matched the log format at all) is kept as its own single-line record
    rather than silently discarded - there's no way to know what it belonged to. Only
    a line immediately following a genuine timestamped header counts as a
    continuation - otherwise a run of non-matching lines (or two adjacent single-line
    records neither of which matched) would collapse into one group instead of
    staying separate.

    Returns (kept_lines, kept_record_count, noise_record_count, total_record_count).
    """
    from .routes.logs import LOG_RE
    groups = []
    last_is_header = False
    for line in raw_lines:
        m = LOG_RE.match(line)
        if m:
            groups.append([_is_noise_record(m.group(3).strip(), m.group(4)), [line]])
            last_is_header = True
        elif last_is_header:
            groups[-1][1].append(line)
        else:
            groups.append([False, [line]])
    kept_groups = [g[1] for g in groups if not g[0]]
    noise_count = len(groups) - len(kept_groups)
    surviving = kept_groups[-want:] if len(kept_groups) > want else kept_groups
    out_lines = [line for group in surviving for line in group]
    return out_lines, len(surviving), noise_count, len(groups)


def _build_log_tail():
    from .config import load_config
    log_path = load_config().get('logging', {}).get('file')
    if not log_path:
        return '(logging.file is unset - app logs to stdout, not captured here)\n'
    from .routes.logs import _tail_lines
    try:
        raw_lines = _tail_lines(log_path, _LOG_SCAN_LINES)
    except OSError as exc:
        return f'(could not read {log_path}: {exc})\n'
    kept_lines, kept_count, noise_count, total_records = _filter_log_noise(
        raw_lines, _LOG_TAIL_LINES)
    if not total_records:
        return '\n'.join(kept_lines) + '\n'
    note = (
        f'(kept the last {kept_count} application log records of {total_records} '
        f'scanned; dropped {noise_count} as werkzeug/APScheduler routine noise)\n'
    )
    return note + '\n'.join(kept_lines) + '\n'


def build_support_bundle(include_names: bool = False) -> bytes:
    """Assemble the sanitized support bundle and return the zip's raw bytes.

    `include_names` is the user's explicit opt-in to shipping real account and channel
    names; it defaults to off, and meta.json records which mode produced the bundle
    either way.

    Table-by-table: a single table's query/serialize failure is caught, logged, and
    recorded in errors.json inside the bundle rather than aborting the whole export. Only
    if every single piece fails does this raise, so the route can return a proper JSON
    error instead of a silently empty zip.
    """
    from .config import load_config

    buf = BytesIO()
    errors = {}
    wrote_anything = False
    # Shared by every table that names a path, so meta.json's disclosure covers the whole
    # export and one unreachable mount trips the breaker once rather than per table.
    statter = _PathStatter()

    # Built before anything is written: every _write_* call sanitizes through it, so it
    # has to know the account names before the first file lands in the zip.
    #
    # This one failure is deliberately fatal rather than per-table, unlike everything
    # below. Without the account list there is no way to sweep account names out of
    # alert bodies, event details and the log tail, so degrading here would not produce
    # a bundle missing one file - it would produce a bundle that leaks the exact strings
    # this pass exists to remove, while still looking complete. Failing closed is the
    # right direction when the two trade off.
    try:
        sanitizer = _Sanitizer(Account.query.all(), include_names=include_names)
    except Exception as exc:
        log.error('Support bundle: cannot read accounts, refusing to build: %s', exc)
        raise RuntimeError(
            'Support bundle export failed: the account list could not be read, so '
            f'account names could not be removed from the bundle ({exc})') from exc

    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:

        def _put(filename, text):
            nonlocal wrote_anything
            zf.writestr(filename, text)
            wrote_anything = True

        def _write_text(filename, text):
            """Free-form text (dvr.log). Its newlines are real characters, so they
            terminate a URL run on their own and sanitizing the whole blob is correct."""
            _put(filename, sanitizer.text(text))

        def _write_json(filename, obj):
            """Structured data: sanitize per value (_redact_deep), then serialize.

            The second sweep over the serialized text is belt-and-braces, not the
            mechanism - _redact_deep has already covered every string, and re-running
            over its output is a no-op: redact_urls_in_text emits
            '<scheme>://[url redacted]' and '[' is outside the URL character class so
            nothing it produced can match again, and the name sweep is a literal
            substring replacement that cannot run past what it matched (which is
            exactly why it is safe to repeat over serialized JSON where the URL regex
            was not - dev/changelog/837). It can therefore only fire on a value the
            deep walk missed, where over-redacting beats shipping the value.
            """
            text = json.dumps(
                _redact_deep(obj, sanitizer), indent=2,
                default=lambda o: _redacted_default(o, sanitizer))
            _put(filename, sanitizer.text(text))

        try:
            _write_json('config.yaml', mask_config(load_config()))
        except Exception as exc:
            log.error('Support bundle: config.yaml failed: %s', exc)
            errors['config.yaml'] = str(exc)

        try:
            _write_text('dvr.log', _build_log_tail())
        except Exception as exc:
            log.error('Support bundle: dvr.log failed: %s', exc)
            errors['dvr.log'] = str(exc)

        try:
            accounts = [_account_to_dict(a, sanitizer) for a in Account.query.all()]
            _write_json('accounts.json', accounts)
        except Exception as exc:
            log.error('Support bundle: accounts.json failed: %s', exc)
            errors['accounts.json'] = str(exc)

        # Before channels.json, not after: the channels named by the events that actually
        # ship are part of what channels.json has to cover, and the cap means that set is
        # only knowable from the written rows. Zip entry order carries no meaning.
        event_channel_ids = set()
        try:
            total_events, event_rows = _channel_event_rows()
            event_channel_ids = {e.channel_id for e in event_rows if e.channel_id}
            _write_json('channel_events.json', {
                'note': _CHANNEL_EVENTS_NOTE,
                'total_event_count': total_events,
                'included_event_count': len(event_rows),
                'events': [_row_to_dict(e) for e in event_rows],
            })
        except Exception as exc:
            log.error('Support bundle: channel_events.json failed: %s', exc)
            errors['channel_events.json'] = str(exc)

        try:
            implicated = _implicated_channel_ids() | event_channel_ids
            rows = (Channel.query.filter(Channel.id.in_(implicated)).all()
                    if implicated else [])
            categories, epg_ids = _channel_pseudonyms(rows)
            payload = {
                'note': _CHANNELS_NOTE,
                'included_because': _CHANNELS_INCLUSION_RULE,
                'included_channel_count': len(rows),
                'channels': [_channel_to_dict(c, sanitizer, categories, epg_ids)
                             for c in rows],
            }
            # Nested rather than folded into the outer guard: the aggregate is a whole-
            # table scan and the individually-shipped rows are the part a reader actually
            # follows, so losing the former must not cost the latter.
            try:
                total, aggregate = _channel_aggregate()
                payload['total_channel_count'] = total
                payload['aggregate'] = aggregate
            except Exception as exc:
                log.error('Support bundle: channels.json aggregate failed: %s', exc)
                errors['channels.json:aggregate'] = str(exc)
                payload['aggregate_error'] = (
                    'The per-shape channel counts could not be built; the channels listed '
                    'above are the implicated ones only and are not the whole table.')
            _write_json('channels.json', payload)
        except Exception as exc:
            log.error('Support bundle: channels.json failed: %s', exc)
            errors['channels.json'] = str(exc)

        try:
            groups = [_group_to_dict(g, sanitizer) for g in ChannelGroup.query.all()]
            _write_json('channel_groups.json', groups)
        except Exception as exc:
            log.error('Support bundle: channel_groups.json failed: %s', exc)
            errors['channel_groups.json'] = str(exc)

        for model, filename in _FULL_ROW_TABLES:
            try:
                path_fields = _PATH_FIELDS.get(model, ())
                rows = []
                for row in model.query.all():
                    out = _row_to_dict(row)
                    for field in path_fields:
                        out[f'{field}_stat'] = statter.stat(out.get(field))
                    rows.append(out)
                _write_json(filename, rows)
            except Exception as exc:
                log.error('Support bundle: %s failed: %s', filename, exc)
                errors[filename] = str(exc)

        try:
            _write_json('apscheduler_jobs.json', _apscheduler_jobs())
        except Exception as exc:
            log.error('Support bundle: apscheduler_jobs.json failed: %s', exc)
            errors['apscheduler_jobs.json'] = str(exc)

        # Last, not first: its redaction disclosure reports how many account-name
        # occurrences were swept out of free text, and its filesystem_check reports what
        # the stat pass managed - neither is final until every other file has been built.
        # Zip entry order carries no meaning.
        try:
            _write_json('meta.json', _build_meta(sanitizer, statter))
        except Exception as exc:
            log.error('Support bundle: meta.json failed: %s', exc)
            errors['meta.json'] = str(exc)

        if errors:
            try:
                _write_json('errors.json', errors)
            except Exception as exc:
                log.error('Support bundle: errors.json itself failed: %s', exc)

    if not wrote_anything:
        raise RuntimeError('Support bundle export failed entirely: ' + json.dumps(errors))

    return buf.getvalue()
