"""
In-app alert creation and routing.

create_alert() is the single entry point for all alert sources.
It writes an Alert row (if in-app enabled for the type) and enqueues
push notifications (handled by notifications.py rate limiter).
"""
import logging
import re
from datetime import datetime

log = logging.getLogger(__name__)

_ALERT_TITLE_DIGITS_RE = re.compile(r'\d+')

# The two per-notification-service standing alerts. Named because app/notifications.py
# raises, dedupes and dismisses them by type in six places; every other type below is
# referenced once by its literal at its single raise site.
#: A service's stored URL is still the example placeholder shown in its own field.
NOTIFICATION_SERVICE_URL_PLACEHOLDER = 'NOTIFICATION_SERVICE_URL_PLACEHOLDER'
#: A service is configured but its sends are failing, so pushes are being dropped.
NOTIFICATION_SERVICE_SEND_FAILED = 'NOTIFICATION_SERVICE_SEND_FAILED'

#: auth.enabled is on with no password_hash, so the gate is inert and the app is serving
#: unauthenticated. Named for the same reason as the two above: app/auth.py raises and
#: dismisses it by type.
AUTH_GATE_INERT = 'AUTH_GATE_INERT'

#: A configured storage directory has stopped being usable. Named for the same reason as
#: the three above: update_storage_path_alert() below raises and dismisses it by type.
STORAGE_PATH_UNUSABLE = 'STORAGE_PATH_UNUSABLE'

#: ffmpeg or ffprobe could not be run on this install. Named for the same reason as the
#: four above: app/toolchain.py raises and dismisses it by type, one row per binary.
EXTERNAL_TOOL_MISSING = 'EXTERNAL_TOOL_MISSING'

#: Types nothing raises any more, kept in ALERT_TYPES below purely so the rows already in
#: the database still render with a label.
#:
#: An alert is for a real problem: content already lost, a recording that will fail, the app
#: broken or needing a human, or guide/stream data going bad (dev/changelog/923). Each type
#: here reports something that is none of those, and each is shown instead on the group,
#: recording, account or page it concerns - which is both quieter and more useful, since it
#: is where somebody asking the question already looks. In the week measured before this
#: changed, these twelve were 189 of 198 alerts raised (dev/changelog/928).
#:
#: Adding a type here means its raise sites go at the same time; ALERT_TYPES keeps the entry.
#: Enforced by tests/test_retired_alert_types.py, which fails on a create_alert() of any of
#: them anywhere in app/.
RETIRED_ALERT_TYPES = frozenset({
    'GROUP_FORMAT_MISMATCH',
    'GROUP_NO_ELIGIBLE_MEMBER',
    'RECORDING_FORMAT_OVERRIDE',
    'RECORDING_FORMAT_CHANGED',
    'SYNC_CHANNELS_NEW',
    'SYNC_CHANNELS_MISSING',
    'SYNC_STREAM_URLS_CONSTRUCTED',
    'MALFORMED_CHANNEL_URLS',
    'DUPLICATE_STREAM_IDS_SKIPPED',
    'HEALTH_CHECK_COMPLETE',
    'JOB_SKIPPED',
    'CHANNEL_HIDE_RULES_NOT_APPLIED',
})

