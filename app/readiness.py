"""The Readiness check: one place that answers "is this install set up correctly", and says
what it tested and what it found for every part of that answer.

The app already knew almost all of this - the toolchain card, the storage alert, each
account's status, the guide-group invariant, the search-index state, the notification
services - but it was scattered across Maintenance, Alerts and the Dashboard, so the person
who had to piece it together was the user. This is product principle 1 applied to setup:
bring forward what is normally hidden, and name the reason. Design record:
`dev/mockups/40-readiness.html`, `dev/changelog/945`, `948`, `950`.

**The registry is the design.** A check is one CHECKS entry - what area it belongs to, what
it claims, the noun it reads as inside a sentence, what it costs to run, what breaks without
it, how it is fixed, and whether the user is allowed to silence it. Adding a check is one
entry; nothing else in the app changes. CAPABILITIES is a second, tiny registry naming which
checks stand behind each thing a person might want to do, so one broken check surfaces in
every place it actually matters rather than as a line item nobody can price.

**Three rules this module exists to keep:**

* **Nothing expensive happens because someone looked at the page.** The three ON_DEMAND
  checks spawn a process, log in to a provider or send a real message, and they run only
  when asked. An on-demand check nobody has run reports NOT_RUN and never borrows the
  verdict it would have had.
* **A check that could not answer is never rendered as a pass.** UNKNOWN outranks READY and
  drags its capability down with it; NOT_RUN deliberately does not, or the whole card would
  be grey on every load, which is the opposite of the rule above.
* **No hidden I/O.** Every check is a pure function of a Context built once per evaluation
  with a fixed number of queries, so the payload costs the same on an install with four
  channels and one with 136,130 (`tests/test_scaling_pages.py`).

**Silencing.** A check the user has told the app not to care about stops counting toward the
verdict and the nav counts, and is drawn dimmed - never hidden, and never with its real state
replaced. Which checks may be silenced is a registry flag rather than a blanket right,
because "I do not want notifications" is a preference and "ffmpeg is missing" is not: a page
that lets you silence the second one is a page that lies. An ignore persists until it is
removed; it does not self-clear when the check starts passing (both deliberate,
`dev/changelog/950`).

Storing it in `UserPref` rather than localStorage is the same rule every other piece of UI
config follows (`DESIGN.md` 3.11): the answer is the user's, so it follows them across
browsers.
"""
import logging
import os
import threading
from collections import namedtuple
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# The states
# ─────────────────────────────────────────────────────────────────────────────
#: Checked, and it is fine.
READY = 'ready'
#: It works today, but it is degraded or it will bite you later.
ATTENTION = 'attention'
#: Something is broken right now.
PROBLEM = 'problem'
#: The check itself could not run. Never a pass - see the module docstring.
UNKNOWN = 'unknown'
#: There is nothing on this install for this check to look at.
NOTHING = 'nothing'
#: Costs real work, and nobody has asked for it yet.
NOT_RUN = 'not_run'

#: Every state, in the order a verdict takes its color from. Callers branch over this
#: explicitly; a trailing `else` here is what lets the next state added render as something
#: it is not (CLAUDE.md's status-enum rule). `checking` is deliberately absent - it is a
#: client-side transient while a run is in flight and is never stored or returned.
STATUSES = (READY, ATTENTION, PROBLEM, UNKNOWN, NOTHING, NOT_RUN)

#: How bad a state is, for picking a capability's own state and the verdict's color. -1
#: means the state is not a verdict at all and never colors anything above it.
STATUS_RANK = {
    READY: 0,
    UNKNOWN: 1,
    ATTENTION: 2,
    PROBLEM: 3,
    NOTHING: -1,
    NOT_RUN: -1,
}

#: What a check costs to answer. CHEAP runs on every evaluation; ON_DEMAND waits to be asked.
CHEAP = 'cheap'
ON_DEMAND = 'ondemand'

# ─────────────────────────────────────────────────────────────────────────────
# A capability's own state
# ─────────────────────────────────────────────────────────────────────────────
CAN = 'can'              # every check behind it is ready
CANNOT = 'cannot'        # at least one is a problem
DEGRADED = 'degraded'    # at least one needs attention
CAP_UNKNOWN = 'unknown'  # at least one could not be checked
NOT_SET_UP = 'none'      # at least one has nothing to look at yet

CAP_STATES = (CANNOT, DEGRADED, CAP_UNKNOWN, NOT_SET_UP, CAN)

#: The mark drawn on the capability row, and the left-edge color class `.grp-item` keys off.
CAP_MARK = {CAN: '✓', CANNOT: '×', DEGRADED: '!', CAP_UNKNOWN: '?', NOT_SET_UP: '–'}
CAP_HEALTH = {CAN: 'st-ok', CANNOT: 'st-bad', DEGRADED: 'st-warn',
              CAP_UNKNOWN: 'st-warn', NOT_SET_UP: 'st-none'}

#: Areas exist as the registry's own organization and as the grouping in the copied report.
#: The page itself does not draw them - the capability list replaced that framing.
AREAS = (
    ('machine', 'This machine'),
    ('provider', 'Your provider'),
    ('content', 'What you will record'),
    ('told', 'Being told'),
)

#: The UserPref key holding the silenced check ids, as a JSON list.
IGNORED_PREF_KEY = 'readiness_ignored'

# ─────────────────────────────────────────────────────────────────────────────
# Registry shapes
# ─────────────────────────────────────────────────────────────────────────────
#: One check. `run` takes a Context and returns a Result.
#:
#: `short` is the check read as a noun ("the recording folder"), used only inside the
#: capability's reason sentence - a `label` is the check's own claim ("The recording folder
#: works"), which is right beside a state pill and wrong in the middle of a sentence.
#: `without` is what stops working, and it is what the verdict's second line quotes.
Check = namedtuple('Check', 'id area label short cost without link ignorable run note')
Check.__new__.__defaults__ = (None, False, None, None)

#: Where a check is fixed when it cannot be fixed in place. `endpoint` is resolved through
#: url_for at evaluation time, so a renamed view function fails the test suite rather than
#: shipping a dead button (the Jinja-hazard rule, applied to a JSON payload).
Link = namedtuple('Link', 'label endpoint')

