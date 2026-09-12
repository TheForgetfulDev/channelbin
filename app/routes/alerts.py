"""
Alert center routes.

GET  /alerts                    - Alert center page
GET  /api/alerts                - JSON list (?unread_only=1 &limit=N &include_dismissed=1)
GET  /api/alerts/unread_count   - {count, error_count, warn_count}
POST /api/alerts/<id>/read      - Mark one alert read
POST /api/alerts/read_all       - Mark all unread alerts read
POST /api/alerts/<id>/dismiss   - Dismiss (hide) one alert
POST /api/alerts/dismiss_all    - Dismiss all read alerts
POST /api/alerts/<id>/ignore    - Acknowledge + suppress future matching alerts
GET  /alerts/ignored            - Ignored-alert-pattern management page
POST /api/alerts/ignored/<id>/remove - Remove one suppression rule
"""
import re
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request, url_for
from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError

from .. import db
from ..alerts import (ALERT_TYPES, SELF_CLEARING_ALERT_TYPES, is_self_clearing,
                      normalize_alert_title)
from ..channel_search import OTHER_NEW, OTHER_REMOVED
from ..database import Alert, IgnoredAlertPattern
from ..db_utils import retry_on_locked
from ..tz_utils import format_local

alerts_bp = Blueprint('alerts', __name__)

#: alert_type -> the channel-search `other` filter value its deep link lands on.
_LIFECYCLE_ALERT_OTHER_VALUE = {
    'SYNC_CHANNELS_NEW': OTHER_NEW,
    'SYNC_CHANNELS_MISSING': OTHER_REMOVED,
}

_OD_JOB_SOURCE_RE = re.compile(r'^od_job_(\d+)$')
_ACCOUNT_SYNC_SOURCE_RE = re.compile(r'^account_sync(?:_retry)?_(\d+)$')


def _resolve_alert_link(a: Alert) -> tuple[str, str] | None:
    """Resolve the single most useful deep link for an alert, or None when nothing
    addressable applies (e.g. a bare subsystem name like 'postprocessor').

    This is the one place an alert becomes a URL - every surface that shows an alert
    (the /alerts page, /api/alerts, the nav banner's details view) reads through here
    rather than re-parsing `a.source` itself, per the existing "parsed here, not in the
    template" precedent this replaces (`_channel_lifecycle_alert_link`).

    The label names the destination and every surface trails it with `→` (DESIGN.md §4's
    forward jump-off), so it is a noun, never a "View X" verb (dev/changelog/924).

    Checked in priority order, each one a known shape produced by app/*.py's
    create_alert() call sites:
      1. `a.recording_id` - exact, not parsed, always wins.
      2. SYNC_CHANNELS_NEW/MISSING's `account:<id>:channels-*` - today's filtered
         channel-browser special case.
      3. `group:<id>:...` (GROUP_FORMAT_MISMATCH) -> the group.
      4. `account:<id>:...` (every other SYNC_*/PROVIDER_* standing alert, over-limit,
         url-drift, ...) -> the account.
      5. `search-index:<name>` (SEARCH_INDEX_REBUILD_FAILED) -> Maintenance.
      6. `od_job_<id>` (on-demand JOB_SKIPPED / HEALTH_CHECK_COMPLETE) -> the job.
      7. `account_sync(_retry)?_<id>` (the two JOB_SKIPPED paths with no recording_id) ->
         the account.
    All parsing is defensive (digit checks, anchored regexes) rather than assuming the
    shape never changes.
    """
    if a.recording_id is not None:
        return url_for('recordings.recording_detail', recording_id=a.recording_id), 'Recording'

    if not a.source:
        return None
    parts = a.source.split(':')

    other_value = _LIFECYCLE_ALERT_OTHER_VALUE.get(a.alert_type)
    if other_value is not None and len(parts) == 3 and parts[0] == 'account' and parts[1].isdigit():
        return (url_for('channels.channel_browser', **{'f.other': other_value, 'f.acct': parts[1]}),
                'Channels')

    if len(parts) >= 2 and parts[1].isdigit():
        if parts[0] == 'group':
            return url_for('channel_groups.group_detail', group_id=int(parts[1])), 'Group'
        if parts[0] == 'account':
            return url_for('accounts.account_detail', account_id=int(parts[1])), 'Account'

    if parts[0] == 'search-index':
        return url_for('system.maintenance'), 'Maintenance'

    m = _OD_JOB_SOURCE_RE.match(a.source)
    if m:
        return url_for('channel_tests.on_demand_job_detail', job_id=int(m.group(1))), 'Health check'

    m = _ACCOUNT_SYNC_SOURCE_RE.match(a.source)
    if m:
        return url_for('accounts.account_detail', account_id=int(m.group(1))), 'Account'

    return None