# Registry of all known alert types.
# severity: ERROR | CRIT | WARN | INFO
#
# self_clearing: True marks a type the app dismisses BY ITSELF once the condition stops
# being true. It is what puts a row under the Alerts page's "Active alerts" card, which
# carries no Dismiss - offering one would let a problem that is still happening be hidden,
# and the card's own name would stop being true (dev/changelog/932).
#
# Set it on a new type only when a clearing path actually exists and you can point at it.
# The flag lives on the entry rather than in a list elsewhere so that it is in front of
# whoever adds the type; tests/test_self_clearing_alerts.py scans app/ for the dismiss call
# sites and fails if the two disagree in either direction.
ALERT_TYPES = {
    'LOG_ERROR':   {'label': 'Application Error (log)',    'severity': 'ERROR'},
    'LOG_CRIT':    {'label': 'Application Critical (log)', 'severity': 'CRIT'},
    'JOB_SKIPPED': {'label': 'Scheduled Job Skipped',      'severity': 'INFO'},
    'MALFORMED_CHANNEL_URLS': {'label': 'Malformed Channel URLs Skipped', 'severity': 'INFO'},
    # A stream_id repeated within one playlist/catalog - the URL-derived id can collide
    # innocuously (a .ts/.m3u8 variant of the same numeric tail) or genuinely conflict (two
    # distinct provider URLs landing on the same parsed/hashed id), and either way the second
    # occurrence is dropped (dev/docs/BUGS.md 2026-08-30). Retired: the count is a column on
    # the sync that skipped them and is shown on the account page (dev/changelog/926).
    'DUPLICATE_STREAM_IDS_SKIPPED': {
        'label': 'Duplicate Stream IDs Skipped', 'severity': 'INFO'},
    # A hide-rule pass was refused for database contention, so the rules are saved but not
    # yet reflected in what you are being offered (dev/changelog/776). Retired: the Hide
    # Rules page itself now banners that state, naming the blocker and the retry time, which
    # is the page the person who just saved the rule is already looking at.
    'CHANNEL_HIDE_RULES_NOT_APPLIED': {
        'label': 'Channel Hide Rules Not Applied Yet', 'severity': 'INFO'},
    # A one-time startup repair moved stored health scores back onto the observations on
    # record, after a repeated post-capture analysis had counted some of them twice
    # (app/health_recompute.py::repair_duplicated_capture_corrections, dev/changelog/951). The
    # scores move with nothing the user did behind them, which is precisely the number a user
    # cannot otherwise explain, so it is announced rather than left on each channel's timeline.
    'HEALTH_SCORES_REPAIRED': {
        'label': 'Channel Health Scores Recomputed', 'severity': 'INFO'},
    'GROUP_FORMAT_MISMATCH': {'label': 'Channel Group Format Mismatch', 'severity': 'WARN'},
    # Retired along with GROUP_FORMAT_MISMATCH above: a group whose lock leaves nothing
    # eligible says so on the group page's own override banner, and the recording made under
    # that override carries a RECORDING_FORMAT_OVERRIDE event that survives onto the artifact
    # - which is the disclosure that actually matters six weeks later.
    'GROUP_NO_ELIGIBLE_MEMBER': {'label': 'Channel Group Has No Eligible Member', 'severity': 'WARN'},
    # A group sitting in the TV Guide with nothing switched on for recording, reached
    # without a human to confirm it (DESIGN-channel-groups-model.md 15, breach path 3).
    # ERROR rather than WARN, and deliberately louder than GROUP_NO_ELIGIBLE_MEMBER above:
    # that one is a group that can still record, badly. This one cannot produce a file at
    # all, and the row will go on looking normal in the guide until somebody fixes it.
    'GROUP_GUIDE_NO_RECORDING_MEMBER': {
        'label': 'Channel Group In Guide Has No Recording Member', 'severity': 'ERROR',
        'self_clearing': True},
    'RECORDING_FORMAT_OVERRIDE': {'label': 'Recording Started Off The Group Format', 'severity': 'WARN'},
    # A recording that actually changed format part-way through. Distinct from the override
    # above, which is about where a recording STARTED: this one is about the finished file
    # having two formats in it, since the container header advertises only the first
    # (dev/changelog/754). Retired: every divergent segment still writes its own
    # RECORDING_FORMAT_CHANGED event, and the recording's detail page is where a question
    # about that file gets asked.
    'RECORDING_FORMAT_CHANGED': {'label': 'Recording Changed Format Mid-Run', 'severity': 'WARN'},
    # Not self_clearing, unlike every other SYNC_* type below: _alert_url_drift refreshes a
    # standing row but has no dismiss branch, because nothing observes the URLs going back
    # to what they were. A drift that has been dealt with is dismissed by hand.
    'PROVIDER_URLS_CHANGED': {'label': 'Provider Stream URLs Changed', 'severity': 'WARN'},
    'SYNC_EPG_FETCH_FAILED': {
        'label': 'EPG Fetch Failed', 'severity': 'WARN', 'self_clearing': True},
    'SYNC_EPG_COLLAPSE_REFUSED': {
        'label': 'EPG Import Refused (Collapse Guard)', 'severity': 'WARN', 'self_clearing': True},
    # Distinct from the two above because the old EPG is already gone by the time this
    # fires: the delete commits before the parse loop, so their "previous EPG data was
    # kept" wording would be false here (DESIGN-sync-resilience.md §3).
    'SYNC_EPG_IMPORT_TRUNCATED': {
        'label': 'EPG Import Cut Short (Truncated Feed)', 'severity': 'WARN',
        'self_clearing': True},
    'SYNC_FEED_SHRUNK': {
        'label': 'Provider Feed Shrunk', 'severity': 'WARN', 'self_clearing': True},
    'SYNC_LIVE_CLASSIFY_UNAVAILABLE': {
        'label': 'Live/VOD Catalog Unavailable', 'severity': 'WARN', 'self_clearing': True},
    'SYNC_LIVE_CLASSIFY_REFUSED': {
        'label': 'Live/VOD Filtering Refused (Collapse Guard)', 'severity': 'WARN',
        'self_clearing': True},
    'SYNC_STREAM_URLS_CONSTRUCTED': {'label': 'Stream URLs Constructed, Not Provided', 'severity': 'INFO'},
    'SYNC_URL_CONSTRUCTION_BLOCKED': {
        'label': 'Cannot Build Stream URLs (No Format Set)', 'severity': 'WARN',
        'self_clearing': True},
    'SYNC_CHANNELS_MISSING': {'label': 'Channels Missing From Provider', 'severity': 'INFO'},
    'SYNC_CHANNELS_NEW': {'label': 'New Channels From Provider', 'severity': 'INFO'},
    # Kept, and kept meaning what it always meant, purely so historical rows still render
    # with a label: nothing writes it any more. A recording that cannot have a slot now
    # waits for one instead of connecting over the limit (dev/changelog/854), which is
    # RECORDING_WAITING_FOR_CONNECTION_SLOT below.
    'RECORDING_OVER_CONNECTION_LIMIT': {'label': 'Recording Started Over Connection Limit', 'severity': 'WARN'},
    # WARN, not ERROR: nothing malfunctioned, and the recording may yet start in full. It
    # is not INFO either - a recording that is not capturing while its window runs is
    # degrading, and the user is the only one who can free a slot or raise the limit.
    'RECORDING_WAITING_FOR_CONNECTION_SLOT': {
        'label': 'Recording Waiting For A Connection Slot', 'severity': 'WARN',
        'self_clearing': True},
    'RECORDING_FAILED_CONNECTION_LIMIT': {
        'label': 'Recording Failed (Waited For A Connection Slot)', 'severity': 'ERROR'},
    'RECORDING_CHANNEL_FAILING': {
        'label': 'Scheduled Recording Channel Failing', 'severity': 'WARN', 'self_clearing': True},
    'SEARCH_INDEX_REBUILD_FAILED': {
        'label': 'Search Index Rebuild Failed', 'severity': 'ERROR', 'self_clearing': True},
    'CONVERSION_FAILED': {
        'label': 'Conversion Failed', 'severity': 'ERROR', 'self_clearing': True},
    # The three below name failures that used to reach the Alerts page only through the
    # log->alert catch-all in app/__init__.py. With no type of their own they had no deep
    # link and, more to the point, nothing that could ever clear them: one sync failure
    # sat open through eight days of successful syncs (dev/changelog/930). ERROR because
    # each one is work already lost, not a degradation.
    'SYNC_FAILED': {
        'label': 'Account Sync Failed', 'severity': 'ERROR', 'self_clearing': True},
    # An account that has fallen a whole sync interval past due because its scheduled syncs
    # keep being deferred past recordings (dev/changelog/941). WARN, not ERROR: nothing has
    # been lost yet, the channel list and guide are just going stale. An individual deferred
    # sync is deliberately NOT an alert - it is shown on the account's own sync history and in
    # the corrected "next sync" time (dev/changelog/923 decision 8).
    'SYNC_ACCOUNT_OVERDUE': {
        'label': 'Account Sync Overdue', 'severity': 'WARN', 'self_clearing': True},
    'RECORDING_MOVE_FAILED': {
        'label': 'Recording Move Failed', 'severity': 'ERROR', 'self_clearing': True},
    # Unlike the two above, this one has no self-clearing path and is not expected to grow
    # one: nothing re-runs a concatenation that found nothing to concatenate, so it is a
    # record of a loss and is cleared only by deleting the recording.
    'CONCATENATION_FAILED': {'label': 'Concatenation Failed', 'severity': 'ERROR'},
    'CONFIG_FILE_MISSING': {'label': 'Config File Missing', 'severity': 'CRIT'},
    'HEALTH_CHECK_COMPLETE': {'label': 'Health Check Completed', 'severity': 'INFO'},
    'HEALTH_CHECK_WINDOW': {
        'label': 'Maintenance Window Closed With Work Left Over', 'severity': 'WARN',
        'self_clearing': True},
    # WARN, not ERROR: the log->alert handler in app/__init__.py already creates an alert
    # for any ERROR-level log record, so this stays WARN to avoid a second, duplicate alert
    # if the lockout is also logged at ERROR - it isn't (log.warning), but the severity
    # still has to not collide with that path.
    'AUTH_LOGIN_LOCKOUT': {'label': 'Login Lockout (Too Many Failed Attempts)', 'severity': 'WARN'},
    NOTIFICATION_SERVICE_URL_PLACEHOLDER: {
        'label': 'Notification Service URL Is Still Its Placeholder', 'severity': 'WARN',
        'self_clearing': True},
    # WARN for the same reason its placeholder sibling above is: nothing in the app
    # malfunctioned, one outbound service is degraded. ERROR would also double up with the
    # log->alert handler, since the send failure is logged on the way here.
    NOTIFICATION_SERVICE_SEND_FAILED: {
        'label': 'Push Notification Service Failing', 'severity': 'WARN',
        'self_clearing': True},
    'SECOND_INSTANCE_DETECTED': {
        'label': 'Second Live Instance Detected', 'severity': 'CRIT'},
    'RECORDING_RESUME_REFUSED': {
        'label': 'Recording Resume Refused (Segment Still Growing)', 'severity': 'ERROR'},
    # WARN, not ERROR: nothing in the app malfunctioned - it was not running. The
    # recording is degraded (its tail was never captured), which is the WARN band the
    # sync-degradation types above already use.
    'CAPTURE_LOST_TO_OUTAGE': {
        'label': 'Capture Lost While Service Was Down', 'severity': 'WARN'},
    'RECORDING_FAILED_CONVERSION_COLLISION': {
        'label': 'Recording Failed (Waited for Conversion)', 'severity': 'ERROR'},
    # WARN, matching the log level the same condition is written at, and matching the band
    # every other "nothing malfunctioned, something the user asked for is degraded" type
    # above already uses. Not ERROR/CRIT: those would also double up with the log->alert
    # handler in app/__init__.py, and the condition here is a configuration state rather
    # than a failure.
    AUTH_GATE_INERT: {
        'label': 'Login Gate Enabled But Has No Password', 'severity': 'WARN',
        'self_clearing': True},
    # ERROR, unlike the WARN band the other "configuration is degraded" types above sit
    # in: a DVR output directory that stopped answering does not degrade recording, it
    # disables it outright, and every recording that starts meanwhile fails immediately.
    # No double-up with the log->alert handler in app/__init__.py, which keys on a log
    # record's own level - the condition is logged at WARNING (app/fs_utils.py).
    STORAGE_PATH_UNUSABLE: {
        'label': 'Storage Path Unusable', 'severity': 'ERROR', 'self_clearing': True},
    # ERROR for the same reason STORAGE_PATH_UNUSABLE above is, and not the WARN band the
    # "something is degraded" types sit in: a missing ffmpeg does not degrade recording, it
    # disables it, and every recording that starts meanwhile fails immediately. A missing
    # ffprobe shares the band deliberately - it is silent rather than loud on every other
    # surface, which is exactly what makes it worth the louder one here. No double-up with
    # the log->alert handler in app/__init__.py: the condition is logged at WARNING
    # (app/toolchain.py).
    EXTERNAL_TOOL_MISSING: {
        'label': 'External Tool Missing (ffmpeg/ffprobe)', 'severity': 'ERROR',
        'self_clearing': True},
}