#: What one check found. `action` is the one-click fix, present only when there genuinely is
#: one. Act in place where it is one click, and send the user to the page that owns it
#: otherwise - never both on the same row (dev/changelog/950).
Result = namedtuple('Result', 'status tested found action detail')
Result.__new__.__defaults__ = (None, None)

#: A one-click fix: a label and the URLs to POST, in order. A list rather than one URL
#: because "sync the 2 stale accounts" is still one click for the user.
Action = namedtuple('Action', 'label urls')


def _cap(*ids):
    return tuple(ids)


# ─────────────────────────────────────────────────────────────────────────────
# The context: every fact the checks read, gathered once
# ─────────────────────────────────────────────────────────────────────────────
class Context:
    """Everything the checks need, fetched once with a fixed number of queries.

    Built per evaluation rather than per check, which is the whole reason the payload does
    not scale with row count: a check is arithmetic over attributes of this object and
    reaches neither the disk nor the database on its own.
    """

    def __init__(self):
        from flask import current_app
        from sqlalchemy import func
        from . import db
        from .channel_groups import guide_groups_missing_recording_member
        from .config import load_config
        from .database import (Account, Alert, Channel, ChannelGroup, ChannelTest,
                               ChannelGroupMember, EPGEntry)
        from .fs_utils import filesystem_type, is_network_filesystem, probe_dir
        from .search_index import readiness_map, SEARCH_INDEX_CHANNELS, SEARCH_INDEX_NAMES
        from .toolchain import describe_tools
        from .tz_utils import get_display_tz_name
        # _disk_bytes lives in the route module because the sidebar meter and the
        # Maintenance card were its only readers. Imported here rather than at module
        # scope so app/ does not import app/routes/ at import time; it is the canonical
        # owner of this statvfs and of the standing storage alert that rides on it, and a
        # second spelling here would be a second answer to "is the DVR folder usable".
        from .routes.system import _disk_bytes, DVR_DIR_ROLE

        self.now = datetime.utcnow()
        self.cfg = load_config()
        rec_cfg = self.cfg['recording']
        sync_cfg = self.cfg.get('sync', {})

        self.tools = describe_tools()
        self.dvr_dir = rec_cfg['dvr_output_dir']
        self.dvr_probe = probe_dir(self.dvr_dir)
        self.disk_total, self.disk_free = _disk_bytes(self.dvr_dir, DVR_DIR_ROLE)

        self.db_path = self.cfg['database']['path']
        self.db_fs = filesystem_type(self.db_path)
        self.db_on_network = is_network_filesystem(self.db_fs)
        self.db_writable, self.db_write_error = _probe_db_write()

        self.tz_name = get_display_tz_name()
        self.secret_key_set = bool(current_app.config.get('SECRET_KEY'))
        self.auth_cfg = current_app.config.get('AUTH') or {}
        self.docker = bool(os.environ.get('CHANNELBIN_DOCKER'))

        self.scheduler_jobs = None
        from .scheduler import get_scheduler
        scheduler = get_scheduler()
        self.scheduler_running = bool(scheduler and scheduler.running)
        if self.scheduler_running:
            try:
                self.scheduler_jobs = len(scheduler.get_jobs())
            except Exception as exc:                      # pragma: no cover - defensive
                log.warning('readiness: could not list scheduler jobs: %s', exc)

        self.accounts = Account.query.order_by(Account.name).all()
        self.default_sync_interval = sync_cfg.get('sync_interval_hours', 12)

        epg_total, epg_horizon = (db.session.query(func.count(EPGEntry.id),
                                                   func.max(EPGEntry.stop_time)).one())
        self.epg_total = epg_total or 0
        self.epg_horizon = epg_horizon

        self.guide_channels = (db.session.query(func.count(Channel.id))
                               .filter(Channel.in_guide.is_(True)).scalar() or 0)
        self.broken_guide_groups, self.guide_groups = guide_groups_missing_recording_member()

        member_ids = [row[0] for row in
                      db.session.query(ChannelGroupMember.channel_id)
                      .join(ChannelGroup, ChannelGroup.id == ChannelGroupMember.group_id)
                      .filter(ChannelGroup.in_guide.is_(True),
                              ChannelGroup.is_system.is_(False),
                              ChannelGroupMember.recording_enabled.is_(True))
                      .distinct().all()]
        self.recording_members = len(member_ids)
        self.tested_members = 0
        if member_ids:
            self.tested_members = (db.session.query(
                func.count(func.distinct(ChannelTest.channel_id)))
                .filter(ChannelTest.channel_id.in_(member_ids)).scalar() or 0)

        readiness = readiness_map()
        self.index_channels = readiness[(SEARCH_INDEX_CHANNELS,)]
        self.index_programs = readiness[SEARCH_INDEX_NAMES]

        notif = self.cfg.get('notifications', {})
        services = notif.get('services', {}) or {}
        self.notify_services = sorted(services)
        self.notify_enabled = sorted(name for name, svc in services.items()
                                     if isinstance(svc, dict) and svc.get('enabled'))

        rows = (db.session.query(Alert.severity, func.count(Alert.id))
                .filter(Alert.dismissed_at.is_(None))
                .group_by(Alert.severity).all())
        self.open_alerts = {sev: n for sev, n in rows}


def _probe_db_write():
    """(writable, why not) for the live database, without writing anything that lasts.

    A SAVEPOINT that is rolled back: it takes the same write path a real commit does - the
    journal, the lock, the read-only check - and leaves nothing behind. `PRAGMA
    quick_check` would answer a different question (is the file corrupt) and a bare SELECT
    answers none at all, since a read-only mount and a full disk both read perfectly.
    """
    from . import db
    try:
        db.session.begin_nested()
        db.session.execute(db.text(
            'CREATE TEMP TABLE IF NOT EXISTS readiness_write_probe (x INTEGER)'))
        db.session.rollback()
        return True, None
    except Exception as exc:                              # noqa: BLE001 - reported, not swallowed
        log.warning('readiness: database write probe failed: %s', exc)
        try:
            db.session.rollback()
        except Exception:                                 # pragma: no cover - defensive
            log.warning('readiness: rollback after a failed write probe also failed')
        return False, str(exc).strip().splitlines()[0] if str(exc).strip() else 'refused'


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers - plain text only
# ─────────────────────────────────────────────────────────────────────────────
def _ago(when, now):
    if when is None:
        return 'never'
    hours = (now - when).total_seconds() / 3600
    if hours < 1:
        return f'{max(1, int(round(hours * 60)))} minutes ago'
    if hours < 48:
        return f'{int(round(hours))} hours ago'
    return f'{int(round(hours / 24))} days ago'