def _is_active_problem(a: Alert) -> bool:
    """True if this row belongs under the Alerts page's "Active alerts" card.

    Both halves matter. The TYPE has to be one the app dismisses by itself, or nothing
    would ever take the row out of a card that offers no Dismiss. And the ROW has to still
    be standing: a dismissed alert is by definition no longer describing the condition it
    was raised for, so one reached through ?include_dismissed=1 is history and belongs
    under Past (dev/changelog/932).

    This is the one place the question is answered - the page partitions on it and the two
    dismiss routes refuse on it, and a second copy of the rule is how the card's contents
    and its no-Dismiss promise would drift apart.
    """
    return a.dismissed_at is None and is_self_clearing(a.alert_type)


def _alert_to_dict(a: Alert) -> dict:
    def _fmt(dt):
        return format_local(dt, 'iso_datetime', none_value=None)

    link = _resolve_alert_link(a)
    return {
        'id':          a.id,
        'alert_type':  a.alert_type,
        'severity':    a.severity,
        'title':       a.title,
        'body':        a.body,
        'source':      a.source,
        'recording_id': a.recording_id,
        'created_at':  _fmt(a.created_at),
        'read_at':     _fmt(a.read_at),
        'dismissed_at': _fmt(a.dismissed_at),
        'is_unread':   a.read_at is None,
        'self_clearing': is_self_clearing(a.alert_type),
        'is_active_problem': _is_active_problem(a),
        'link':        link[0] if link else None,
        'link_label':  link[1] if link else None,
    }


@alerts_bp.route('/alerts')
def alert_center():
    include_dismissed = request.args.get('include_dismissed') == '1'
    q = Alert.query.order_by(Alert.created_at.desc())
    if not include_dismissed:
        q = q.filter(Alert.dismissed_at.is_(None))
    alerts = q.limit(500).all()
    # Partitioned in Python off a frozenset lookup rather than as two queries: the rows are
    # already loaded, and a second query here would be one more per page for no new data
    # (CLAUDE.md, no hidden I/O in per-row loops - tests/test_scaling_pages.py measures it).
    active_alerts, past_alerts = [], []
    for a in alerts:
        (active_alerts if _is_active_problem(a) else past_alerts).append(a)
    unread_count = Alert.query.filter(Alert.read_at.is_(None),
                                      Alert.dismissed_at.is_(None)).count()
    alert_links = {a.id: _resolve_alert_link(a) for a in alerts}
    return render_template('alerts.html',
                           active_alerts=active_alerts,
                           past_alerts=past_alerts,
                           include_dismissed=include_dismissed,
                           unread_count=unread_count,
                           alert_links=alert_links)


@alerts_bp.route('/api/alerts')
def api_alerts():
    unread_only     = request.args.get('unread_only') == '1'
    include_dismissed = request.args.get('include_dismissed') == '1'
    limit           = min(int(request.args.get('limit', 100)), 500)

    q = Alert.query.order_by(Alert.created_at.desc())
    if not include_dismissed:
        q = q.filter(Alert.dismissed_at.is_(None))
    if unread_only:
        q = q.filter(Alert.read_at.is_(None))
    alerts = q.limit(limit).all()
    return jsonify([_alert_to_dict(a) for a in alerts])