#: The self_clearing types above, as a set. Derived rather than written out a second time:
#: two hand-kept copies of this membership is how the "Active alerts" card would come to
#: claim a type nothing clears.
SELF_CLEARING_ALERT_TYPES = frozenset(
    name for name, meta in ALERT_TYPES.items() if meta.get('self_clearing'))


def is_self_clearing(alert_type: str) -> bool:
    """True if the app dismisses this type by itself once its condition stops being true.

    An open alert of such a type describes a problem that is STILL HAPPENING, so the Alerts
    page lists it under "Active alerts" and offers no Dismiss: hiding it would throw away
    the only standing evidence of something nobody has fixed, and the app would put it
    straight back (dev/changelog/932). An unknown type is never self-clearing - there is no
    code that would clear it.
    """
    return alert_type in SELF_CLEARING_ALERT_TYPES


def _get_routing(alert_type: str) -> dict:
    """Return routing config for an alert type, merged with defaults."""
    from .config import load_config
    cfg = load_config()
    routing = cfg.get('notifications', {}).get('routing', {})
    defaults = {'in_app': True, 'push_services': []}
    return {**defaults, **routing.get(alert_type, {})}


def _build_recording_link(recording_id):
    """Absolute URL to a recording's detail page, for push notifications.

    Returns None if there's no recording_id or no base_url configured (background
    threads have no Flask request context, so url_for(_external=True) isn't usable -
    the base URL has to come from config instead).
    """
    if not recording_id:
        return None
    from .config import load_config
    base_url = load_config().get('notifications', {}).get('base_url', '').strip()
    if not base_url:
        return None
    return f"{base_url.rstrip('/')}/recordings/{recording_id}"


