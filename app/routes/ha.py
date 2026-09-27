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
from werkzeug.security import check_password_hash

from ..config import load_config
from ..url_utils import mask_account_urls_in_text
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
    """capturing/converting counts, the capturing recordings and the next scheduled one,
    from one query - the same statuses dashboard.py::_metric_tiles reads, recomputed here
    rather than calling it directly since that function returns HTML-formatted tile strings,
    not JSON. Recording.channel is lazy='joined', so reading its name per capturing row
    costs no query of its own (tests/test_ha_api.py::CapturingListQueryCountTests)."""
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
    # capturing_count stays beside the list rather than being derived from it by the
    # consumer: the shipped 1.0.x integration reads it by name (dev/changelog/1146).
    return {
        'capturing_count': len(capturing),
        'capturing': [{
            'id': r.id,
            'name': r.name,
            'channel': r.channel.name if r.channel else None,
            'started_at': r.started_at.isoformat() if r.started_at else None,
            'stop_time': r.stop_time.isoformat(),
        } for r in capturing],
        'converting_count': len(converting),
        'next_recording': {
            'id': nxt.id,
            'name': nxt.name,
            'channel': nxt.channel.name if nxt.channel else None,
            'start_time': nxt.start_time.isoformat(),
        } if nxt else None,
    }


def _iso(value):
    return value.isoformat() if value else None


def _account_status_summary(cfg):
    """total/ok/error account counts, plus one entry per account for the integration's
    per-account devices, keyed on id so a rename never breaks an automation.

    The counts keep their names and meanings - the shipped integration reads them - and are
    counted from the same rows as the list rather than a separate GROUP BY. Next sync comes
    from next_sync_map(), never the stored column, which goes stale when a sync is deferred
    (dev/changelog/941). Connections in use are one registry read for every account.
    last_error is masked on the way out even though the sync stores it masked: it leaves
    the box here, and the account's own URLs are secret in full."""
    from ..accounts import next_sync_map
    from .. import connection_limits as connlim

    accounts = Account.query.order_by(Account.id).all()
    next_sync = next_sync_map(accounts)
    in_use = connlim.holder_counts()
    default_max = cfg.get('accounts', {}).get('default_max_connections', 1)
    return {
        'total': len(accounts),
        'ok_count': sum(1 for a in accounts if a.status == 'OK'),
        'error_count': sum(1 for a in accounts if a.status == 'ERROR'),
        'list': [{
            'id': a.id,
            'name': a.name,
            'type': a.account_type,
            'status': a.status,
            'last_sync_at': _iso(a.last_sync_at),
            'next_sync_at': _iso(next_sync.get(a.id)),
            'last_error': mask_account_urls_in_text(a.last_error, a.m3u_url, a.epg_url,
                                                    a.base_url),
            'channel_count': a.channel_count or 0,
            'hidden_channel_count': a.hidden_channel_count or 0,
            'provider_exp_date': _iso(a.provider_exp_date),
            'connections_in_use': in_use.get(a.id, 0),
            'max_connections': connlim.limit_for_account(a, default_max),
        } for a in accounts],
    }


@ha_bp.route('/api/ha/v1/status')
def status():
    """Combined recording/disk/alerts/accounts snapshot for HA to poll. Assembled from
    existing aggregation helpers (system.py::_disk_bytes, alerts.py::_unread_severity_counts
    and _latest_unread_alert) plus the two summary helpers above - never one query per
    field, per CLAUDE.md's no-hidden-I/O-in-loops rule (this isn't a loop, but the same "one
    query, not N" spirit)."""
    from .alerts import _latest_unread_alert, _unread_severity_counts
    from .system import DVR_DIR_ROLE, _disk_bytes

    cfg = load_config()
    dvr_dir = cfg['recording']['dvr_output_dir']
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

    # app_version is what the integration checks against its declared minimum. The integration
    # is published as its own repository, channelbin-homeassistant. A server that omits this
    # key predates the check and is read as too old there, so it is never renamed or dropped.
    return jsonify({
        'app_version': __version__,
        'recording': _recording_summary(),
        'disk': disk,
        'alerts': alerts,
        'accounts': _account_status_summary(cfg),
    })