def _ahead(when, now):
    if when is None:
        return 'nothing'
    hours = (when - now).total_seconds() / 3600
    if hours < 0:
        return f'already ended {_ago(when, now)}'
    if hours < 48:
        return f'{int(round(hours))} more hours'
    return f'{int(round(hours / 24))} more days'


def _tb(value):
    return f'{value / 2 ** 40:.1f} TB'


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + 's')


# ─────────────────────────────────────────────────────────────────────────────
# The checks
# ─────────────────────────────────────────────────────────────────────────────
#: How a resolved binary was found, in the same words the Maintenance card uses - the two
#: surfaces sit on one page and must not describe the same fact differently.
_TOOL_SOURCE = {
    'path': 'found on PATH',
    'sibling': 'found beside the configured ffmpeg',
    'configured': 'set in Settings',
}


def _tool_result(info, name):
    tested = f"Ran {info.get('path') or name} -version"
    if not info.get('found'):
        return Result(PROBLEM, tested,
                      f"It could not be run at {info.get('path') or info.get('configured') or name}")
    source = _TOOL_SOURCE.get(info.get('source'), 'resolved')
    return Result(READY, tested, f"Version {info.get('version') or 'unknown'}, {source}")


def _check_ffmpeg(ctx):
    return _tool_result(ctx.tools['ffmpeg'], 'ffmpeg')


def _check_ffprobe(ctx):
    return _tool_result(ctx.tools['ffprobe'], 'ffprobe')


def _check_ffmpeg_build(ctx):
    """On-demand: what this ffmpeg build actually carries, component by component."""
    from .toolchain import CAPABILITIES, describe_capabilities
    tested = ("Listed this build's filters and codecs and looked for the "
              f'{len(CAPABILITIES)} components ChannelBin invokes')
    if not ctx.tools['ffmpeg'].get('found'):
        return Result(NOTHING, tested, 'There is no ffmpeg to describe')
    described = describe_capabilities(ctx.tools['ffmpeg'])
    if described is None:
        return Result(UNKNOWN, tested, 'This build could not be listed at all')
    missing = [c['label'] for c in described if c['available'] is False]
    unsure = [c['label'] for c in described if c['available'] is None]
    detail = [{'label': c['label'], 'available': c['available'],
               'used_for': c['used_for'], 'without': c['without']} for c in described]
    if missing:
        return Result(ATTENTION, tested, 'Missing: ' + ', '.join(missing), None, detail)
    if unsure:
        return Result(UNKNOWN, tested, 'Could not tell about: ' + ', '.join(unsure), None, detail)
    return Result(READY, tested, f'All {len(described)} present', None, detail)


def _check_storage_dvr(ctx):
    from .fs_utils import PATH_OK, describe_dir_problem
    tested = f'Probed the recording folder at {ctx.dvr_dir}'
    if ctx.dvr_probe.outcome != PATH_OK:
        return Result(PROBLEM, tested, describe_dir_problem(ctx.dvr_dir, ctx.dvr_probe))
    if not os.access(ctx.dvr_dir, os.W_OK):
        return Result(PROBLEM, tested, f'{ctx.dvr_dir} exists but this process may not write to it')
    return Result(READY, tested, f'{ctx.dvr_dir} is there and writable')


def _check_storage_space(ctx):
    tested = f'Asked the filesystem holding {ctx.dvr_dir} how much is left'
    if ctx.disk_total is None:
        return Result(UNKNOWN, tested,
                      'No honest answer: the filesystem that holds it did not respond')
    used_pct = round((ctx.disk_total - ctx.disk_free) / ctx.disk_total * 100, 1) if ctx.disk_total else 0
    found = f'{_tb(ctx.disk_free)} free, {used_pct}% used'
    if used_pct > 95:
        return Result(PROBLEM, tested, found)
    if used_pct > 85:
        return Result(ATTENTION, tested, found)
    return Result(READY, tested, found)


def _check_db_local(ctx):
    tested = f'Looked up the filesystem under {ctx.db_path} in /proc/mounts'
    if ctx.db_fs is None:
        return Result(UNKNOWN, tested, 'Nothing in the mount table covers that path')
    if ctx.db_on_network:
        return Result(PROBLEM, tested, f'{ctx.db_fs}, which is a network filesystem')
    return Result(READY, tested, f'{ctx.db_fs}, which is local')


def _check_db_write(ctx):
    tested = 'Opened a write transaction against the live database and rolled it back'
    if not ctx.db_writable:
        return Result(PROBLEM, tested, f'Refused: {ctx.db_write_error}')
    return Result(READY, tested, 'It accepted the write')


def _check_scheduler(ctx):
    tested = 'Asked APScheduler whether it is running and what it has registered'
    if not ctx.scheduler_running:
        return Result(PROBLEM, tested, 'The scheduler is not running in this process')
    if ctx.scheduler_jobs is None:
        return Result(UNKNOWN, tested, 'It is running, but its job list could not be read')
    return Result(READY, tested,
                  f'Running, with {ctx.scheduler_jobs} {_plural(ctx.scheduler_jobs, "job")} registered')


def _check_timezone(ctx):
    tested = 'Resolved the configured display zone against the system zone database'
    if not ctx.tz_name:
        return Result(PROBLEM, tested, 'No display timezone resolved, so every time reads as UTC')
    return Result(READY, tested, ctx.tz_name)


def _check_secret_key(ctx):
    # Never names the key or any part of it: flask.secret_key is a sensitive config leaf
    # (CLAUDE.md Config secrets), so this reports a boolean and nothing else.
    tested = 'Checked that a session signing key is stored'
    if not ctx.secret_key_set:
        return Result(PROBLEM, tested, 'No key is set')
    return Result(READY, tested, 'A key is set (never shown here or anywhere else)')


