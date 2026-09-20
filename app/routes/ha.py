"""Home Assistant integration API - read-only, polled by the HA custom_component.

Gated independently of the interactive session/password gate (app/auth.py): HA has no
browser to carry a session cookie, so every route on this blueprint instead requires a
static API key (X-API-Key header) checked against integrations.home_assistant.api_key_hash,
generated once from Settings > Integrations. app/auth.py::SELF_GATED_BLUEPRINTS names this
blueprint so the interactive gate does not also demand a browser login on top of this one.

This file's `/status` route is the real combined payload the custom_component's
DataUpdateCoordinator polls every ~45s. Every response here is a bare JSON object, not the
app's usual {'success': True, ...} envelope - a deliberate, documented deviation from
CLAUDE.md's JSON API envelope rule because the consumer is external to this app, not its
own JS.
"""
import logging

from flask import Blueprint, jsonify, request
from sqlalchemy import func
from werkzeug.security import check_password_hash

from .. import db
from ..config import load_config
from ..version import __version__
from ..database import (
    Account, Recording,
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_CONCATENATING,
    REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
)

log = logging.getLogger(__name__)

ha_bp = Blueprint('ha', __name__)


def _ha_key_ok(ha_cfg: dict, supplied: str) -> bool:
    """Same two-condition shape as auth.gate_active(): 'enabled' alone proves nothing
    without a stored key to check against, and a stored key without 'enabled' must not
    authenticate either - each is checked, not inferred from the other."""
    if not ha_cfg.get('enabled'):
        return False
    api_key_hash = (ha_cfg.get('api_key_hash') or '').strip()
    if not api_key_hash or not supplied:
        return False
    return check_password_hash(api_key_hash, supplied)


@ha_bp.before_request
def _require_api_key():
    ha_cfg = (load_config().get('integrations') or {}).get('home_assistant') or {}
    supplied = request.headers.get('X-API-Key') or ''
    if not _ha_key_ok(ha_cfg, supplied):
        # Never distinguish "integration disabled" / "no key set" / "wrong key" in the
        # response - same generic-failure precedent as the login gate (app/routes/auth.py).
        return jsonify({'error': 'Invalid or missing API key'}), 401
    return None


@ha_bp.route('/api/ha/v1/ping')
def ping():
    """Placeholder proving the API-key gate works end to end. Superseded by /status below
    for real polling, kept as a lightweight reachability check."""
    return jsonify({'success': True})


def _recording_summary():
    """capturing/converting counts + the next scheduled recording, from one query - the
    same statuses dashboard.py::_metric_tiles reads, recomputed here rather than calling
    it directly since that function returns HTML-formatted tile strings, not JSON."""
    recs = Recording.query.filter(
        Recording.status.in_([REC_STATUS_IN_PROGRESS, REC_STATUS_CONVERTING,
                              REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING,
                              REC_STATUS_SCHEDULED])
    ).order_by(Recording.start_time).all()
    capturing = [r for r in recs if r.status == REC_STATUS_IN_PROGRESS]
    converting = [r for r in recs if r.status in (REC_STATUS_CONVERTING, REC_STATUS_CONCATENATING,
                                                  REC_STATUS_ANALYZING)]
    scheduled = [r for r in recs if r.status == REC_STATUS_SCHEDULED]
    nxt = scheduled[0] if scheduled else None
    return {
        'capturing_count': len(capturing),
        'converting_count': len(converting),
        'next_recording': {
            'id': nxt.id,
            'name': nxt.name,
            'channel': nxt.channel.name if nxt.channel else None,
            'start_time': nxt.start_time.isoformat(),
        } if nxt else None,
    }


def _account_status_summary():
    """total/ok/error account counts from one GROUP BY query - no existing helper returns
    exactly this shape (dashboard.py's error-account list is a different consumer's need)."""
    counts = dict(db.session.query(Account.status, func.count(Account.id))
                  .group_by(Account.status).all())
    return {
        'total': sum(counts.values()),
        'ok_count': counts.get('OK', 0),
        'error_count': counts.get('ERROR', 0),
    }


@ha_bp.route('/api/ha/v1/status')
def status():
    """Combined recording/disk/alerts/accounts snapshot for HA to poll. Assembled from
    existing aggregation helpers (system.py::_disk_bytes, alerts.py::_unread_severity_counts
    and _latest_unread_alert) plus the two single-query helpers above - never one query per
    field, per CLAUDE.md's no-hidden-I/O-in-loops rule (this isn't a loop, but the same "one
    query, not N" spirit)."""
    from .alerts import _latest_unread_alert, _unread_severity_counts
    from .system import DVR_DIR_ROLE, _disk_bytes

    dvr_dir = load_config()['recording']['dvr_output_dir']
    disk_total, disk_free = _disk_bytes(dvr_dir, DVR_DIR_ROLE)
    disk_used = (disk_total - disk_free) if disk_total is not None and disk_free is not None else None
    disk = {
        'free_bytes': disk_free,
        'used_bytes': disk_used,
        'total_bytes': disk_total,
        'used_pct': round(disk_used / disk_total * 100, 1) if disk_total else None,
    }

    # `unread_count` and `latest` keep their meanings (every unread alert, and the newest
    # one) because the custom_component reads them by name; the split is additive.
    counts = _unread_severity_counts()
    alerts = {
        'unread_count': counts['count'],
        'error_count': counts['error_count'],
        'warn_count': counts['warn_count'],
        'latest': _latest_unread_alert(),
    }

    # app_version is what the integration checks against its declared minimum
    # (custom_components/channelbin/compat.py). A server that omits it predates the check
    # and is read as too old there, so this key is never renamed or dropped.
    return jsonify({
        'app_version': __version__,
        'recording': _recording_summary(),
        'disk': disk,
        'alerts': alerts,
        'accounts': _account_status_summary(),
    })