def normalize_alert_title(title: str) -> str:
    """Collapse embedded numbers in an alert title to a placeholder, so two alerts that
    differ only in a count/id compare equal. This is the match key an ignored-alert
    pattern is keyed on - MALFORMED_CHANNEL_URLS's title carries a live count that changes
    every sync ('skipped 250 malformed channel URL(s)' -> 'skipped 251 ...')."""
    return _ALERT_TITLE_DIGITS_RE.sub('#', title or '')


def has_open_alert(alert_type: str, source: str) -> bool:
    """True if an undismissed alert of this (type, source) pair already stands.

    The read half of the standing-alert pattern several subsystems share: raise one row
    for a condition that persists, dedupe every later observation against it, and dismiss
    it when the condition clears. `source` is the key that separates one instance of the
    condition from another (a service name, a recording/channel pair, a subsystem).
    """
    from .database import Alert
    return Alert.query.filter_by(
        alert_type=alert_type, source=source, dismissed_at=None).first() is not None


def dismiss_open_alerts(alert_type: str, source: str):
    """Dismiss every undismissed alert of this (type, source) pair - the clear half of
    has_open_alert's pattern. A no-op when nothing stands, so it is safe to call on every
    healthy pass rather than only on the transition."""
    from . import db
    from .database import Alert
    from .db_utils import retry_on_locked

    @retry_on_locked()
    def _dismiss():
        rows = Alert.query.filter_by(
            alert_type=alert_type, source=source, dismissed_at=None).all()
        if not rows:
            return
        now = datetime.utcnow()
        for row in rows:
            row.dismissed_at = now
        db.session.commit()
    _dismiss()