def _check_auth_gate(ctx):
    from .auth import gate_active, gate_enabled_without_password
    tested = 'Compared the login switch against whether a password is actually stored'
    if gate_enabled_without_password(ctx.auth_cfg):
        return Result(PROBLEM, tested, 'Login is on but no password is stored')
    if gate_active(ctx.auth_cfg):
        return Result(READY, tested, 'Login is on and a password is set')
    return Result(READY, tested, 'Login is off, which is a choice this app supports')


def _check_accounts_any(ctx):
    tested = 'Counted the provider accounts that are set up'
    if not ctx.accounts:
        return Result(PROBLEM, tested, 'No accounts are set up')
    channels = sum(a.channel_count or 0 for a in ctx.accounts)
    return Result(READY, tested,
                  f'{len(ctx.accounts)} {_plural(len(ctx.accounts), "account")}, '
                  f'{channels:,} channels between them')


def _check_account_login(ctx):
    """On-demand: log in to each provider account once, with its stored credentials.

    Deliberately not gated on whether a recording is running. This is a `player_api.php`
    call to the account's API endpoint, which is a different service from the stream
    endpoint the capture is on (CLAUDE.md) - it takes no stream connection and cannot
    starve a recording of one. One call per account, only when asked, is also nowhere near
    enough traffic to put the account at risk.
    """
    from .xtream_client import XtreamClient
    tested = 'Logged in to each account once, with the credentials stored for it'
    if not ctx.accounts:
        return Result(NOTHING, tested, 'There are no accounts to log in to')
    xtream = [a for a in ctx.accounts if a.account_type == 'xtream']
    if not xtream:
        return Result(NOTHING, 'Looked for an account with a login endpoint to test',
                      'Every account is an M3U/XMLTV pair, which has no login step - its URLs '
                      'are only exercised by a sync')
    timeout = ctx.cfg.get('sync', {}).get('request_timeout_seconds', 30)
    failures = []
    for account in xtream:
        try:
            XtreamClient(account.base_url, account.username, account.password,
                         timeout=timeout, cfg=ctx.cfg).check_auth()
        except Exception as exc:                          # noqa: BLE001 - reported, not swallowed
            failures.append(f'{account.name}: {str(exc).strip().splitlines()[0]}')
    skipped = len(ctx.accounts) - len(xtream)
    tail = f' ({skipped} M3U {_plural(skipped, "account")} have no login step)' if skipped else ''
    if failures:
        return Result(PROBLEM, tested,
                      f'{len(failures)} of {len(xtream)} refused: ' + '; '.join(failures) + tail)
    return Result(READY, tested,
                  f'All {len(xtream)} answered and authenticated{tail}')


def _check_account_sync(ctx):
    tested = "Compared each account's last successful sync against its own interval"
    if not ctx.accounts:
        return Result(NOTHING, tested, 'There are no accounts to sync')
    bad = [a for a in ctx.accounts if a.status == 'ERROR']
    never = [a for a in ctx.accounts if a.last_sync_at is None and a.status != 'ERROR']
    overdue = []
    worst_ratio = 0.0
    for account in ctx.accounts:
        if account.last_sync_at is None:
            continue
        interval = account.sync_interval_hours or ctx.default_sync_interval
        age = (ctx.now - account.last_sync_at).total_seconds() / 3600
        ratio = age / interval if interval else 0
        worst_ratio = max(worst_ratio, ratio)
        if ratio > 1:
            overdue.append((account, age, interval))
    action = None
    stale = [a for a, _age, _iv in overdue] + never
    if stale:
        action = Action(f'Sync {len(stale)} {_plural(len(stale), "account")} now',
                        [f'/api/accounts/{a.id}/sync' for a in stale])
    if bad:
        return Result(PROBLEM, tested,
                      f'{len(bad)} of {len(ctx.accounts)} last failed to sync: '
                      + ', '.join(a.name for a in bad), action)
    if never:
        return Result(PROBLEM, tested,
                      f'{len(never)} of {len(ctx.accounts)} {_plural(len(never), "has", "have")} '
                      'never synced: ' + ', '.join(a.name for a in never), action)
    if worst_ratio > 2:
        return Result(PROBLEM, tested,
                      f'{len(overdue)} of {len(ctx.accounts)} {_plural(len(overdue), "is", "are")} '
                      'more than two intervals overdue', action)
    if overdue:
        account, age, interval = max(overdue, key=lambda row: row[1] / row[2])
        return Result(ATTENTION, tested,
                      f'{account.name} last synced {int(round(age))} hours ago, '
                      f'on a {interval} hour interval', action)
    newest = max(a.last_sync_at for a in ctx.accounts)
    return Result(READY, tested, f'Every account is current, the oldest {_ago(newest, ctx.now)}')


def _check_epg_loaded(ctx):
    tested = 'Counted the stored program entries and looked at how far ahead they run'
    if not ctx.epg_total:
        return Result(PROBLEM, tested, 'No guide data has been imported')
    horizon = ctx.epg_horizon
    found = f'{ctx.epg_total:,} entries, running {_ahead(horizon, ctx.now)}'
    if horizon is None or horizon <= ctx.now:
        return Result(PROBLEM, tested, f'{ctx.epg_total:,} entries, but all of them have ended')
    if horizon - ctx.now < timedelta(hours=12):
        return Result(ATTENTION, tested, found)
    return Result(READY, tested, found)


def _check_guide_content(ctx):
    tested = 'Counted the channels and groups that feed a TV Guide row'
    total = ctx.guide_channels + ctx.guide_groups
    if not total:
        return Result(ATTENTION, tested, 'Nothing is in the guide yet')
    return Result(READY, tested,
                  f'{ctx.guide_groups} {_plural(ctx.guide_groups, "group")} and '
                  f'{ctx.guide_channels} individual {_plural(ctx.guide_channels, "channel")}')


def _check_guide_groups(ctx):
    tested = 'Checked each guide group for at least one member with Recording switched on'
    if not ctx.guide_groups:
        return Result(NOTHING, tested, 'No groups are in the guide')
    broken = ctx.broken_guide_groups
    if broken:
        return Result(PROBLEM, tested,
                      f'{len(broken)} of {ctx.guide_groups} {_plural(len(broken), "has", "have")} '
                      'nobody switched on: ' + ', '.join(g.name for g in broken))
    return Result(READY, tested,
                  f'All {ctx.guide_groups} have at least one, '
                  f'{ctx.recording_members} recording members in total')