#: The banner's order, worst first. INFO is absent on purpose: it never reaches the banner
#: or the nav counter, only the Alerts page (dev/changelog/923).
_BANNER_SEVERITY_RANK = {'CRIT': 0, 'ERROR': 1, 'WARN': 2}
#: What the nav's red count covers; WARN is the yellow one.
_RED_SEVERITIES = ('CRIT', 'ERROR')


def _unread_severity_counts():
    """Unread alerts, counted once per severity in one query.

    `count` is every unread alert, INFO included - the Alerts page's "N unread" and Home
    Assistant's `unread_count` mean that. `error_count` and `warn_count` are the nav's red
    and yellow counts, which never include INFO."""
    by_severity = dict(
        db.session.query(Alert.severity, func.count(Alert.id))
        .filter(Alert.read_at.is_(None), Alert.dismissed_at.is_(None))
        .group_by(Alert.severity).all())
    return {
        'count': sum(by_severity.values()),
        'error_count': sum(by_severity.get(s, 0) for s in _RED_SEVERITIES),
        'warn_count': by_severity.get('WARN', 0),
    }


def _summary_alert_dict(a: Alert) -> dict:
    link = _resolve_alert_link(a)
    return {'id': a.id, 'severity': a.severity, 'title': a.title, 'body': a.body,
            'created_at': a.created_at.isoformat(),
            'created_label': format_local(a.created_at),
            'is_active_problem': _is_active_problem(a),
            'link': link[0] if link else None, 'link_label': link[1] if link else None}


def _unread_alert_summary():
    """The nav's alert payload: the unread counts plus the one alert the banner shows.

    The banner is the MOST SEVERE unread alert, newest within a severity, and never an INFO
    one: showing the newest let a routine note sit above four unread errors
    (dev/changelog/923). `more` is how many other unread errors and warnings are behind it."""
    summary = _unread_severity_counts()
    top = (Alert.query
           .filter(Alert.read_at.is_(None), Alert.dismissed_at.is_(None),
                   Alert.severity.in_(tuple(_BANNER_SEVERITY_RANK)))
           .order_by(case(_BANNER_SEVERITY_RANK, value=Alert.severity),
                     Alert.created_at.desc(), Alert.id.desc())
           .first())
    summary['banner'] = _summary_alert_dict(top) if top else None
    summary['more'] = summary['error_count'] + summary['warn_count'] - 1 if top else 0
    return summary


def _latest_unread_alert():
    """The newest unread alert of any severity, for Home Assistant's `latest`. Deliberately
    not the banner's pick: that key is an API, and it has always meant the newest one."""
    a = (Alert.query.filter(Alert.read_at.is_(None), Alert.dismissed_at.is_(None))
         .order_by(Alert.created_at.desc(), Alert.id.desc()).first())
    return _summary_alert_dict(a) if a else None


@alerts_bp.route('/api/alerts/unread_count')
def api_unread_count():
    return jsonify(_unread_severity_counts())


@alerts_bp.route('/api/alerts/<int:alert_id>/read', methods=['POST'])
@retry_on_locked()
def api_mark_read(alert_id):
    alert = db.session.get(Alert, alert_id)
    if not alert:
        return jsonify({'error': 'Not found'}), 404
    if alert.read_at is None:
        alert.read_at = datetime.utcnow()
        db.session.commit()
    return jsonify({'success': True})


@alerts_bp.route('/api/alerts/read_all', methods=['POST'])
@retry_on_locked()
def api_read_all():
    now = datetime.utcnow()
    Alert.query.filter(Alert.read_at.is_(None)).update({'read_at': now})
    db.session.commit()
    return jsonify({'success': True})