def _dismiss_recording_alerts(recording_id: int, alert_type: str = None,
                              unlink: bool = False) -> bool:
    """Dismiss the open alerts carrying `recording_id` - and, when `unlink`, drop the id
    from every row that carries it, dismissed or not.

    Mutates without committing and returns whether anything changed; the two wrappers
    below own the commit. An already-dismissed row keeps its original timestamp, per the
    anchors-only-move-forward rule.
    """
    from .database import Alert

    q = Alert.query.filter(Alert.recording_id == recording_id)
    if alert_type is not None:
        q = q.filter(Alert.alert_type == alert_type)
    now = datetime.utcnow()
    changed = False
    for row in q.all():
        if row.dismissed_at is None:
            row.dismissed_at = now
            changed = True
        if unlink:
            row.recording_id = None
            changed = True
    return changed


def dismiss_open_alerts_for_recording(recording_id: int, alert_type: str = None):
    """Dismiss every open alert carrying `recording_id`, optionally narrowed to one type -
    the recording-keyed sibling of dismiss_open_alerts.

    A condition about one recording whose raise sites do not share a single `source`
    string cannot be cleared by the (type, source) pair, but the id column names it
    exactly. A no-op when nothing stands.

    The row keeps its recording_id: the recording still exists, and the dismissed alert is
    part of its history. Deleting the recording is the other case, and goes through
    database.py::detach_recording_references instead.
    """
    from . import db
    from .database import Alert  # noqa: F401 - imported for the query inside the closure
    from .db_utils import retry_on_locked

    @retry_on_locked()
    def _dismiss():
        if _dismiss_recording_alerts(recording_id, alert_type):
            db.session.commit()

    _dismiss()