def _check_channels_tested(ctx):
    # No one-click fix on purpose: starting a health check run means choosing which
    # channels and which profile, so there is no single URL to POST - and the rule is act
    # in place only where it is one click (dev/changelog/950).
    tested = 'Looked for a health check result on every recording-enabled guide-group member'
    if not ctx.recording_members:
        return Result(NOTHING, tested, 'There are no recording members to check')
    found = (f'{ctx.tested_members} of {ctx.recording_members} have been checked at least once')
    if ctx.tested_members < ctx.recording_members:
        return Result(ATTENTION, tested, found)
    return Result(READY, tested, found)


def _check_search_index(ctx):
    tested = "Read each index's state and asked whether it is safe to query, not just whether it built"
    reasons = [reason for _ready, reason in (ctx.index_channels, ctx.index_programs) if reason]
    action = Action('Rebuild now', [_url('settings.api_search_index_rebuild')])
    if reasons:
        return Result(ATTENTION, tested, '; '.join(reasons).capitalize(), action)
    return Result(READY, tested, 'Both the channel and the program index are ready')


def _check_notify_any(ctx):
    tested = 'Counted the notification services that are switched on'
    if not ctx.notify_services:
        return Result(ATTENTION, tested, 'No notification services are configured at all')
    if not ctx.notify_enabled:
        return Result(ATTENTION, tested,
                      f'None of the {len(ctx.notify_services)} configured services is switched on')
    return Result(READY, tested,
                  f'{len(ctx.notify_enabled)} on: ' + ', '.join(ctx.notify_enabled))


def _check_notify_delivers(ctx):
    """On-demand: send one real message through every enabled service."""
    from .notifications import send_test
    tested = 'Sent one real test message through each service that is switched on'
    if not ctx.notify_enabled:
        return Result(NOTHING, tested, 'Nothing is switched on, so there was nothing to send')
    failures = []
    for name in ctx.notify_enabled:
        ok, err = send_test(name)
        if not ok:
            failures.append(f'{name}: {err or "refused the message"}')
    action = Action('Send another test',
                    [_url('settings.api_notifications_service_test', name=n)
                     for n in ctx.notify_enabled])
    if failures:
        return Result(PROBLEM, tested, '; '.join(failures), action)
    return Result(READY, tested,
                  'Every service accepted the message: ' + ', '.join(ctx.notify_enabled), action)


def _check_alerts_open(ctx):
    tested = 'Counted the alerts that are open and undismissed'
    total = sum(ctx.open_alerts.values())
    if not total:
        return Result(READY, tested, 'No open alerts')
    parts = ', '.join(f'{n} {sev}' for sev, n in sorted(ctx.open_alerts.items()))
    if ctx.open_alerts.get('CRIT') or ctx.open_alerts.get('ERROR'):
        return Result(PROBLEM, tested, f'{total} open: {parts}')
    if ctx.open_alerts.get('WARN'):
        return Result(ATTENTION, tested, f'{total} open: {parts}')
    return Result(READY, tested, f'{total} open, none above INFO: {parts}')


def _url(endpoint, **values):
    """The path for a view function, from inside a request or outside one.

    `url_for()` refuses to build outside a request context unless SERVER_NAME is set, and
    this module is evaluated from the nav poll's cache warm-up as well as from its own
    route. Binding the app's own url_map performs the same resolution, and it still raises
    on an endpoint that does not exist - which is the point: a renamed view function fails
    in the test suite rather than shipping a dead button.
    """
    from flask import current_app, has_request_context, url_for
    if has_request_context():
        return url_for(endpoint, **values)
    return current_app.url_map.bind('localhost').build(endpoint, values)


