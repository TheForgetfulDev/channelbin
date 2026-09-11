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

# Registry of all known alert types.
# severity: ERROR | CRIT | WARN | INFO
ALERT_TYPES = {
    'LOG_ERROR':   {'label': 'Application Error (log)',    'severity': 'ERROR'},
    'LOG_CRIT':    {'label': 'Application Critical (log)', 'severity': 'CRIT'},
    'JOB_SKIPPED': {'label': 'Scheduled Job Skipped',      'severity': 'INFO'},
    'MALFORMED_CHANNEL_URLS': {'label': 'Malformed Channel URLs Skipped', 'severity': 'INFO'},
    # A stream_id repeated within one playlist/catalog - the URL-derived id can collide
    # innocuously (a .ts/.m3u8 variant of the same numeric tail) or genuinely conflict (two
    # distinct provider URLs landing on the same parsed/hashed id), and either way the second
    # occurrence is silently dropped without this alert (dev/docs/BUGS.md 2026-08-30).
    'DUPLICATE_STREAM_IDS_SKIPPED': {
        'label': 'Duplicate Stream IDs Skipped', 'severity': 'INFO'},
    # A hide-rule pass was refused for database contention, so the rules are saved but not
    # yet reflected in what you are being offered. INFO rather than WARN: it retries itself
    # within minutes and the body says so - what would be indefensible is the rules quietly
    # doing nothing with no surface saying why (dev/changelog/776).
    'CHANNEL_HIDE_RULES_NOT_APPLIED': {
        'label': 'Channel Hide Rules Not Applied Yet', 'severity': 'INFO'},
    'GROUP_FORMAT_MISMATCH': {'label': 'Channel Group Format Mismatch', 'severity': 'WARN'},
    # The two voices DESIGN-channel-groups-model.md 15.2 asks for ahead of the recording
    # itself: the moment a group's format lock leaves nothing eligible, and the moment a
    # recording goes ahead under that override. Both are push moments the user may not be
    # present for, which is why neither is left to a page they would have to visit.
    'GROUP_NO_ELIGIBLE_MEMBER': {'label': 'Channel Group Has No Eligible Member', 'severity': 'WARN'},
    # A group sitting in the TV Guide with nothing switched on for recording, reached
    # without a human to confirm it (DESIGN-channel-groups-model.md 15, breach path 3).
    # ERROR rather than WARN, and deliberately louder than GROUP_NO_ELIGIBLE_MEMBER above:
    # that one is a group that can still record, badly. This one cannot produce a file at
    # all, and the row will go on looking normal in the guide until somebody fixes it.
    'GROUP_GUIDE_NO_RECORDING_MEMBER': {
        'label': 'Channel Group In Guide Has No Recording Member', 'severity': 'ERROR'},
    'RECORDING_FORMAT_OVERRIDE': {'label': 'Recording Started Off The Group Format', 'severity': 'WARN'},
    # A recording that actually changed format part-way through. Distinct from the override
    # above, which is about where a recording STARTED: this one is about the finished file
    # having two formats in it, which no other surface would ever mention - the container
    # header advertises only the first (dev/changelog/754).
    'RECORDING_FORMAT_CHANGED': {'label': 'Recording Changed Format Mid-Run', 'severity': 'WARN'},
    'PROVIDER_URLS_CHANGED': {'label': 'Provider Stream URLs Changed', 'severity': 'WARN'},
    'SYNC_EPG_FETCH_FAILED': {'label': 'EPG Fetch Failed', 'severity': 'WARN'},
    'SYNC_EPG_COLLAPSE_REFUSED': {'label': 'EPG Import Refused (Collapse Guard)', 'severity': 'WARN'},
    # Distinct from the two above because the old EPG is already gone by the time this
    # fires: the delete commits before the parse loop, so their "previous EPG data was
    # kept" wording would be false here (DESIGN-sync-resilience.md §3).
    'SYNC_EPG_IMPORT_TRUNCATED': {'label': 'EPG Import Cut Short (Truncated Feed)', 'severity': 'WARN'},
    'SYNC_FEED_SHRUNK': {'label': 'Provider Feed Shrunk', 'severity': 'WARN'},
    'SYNC_LIVE_CLASSIFY_UNAVAILABLE': {'label': 'Live/VOD Catalog Unavailable', 'severity': 'WARN'},
    'SYNC_LIVE_CLASSIFY_REFUSED': {'label': 'Live/VOD Filtering Refused (Collapse Guard)', 'severity': 'WARN'},
    'SYNC_STREAM_URLS_CONSTRUCTED': {'label': 'Stream URLs Constructed, Not Provided', 'severity': 'INFO'},
    'SYNC_URL_CONSTRUCTION_BLOCKED': {'label': 'Cannot Build Stream URLs (No Format Set)', 'severity': 'WARN'},
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
        'label': 'Recording Waiting For A Connection Slot', 'severity': 'WARN'},
    'RECORDING_FAILED_CONNECTION_LIMIT': {
        'label': 'Recording Failed (Waited For A Connection Slot)', 'severity': 'ERROR'},
    'RECORDING_CHANNEL_FAILING': {'label': 'Scheduled Recording Channel Failing', 'severity': 'WARN'},
    'SEARCH_INDEX_REBUILD_FAILED': {'label': 'Search Index Rebuild Failed', 'severity': 'ERROR'},
    'CONVERSION_FAILED': {'label': 'Conversion Failed', 'severity': 'ERROR'},
    'CONFIG_FILE_MISSING': {'label': 'Config File Missing', 'severity': 'CRIT'},
    'HEALTH_CHECK_COMPLETE': {'label': 'Health Check Completed', 'severity': 'INFO'},
    'HEALTH_CHECK_WINDOW': {'label': 'Maintenance Window Closed With Work Left Over', 'severity': 'WARN'},
    # One-time, on the first startup after the automatic TV Guide check was retargeted to
    # one probe per guide row (dev/changelog/752). What a scheduled check tests is the
    # user's business, so it is announced rather than quietly widened or narrowed.
    'HEALTH_CHECK_TARGETS_CHANGED': {'label': 'Automatic Health Check Retargeted', 'severity': 'INFO'},
    # WARN, not ERROR: the log->alert handler in app/__init__.py already creates an alert
    # for any ERROR-level log record, so this stays WARN to avoid a second, duplicate alert
    # if the lockout is also logged at ERROR - it isn't (log.warning), but the severity
    # still has to not collide with that path.
    'AUTH_LOGIN_LOCKOUT': {'label': 'Login Lockout (Too Many Failed Attempts)', 'severity': 'WARN'},
    NOTIFICATION_SERVICE_URL_PLACEHOLDER: {
        'label': 'Notification Service URL Is Still Its Placeholder', 'severity': 'WARN'},
    # WARN for the same reason its placeholder sibling above is: nothing in the app
    # malfunctioned, one outbound service is degraded. ERROR would also double up with the
    # log->alert handler, since the send failure is logged on the way here.
    NOTIFICATION_SERVICE_SEND_FAILED: {
        'label': 'Push Notification Service Failing', 'severity': 'WARN'},
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
        'label': 'Login Gate Enabled But Has No Password', 'severity': 'WARN'},
    # ERROR, unlike the WARN band the other "configuration is degraded" types above sit
    # in: a DVR output directory that stopped answering does not degrade recording, it
    # disables it outright, and every recording that starts meanwhile fails immediately.
    # No double-up with the log->alert handler in app/__init__.py, which keys on a log
    # record's own level - the condition is logged at WARNING (app/fs_utils.py).
    STORAGE_PATH_UNUSABLE: {
        'label': 'Storage Path Unusable', 'severity': 'ERROR'},
    # ERROR for the same reason STORAGE_PATH_UNUSABLE above is, and not the WARN band the
    # "something is degraded" types sit in: a missing ffmpeg does not degrade recording, it
    # disables it, and every recording that starts meanwhile fails immediately. A missing
    # ffprobe shares the band deliberately - it is silent rather than loud on every other
    # surface, which is exactly what makes it worth the louder one here. No double-up with
    # the log->alert handler in app/__init__.py: the condition is logged at WARNING
    # (app/toolchain.py).
    EXTERNAL_TOOL_MISSING: {
        'label': 'External Tool Missing (ffmpeg/ffprobe)', 'severity': 'ERROR'},
}


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