def detach_recording_alerts(recording_id: int):
    """Dismiss and unlink every alert naming a recording that is being deleted.

    Mutates without committing: the delete paths call this inside the same
    retry_on_locked closure that deletes the Recording row, so the row and the alerts
    naming it cannot end up in different states. Reached through
    database.py::detach_recording_references, which is what those paths actually call -
    the id has to come off more tables than this one.
    """
    _dismiss_recording_alerts(recording_id, unlink=True)


def update_storage_path_alert(path, probe, what: str, consequence: str):
    """Raise - or clear - the standing alert for a configured storage directory.

    Create-or-dismiss on a single row keyed source=path, mirroring
    app/auth.py::refresh_auth and app/notifications.py::alert_placeholder_url. The path
    is the key because two configured directories fail independently and each has to be
    able to recover on its own; the type alone could not tell them apart.

    `probe` is a fs_utils.DirProbe. Any outcome other than PATH_OK raises, because a
    directory the user configured and the app cannot use is a stopped feature whatever
    the errno was - describe_dir_problem() already renders each one as its own sentence.
    `what` names the directory's role ('DVR output directory') and `consequence` names
    what stops working, since the path alone says neither.

    Callers must gate this on an actual change of outcome: the disk readout behind it is
    polled every 15s per open browser tab, and an ungated call would put two queries on
    every one of those polls forever.
    """
    from flask import has_app_context
    from .fs_utils import PATH_OK, describe_dir_problem

    if not has_app_context():
        return
    try:
        if probe.outcome == PATH_OK:
            dismiss_open_alerts(STORAGE_PATH_UNUSABLE, path)
            return
        if has_open_alert(STORAGE_PATH_UNUSABLE, path):
            return
        create_alert(
            STORAGE_PATH_UNUSABLE,
            f'{what} is unusable',
            body=(f'{describe_dir_problem(path, probe)}. {consequence} This alert clears '
                  f'itself once the path answers again.'),
            source=path)
    except Exception:
        # Never let the diagnostic break the thing it is describing: this sits on the
        # disk-readout path, and a failed alert write must not turn the sidebar's stats
        # poll into a 500 (CLAUDE.md, product principles corollary).
        log.exception('Could not update the storage-path alert for %s', path)