# ─────────────────────────────────────────────────────────────────────────────
# THE CHECK REGISTRY
# ─────────────────────────────────────────────────────────────────────────────
CHECKS = (
    # ── This machine ──────────────────────────────────────────────────────
    Check('ffmpeg', 'machine', 'ffmpeg is installed', 'ffmpeg', CHEAP,
          'Nothing can be captured or converted. Every recording and every health check '
          'fails the moment it starts.',
          Link('Settings', 'settings.settings'), False, _check_ffmpeg),
    Check('ffprobe', 'machine', 'ffprobe is installed', 'ffprobe', CHEAP,
          'ChannelBin can still record, but it cannot read a stream or a finished file: no '
          'format detection, no recording health numbers, and no channel-group format matching.',
          Link('Settings', 'settings.settings'), False, _check_ffprobe),
    Check('ffmpeg_build', 'machine', 'This ffmpeg has the parts ChannelBin uses',
          'this ffmpeg build', ON_DEMAND,
          'A build missing a component fails only at the moment it is needed, which is '
          'usually the end of a long recording.',
          Link('Maintenance', 'system.maintenance'), True, _check_ffmpeg_build,
          'Lists every filter and codec the build carries, which costs two processes, so it '
          'never runs because you opened the page.'),
    Check('storage_dvr', 'machine', 'The recording folder works', 'the recording folder', CHEAP,
          'Any recording that starts now fails immediately, with nothing captured.',
          Link('Settings', 'settings.settings'), False, _check_storage_dvr),
    Check('storage_space', 'machine', 'There is room to record', 'free disk space', CHEAP,
          'A recording that runs out of disk stops mid-capture, and what was written up to '
          'that point is all you get.',
          Link('Recordings', 'recordings.index'), True, _check_storage_space),
    Check('db_local', 'machine', 'The database is on local disk', 'where the database lives',
          CHEAP,
          'SQLite over a network filesystem loses writes and locks unpredictably. A DVR '
          'database on a network share is a corruption waiting to happen.',
          Link('Settings', 'settings.settings'), False, _check_db_local),
    Check('db_write', 'machine', 'The database accepts writes', 'writing to the database', CHEAP,
          'Nothing can be scheduled, recorded or synced. The app loads and then fails at the '
          'first action.',
          Link('Maintenance', 'system.maintenance'), False, _check_db_write),
    Check('scheduler', 'machine', 'The scheduler is running', 'the scheduler', CHEAP,
          'Scheduled recordings never start, and nothing would say so until you noticed the '
          'file was never made.',
          Link('Jobs', 'jobs.jobs_page'), False, _check_scheduler),
    Check('timezone', 'machine', 'A display timezone is set', 'the display timezone', CHEAP,
          'Every time on every page, and every recording window, would be read in UTC. A '
          'show would appear to be on at the wrong hour.',
          Link('Settings', 'settings.settings'), False, _check_timezone),
    Check('secret_key', 'machine', 'The session signing key is set', 'the session signing key',
          CHEAP,
          'Sessions cannot be signed, so nobody stays logged in and the login gate can be '
          'walked around.',
          Link('Settings', 'settings.settings'), False, _check_secret_key),
    Check('auth_gate', 'machine', 'The login gate does what you asked', 'the login gate', CHEAP,
          'Login switched on with no password stored serves every page unauthenticated while '
          'looking protected. That is worse than having it off on purpose.',
          Link('Settings', 'settings.settings'), False, _check_auth_gate),

    # ── Your provider ─────────────────────────────────────────────────────
    Check('accounts_any', 'provider', 'You have at least one account', 'your provider accounts',
          CHEAP,
          'There are no channels, no guide and nothing to record. This is the first thing a '
          'new install needs.',
          Link('Accounts', 'accounts.accounts_list'), False, _check_accounts_any),
    Check('account_login', 'provider', 'Every account still logs in', 'the provider logins',
          ON_DEMAND,
          'An account whose credentials have lapsed keeps its old channel list, so nothing '
          'looks wrong until a recording tries to open a stream and the provider refuses.',
          Link('Accounts', 'accounts.accounts_list'), True, _check_account_login,
          'Opens a connection to each provider, so it never runs because you opened the page.'),
    Check('account_sync', 'provider', 'Every account synced recently', 'the account syncs', CHEAP,
          'Channels the provider has moved or dropped go on looking fine in your guide until '
          'the moment you try to record one.',
          Link('Accounts', 'accounts.accounts_list'), True, _check_account_sync),
    Check('epg_loaded', 'provider', 'Guide data is loaded and runs ahead', 'the guide data', CHEAP,
          'The TV Guide is empty or has run out, so scheduling from it records the wrong '
          'thing or nothing at all.',
          Link('Accounts', 'accounts.accounts_list'), True, _check_epg_loaded),

    # ── What you will record ──────────────────────────────────────────────
    Check('guide_content', 'content', 'The TV Guide has something in it',
          'what is in the TV Guide', CHEAP,
          'The guide is empty, so there is nothing to browse and nothing to schedule from.',
          Link('Channels', 'channels.channel_browser'), False, _check_guide_content),
    Check('guide_groups', 'content', 'Every group in the guide can record', 'the guide groups',
          CHEAP,
          'The guide row looks completely normal and cannot produce a file. This is the one '
          'thing in the group model the app refuses outright rather than warning about.',
          Link('Channel groups', 'channel_groups.groups_page'), False, _check_guide_groups),
    Check('channels_tested', 'content', 'The channels you record from have been checked',
          'the channel health checks', CHEAP,
          'An untested feed has no measured format, so a group cannot rank it or fail over to '
          'it with any confidence.',
          Link('Channel groups', 'channel_groups.groups_page'), True, _check_channels_tested),
    Check('search_index', 'content', 'Channel and program search are indexed',
          'the search indexes', CHEAP,
          'Search still returns correct results, but by scanning instead of by index. On a '
          'large channel list that is the difference between instant and several seconds.',
          Link('Maintenance', 'system.maintenance'), True, _check_search_index),

    # ── Being told ────────────────────────────────────────────────────────
    Check('notify_any', 'told', 'Something is set up to tell you',
          'your notification services', CHEAP,
          'A failed recording is only ever discovered by opening the app and looking. That is '
          'the exact silence ChannelBin was built to end.',
          Link('Notifications', 'settings.notifications_settings'), True, _check_notify_any),
    Check('notify_delivers', 'told', 'Each service can actually deliver',
          'notification delivery', ON_DEMAND,
          'A service that is configured but silently failing is worse than no service: the '
          'app believes it told you.',
          Link('Notifications', 'settings.notifications_settings'), True, _check_notify_delivers,
          'Sends a real message, so it never runs because you opened the page.'),
    Check('alerts_open', 'told', 'Nothing is currently alerting', 'the open alerts', CHEAP,
          'An alert stays open because the thing it names is still true. Readiness cannot '
          'claim the install is fine while one is standing.',
          Link('Alerts', 'alerts.alert_center'), True, _check_alerts_open),
)

CHECKS_BY_ID = {c.id: c for c in CHECKS}

# ─────────────────────────────────────────────────────────────────────────────
# THE CAPABILITY REGISTRY
# ─────────────────────────────────────────────────────────────────────────────
#: The same checks read as things a person might want to do. Order is fixed and Record is
#: first: the verdict sentence already names the worst thing, and a list that reorders
#: itself between visits is one nobody can learn (`dev/changelog/950`).
#:
#: Every check stands behind at least one capability. The capability list is the only view
#: of them the page draws, so a check behind none of them is a check nobody can see, and
#: `tests/test_readiness_registry.py` fails on one.
Capability = namedtuple('Capability', 'id label needs')

CAPABILITIES = (
    Capability('record', 'Record a program right now',
               _cap('ffmpeg', 'storage_dvr', 'storage_space', 'db_write', 'accounts_any',
                    'account_login', 'guide_content')),
    Capability('schedule', 'Start a recording on its own while you are out',
               _cap('scheduler', 'db_write', 'timezone', 'account_sync')),
    Capability('guide', 'See what is on',
               _cap('epg_loaded', 'guide_content', 'account_sync', 'accounts_any')),
    Capability('failover', 'Survive a feed dropping mid-recording',
               _cap('guide_groups', 'ffprobe', 'channels_tested')),
    Capability('convert', 'Convert a finished recording to MP4',
               _cap('ffmpeg', 'ffmpeg_build')),
    Capability('measure', 'Know how good a recording actually was', _cap('ffprobe')),
    Capability('shots', 'Take screenshots and live thumbnails',
               _cap('ffmpeg', 'ffmpeg_build')),
    Capability('search', 'Search your channels and programs instantly', _cap('search_index')),
    Capability('notify', 'Be told when something breaks',
               _cap('notify_any', 'notify_delivers')),
    Capability('safe', 'Keep your database and your schedule intact',
               _cap('db_local', 'db_write', 'secret_key', 'auth_gate')),
    Capability('clear', 'Be sure nothing is wrong right now', _cap('alerts_open')),
)