@alerts_bp.route('/api/alerts/<int:alert_id>/dismiss', methods=['POST'])
@retry_on_locked()
def api_dismiss(alert_id):
    alert = db.session.get(Alert, alert_id)
    if not alert:
        return jsonify({'error': 'Not found'}), 404
    # Refused here, not merely hidden in the template: a problem that is still happening
    # must not be dismissable, and the UI is not where that is enforced (CLAUDE.md,
    # enforcement lives server-side). The app takes this row away itself when the condition
    # clears, so dismissing it would only destroy the standing evidence for as long as it
    # stayed broken.
    if _is_active_problem(alert):
        return jsonify({'error': 'This problem is still happening, so it cannot be '
                                 'dismissed. It clears itself once it is fixed.'}), 409
    now = datetime.utcnow()
    if alert.read_at is None:
        alert.read_at = now
    alert.dismissed_at = now
    db.session.commit()
    return jsonify({'success': True})


@alerts_bp.route('/api/alerts/dismiss_all', methods=['POST'])
@retry_on_locked()
def api_dismiss_all():
    now = datetime.utcnow()
    # A bulk action a guard does not cover is not a guard: "Dismiss all read" is the easiest
    # route there is to hiding every standing problem at once, so the still-happening rows
    # are excluded in the UPDATE itself. Open + self-clearing IS _is_active_problem, spelled
    # as SQL because this never loads the rows.
    Alert.query.filter(
        Alert.read_at.isnot(None),
        Alert.dismissed_at.is_(None),
        Alert.alert_type.notin_(tuple(SELF_CLEARING_ALERT_TYPES)),
    ).update({'dismissed_at': now}, synchronize_session=False)
    db.session.commit()
    return jsonify({'success': True})


@retry_on_locked()
def _get_or_create_ignored_pattern(alert_type, title):
    """One commit, with a self-contained duplicate fallback rather than a second
    sequential commit - the "each commit gets its own decorated closure" rule is about
    not re-running two conceptually separate writes on retry, not about never handling a
    constraint collision inline."""
    pattern_text = normalize_alert_title(title)
    existing = IgnoredAlertPattern.query.filter_by(
        alert_type=alert_type, title_pattern=pattern_text).first()
    if existing:
        return existing.id
    row = IgnoredAlertPattern(
        alert_type=alert_type, title_pattern=pattern_text,
        example_title=title[:255] if title else '', created_at=datetime.utcnow())
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = IgnoredAlertPattern.query.filter_by(
            alert_type=alert_type, title_pattern=pattern_text).first()
        return existing.id if existing else None
    return row.id


@retry_on_locked()
def _dismiss_alert(alert_id):
    alert = db.session.get(Alert, alert_id)
    if not alert:
        return False
    now = datetime.utcnow()
    if alert.read_at is None:
        alert.read_at = now
    alert.dismissed_at = now
    db.session.commit()
    return True


@alerts_bp.route('/api/alerts/<int:alert_id>/ignore', methods=['POST'])
def api_ignore(alert_id):
    alert = db.session.get(Alert, alert_id)
    if not alert:
        return jsonify({'error': 'Not found'}), 404
    pattern_id = _get_or_create_ignored_pattern(alert.alert_type, alert.title)
    _dismiss_alert(alert_id)
    return jsonify({'success': True, 'pattern_id': pattern_id})


@alerts_bp.route('/alerts/ignored')
def ignored_alerts():
    patterns = IgnoredAlertPattern.query.order_by(IgnoredAlertPattern.created_at.desc()).all()
    type_labels = {k: v['label'] for k, v in ALERT_TYPES.items()}
    return render_template('alerts_ignored.html', patterns=patterns, type_labels=type_labels)


@alerts_bp.route('/api/alerts/ignored/<int:pattern_id>/remove', methods=['POST'])
@retry_on_locked()
def api_remove_ignored_pattern(pattern_id):
    row = db.session.get(IgnoredAlertPattern, pattern_id)
    if not row:
        return jsonify({'error': 'Not found'}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({'success': True})