def _record_if_ignored(alert_type: str, title: str) -> bool:
    """True (and bumps the pattern's match_count/last_matched_at) if this alert matches a
    user-created IgnoredAlertPattern - the caller must skip writing the Alert row and
    enqueuing push. False if nothing matches, or the check itself failed (fail open: an
    alert an ignore-check can't confirm is suppressed still gets surfaced, never dropped)."""
    from . import db
    from .database import IgnoredAlertPattern
    from .db_utils import retry_on_locked

    pattern_text = normalize_alert_title(title)

    @retry_on_locked()
    def _bump_and_commit():
        row = IgnoredAlertPattern.query.filter_by(
            alert_type=alert_type, title_pattern=pattern_text).first()
        if row is None:
            return False
        row.match_count = (row.match_count or 0) + 1
        row.last_matched_at = datetime.utcnow()
        db.session.commit()
        return True

    try:
        return _bump_and_commit()
    except Exception:
        log.exception('Failed to check ignored-alert patterns (type=%s)', alert_type)
        try:
            db.session.rollback()
        except Exception:
            log.warning('Ignored-alert-pattern check rollback also failed', exc_info=True)
        return False


def create_alert(alert_type: str, title: str, body: str = None,
                 source: str = None, recording_id: int = None):
    """Create an in-app Alert and enqueue push notifications per config routing.

    Safe to call from any thread (uses Flask app context internally).
    Silently skips unknown alert_types, and silently skips alert_types the user has
    ignored via a matching IgnoredAlertPattern (the underlying condition is still logged
    normally by whatever code called this - only the Alert row and push are suppressed).
    """
    type_meta = ALERT_TYPES.get(alert_type)
    if type_meta is None:
        log.debug('create_alert: unknown alert_type %r, skipping', alert_type)
        return

    if _record_if_ignored(alert_type, title):
        return

    routing = _get_routing(alert_type)

    if routing.get('in_app', True):
        _write_alert_row(alert_type, type_meta['severity'], title, body, source, recording_id)

    push_services = routing.get('push_services') or []
    if push_services:
        try:
            from .notifications import enqueue_push
            link = _build_recording_link(recording_id)
            enqueue_push(push_services, title, body or '', alert_type=alert_type, link=link)
        except Exception:
            log.exception('Failed to enqueue push notification for %s', alert_type)


def _write_alert_row(alert_type, severity, title, body, source, recording_id):
    """Write one Alert row inside an app context, catching any DB errors."""
    from . import db
    from .database import Alert
    from .db_utils import retry_on_locked

    @retry_on_locked()
    def _add_and_commit():
        alert = Alert(
            alert_type=alert_type,
            severity=severity,
            title=title[:255] if title else '',
            body=body,
            source=source,
            recording_id=recording_id,
            created_at=datetime.utcnow(),
        )
        db.session.add(alert)
        db.session.commit()

    try:
        _add_and_commit()
    except Exception:
        log.exception('Failed to write alert row (type=%s)', alert_type)
        try:
            db.session.rollback()
        except Exception:
            log.warning('Alert-write rollback also failed', exc_info=True)


def cleanup_old_alerts(app):
    """Delete *dismissed* alerts older than alerts.keep_days. 0 = keep forever.

    Only resolved (dismissed) alerts are ever removed; active/unread alerts are kept
    regardless of age. This is the only alert-table retention - dismissing a row just
    sets dismissed_at, so without this the table grows without bound."""
    from . import db
    from .config import load_config
    from .database import Alert
    from .db_utils import retry_on_locked
    from datetime import timedelta

    with app.app_context():
        keep_days = load_config().get('alerts', {}).get('keep_days', 90)
        if not keep_days or keep_days <= 0:
            return
        cutoff = datetime.utcnow() - timedelta(days=keep_days)

        @retry_on_locked()
        def _delete_and_commit():
            n = Alert.query.filter(
                Alert.dismissed_at.isnot(None),
                Alert.dismissed_at < cutoff,
            ).delete(synchronize_session=False)
            db.session.commit()
            return n

        deleted = _delete_and_commit()
        if deleted:
            log.info('Alert cleanup: deleted %d dismissed alert(s) older than %d day(s)',
                     deleted, keep_days)