# ─────────────────────────────────────────────────────────────────────────────
# On-demand results, and the nav summary cache
# ─────────────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
#: {check id: {'status','tested','found','action','detail','at'}} for the ON_DEMAND checks.
#: Process-wide and deliberately not persisted: it answers "what happened when you last
#: asked", and a restart has genuinely un-asked the question.
_ondemand: dict = {}
#: The nav counts, cached so a 15-second poll in three open tabs does not re-evaluate nine
#: times a minute. Never holds an on-demand result it forced - it reads whatever _ondemand
#: already has.
_nav_cache: dict = {}
NAV_CACHE_SECONDS = 30


def reset_for_tests():
    """Drop every process-global answer. Called by tests/support/app.py."""
    with _lock:
        _ondemand.clear()
        _nav_cache.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Silencing
# ─────────────────────────────────────────────────────────────────────────────
def ignored_check_ids() -> set:
    """The checks the user has told this install not to count. Unknown ids are dropped."""
    import json
    from . import db
    from .database import UserPref
    pref = db.session.get(UserPref, IGNORED_PREF_KEY)
    if not pref or not pref.value:
        return set()
    try:
        stored = json.loads(pref.value)
    except ValueError:
        log.warning('readiness: %s is not valid JSON; treating it as empty', IGNORED_PREF_KEY)
        return set()
    if not isinstance(stored, list):
        return set()
    return {cid for cid in stored if cid in CHECKS_BY_ID}


def set_check_ignored(check_id: str, ignored: bool) -> set:
    """Silence or un-silence one check. Returns the new set.

    Refuses a check whose registry entry says it may not be silenced - the enforcement has
    to be here rather than in the template, because a button the UI does not draw is still
    a route somebody can POST to (CLAUDE.md's server-side enforcement rule).
    """
    import json
    from . import db
    from .database import UserPref
    from .db_utils import retry_on_locked
    check = CHECKS_BY_ID.get(check_id)
    if check is None:
        raise KeyError(check_id)
    if not check.ignorable:
        raise ValueError(f'{check_id} cannot be silenced')

    @retry_on_locked()
    def _save_and_commit():
        pref = db.session.get(UserPref, IGNORED_PREF_KEY)
        if pref is None:
            pref = UserPref(key=IGNORED_PREF_KEY)
            db.session.add(pref)
        try:
            current = set(json.loads(pref.value)) if pref.value else set()
        except ValueError:
            current = set()
        current = {cid for cid in current if cid in CHECKS_BY_ID}
        if ignored:
            current.add(check_id)
        else:
            current.discard(check_id)
        pref.value = json.dumps(sorted(current))
        db.session.commit()
        return current

    with _lock:
        _nav_cache.clear()
    return _save_and_commit()


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation
# ─────────────────────────────────────────────────────────────────────────────
def _resolve(check, ctx, stored):
    """One check's state, given what has been run. NOT_RUN is decided here and nowhere else.

    An ON_DEMAND check that nobody has asked for has no verdict and must never borrow the
    one it would have had, which is why this never calls `check.run` for it.
    """
    if check.cost == ON_DEMAND:
        answer = stored.get(check.id)
        if answer is None:
            return Result(NOT_RUN, 'Not run yet - it waits for you to ask',
                          'No answer yet'), None
        return Result(answer['status'], answer['tested'], answer['found'],
                      answer.get('action'), answer.get('detail')), answer.get('at')
    try:
        return check.run(ctx), None
    except Exception as exc:                              # noqa: BLE001 - reported, not swallowed
        # A check that raises is the one thing that must never read as a pass. UNKNOWN is
        # exactly that state, and the exception reaches the user rather than only the log.
        log.exception('readiness: check %s raised', check.id)
        return Result(UNKNOWN, 'The check itself failed to run',
                      f'{type(exc).__name__}: {exc}'), None


def _capability_state(cap, by_id):
    """A capability's own state, over the checks that still count.

    Two calls the module docstring argues for, both applied here: an ignored check is not
    consulted at all, and NOT_RUN does not drag the capability down while UNKNOWN does.
    """
    rows = [by_id[cid] for cid in cap.needs if cid in by_id]
    counted = [r for r in rows if not r['ignored']]
    blockers = [r for r in counted if r['status'] == PROBLEM]
    soft = [r for r in counted if r['status'] == ATTENTION]
    unsure = [r for r in counted if r['status'] == UNKNOWN]
    nothing = [r for r in counted if r['status'] == NOTHING]
    not_run = [r for r in counted if r['status'] == NOT_RUN]
    if blockers:
        state = CANNOT
    elif soft:
        state = DEGRADED
    elif unsure:
        state = CAP_UNKNOWN
    elif nothing:
        # Anything with nothing to look at means the capability is not set up, not that it
        # works. A fresh install has no groups, so "survive a feed dropping" is neither a
        # yes nor a failure - claiming Yes because the other two checks passed would be the
        # page's worst possible lie.
        state = NOT_SET_UP
    else:
        state = CAN
    return {
        'id': cap.id,
        'label': cap.label,
        'state': state,
        'mark': CAP_MARK[state],
        'health': CAP_HEALTH[state],
        'needs': [r['id'] for r in rows],
        'blockers': [r['id'] for r in blockers],
        'degraded': [r['id'] for r in soft],
        'unknown': [r['id'] for r in unsure],
        'nothing': [r['id'] for r in nothing],
        'not_run': [r['id'] for r in not_run],
        'ignored': [r['id'] for r in rows if r['ignored']],
    }


def _verdict(caps, rows, counts):
    """The sentence, which is the whole point of the page.

    Written server-side rather than in the browser because it is the headline this feature
    exists to produce, and a headline is worth asserting on in a test.
    """
    blocked = [c for c in caps if c['state'] == CANNOT]
    degraded = [c for c in caps if c['state'] == DEGRADED]
    unsure = [c for c in caps if c['state'] == CAP_UNKNOWN]
    by_id = {r['id']: r for r in rows}
    if blocked:
        first = blocked[0]
        rest = len(blocked) - 1
        lower = first['label'][0].lower() + first['label'][1:]
        tail = (f', and {rest} other {_plural(rest, "thing")} '
                f'{_plural(rest, "is", "are")} blocked too') if rest else ''
        return {'level': 'bad', 'mark': '!',
                'head': f'Not ready: you cannot {lower}{tail}',
                'sub': by_id[first['blockers'][0]]['without']}
    if degraded:
        first = degraded[0]
        lower = first['label'][0].lower() + first['label'][1:]
        return {'level': 'warn', 'mark': '!',
                'head': (f'Ready to record, but {len(degraded)} '
                         f'{_plural(len(degraded), "thing")} '
                         f'{_plural(len(degraded), "is", "are")} not at full strength'),
                'sub': (f'You can still {lower}, with a caveat. '
                        + by_id[first['degraded'][0]]['without'])}
    if unsure:
        n = len(unsure)
        return {'level': 'warn', 'mark': '?',
                'head': f'{n} {_plural(n, "thing")} could not be checked',
                'sub': ('Nothing here is known to be broken. These checks could not answer, '
                        'which is not the same as passing, so this card will not claim the '
                        'install is ready.')}
    if counts[NOT_RUN]:
        n = counts[NOT_RUN]
        return {'level': 'ok', 'mark': '✓',
                'head': 'Ready to record',
                'sub': (f'Everything checked so far is fine. {n} '
                        f'{_plural(n, "check costs", "checks cost")} real work - a process, '
                        f'a provider login, a message - so {_plural(n, "it has", "they have")} '
                        f'not run. Run {_plural(n, "it", "them")} when you want the full answer.')}
    return {'level': 'ok', 'mark': '✓',
            'head': 'Ready: everything you would want to do works',
            'sub': (f'All {counts["counted"]} checks that count passed: the toolchain, the '
                    'storage, the database, your accounts, the guide and the way you get told '
                    'when something goes wrong.')}


def evaluate(ctx=None, ignored=None) -> dict:
    """The whole payload: every check, every capability, the verdict and the nav counts."""
    if ignored is None:
        ignored = ignored_check_ids()
    if ctx is None:
        ctx = Context()
    with _lock:
        stored = dict(_ondemand)

    rows = []
    for check in CHECKS:
        result, ran_at = _resolve(check, ctx, stored)
        action = result.action
        rows.append({
            'id': check.id,
            'area': check.area,
            'label': check.label,
            'short': check.short,
            'cost': check.cost,
            'status': result.status,
            'tested': result.tested,
            'found': result.found,
            'without': check.without,
            'note': check.note,
            'detail': result.detail,
            'ignorable': check.ignorable,
            'ignored': check.id in ignored,
            'last_run': ran_at,
            # One click here, or a link to the page that owns it - never both on one row.
            'action': ({'label': action.label, 'urls': list(action.urls)}
                       if action is not None else None),
            'link': ({'label': check.link.label, 'url': _url(check.link.endpoint)}
                     if action is None and check.link else None),
        })

    by_id = {r['id']: r for r in rows}
    caps = [_capability_state(cap, by_id) for cap in CAPABILITIES]
    counted = [r for r in rows if not r['ignored']]
    counts = {status: sum(1 for r in counted if r['status'] == status) for status in STATUSES}
    counts['counted'] = len(counted)
    counts['ignored'] = len(rows) - len(counted)
    nav = nav_counts(caps)
    with _lock:
        _nav_cache.update({'at': datetime.utcnow(), 'value': nav})
    return {
        'checks': rows,
        'capabilities': caps,
        'verdict': _verdict(caps, rows, counts),
        'counts': counts,
        'nav': nav,
        'areas': [{'id': aid, 'label': label} for aid, label in AREAS],
        'generated_at': datetime.utcnow().isoformat(),
    }


def nav_counts(caps) -> dict:
    """{'blocked': n, 'degraded': n} - what the Maintenance nav link counts.

    Capabilities, not checks: one broken binary blocks four capabilities, and a tally of
    checks would read as four alarms for one problem. An ignored check has already been
    left out of every capability's state, so it cannot reach this.
    """
    return {
        'blocked': sum(1 for c in caps if c['state'] == CANNOT),
        'degraded': sum(1 for c in caps if c['state'] == DEGRADED),
    }


def nav_summary() -> dict:
    """The nav counts for the /api/nav-status poll, cached for NAV_CACHE_SECONDS.

    Cached because every open browser tab asks every 15 seconds and the answer costs a
    statvfs, a mount-table read and a handful of counts. Never runs an on-demand check -
    it reads whatever has already been asked for, so a poll can never spawn a process or
    open a provider connection.
    """
    with _lock:
        cached = dict(_nav_cache)
    if cached.get('at') and (datetime.utcnow() - cached['at']).total_seconds() < NAV_CACHE_SECONDS:
        return cached['value']
    try:
        return evaluate()['nav']
    except Exception as exc:                              # noqa: BLE001 - never break the poll
        log.warning('readiness: nav summary failed: %s', exc)
        return {'blocked': 0, 'degraded': 0}


def run_check(check_id: str) -> dict:
    """Run one ON_DEMAND check now and store its answer. Returns the fresh payload."""
    check = CHECKS_BY_ID.get(check_id)
    if check is None:
        raise KeyError(check_id)
    if check.cost != ON_DEMAND:
        raise ValueError(f'{check_id} runs on every evaluation and is not asked for')
    ctx = Context()
    result, _ = _resolve(check._replace(cost=CHEAP), ctx, {})
    action = result.action
    with _lock:
        _ondemand[check_id] = {
            'status': result.status,
            'tested': result.tested,
            'found': result.found,
            'detail': result.detail,
            'action': action,
            'at': datetime.utcnow().isoformat(),
        }
        _nav_cache.clear()
    return evaluate(ctx=ctx)


def pending_ondemand_ids() -> list:
    """The ON_DEMAND checks nobody has asked for yet, in registry order."""
    with _lock:
        stored = set(_ondemand)
    return [c.id for c in CHECKS if c.cost == ON_DEMAND and c.id not in stored]
